"""One-shot, sequential, no-retry runner for the authorized Qwen batch-1 pilot.

This module is deliberately separate from the API, Worker, SQLite task database,
gold annotations, and GitHub writeback. Importing it performs no external call.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BATCH_ID = "human_review_batch1"
MAX_REQUEST_BUDGET = 11
ALLOWED_INPUT_FIELDS = {"repository", "issue_number", "title", "body"}
FORBIDDEN_INPUT_FIELDS = {
    "gold_category",
    "gold_priority",
    "review_status",
    "review_notes",
    "candidate_category",
    "existing_labels",
    "sample_group",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate_blind_input(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError("Blind input must contain an items array")
    items = payload["items"]
    if len(items) != MAX_REQUEST_BUDGET:
        raise RuntimeError("Blind input must contain exactly 11 items")
    seen: set[int] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != ALLOWED_INPUT_FIELDS:
            raise RuntimeError("Blind input has unexpected fields")
        if set(item) & FORBIDDEN_INPUT_FIELDS:
            raise RuntimeError("Gold or reference labels leaked into blind input")
        number = item.get("issue_number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise RuntimeError("Blind input has an invalid issue_number")
        if number in seen or number == 337331:
            raise RuntimeError("Blind input contains a duplicate or excluded issue")
        seen.add(number)
        for field in ("repository", "title", "body"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise RuntimeError(f"Blind input has an empty {field}")
    return items


def parse_model_response(raw: str):
    """Apply exactly the production JSON cleanup and Pydantic schema."""
    from pydantic import ValidationError
    from issuepilot.models import TriageResult

    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        return TriageResult.model_validate(json.loads(text))
    except (ValueError, ValidationError) as exc:
        raise RuntimeError("schema_parse_failure") from exc


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "batch_id": BATCH_ID,
            "max_request_budget": MAX_REQUEST_BUDGET,
            "attempted": {},
            "updated_at": utc_now(),
        }
    registry = load_json(path)
    if (
        registry.get("batch_id") != BATCH_ID
        or registry.get("max_request_budget") != MAX_REQUEST_BUDGET
        or not isinstance(registry.get("attempted"), dict)
    ):
        raise RuntimeError("Batch registry is malformed")
    return registry


def discover_attempted_ids(evaluation_dir: Path) -> set[int]:
    attempted = set()
    for ledger in evaluation_dir.glob("qwen_baseline_20261027_*/request_ledger.jsonl"):
        for line in ledger.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if int(record.get("attempt_count", 0)) >= 1:
                attempted.add(int(record["issue_number"]))
    return attempted


def reserve_attempt(
    registry_path: Path, issue_number: int, run_id: str, started_at: str
) -> dict[str, Any]:
    registry = load_registry(registry_path)
    attempted = registry["attempted"]
    key = str(issue_number)
    if key in attempted or int(attempted.get(key, {}).get("attempt_count", 0)) >= 1:
        raise RuntimeError(f"Issue {issue_number} was already attempted")
    if len(attempted) >= MAX_REQUEST_BUDGET:
        raise RuntimeError("Batch request budget exhausted")
    attempted[key] = {
        "issue_number": issue_number,
        "attempt_count": 1,
        "request_state": "IN_FLIGHT",
        "run_id": run_id,
        "request_started_at": started_at,
        "request_completed_at": None,
    }
    registry["updated_at"] = started_at
    atomic_json(registry_path, registry)
    return registry


def update_registry(
    registry_path: Path, issue_number: int, state: str, completed_at: str
) -> None:
    registry = load_registry(registry_path)
    record = registry["attempted"].get(str(issue_number))
    if not record or record.get("attempt_count") != 1:
        raise RuntimeError("Cannot update an unreserved request")
    record["request_state"] = state
    record["request_completed_at"] = completed_at
    registry["updated_at"] = completed_at
    atomic_json(registry_path, registry)


def write_predictions_csv(path: Path, predictions: list[dict[str, Any]]) -> None:
    fields = [
        "issue_number",
        "predicted_category",
        "predicted_priority",
        "system_success",
        "system_error",
        "api_call_succeeded",
        "request_state",
        "request_started_at",
        "request_completed_at",
        "request_id",
        "input_tokens",
        "output_tokens",
        "model_name",
    ]
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(predictions)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _usage_value(usage: Any, field: str) -> int | None:
    value = getattr(usage, field, None) if usage is not None else None
    return value if isinstance(value, int) else None


def _safe_error_type(exc: Exception) -> str:
    return type(exc).__name__


def run(args: argparse.Namespace) -> int:
    if not args.execute_real_qwen:
        raise RuntimeError("Real execution requires --execute-real-qwen")

    root = Path(__file__).resolve().parents[1]
    evaluation_dir = root / "evaluation"
    run_dir = Path(args.run_dir).resolve()
    registry_path = Path(args.registry).resolve()
    blind_path = Path(args.blind_input).resolve()
    if run_dir.parent != evaluation_dir.resolve() or not run_dir.name.startswith(
        "qwen_baseline_20261027_"
    ):
        raise RuntimeError("Run directory must be a new qwen_baseline_20261027_* directory")
    if run_dir.exists():
        raise RuntimeError("Run directory already exists; refusing to overwrite or resume")
    if registry_path != (evaluation_dir / "qwen_batch1_request_registry.json").resolve():
        raise RuntimeError("Unexpected batch registry path")

    existing_registry = load_registry(registry_path)
    discovered = discover_attempted_ids(evaluation_dir)
    registered = {int(value) for value in existing_registry["attempted"]}
    if discovered != registered:
        raise RuntimeError("Existing ledgers and batch registry require manual reconciliation")
    if registered:
        raise RuntimeError("This batch already has attempted requests; refusing a new run")

    blind_payload = load_json(blind_path)
    items = validate_blind_input(blind_payload)
    if sha256_file(blind_path) != args.blind_sha256.upper():
        raise RuntimeError("Blind input hash changed")

    # This is the project's existing configuration mechanism. Never print key.
    import issuepilot.config  # noqa: F401
    from issuepilot.triage import SYSTEM

    key = os.getenv("QWEN_API_KEY", "").strip()
    if not key or key.startswith("replace-"):
        raise RuntimeError("Qwen API key is not configured")
    model = os.getenv("QWEN_MODEL", "qwen3.8-flash")
    base_url = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    prompt_version = "triage-system-sha256:" + hashlib.sha256(
        SYSTEM.encode("utf-8")
    ).hexdigest()
    if model != blind_payload.get("model_name"):
        raise RuntimeError("Configured model differs from frozen blind input")
    if prompt_version != blind_payload.get("prompt_version"):
        raise RuntimeError("Prompt differs from frozen blind input")

    from openai import APIConnectionError, APIStatusError, OpenAI

    run_id = run_dir.name
    started_at = utc_now()
    run_dir.mkdir(parents=False, exist_ok=False)
    ledger_path = run_dir / "request_ledger.jsonl"
    predictions_jsonl = run_dir / "predictions.jsonl"
    ledger_path.touch(exist_ok=False)
    predictions_jsonl.touch(exist_ok=False)
    manifest = {
        "evaluation_run_id": run_id,
        "batch_id": BATCH_ID,
        "model_name": model,
        "prompt_version": prompt_version,
        "gold_dataset_sha256": args.gold_sha256.upper(),
        "source_dataset_sha256": args.source_sha256.upper(),
        "blind_input_sha256": args.blind_sha256.upper(),
        "ordered_issue_ids": [item["issue_number"] for item in items],
        "configured_temperature": 0,
        "configured_max_tokens": None,
        "thinking_enabled": False,
        "api_sdk": "openai",
        "api_sdk_version": importlib.metadata.version("openai"),
        "category_schema": ["bug", "feature_request", "question", "other"],
        "scoring_classes": ["bug", "feature_request"],
        "start_time": started_at,
        "finish_time": None,
        "max_request_budget": MAX_REQUEST_BUDGET,
        "automatic_retries": 0,
        "status": "RUNNING",
        "gold_labels_used_as_model_input": False,
        "github_operations": 0,
    }
    atomic_json(run_dir / "manifest.json", manifest)

    client = OpenAI(api_key=key, base_url=base_url, timeout=45, max_retries=0)
    predictions: list[dict[str, Any]] = []
    stop_on_failure = False
    for item in items:
        issue_number = item["issue_number"]
        request_started = utc_now()
        reserve_attempt(registry_path, issue_number, run_id, request_started)
        initial_ledger = {
            "issue_number": issue_number,
            "request_state": "IN_FLIGHT",
            "attempt_count": 1,
            "request_started_at": request_started,
            "request_completed_at": None,
            "response_received": False,
            "error_type": "",
            "model_name": model,
            "request_id": None,
            "input_tokens": None,
            "output_tokens": None,
        }
        append_jsonl(ledger_path, initial_ledger)
        print(f"REQUEST_START issue={issue_number} attempt=1", flush=True)

        raw = ""
        request_id = None
        input_tokens = None
        output_tokens = None
        api_call_succeeded = False
        response_received = False
        state = "UNKNOWN_OUTCOME"
        error_type = "unknown_outcome"
        parsed = None
        try:
            payload = {
                "title": str(item.get("title", ""))[:500],
                "body": str(item.get("body", ""))[:7000],
                "repository": str(item.get("repository", "")),
            }
            response = client.chat.completions.create(
                model=model,
                temperature=0,
                extra_body={"enable_thinking": False},
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {
                        "role": "user",
                        "content": "Classify this issue:\n"
                        + json.dumps(payload, ensure_ascii=False),
                    },
                ],
            )
            response_received = True
            api_call_succeeded = True
            request_id = getattr(response, "id", None)
            usage = getattr(response, "usage", None)
            input_tokens = _usage_value(usage, "prompt_tokens")
            output_tokens = _usage_value(usage, "completion_tokens")
            raw = response.choices[0].message.content or ""
            parsed = parse_model_response(raw)
            state = "COMPLETED"
            error_type = ""
        except RuntimeError as exc:
            if str(exc) != "schema_parse_failure":
                raise
            state = "FAILED"
            error_type = "schema_parse_failure"
        except APIStatusError as exc:
            response_received = True
            state = "FAILED"
            error_type = f"api_http_{getattr(exc, 'status_code', 'error')}"
        except APIConnectionError as exc:
            state = "UNKNOWN_OUTCOME"
            error_type = _safe_error_type(exc)
        except Exception as exc:
            state = "UNKNOWN_OUTCOME"
            error_type = _safe_error_type(exc)

        completed_at = utc_now()
        terminal_ledger = {
            "issue_number": issue_number,
            "request_state": state,
            "attempt_count": 1,
            "request_started_at": request_started,
            "request_completed_at": completed_at,
            "response_received": response_received,
            "error_type": error_type,
            "model_name": model,
            "request_id": request_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        append_jsonl(ledger_path, terminal_ledger)
        prediction = {
            "issue_number": issue_number,
            "predicted_category": parsed.category if parsed else "",
            "predicted_priority": parsed.priority if parsed else "",
            "system_success": parsed is not None,
            "system_error": error_type,
            "api_call_succeeded": api_call_succeeded,
            "response_received": response_received,
            "request_state": state,
            "request_started_at": request_started,
            "request_completed_at": completed_at,
            "request_id": request_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model_name": model,
            "raw_response": raw,
            "summary": parsed.summary if parsed else "",
            "rationale": parsed.rationale if parsed else "",
            "confidence": parsed.confidence if parsed else None,
        }
        append_jsonl(predictions_jsonl, prediction)
        predictions.append(prediction)
        write_predictions_csv(run_dir / "predictions.csv", predictions)
        update_registry(registry_path, issue_number, state, completed_at)
        print(
            f"REQUEST_END issue={issue_number} state={state} "
            f"input_tokens={input_tokens} output_tokens={output_tokens}",
            flush=True,
        )
        if state != "COMPLETED":
            stop_on_failure = True
            break

    finished_at = utc_now()
    api_failures = sum(not item["api_call_succeeded"] for item in predictions)
    schema_failures = sum(item["system_error"] == "schema_parse_failure" for item in predictions)
    bundle = {
        "metadata": {
            "run_id": run_id,
            "model_name": model,
            "prompt_version": prompt_version,
            "run_started_at_utc": started_at,
            "run_finished_at_utc": finished_at,
            "sample_count": len(predictions),
            "call_failure_count": api_failures,
            "schema_failure_count": schema_failures,
            "max_paid_calls": MAX_REQUEST_BUDGET,
            "paid_retry_count": 0,
            "gold_labels_used_as_input": False,
            "raw_predictions_retained": True,
        },
        "items": predictions,
    }
    atomic_json(run_dir / "prediction_run.json", bundle)
    manifest.update(
        finish_time=finished_at,
        status="STOPPED_ON_FAILURE" if stop_on_failure else "COMPLETED",
        actual_request_attempts=len(predictions),
        successful_responses=sum(item["api_call_succeeded"] for item in predictions),
        api_failures=api_failures,
        schema_failures=schema_failures,
    )
    atomic_json(run_dir / "manifest.json", manifest)

    # A credential must never be serialized, including in raw model text.
    for path in run_dir.iterdir():
        if path.is_file() and key in path.read_text(encoding="utf-8-sig"):
            raise RuntimeError("Credential leak detected in run output")
    print(f"RUN_STATUS={manifest['status']}")
    print(f"REQUEST_ATTEMPTS={len(predictions)}")
    print(f"RUN_DIRECTORY={run_dir}")
    return 0 if not stop_on_failure else 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-real-qwen", action="store_true")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--blind-input", required=True)
    parser.add_argument("--gold-sha256", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--blind-sha256", required=True)
    args = parser.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()

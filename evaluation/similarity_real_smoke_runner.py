"""One-shot, budgeted real-Qwen smoke runner for the similarity enhancement.

This module never starts the API/Worker, touches SQLite, or calls GitHub. Every
model attempt is durably reserved before the network request and is never
retried, including after an uncertain outcome.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from issuepilot.similarity import LocalIssueRetriever, classify_issue_enhanced
from issuepilot.triage import SYSTEM, classify_issue

BATCH_ID = "similarity_real_smoke_batch1"
SELECTED_ISSUE_IDS = (244597, 342692, 342851)
REQUEST_STAGE = "base_triage"
MAX_BATCH_REQUESTS = 6
MAX_REQUESTS_PER_ISSUE = 2
EXPECTED_REQUESTS_PER_ISSUE = 1
ALLOWED_INPUT_FIELDS = {"repository", "issue_number", "title", "body"}
FORBIDDEN_INPUT_FIELDS = {
    "gold_category", "gold_priority", "review_notes", "review_status",
    "candidate_category", "sample_group", "existing_labels", "url",
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_dataset(path: Path) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError("source_dataset_invalid")
    by_id = {item.get("issue_number"): item for item in payload["items"] if isinstance(item, dict)}
    if len(by_id) != len(payload["items"]):
        raise RuntimeError("source_dataset_has_duplicate_or_invalid_ids")
    selected = []
    for number in SELECTED_ISSUE_IDS:
        source = by_id.get(number)
        if source is None:
            raise RuntimeError(f"selected_issue_missing_{number}")
        item = {field: source.get(field, "") for field in ALLOWED_INPUT_FIELDS}
        if set(item) != ALLOWED_INPUT_FIELDS or set(item) & FORBIDDEN_INPUT_FIELDS:
            raise RuntimeError("frozen_input_field_violation")
        selected.append(item)
    return selected, by_id


def _scan_prior_ledgers(evaluation_dir: Path) -> set[str]:
    attempts: set[str] = set()
    for ledger in evaluation_dir.glob("similarity_real_smoke_*/request_ledger.jsonl"):
        for line in ledger.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            attempts.add(f"{row['issue_number']}:{row['request_stage']}")
    return attempts


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "batch_id": BATCH_ID,
            "max_batch_requests": MAX_BATCH_REQUESTS,
            "max_requests_per_issue": MAX_REQUESTS_PER_ISSUE,
            "attempted": {},
            "updated_at": None,
        }
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if payload.get("batch_id") != BATCH_ID:
        raise RuntimeError("registry_batch_mismatch")
    return payload


def reserve_attempt(registry_path: Path, issue_number: int, run_id: str) -> dict[str, Any]:
    registry = load_registry(registry_path)
    key = f"{issue_number}:{REQUEST_STAGE}"
    attempted = registry["attempted"]
    if key in attempted:
        raise RuntimeError(f"stage_already_attempted_{key}")
    if len(attempted) >= MAX_BATCH_REQUESTS:
        raise RuntimeError("batch_request_budget_exhausted")
    per_issue = sum(record["issue_number"] == issue_number for record in attempted.values())
    if per_issue >= MAX_REQUESTS_PER_ISSUE:
        raise RuntimeError(f"issue_request_budget_exhausted_{issue_number}")
    started = utc_now()
    record = {
        "issue_number": issue_number,
        "request_stage": REQUEST_STAGE,
        "attempt_count": 1,
        "request_state": "IN_FLIGHT",
        "request_started_at": started,
        "request_completed_at": None,
        "run_id": run_id,
        "batch_budget_used": len(attempted) + 1,
    }
    attempted[key] = record
    registry["updated_at"] = started
    atomic_json(registry_path, registry)
    return record


def finish_attempt(registry_path: Path, issue_number: int, state: str) -> dict[str, Any]:
    if state not in {"COMPLETED", "FAILED", "UNKNOWN_OUTCOME"}:
        raise ValueError("invalid_terminal_state")
    registry = load_registry(registry_path)
    key = f"{issue_number}:{REQUEST_STAGE}"
    record = registry["attempted"][key]
    record["request_state"] = state
    record["request_completed_at"] = utc_now()
    registry["updated_at"] = record["request_completed_at"]
    atomic_json(registry_path, registry)
    return record


def _validate_evidence(
    issue_number: int, analysis: dict[str, Any], source_by_id: dict[int, dict[str, Any]]
) -> bool:
    candidates = analysis["candidate_issues"]
    if len(candidates) > 5 or len({item["issue_number"] for item in candidates}) != len(candidates):
        return False
    for candidate in candidates:
        source = source_by_id.get(candidate["issue_number"])
        if source is None or candidate["issue_number"] == issue_number:
            return False
        if candidate["source_url"] != source.get("url"):
            return False
    return analysis["needs_human_review"] is True and analysis["tool_calls"] <= 1 and analysis["flow_steps"] <= 3


def execute(
    args: argparse.Namespace,
    *,
    classifier: Callable[[dict], Any] = classify_issue,
) -> dict[str, Any]:
    source_path = Path(args.source).resolve()
    run_dir = Path(args.run_dir).resolve()
    registry_path = Path(args.registry).resolve()
    evaluation_dir = run_dir.parent
    if run_dir.exists():
        raise RuntimeError("run_directory_already_exists")
    prior = _scan_prior_ledgers(evaluation_dir)
    registry = load_registry(registry_path)
    if prior or registry["attempted"]:
        raise RuntimeError("similarity_smoke_batch_already_attempted")
    if sha256_file(source_path) != args.source_sha256.upper():
        raise RuntimeError("source_dataset_hash_mismatch")
    model_name = os.getenv("QWEN_MODEL", "qwen3.8-flash")
    if model_name != "qwen3.8-flash":
        raise RuntimeError("unexpected_model_configuration")
    key = os.getenv("QWEN_API_KEY", "").strip()
    if args.execute_real_qwen and (not key or key.startswith("replace-")):
        raise RuntimeError("qwen_api_key_not_configured")

    selected, source_by_id = _load_dataset(source_path)
    run_dir.mkdir(parents=True)
    run_id = run_dir.name
    frozen_input = {
        "batch_id": BATCH_ID,
        "items": selected,
        "forbidden_fields": sorted(FORBIDDEN_INPUT_FIELDS),
    }
    atomic_json(run_dir / "frozen_input.json", frozen_input)
    atomic_json(registry_path, registry)
    manifest = {
        "run_id": run_id,
        "batch_id": BATCH_ID,
        "status": "RUNNING",
        "selected_issue_ids": list(SELECTED_ISSUE_IDS),
        "source_dataset_sha256": args.source_sha256.upper(),
        "frozen_input_sha256": sha256_file(run_dir / "frozen_input.json"),
        "model_name": model_name,
        "prompt_sha256": hashlib.sha256(SYSTEM.encode("utf-8")).hexdigest().upper(),
        "temperature": 0,
        "thinking_enabled": False,
        "sdk_max_retries": 0,
        "enhanced_entrypoint": "issuepilot.similarity.classify_issue_enhanced",
        "candidate_corpus_size": len(source_by_id),
        "max_batch_requests": MAX_BATCH_REQUESTS,
        "max_requests_per_issue": MAX_REQUESTS_PER_ISSUE,
        "expected_requests_per_issue": EXPECTED_REQUESTS_PER_ISSUE,
        "start_time": utc_now(),
        "finish_time": None,
        "github_write_operations": 0,
        "sqlite_operations": 0,
    }
    atomic_json(run_dir / "manifest.json", manifest)
    retriever = LocalIssueRetriever(source_path)
    results: list[dict[str, Any]] = []
    stop_reason = None

    for issue in selected:
        number = issue["issue_number"]
        reserved = reserve_attempt(registry_path, number, run_id)
        append_jsonl(run_dir / "request_ledger.jsonl", reserved)
        started = time.monotonic()
        try:
            if not args.execute_real_qwen:
                raise RuntimeError("real_qwen_execution_not_authorized")
            result = classify_issue_enhanced(issue, classifier=classifier, retriever=retriever)
            elapsed = round(time.monotonic() - started, 6)
            analysis = result.similarity_analysis.model_dump()
            evidence_valid = _validate_evidence(number, analysis, source_by_id)
            if not evidence_valid:
                raise RuntimeError("retrieval_evidence_validation_failed")
            terminal = finish_attempt(registry_path, number, "COMPLETED")
            append_jsonl(run_dir / "request_ledger.jsonl", terminal)
            record = {
                "issue_number": number,
                "title": issue["title"],
                "model_response_succeeded": True,
                "model_category": result.category,
                "model_priority": result.priority,
                "model_confidence": result.confidence,
                "search_decision": analysis["decision_reason"],
                "search_performed": analysis["search_performed"],
                "search_status": analysis["search_status"],
                "tool_calls": analysis["tool_calls"],
                "candidate_issues": analysis["candidate_issues"],
                "duplicate_assessment": analysis["duplicate_assessment"],
                "evidence_summary": analysis["evidence_summary"],
                "evidence_validated": evidence_valid,
                "needs_human_review": analysis["needs_human_review"],
                "flow_steps": analysis["flow_steps"],
                "model_request_count": 1,
                "api_error": None,
                "schema_error": None,
                "elapsed_seconds": elapsed,
                "unexpected_side_effects": False,
            }
            results.append(record)
            append_jsonl(run_dir / "results.jsonl", record)
        except Exception as exc:
            elapsed = round(time.monotonic() - started, 6)
            error_code = str(exc) if str(exc) in {
                "invalid_model_output", "retrieval_evidence_validation_failed"
            } else type(exc).__name__
            uncertain = type(exc).__name__ in {"APITimeoutError", "APIConnectionError", "TimeoutError"}
            terminal = finish_attempt(
                registry_path, number, "UNKNOWN_OUTCOME" if uncertain else "FAILED"
            )
            append_jsonl(run_dir / "request_ledger.jsonl", terminal)
            record = {
                "issue_number": number,
                "title": issue["title"],
                "model_response_succeeded": False,
                "model_request_count": 1,
                "api_error": None if error_code == "invalid_model_output" else error_code,
                "schema_error": error_code if error_code == "invalid_model_output" else None,
                "elapsed_seconds": elapsed,
                "unexpected_side_effects": False,
            }
            results.append(record)
            append_jsonl(run_dir / "results.jsonl", record)
            stop_reason = error_code
            break

    registry = load_registry(registry_path)
    manifest.update(
        {
            "status": "COMPLETED" if stop_reason is None and len(results) == len(selected) else "STOPPED_ON_FAILURE",
            "finish_time": utc_now(),
            "actual_qwen_request_attempts": len(registry["attempted"]),
            "successful_responses": sum(item["model_response_succeeded"] for item in results),
            "real_tool_executions": sum(item.get("tool_calls", 0) for item in results),
            "stop_reason": stop_reason,
        }
    )
    atomic_json(run_dir / "results.json", {"manifest": manifest, "items": results})
    atomic_json(run_dir / "manifest.json", manifest)
    return {"manifest": manifest, "items": results}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-real-qwen", action="store_true")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--source-sha256", required=True)
    args = parser.parse_args()
    result = execute(args)
    manifest = result["manifest"]
    print(f"REAL_SMOKE_STATUS={manifest['status']}")
    print(f"REAL_QWEN_API_CALLS={manifest['actual_qwen_request_attempts']}")
    print(f"SUCCESSFUL_RESPONSES={manifest['successful_responses']}")
    print(f"REAL_TOOL_EXECUTIONS={manifest['real_tool_executions']}")


if __name__ == "__main__":
    main()

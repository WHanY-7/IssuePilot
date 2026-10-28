"""Reproducible offline evaluation for IssuePilot triage predictions.

This module never calls a model or GitHub. Candidate labels are review aids only;
metrics use fields explicitly marked as human-verified gold labels.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


CATEGORIES = ("bug", "feature_request", "question", "other")
PRIORITIES = ("P0", "P1", "P2", "P3")
REVIEW_STATUSES = ("unverified", "verified", "excluded")
ANNOTATION_FIELDS = (
    "issue_number",
    "title",
    "source_url",
    "issue_summary",
    "candidate_category",
    "gold_category",
    "gold_priority",
    "review_notes",
    "review_status",
)
PREDICTION_FIELDS = (
    "issue_number",
    "predicted_category",
    "predicted_priority",
    "system_success",
    "system_error",
)
PREDICTION_RUN_METADATA_FIELDS = (
    "run_id",
    "model_name",
    "prompt_version",
    "run_started_at_utc",
    "run_finished_at_utc",
    "sample_count",
    "call_failure_count",
    "schema_failure_count",
    "max_paid_calls",
    "paid_retry_count",
    "gold_labels_used_as_input",
    "raw_predictions_retained",
)


def _read_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        records = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            raise ValueError("JSON must be an array or an object with an items array")
        if not all(isinstance(record, dict) for record in records):
            raise ValueError("Every record must be an object")
        return records
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    raise ValueError("Only .json and .csv files are supported")


def _positive_issue_number(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("issue_number must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("issue_number must be a positive integer") from exc
    if number <= 0 or str(value).strip() != str(number):
        raise ValueError("issue_number must be a positive integer")
    return number


def _normalized_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def load_dataset(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("Dataset must be an object with an items array")
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in payload["items"]:
        if not isinstance(raw, dict):
            raise ValueError("Every dataset item must be an object")
        number = _positive_issue_number(raw.get("issue_number"))
        if number in seen:
            raise ValueError(f"Duplicate issue_number: {number}")
        seen.add(number)
        for field in ("title", "body", "url"):
            if not _normalized_text(raw.get(field)):
                raise ValueError(f"Issue {number} has an empty {field}")
        labels = raw.get("existing_labels", [])
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            raise ValueError(f"Issue {number} has malformed existing_labels")
        records.append(dict(raw))
    return records


def candidate_category(issue: dict[str, Any]) -> str:
    """Return review assistance, never a gold label."""
    normalized = {label.strip().lower() for label in issue.get("existing_labels", [])}
    candidates = set()
    if "bug" in normalized:
        candidates.add("bug")
    if "feature-request" in normalized or "feature request" in normalized:
        candidates.add("feature_request")
    if len(candidates) == 1:
        return candidates.pop()
    group = _normalized_text(issue.get("sample_group"))
    if group in {"bug", "feature_request"}:
        return group
    return ""


def _summary(body: str, limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", body).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_annotation_template(dataset: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for issue in dataset:
        rows.append(
            {
                "issue_number": issue["issue_number"],
                "title": _normalized_text(issue.get("title")),
                "source_url": _normalized_text(issue.get("url")),
                "issue_summary": _summary(_normalized_text(issue.get("body"))),
                "candidate_category": candidate_category(issue),
                "gold_category": "",
                "gold_priority": "",
                "review_notes": "",
                "review_status": "unverified",
            }
        )
    return rows


def write_annotation_templates(
    rows: list[dict[str, Any]], csv_path: str | Path, json_path: str | Path
) -> None:
    csv_target, json_target = Path(csv_path), Path(json_path)
    csv_target.parent.mkdir(parents=True, exist_ok=True)
    json_target.parent.mkdir(parents=True, exist_ok=True)
    with csv_target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANNOTATION_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    json_target.write_text(
        json.dumps({"schema_version": 1, "items": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_annotations(path: str | Path) -> list[dict[str, Any]]:
    records = _read_records(Path(path))
    result = []
    seen: set[int] = set()
    for raw in records:
        missing = [field for field in ANNOTATION_FIELDS if field not in raw]
        if missing:
            raise ValueError(f"Annotation record is missing fields: {', '.join(missing)}")
        number = _positive_issue_number(raw["issue_number"])
        if number in seen:
            raise ValueError(f"Duplicate annotation issue_number: {number}")
        seen.add(number)
        category = _normalized_text(raw["gold_category"])
        priority = _normalized_text(raw["gold_priority"]).upper()
        status = _normalized_text(raw["review_status"]).lower()
        if status not in REVIEW_STATUSES:
            raise ValueError(f"Issue {number} has invalid review_status")
        if category and category not in CATEGORIES:
            raise ValueError(f"Issue {number} has invalid gold_category")
        if priority and priority not in PRIORITIES:
            raise ValueError(f"Issue {number} has invalid gold_priority")
        if status == "verified" and not (category or priority):
            raise ValueError(f"Issue {number} is verified but has no gold label")
        item = {field: _normalized_text(raw[field]) for field in ANNOTATION_FIELDS}
        item["issue_number"] = number
        item["gold_category"] = category
        item["gold_priority"] = priority
        item["review_status"] = status
        result.append(item)
    return result


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = _normalized_text(value).lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError("system_success must be true or false")


def load_predictions(path: str | Path) -> list[dict[str, Any]]:
    records = _read_records(Path(path))
    result = []
    seen: set[int] = set()
    for raw in records:
        missing = [field for field in PREDICTION_FIELDS if field not in raw]
        if missing:
            raise ValueError(f"Prediction record is missing fields: {', '.join(missing)}")
        number = _positive_issue_number(raw["issue_number"])
        if number in seen:
            raise ValueError(f"Duplicate prediction issue_number: {number}")
        seen.add(number)
        success = _parse_bool(raw["system_success"])
        category = _normalized_text(raw["predicted_category"])
        priority = _normalized_text(raw["predicted_priority"]).upper()
        error = _normalized_text(raw["system_error"])
        if success and (category not in CATEGORIES or priority not in PRIORITIES):
            raise ValueError(f"Issue {number} successful prediction has invalid structured output")
        if not success and not error:
            raise ValueError(f"Issue {number} failed prediction must include system_error")
        result.append(
            {
                "issue_number": number,
                "predicted_category": category,
                "predicted_priority": priority,
                "system_success": success,
                "system_error": error,
            }
        )
    return result


def _timestamp(value: Any, field: str) -> datetime:
    text = _normalized_text(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def load_prediction_run(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load an auditable prediction bundle for a paid evaluation run."""
    target = Path(path)
    if target.suffix.lower() != ".json":
        raise ValueError("Auditable prediction runs must use JSON")
    payload = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict):
        raise ValueError("Prediction run must contain a metadata object")
    if not isinstance(payload.get("items"), list):
        raise ValueError("Prediction run must contain an items array")
    metadata = payload["metadata"]
    missing = [field for field in PREDICTION_RUN_METADATA_FIELDS if field not in metadata]
    if missing:
        raise ValueError(f"Prediction run metadata is missing fields: {', '.join(missing)}")
    for field in ("run_id", "model_name", "prompt_version"):
        if not _normalized_text(metadata[field]):
            raise ValueError(f"Prediction run metadata {field} must not be blank")
    started = _timestamp(metadata["run_started_at_utc"], "run_started_at_utc")
    finished = _timestamp(metadata["run_finished_at_utc"], "run_finished_at_utc")
    if finished < started:
        raise ValueError("Prediction run finished before it started")

    predictions = load_predictions(target)
    for item in payload["items"]:
        if "raw_response" not in item or not isinstance(item["raw_response"], str):
            raise ValueError("Every prediction run item must retain raw_response")
    integers = {}
    for field in (
        "sample_count",
        "call_failure_count",
        "schema_failure_count",
        "max_paid_calls",
        "paid_retry_count",
    ):
        value = metadata[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Prediction run metadata {field} must be a non-negative integer")
        integers[field] = value
    schema_failures = sum(
        not item["system_success"] and item["system_error"] == "schema_parse_failure"
        for item in predictions
    )
    call_failures = sum(
        not item["system_success"] and item["system_error"] != "schema_parse_failure"
        for item in predictions
    )
    if integers["sample_count"] != len(predictions):
        raise ValueError("Prediction run sample_count does not match items")
    if integers["call_failure_count"] != call_failures:
        raise ValueError("Prediction run call_failure_count does not match items")
    if integers["schema_failure_count"] != schema_failures:
        raise ValueError("Prediction run schema_failure_count does not match items")
    if integers["max_paid_calls"] < len(predictions):
        raise ValueError("Prediction run exceeds max_paid_calls")
    if integers["paid_retry_count"] != 0:
        raise ValueError("Paid evaluation runs must not automatically retry")
    if _parse_bool(metadata["gold_labels_used_as_input"]):
        raise ValueError("Gold labels must not be used as model input")
    if not _parse_bool(metadata["raw_predictions_retained"]):
        raise ValueError("Raw predictions must be retained")
    normalized = {field: metadata[field] for field in PREDICTION_RUN_METADATA_FIELDS}
    return normalized, predictions


def _classification_metrics(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    if not pairs:
        return {"accuracy": None, "macro_f1": None, "classes_in_gold": [], "per_class": {}}
    classes = sorted({gold for gold, _ in pairs})
    correct = sum(gold == predicted for gold, predicted in pairs)
    per_class = {}
    f1_values = []
    for label in classes:
        tp = sum(gold == label and predicted == label for gold, predicted in pairs)
        fp = sum(gold != label and predicted == label for gold, predicted in pairs)
        fn = sum(gold == label and predicted != label for gold, predicted in pairs)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        support = sum(gold == label for gold, _ in pairs)
        per_class[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
        }
        f1_values.append(f1)
    return {
        "accuracy": correct / len(pairs),
        "macro_f1": sum(f1_values) / len(f1_values),
        "classes_in_gold": classes,
        "per_class": per_class,
    }


def evaluate(
    annotations: list[dict[str, Any]], predictions: list[dict[str, Any]]
) -> dict[str, Any]:
    prediction_by_issue = {item["issue_number"]: item for item in predictions}
    annotation_numbers = {item["issue_number"] for item in annotations}
    unknown = sorted(set(prediction_by_issue) - annotation_numbers)
    if unknown:
        raise ValueError(f"Predictions contain unknown issue_number values: {unknown}")

    category_pairs: list[tuple[str, str]] = []
    priority_pairs: list[tuple[str, str]] = []
    comparisons = []
    errors: Counter[str] = Counter()
    verified_category_count = 0
    verified_priority_count = 0
    category_correct_end_to_end = 0
    priority_correct_end_to_end = 0

    for annotation in annotations:
        prediction = prediction_by_issue.get(annotation["issue_number"])
        category_gold = (
            annotation["gold_category"] if annotation["review_status"] == "verified" else ""
        )
        priority_gold = (
            annotation["gold_priority"] if annotation["review_status"] == "verified" else ""
        )
        verified_category_count += bool(category_gold)
        verified_priority_count += bool(priority_gold)
        error_types = []
        category_match: bool | None = None
        priority_match: bool | None = None

        if prediction is None:
            if category_gold or priority_gold:
                error_types.append("missing_prediction")
        elif not prediction["system_success"]:
            error_types.append("system_failure")
        else:
            if category_gold:
                category_match = category_gold == prediction["predicted_category"]
                category_pairs.append((category_gold, prediction["predicted_category"]))
                category_correct_end_to_end += category_match
                if not category_match:
                    error_types.append("category_mismatch")
            if priority_gold:
                priority_match = priority_gold == prediction["predicted_priority"]
                priority_pairs.append((priority_gold, prediction["predicted_priority"]))
                priority_correct_end_to_end += priority_match
                if not priority_match:
                    error_types.append("priority_mismatch")

        errors.update(error_types)
        comparisons.append(
            {
                **{field: annotation[field] for field in ANNOTATION_FIELDS},
                "predicted_category": prediction["predicted_category"] if prediction else "",
                "predicted_priority": prediction["predicted_priority"] if prediction else "",
                "system_success": prediction["system_success"] if prediction else "",
                "system_error": prediction["system_error"] if prediction else "",
                "category_match": category_match if category_match is not None else "",
                "priority_match": priority_match if priority_match is not None else "",
                "error_types": ";".join(error_types),
            }
        )

    category = _classification_metrics(category_pairs)
    category.update(
        {
            "verified_gold_count": verified_category_count,
            "scored_count": len(category_pairs),
            "prediction_coverage": (
                len(category_pairs) / verified_category_count if verified_category_count else None
            ),
            "end_to_end_accuracy": (
                category_correct_end_to_end / verified_category_count
                if verified_category_count
                else None
            ),
            "status": (
                "COMPUTED"
                if category_pairs
                else "NOT_COMPUTABLE_NO_VERIFIED_GOLD"
                if not verified_category_count
                else "NOT_COMPUTABLE_NO_VALID_PREDICTIONS"
            ),
        }
    )
    priority = {
        "verified_gold_count": verified_priority_count,
        "scored_count": len(priority_pairs),
        "consistency": (
            sum(gold == predicted for gold, predicted in priority_pairs) / len(priority_pairs)
            if priority_pairs
            else None
        ),
        "prediction_coverage": (
            len(priority_pairs) / verified_priority_count if verified_priority_count else None
        ),
        "end_to_end_consistency": (
            priority_correct_end_to_end / verified_priority_count
            if verified_priority_count
            else None
        ),
        "status": (
            "COMPUTED"
            if priority_pairs
            else "NOT_COMPUTABLE_NO_VERIFIED_GOLD"
            if not verified_priority_count
            else "NOT_COMPUTABLE_NO_VALID_PREDICTIONS"
        ),
    }
    attempted = len(predictions)
    successful = sum(item["system_success"] for item in predictions)
    return {
        "summary": {
            "dataset_size": len(annotations),
            "review_status_counts": dict(Counter(row["review_status"] for row in annotations)),
            "prediction_record_count": attempted,
            "system_task_success_count": successful,
            "system_task_success_rate": successful / attempted if attempted else None,
            "category_metrics": category,
            "priority_metrics": priority,
            "error_type_counts": dict(errors),
            "limitations": [
                "Candidate categories and original GitHub labels are not gold labels.",
                "Only review_status=verified labels are used for model-quality metrics.",
                "System success rate is reported separately from classification quality.",
                "Small-sample results must not be represented as production performance.",
            ],
        },
        "comparisons": comparisons,
    }


def save_results(result: dict[str, Any], json_path: str | Path, csv_path: str | Path) -> None:
    json_target, csv_target = Path(json_path), Path(csv_path)
    json_target.parent.mkdir(parents=True, exist_ok=True)
    csv_target.parent.mkdir(parents=True, exist_ok=True)
    json_target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    comparisons = result["comparisons"]
    fields = list(ANNOTATION_FIELDS) + [
        "predicted_category",
        "predicted_priority",
        "system_success",
        "system_error",
        "category_match",
        "priority_match",
        "error_types",
    ]
    with csv_target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(comparisons)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    template = subparsers.add_parser("template", help="Create blank human annotation files")
    template.add_argument("--dataset", required=True)
    template.add_argument("--csv", required=True)
    template.add_argument("--json", required=True)
    run = subparsers.add_parser("evaluate", help="Evaluate saved predictions offline")
    run.add_argument("--annotations", required=True)
    prediction_source = run.add_mutually_exclusive_group()
    prediction_source.add_argument("--predictions")
    prediction_source.add_argument("--prediction-run")
    run.add_argument("--output-prefix", required=True)
    args = parser.parse_args()

    if args.command == "template":
        rows = build_annotation_template(load_dataset(args.dataset))
        write_annotation_templates(rows, args.csv, args.json)
        print(json.dumps({"items": len(rows), "verified": 0, "model_calls": 0}))
        return

    annotations = load_annotations(args.annotations)
    run_metadata = None
    if args.prediction_run:
        run_metadata, predictions = load_prediction_run(args.prediction_run)
    else:
        predictions = load_predictions(args.predictions) if args.predictions else []
    result = evaluate(annotations, predictions)
    if run_metadata is not None:
        result["summary"]["prediction_run_metadata"] = run_metadata
    prefix = Path(args.output_prefix)
    save_results(result, prefix.with_suffix(".json"), prefix.with_suffix(".csv"))
    print(json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()


"""Offline finalization for a completed or safely stopped Qwen pilot run."""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any

from evaluation.qwen_baseline_runner import atomic_json, sha256_file
from evaluation.triage_quality import load_annotations, load_dataset, load_prediction_run


SCORING_CLASSES = ("bug", "feature_request")
PREDICTION_COLUMNS = ("bug", "feature_request", "question", "other")


def atomic_text(path: Path, text: str) -> None:
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def compute_binary_metrics(
    gold_by_id: dict[int, dict[str, Any]], predictions: list[dict[str, Any]]
) -> dict[str, Any]:
    valid = [item for item in predictions if item["system_success"]]
    pairs = [(gold_by_id[item["issue_number"]]["gold_category"], item["predicted_category"]) for item in valid]
    correct = sum(gold == predicted for gold, predicted in pairs)
    per_class = {}
    for label in SCORING_CLASSES:
        tp = sum(gold == label and predicted == label for gold, predicted in pairs)
        fp = sum(gold != label and predicted == label for gold, predicted in pairs)
        fn = sum(gold == label and predicted != label for gold, predicted in pairs)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        per_class[label] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": _f1(precision, recall),
            "support_valid_predictions": sum(gold == label for gold, _ in pairs),
            "support_all_gold": sum(
                row["gold_category"] == label for row in gold_by_id.values()
            ),
        }
    confusion = {
        gold: {predicted: 0 for predicted in PREDICTION_COLUMNS} for gold in SCORING_CLASSES
    }
    for gold, predicted in pairs:
        confusion[gold][predicted] += 1
    schema_failures = sum(item["system_error"] == "schema_parse_failure" for item in predictions)
    # load_prediction_run intentionally returns only the normalized evaluation
    # fields. A schema failure still means that the API returned a response;
    # every other unsuccessful record is an API/request failure.
    api_successes = sum(item["system_success"] for item in predictions) + schema_failures
    api_failures = len(predictions) - api_successes
    macro_f1 = (
        sum(per_class[label]["f1"] for label in SCORING_CLASSES) / len(SCORING_CLASSES)
        if valid
        else None
    )
    return {
        "gold_sample_count": len(gold_by_id),
        "request_attempt_count": len(predictions),
        "successful_response_count": api_successes,
        "api_failure_count": api_failures,
        "schema_failure_count": schema_failures,
        "valid_classification_prediction_count": len(valid),
        "not_attempted_count": len(gold_by_id) - len(predictions),
        "accuracy_valid_predictions": _ratio(correct, len(valid)),
        "macro_f1_valid_predictions_fixed_classes": macro_f1,
        "per_class": per_class,
        "confusion_matrix_valid_predictions": confusion,
        "classification_coverage": _ratio(len(valid), len(gold_by_id)),
        "api_success_rate": _ratio(api_successes, len(predictions)),
        "schema_parsing_success_rate": _ratio(len(valid), api_successes),
        "end_to_end_correct_count": correct,
        "end_to_end_accuracy": _ratio(correct, len(gold_by_id)),
        "metric_denominators": {
            "valid_prediction_metrics": len(valid),
            "coverage_and_end_to_end": len(gold_by_id),
            "api_success_rate": len(predictions),
            "schema_parsing_success_rate": api_successes,
        },
    }


def _one_line(value: Any, limit: int = 1000) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _build_error_cases_legacy_mojibake(
    gold_by_id: dict[int, dict[str, Any]], predictions: list[dict[str, Any]]
) -> tuple[str, int]:
    classification_errors = [
        item
        for item in predictions
        if item["system_success"]
        and item["predicted_category"] != gold_by_id[item["issue_number"]]["gold_category"]
    ]
    schema_errors = [item for item in predictions if item["system_error"] == "schema_parse_failure"]
    api_errors = [
        item
        for item in predictions
        if not item["system_success"] and item["system_error"] != "schema_parse_failure"
    ]
    correct = [
        item
        for item in predictions
        if item["system_success"]
        and item["predicted_category"] == gold_by_id[item["issue_number"]]["gold_category"]
    ]
    lines = [
        "# Qwen batch-1 error cases",
        "",
        "下列内容区分模型实际输出、原工单事实与分析人员推测；不推断隐藏思考过程。",
        "",
        f"- 分类错误：{len(classification_errors)}",
        f"- API 错误/不确定结果：{len(api_errors)}",
        f"- Schema 解析失败：{len(schema_errors)}",
        f"- 有效且分类正确：{len(correct)}",
        "",
        "## Classification errors",
        "",
    ]
    if not classification_errors:
        lines.extend(["本次没有有效预测上的分类错误。", ""])
    for item in classification_errors:
        gold = gold_by_id[item["issue_number"]]
        predicted = item["predicted_category"]
        if gold["gold_category"] == "bug" and predicted == "feature_request":
            hypothesis = "可能将对现有异常行为的描述误读为改进请求（分析人员推测）。"
        elif gold["gold_category"] == "feature_request" and predicted == "bug":
            hypothesis = "可能将新增能力请求误读为既有功能失效（分析人员推测）。"
        else:
            hypothesis = "可能未识别本次二分类边界中的核心意图（分析人员推测）。"
        lines.extend(
            [
                f"### Issue #{item['issue_number']}",
                "",
                f"- 原始标题：{gold['title']}",
                f"- 原始来源 URL：{gold['source_url']}",
                f"- 人工类别：{gold['gold_category']}",
                f"- 模型实际预测类别：{predicted}",
                f"- 模型给出的简要理由：{_one_line(item.get('rationale'))}",
                f"- 原工单关键事实：{_one_line(gold.get('issue_summary'))}",
                f"- 可能误判原因：{hypothesis}",
                "- 错误类型：意图识别错误或类别边界偏移；若材料本身不足，应另行人工复核。",
                "",
            ]
        )
    lines.extend(["## API and schema failures", ""])
    if not api_errors and not schema_errors:
        lines.extend(["本次没有 API 错误或 Schema 解析失败。", ""])
    for item in api_errors + schema_errors:
        gold = gold_by_id[item["issue_number"]]
        lines.extend(
            [
                f"- Issue #{item['issue_number']} — {gold['title']}: {item['system_error']}",
            ]
        )
    lines.extend(["", "## Correct predictions", "", f"有效且分类正确的样本数：{len(correct)}。", ""])
    return "\n".join(lines), len(classification_errors)


def build_error_cases(
    gold_by_id: dict[int, dict[str, Any]], predictions: list[dict[str, Any]]
) -> tuple[str, int]:
    """Build a UTF-8 error report while keeping facts and hypotheses distinct."""
    classification_errors = [
        item
        for item in predictions
        if item["system_success"]
        and item["predicted_category"] != gold_by_id[item["issue_number"]]["gold_category"]
    ]
    schema_errors = [item for item in predictions if item["system_error"] == "schema_parse_failure"]
    api_errors = [
        item
        for item in predictions
        if not item["system_success"] and item["system_error"] != "schema_parse_failure"
    ]
    correct = [
        item
        for item in predictions
        if item["system_success"]
        and item["predicted_category"] == gold_by_id[item["issue_number"]]["gold_category"]
    ]
    lines = [
        "# Qwen batch-1 error cases",
        "",
        "以下内容区分模型实际输出、原工单事实与分析人员推测；不推断隐藏思考过程。",
        "",
        f"- 分类错误：{len(classification_errors)}",
        f"- API 错误/不确定结果：{len(api_errors)}",
        f"- Schema 解析失败：{len(schema_errors)}",
        f"- 有效且分类正确：{len(correct)}",
        "",
        "## Classification errors",
        "",
    ]
    if not classification_errors:
        lines.extend(["本次没有有效预测上的分类错误。", ""])
    for item in classification_errors:
        gold = gold_by_id[item["issue_number"]]
        predicted = item["predicted_category"]
        if gold["gold_category"] == "bug" and predicted == "feature_request":
            hypothesis = "可能将对现有异常行为的描述误读为改进请求（分析人员推测）。"
        elif gold["gold_category"] == "feature_request" and predicted == "bug":
            hypothesis = "可能将新增能力请求误读为已有功能失效（分析人员推测）。"
        else:
            hypothesis = "可能未识别本次二分类边界中的核心意图（分析人员推测）。"
        lines.extend(
            [
                f"### Issue #{item['issue_number']}",
                "",
                f"- 原始标题：{gold['title']}",
                f"- 原始来源 URL：{gold['source_url']}",
                f"- 人工类别：{gold['gold_category']}",
                f"- 模型实际预测类别：{predicted}",
                f"- 模型给出的简要理由：{_one_line(item.get('rationale'))}",
                f"- 原工单关键事实：{_one_line(gold.get('issue_summary'))}",
                f"- 可能误判原因：{hypothesis}",
                "- 错误类型：意图识别错误或类别边界偏移；若材料本身不足，应另行人工复核。",
                "",
            ]
        )
    lines.extend(["## API and schema failures", ""])
    if not api_errors and not schema_errors:
        lines.extend(["本次没有 API 错误或 Schema 解析失败。", ""])
    for item in api_errors + schema_errors:
        gold = gold_by_id[item["issue_number"]]
        lines.append(f"- Issue #{item['issue_number']} — {gold['title']}: {item['system_error']}")
    lines.extend(["", "## Correct predictions", "", f"有效且分类正确的样本数：{len(correct)}。", ""])
    return "\n".join(lines), len(classification_errors)


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).resolve()
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    gold_path = Path(args.gold).resolve()
    source_path = Path(args.source).resolve()
    registry_path = Path(args.registry).resolve()
    if sha256_file(gold_path) != manifest["gold_dataset_sha256"]:
        raise RuntimeError("Gold CSV changed during the run")
    if sha256_file(source_path) != manifest["source_dataset_sha256"]:
        raise RuntimeError("Source dataset changed during the run")

    annotations = load_annotations(gold_path)
    verified = [
        row
        for row in annotations
        if row["review_status"] == "verified" and row["gold_category"] in SCORING_CLASSES
    ]
    if len(verified) != 11 or any(row["gold_priority"] for row in verified):
        raise RuntimeError("Gold set no longer matches the frozen category-only pilot")
    gold_by_id = {row["issue_number"]: row for row in verified}
    metadata, predictions = load_prediction_run(run_dir / "prediction_run.json")
    prediction_ids = [item["issue_number"] for item in predictions]
    if len(prediction_ids) != len(set(prediction_ids)) or 337331 in prediction_ids:
        raise RuntimeError("Prediction IDs are duplicated or include the excluded issue")
    if any(number not in gold_by_id for number in prediction_ids):
        raise RuntimeError("Prediction contains an issue outside the frozen gold set")

    ledger_lines = [
        json.loads(line)
        for line in (run_dir / "request_ledger.jsonl").read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    starts = [row for row in ledger_lines if row["request_state"] == "IN_FLIGHT"]
    terminals = [row for row in ledger_lines if row["request_state"] != "IN_FLIGHT"]
    if len(starts) != len(predictions) or len(terminals) != len(predictions):
        raise RuntimeError("Ledger and prediction counts differ")
    if any(row["attempt_count"] != 1 for row in ledger_lines):
        raise RuntimeError("A request attempt count exceeded one")
    registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    if {int(number) for number in registry["attempted"]} != set(prediction_ids):
        raise RuntimeError("Batch registry and predictions differ")
    jsonl_predictions = [
        json.loads(line)
        for line in (run_dir / "predictions.jsonl").read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    with (run_dir / "predictions.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        csv_predictions = list(csv.DictReader(handle))
    if len(jsonl_predictions) != len(predictions) or len(csv_predictions) != len(predictions):
        raise RuntimeError("Prediction output formats are inconsistent")

    metrics = compute_binary_metrics(gold_by_id, predictions)
    metrics["evaluation_run_id"] = metadata["run_id"]
    metrics["model_name"] = metadata["model_name"]
    metrics["prompt_version"] = metadata["prompt_version"]
    metrics["scoring_classes"] = list(SCORING_CLASSES)
    metrics["priority_metrics_status"] = "NOT_COMPUTED_NO_GOLD_PRIORITY"
    atomic_json(run_dir / "metrics.json", metrics)
    rows = []
    for key in (
        "gold_sample_count",
        "request_attempt_count",
        "successful_response_count",
        "api_failure_count",
        "schema_failure_count",
        "valid_classification_prediction_count",
        "accuracy_valid_predictions",
        "macro_f1_valid_predictions_fixed_classes",
        "classification_coverage",
        "api_success_rate",
        "schema_parsing_success_rate",
        "end_to_end_accuracy",
    ):
        rows.append({"metric": key, "value": metrics[key]})
    for label in SCORING_CLASSES:
        for metric in ("precision", "recall", "f1", "tp", "fp", "fn"):
            rows.append(
                {"metric": f"{label}_{metric}", "value": metrics["per_class"][label][metric]}
            )
    temp_csv = run_dir / "metrics.csv.tmp"
    with temp_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("metric", "value"))
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_csv, run_dir / "metrics.csv")

    error_markdown, error_count = build_error_cases(gold_by_id, predictions)
    atomic_text(run_dir / "error_cases.md", error_markdown)
    input_tokens = sum(item.get("input_tokens") or 0 for item in predictions)
    output_tokens = sum(item.get("output_tokens") or 0 for item in predictions)
    summary = [
        "# Qwen batch-1 run summary",
        "",
        f"- Run: {metadata['run_id']}",
        f"- Model: {metadata['model_name']}",
        f"- Prompt: {metadata['prompt_version']}",
        f"- Requests attempted: {metrics['request_attempt_count']} / 11",
        f"- Successful API responses: {metrics['successful_response_count']}",
        f"- API failures: {metrics['api_failure_count']}",
        f"- Schema failures: {metrics['schema_failure_count']}",
        f"- Valid classification predictions: {metrics['valid_classification_prediction_count']}",
        f"- Accuracy (valid predictions): {metrics['accuracy_valid_predictions']}",
        f"- Macro-F1 (fixed bug/feature_request): {metrics['macro_f1_valid_predictions_fixed_classes']}",
        f"- Classification coverage: {metrics['classification_coverage']}",
        f"- End-to-end accuracy (all 11 gold samples): {metrics['end_to_end_accuracy']}",
        f"- Input tokens reported: {input_tokens}",
        f"- Output tokens reported: {output_tokens}",
        f"- Classification error cases: {error_count}",
        "- Priority metrics: not computed because gold_priority is blank.",
        "- GitHub writes: 0.",
        "",
        "This is a small, selected, single-repository locked-gold pilot. It is not a strict external test set and does not establish production performance.",
        "",
    ]
    atomic_text(run_dir / "run_summary.md", "\n".join(summary))
    return {"metrics": metrics, "error_cases_count": error_count, "input_tokens": input_tokens, "output_tokens": output_tokens}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--gold", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--registry", required=True)
    args = parser.parse_args()
    result = finalize(args)
    metrics = result["metrics"]
    print(f"VALID_CLASSIFICATION_PREDICTIONS={metrics['valid_classification_prediction_count']}")
    print(f"ACCURACY={metrics['accuracy_valid_predictions']}")
    print(f"MACRO_F1={metrics['macro_f1_valid_predictions_fixed_classes']}")
    print(f"ERROR_CASES_COUNT={result['error_cases_count']}")


if __name__ == "__main__":
    main()

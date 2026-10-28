import json
import tempfile
import unittest
from pathlib import Path

from evaluation.qwen_baseline_finalize import compute_binary_metrics
from evaluation.qwen_baseline_runner import (
    load_registry,
    parse_model_response,
    reserve_attempt,
    validate_blind_input,
)


class QwenBaselineSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="issuepilot-qwen-runner-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_blind_input_rejects_gold_and_excluded_issue(self):
        items = [
            {"repository": "microsoft/vscode", "issue_number": number, "title": "T", "body": "B"}
            for number in range(1, 12)
        ]
        self.assertEqual(len(validate_blind_input({"items": items})), 11)
        items[0]["gold_category"] = "bug"
        with self.assertRaisesRegex(RuntimeError, "unexpected fields"):
            validate_blind_input({"items": items})
        items[0].pop("gold_category")
        items[0]["issue_number"] = 337331
        with self.assertRaisesRegex(RuntimeError, "excluded issue"):
            validate_blind_input({"items": items})

    def test_registry_persists_before_request_and_blocks_duplicates(self):
        path = self.root / "registry.json"
        reserve_attempt(path, 123, "run-1", "2026-10-27T12:00:00+00:00")
        record = load_registry(path)["attempted"]["123"]
        self.assertEqual(record["request_state"], "IN_FLIGHT")
        self.assertEqual(record["attempt_count"], 1)
        with self.assertRaisesRegex(RuntimeError, "already attempted"):
            reserve_attempt(path, 123, "run-2", "2026-10-27T12:01:00+00:00")

    def test_production_schema_parser_preserves_actual_category(self):
        raw = json.dumps(
            {
                "category": "other",
                "priority": "P3",
                "confidence": 0.6,
                "summary": "The issue intent is unclear",
                "rationale": "The supplied report does not fit the main classes",
                "missing_information": [],
                "suggested_labels": [],
                "reply_draft": "Please provide more details about the requested outcome.",
            }
        )
        self.assertEqual(parse_model_response(raw).category, "other")
        with self.assertRaisesRegex(RuntimeError, "schema_parse_failure"):
            parse_model_response('{"category":"bug"}')

    def test_fixed_binary_metrics_keep_other_and_failures_visible(self):
        gold = {
            1: {"gold_category": "bug"},
            2: {"gold_category": "feature_request"},
            3: {"gold_category": "bug"},
            4: {"gold_category": "feature_request"},
        }
        predictions = [
            {
                "issue_number": 1,
                "predicted_category": "bug",
                "system_success": True,
                "system_error": "",
                "api_call_succeeded": True,
            },
            {
                "issue_number": 2,
                "predicted_category": "other",
                "system_success": True,
                "system_error": "",
                "api_call_succeeded": True,
            },
            {
                "issue_number": 3,
                "predicted_category": "",
                "system_success": False,
                "system_error": "APIConnectionError",
                "api_call_succeeded": False,
            },
        ]
        metrics = compute_binary_metrics(gold, predictions)
        self.assertEqual(metrics["accuracy_valid_predictions"], 0.5)
        self.assertEqual(metrics["macro_f1_valid_predictions_fixed_classes"], 0.5)
        self.assertEqual(metrics["classification_coverage"], 0.5)
        self.assertEqual(metrics["end_to_end_accuracy"], 0.25)
        self.assertEqual(metrics["confusion_matrix_valid_predictions"]["feature_request"]["other"], 1)
        self.assertEqual(metrics["api_failure_count"], 1)

    def test_schema_failure_counts_as_api_response_not_api_failure(self):
        gold = {1: {"gold_category": "bug"}}
        predictions = [
            {
                "issue_number": 1,
                "predicted_category": "",
                "system_success": False,
                "system_error": "schema_parse_failure",
            }
        ]
        metrics = compute_binary_metrics(gold, predictions)
        self.assertEqual(metrics["successful_response_count"], 1)
        self.assertEqual(metrics["api_failure_count"], 0)
        self.assertEqual(metrics["schema_failure_count"], 1)


if __name__ == "__main__":
    unittest.main()

import csv
import json
import tempfile
import unittest
from pathlib import Path

from evaluation.triage_quality import (
    ANNOTATION_FIELDS,
    build_annotation_template,
    evaluate,
    load_annotations,
    load_dataset,
    load_prediction_run,
    load_predictions,
    save_results,
    write_annotation_templates,
)


def annotation(number, category="", priority="", status="unverified"):
    return {
        "issue_number": number,
        "title": f"Issue {number}",
        "source_url": f"https://example.invalid/{number}",
        "issue_summary": "Human-readable summary",
        "candidate_category": "bug",
        "gold_category": category,
        "gold_priority": priority,
        "review_notes": "Independent human review evidence",
        "review_status": status,
    }


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="issuepilot-eval-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_dataset_validation_and_unlabeled_candidate(self):
        path = self.write_json(
            "dataset.json",
            {
                "items": [
                    {
                        "issue_number": 1,
                        "title": "Crash",
                        "body": "Application crashes while opening a file.",
                        "url": "https://example.invalid/1",
                        "existing_labels": ["bug"],
                        "sample_group": "bug",
                    },
                    {
                        "issue_number": 2,
                        "title": "Needs review",
                        "body": "No source label was attached to this report.",
                        "url": "https://example.invalid/2",
                        "existing_labels": [],
                        "sample_group": "unlabeled",
                    },
                ]
            },
        )
        rows = build_annotation_template(load_dataset(path))
        self.assertEqual(rows[0]["candidate_category"], "bug")
        self.assertEqual(rows[1]["candidate_category"], "")
        self.assertTrue(all(row["gold_category"] == "" for row in rows))
        self.assertTrue(all(row["review_status"] == "unverified" for row in rows))

    def test_annotation_round_trip_csv_and_json(self):
        rows = [annotation(1), annotation(2)]
        csv_path, json_path = self.root / "gold.csv", self.root / "gold.json"
        write_annotation_templates(rows, csv_path, json_path)
        self.assertEqual(load_annotations(csv_path), load_annotations(json_path))
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(tuple(csv.DictReader(handle).fieldnames), ANNOTATION_FIELDS)

    def test_no_gold_and_no_predictions_never_fabricates_metrics(self):
        result = evaluate([annotation(1), annotation(2)], [])
        summary = result["summary"]
        self.assertEqual(summary["category_metrics"]["status"], "NOT_COMPUTABLE_NO_VERIFIED_GOLD")
        self.assertIsNone(summary["category_metrics"]["accuracy"])
        self.assertIsNone(summary["category_metrics"]["macro_f1"])
        self.assertIsNone(summary["priority_metrics"]["consistency"])
        self.assertIsNone(summary["system_task_success_rate"])

    def test_accuracy_macro_f1_priority_and_error_cases(self):
        annotations = [
            annotation(1, "bug", "P1", "verified"),
            annotation(2, "bug", "P2", "verified"),
            annotation(3, "feature_request", "P3", "verified"),
            annotation(4),
        ]
        predictions = [
            {"issue_number": 1, "predicted_category": "bug", "predicted_priority": "P1", "system_success": True, "system_error": ""},
            {"issue_number": 2, "predicted_category": "feature_request", "predicted_priority": "P3", "system_success": True, "system_error": ""},
            {"issue_number": 3, "predicted_category": "feature_request", "predicted_priority": "P3", "system_success": True, "system_error": ""},
            {"issue_number": 4, "predicted_category": "other", "predicted_priority": "P3", "system_success": True, "system_error": ""},
        ]
        result = evaluate(annotations, predictions)
        category = result["summary"]["category_metrics"]
        priority = result["summary"]["priority_metrics"]
        self.assertAlmostEqual(category["accuracy"], 2 / 3)
        self.assertAlmostEqual(category["macro_f1"], (2 / 3 + 2 / 3) / 2)
        self.assertAlmostEqual(priority["consistency"], 2 / 3)
        self.assertEqual(result["summary"]["error_type_counts"], {"category_mismatch": 1, "priority_mismatch": 1})

    def test_system_failure_is_separate_from_model_quality(self):
        annotations = [
            annotation(1, "bug", "P1", "verified"),
            annotation(2, "feature_request", "P2", "verified"),
        ]
        predictions = [
            {"issue_number": 1, "predicted_category": "bug", "predicted_priority": "P1", "system_success": True, "system_error": ""},
            {"issue_number": 2, "predicted_category": "", "predicted_priority": "", "system_success": False, "system_error": "timeout"},
        ]
        result = evaluate(annotations, predictions)["summary"]
        self.assertEqual(result["system_task_success_rate"], 0.5)
        self.assertEqual(result["category_metrics"]["accuracy"], 1.0)
        self.assertEqual(result["category_metrics"]["prediction_coverage"], 0.5)
        self.assertEqual(result["category_metrics"]["end_to_end_accuracy"], 0.5)
        self.assertEqual(result["error_type_counts"], {"system_failure": 1})

    def test_structured_prediction_and_annotation_reject_bad_input(self):
        bad_predictions = self.write_json(
            "bad_predictions.json",
            [{"issue_number": 1, "predicted_category": "defect", "predicted_priority": "urgent", "system_success": True, "system_error": ""}],
        )
        with self.assertRaisesRegex(ValueError, "invalid structured output"):
            load_predictions(bad_predictions)
        bad_annotations = self.write_json(
            "bad_annotations.json", {"items": [annotation(1, status="verified")]}
        )
        with self.assertRaisesRegex(ValueError, "verified but has no gold label"):
            load_annotations(bad_annotations)

    def test_missing_prediction_and_json_csv_result_output(self):
        result = evaluate([annotation(1, "bug", "P2", "verified")], [])
        self.assertEqual(result["summary"]["error_type_counts"], {"missing_prediction": 1})
        json_path, csv_path = self.root / "result.json", self.root / "result.csv"
        save_results(result, json_path, csv_path)
        self.assertTrue(json_path.is_file())
        self.assertTrue(csv_path.is_file())
        saved = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["comparisons"][0]["error_types"], "missing_prediction")

    def test_auditable_prediction_run_metadata_and_raw_records(self):
        payload = {
            "metadata": {
                "run_id": "batch1-qwen-001",
                "model_name": "qwen3.8-flash",
                "prompt_version": "triage-system-sha256:abc123",
                "run_started_at_utc": "2026-10-27T12:00:00Z",
                "run_finished_at_utc": "2026-10-27T12:01:00Z",
                "sample_count": 2,
                "call_failure_count": 1,
                "schema_failure_count": 0,
                "max_paid_calls": 11,
                "paid_retry_count": 0,
                "gold_labels_used_as_input": False,
                "raw_predictions_retained": True,
            },
            "items": [
                {
                    "issue_number": 1,
                    "predicted_category": "bug",
                    "predicted_priority": "P2",
                    "system_success": True,
                    "system_error": "",
                    "raw_response": '{"category":"bug","priority":"P2"}',
                },
                {
                    "issue_number": 2,
                    "predicted_category": "",
                    "predicted_priority": "",
                    "system_success": False,
                    "system_error": "timeout",
                    "raw_response": "",
                },
            ],
        }
        path = self.write_json("prediction_run.json", payload)
        metadata, predictions = load_prediction_run(path)
        self.assertEqual(metadata["sample_count"], 2)
        self.assertEqual(metadata["call_failure_count"], 1)
        self.assertEqual(len(predictions), 2)

    def test_prediction_run_rejects_gold_input_or_paid_retries(self):
        metadata = {
            "run_id": "unsafe-run",
            "model_name": "qwen3.8-flash",
            "prompt_version": "frozen-prompt",
            "run_started_at_utc": "2026-10-27T12:00:00+00:00",
            "run_finished_at_utc": "2026-10-27T12:00:01+00:00",
            "sample_count": 0,
            "call_failure_count": 0,
            "schema_failure_count": 0,
            "max_paid_calls": 11,
            "paid_retry_count": 1,
            "gold_labels_used_as_input": True,
            "raw_predictions_retained": True,
        }
        path = self.write_json("unsafe_run.json", {"metadata": metadata, "items": []})
        with self.assertRaisesRegex(ValueError, "must not automatically retry"):
            load_prediction_run(path)
        metadata["paid_retry_count"] = 0
        path = self.write_json("unsafe_run.json", {"metadata": metadata, "items": []})
        with self.assertRaisesRegex(ValueError, "Gold labels must not be used"):
            load_prediction_run(path)


if __name__ == "__main__":
    unittest.main()

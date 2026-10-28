import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SimilarityRealSmokeRunnerTests(unittest.TestCase):
    def setUp(self):
        from evaluation import similarity_real_smoke_runner as runner
        from issuepilot.models import TriageResult

        self.runner = runner
        self.TriageResult = TriageResult
        self.environment = patch.dict(
            os.environ,
            {
                "QWEN_API_KEY": "test-only-placeholder-key",
                "QWEN_MODEL": "qwen3.8-flash",
            },
        )
        self.environment.start()
        self.temp = tempfile.TemporaryDirectory(
            prefix="issuepilot-real-smoke-",
            dir=Path(__file__).resolve().parents[1] / "data",
        )
        self.root = Path(self.temp.name)
        items = []
        for number, title, body in (
            (244597, "Editor crash on open", "The editor crashes when a file opens."),
            (342692, "Add trophy icon", "Please use a trophy icon for comparison."),
            (342851, "Account preference unclear", "The selected account appears inconsistent."),
            (99, "Opening a file crashes editor", "A file open operation crashes the editor."),
        ):
            items.append(
                {
                    "repository": "example/project",
                    "issue_number": number,
                    "title": title,
                    "body": body,
                    "sample_group": "synthetic",
                    "existing_labels": [],
                    "url": f"https://github.com/example/project/issues/{number}",
                }
            )
        self.source = self.root / "source.json"
        self.source.write_text(json.dumps({"items": items}), encoding="utf-8")
        self.registry = self.root / "registry.json"

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def _classifier(self, issue):
        category = "question" if issue["issue_number"] == 342851 else (
            "feature_request" if issue["issue_number"] == 342692 else "bug"
        )
        return self.TriageResult(
            category=category,
            priority="P2",
            confidence=0.8,
            summary="Synthetic smoke triage",
            rationale="Synthetic classifier response for offline safety testing.",
            missing_information=[],
            suggested_labels=[],
            reply_draft="Please review this synthetic test result before any action.",
        )

    def _args(self, run_name="similarity_real_smoke_test"):
        return argparse.Namespace(
            execute_real_qwen=True,
            run_dir=str(self.root / run_name),
            registry=str(self.registry),
            source=str(self.source),
            source_sha256=self.runner.sha256_file(self.source),
        )

    def test_mock_run_freezes_blind_input_and_enforces_budgets(self):
        result = self.runner.execute(self._args(), classifier=self._classifier)
        manifest = result["manifest"]
        self.assertEqual(manifest["actual_qwen_request_attempts"], 3)
        self.assertEqual(manifest["successful_responses"], 3)
        self.assertEqual(manifest["real_tool_executions"], 2)
        frozen = json.loads((self.root / "similarity_real_smoke_test" / "frozen_input.json").read_text())
        self.assertTrue(all(set(item) == self.runner.ALLOWED_INPUT_FIELDS for item in frozen["items"]))
        registry = self.runner.load_registry(self.registry)
        self.assertEqual(len(registry["attempted"]), 3)
        self.assertTrue(all(item["attempt_count"] == 1 for item in registry["attempted"].values()))

    def test_registry_and_prior_ledger_block_a_new_run_directory(self):
        self.runner.execute(self._args("similarity_real_smoke_first"), classifier=self._classifier)
        with self.assertRaisesRegex(RuntimeError, "already_attempted"):
            self.runner.execute(self._args("similarity_real_smoke_second"), classifier=self._classifier)


if __name__ == "__main__":
    unittest.main()

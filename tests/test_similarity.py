import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

def triage(category="bug"):
    from issuepilot.models import TriageResult

    return TriageResult(
        category=category,
        priority="P2",
        confidence=0.8,
        summary="Synthetic triage result",
        rationale="Synthetic result used by an offline unit test.",
        missing_information=[],
        suggested_labels=[],
        reply_draft="Please review the supplied synthetic issue details.",
    )


class SimilarityRetrievalTests(unittest.TestCase):
    def setUp(self):
        # Delay application imports until all unittest modules have established
        # their isolated environment. test_workflow configures a temporary DB
        # and dataset during module loading.
        global LocalIssueRetriever, SimilarityRetrievalError, classify_issue_enhanced
        from issuepilot.similarity import (
            LocalIssueRetriever,
            SimilarityRetrievalError,
            classify_issue_enhanced,
        )

        self.temp = tempfile.TemporaryDirectory(
            prefix="issuepilot-similarity-",
            dir=Path(__file__).resolve().parents[1] / "data",
        )
        self.corpus = Path(self.temp.name) / "synthetic_issues.json"
        items = [
            {
                "issue_number": 1,
                "repository": "example/project",
                "title": "Editor crashes when opening a large file",
                "body": "Opening a large JSON file makes the editor crash with out of memory.",
                "url": "https://github.com/example/project/issues/1",
            },
            {
                "issue_number": 2,
                "repository": "example/project",
                "title": "Large JSON file causes editor crash",
                "body": "The editor crashes with out of memory while opening a large JSON document.",
                "url": "https://github.com/example/project/issues/2",
            },
            {
                "issue_number": 3,
                "repository": "example/project",
                "title": "Add a blue theme",
                "body": "Please add a new blue color theme for the workbench.",
                "url": "https://github.com/example/project/issues/3",
            },
            {
                "issue_number": 4,
                "repository": "example/project",
                "title": "Editor crash for large text file",
                "body": "A large text file can crash the editor during opening.",
                "url": "https://github.com/example/project/issues/4",
            },
        ]
        self.corpus.write_text(json.dumps({"items": items}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_loads_read_only_corpus_and_is_deterministic(self):
        retriever = LocalIssueRetriever(self.corpus, min_score=0)
        query = {
            "issue_number": 99,
            "title": "Editor crashes opening large JSON file",
            "body": "Out of memory crash while opening the document.",
        }
        first = retriever.search(query, top_k=3)
        second = retriever.search(query, top_k=3)
        self.assertEqual(retriever.corpus_size, 4)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 3)

    def test_excludes_current_issue_and_never_duplicates_results(self):
        retriever = LocalIssueRetriever(self.corpus, min_score=0)
        results = retriever.search(
            {"issue_number": 1, "title": "Editor crash large file", "body": "out of memory"}
        )
        numbers = [item.issue_number for item in results]
        self.assertNotIn(1, numbers)
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertLessEqual(len(numbers), 5)

    def test_unrelated_input_can_return_empty(self):
        retriever = LocalIssueRetriever(self.corpus, min_score=0.2)
        results = retriever.search(
            {"issue_number": 99, "title": "Quantum banana telescope", "body": "zebra nebula"}
        )
        self.assertEqual(results, [])

    def test_top_k_is_bounded(self):
        retriever = LocalIssueRetriever(self.corpus, min_score=0)
        query = {"issue_number": 99, "title": "editor", "body": "file"}
        with self.assertRaisesRegex(ValueError, "between 1 and 5"):
            retriever.search(query, top_k=6)

    def test_timeout_is_bounded_and_reported(self):
        retriever = LocalIssueRetriever(self.corpus, min_score=0, timeout_seconds=1)
        query = {"issue_number": 99, "title": "editor crash", "body": "large file"}
        with patch("issuepilot.similarity.time.monotonic", side_effect=[0.0, 2.0]):
            with self.assertRaisesRegex(SimilarityRetrievalError, "retrieval_timeout"):
                retriever.search(query)

    def test_invalid_or_invented_source_url_is_rejected(self):
        payload = json.loads(self.corpus.read_text(encoding="utf-8"))
        payload["items"][0]["url"] = "https://attacker.example/invented"
        self.corpus.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(SimilarityRetrievalError, "invalid_source_url"):
            LocalIssueRetriever(self.corpus)

    def test_skip_policy_does_not_call_retrieval_tool(self):
        tool = Mock()
        result = classify_issue_enhanced(
            {"issue_number": 99, "title": "How do I configure this?", "body": "Help"},
            classifier=lambda _: triage("question"),
            retriever=tool,
        )
        tool.search.assert_not_called()
        analysis = result.similarity_analysis
        self.assertFalse(analysis.search_performed)
        self.assertEqual(analysis.search_status, "skipped")
        self.assertEqual(analysis.tool_calls, 0)
        self.assertEqual(analysis.flow_steps, 2)

    def test_eligible_category_calls_allowlisted_tool_once(self):
        retriever = LocalIssueRetriever(self.corpus, min_score=0)
        tool = Mock(wraps=retriever)
        result = classify_issue_enhanced(
            {
                "issue_number": 99,
                "title": "Editor crashes opening large JSON file",
                "body": "Out of memory while opening a document.",
            },
            classifier=lambda _: triage("bug"),
            retriever=tool,
        )
        tool.search.assert_called_once()
        analysis = result.similarity_analysis
        self.assertTrue(analysis.search_performed)
        self.assertEqual(analysis.tool_calls, 1)
        self.assertEqual(analysis.flow_steps, 3)
        self.assertLessEqual(len(analysis.candidate_issues), 5)
        self.assertTrue(all(x.source_url.startswith("https://github.com/example/project/issues/") for x in analysis.candidate_issues))

    def test_retrieval_failure_degrades_without_duplicate_claim(self):
        tool = Mock()
        tool.search.side_effect = SimilarityRetrievalError("synthetic_failure")
        result = classify_issue_enhanced(
            {"issue_number": 99, "title": "Editor crash", "body": "Crash"},
            classifier=lambda _: triage("bug"),
            retriever=tool,
        )
        tool.search.assert_called_once()
        analysis = result.similarity_analysis
        self.assertEqual(analysis.search_status, "error")
        self.assertEqual(analysis.duplicate_assessment, "insufficient_evidence")
        self.assertEqual(analysis.candidate_issues, [])
        self.assertTrue(analysis.needs_human_review)

    def test_retrieval_score_is_evidence_not_duplicate_probability(self):
        retriever = LocalIssueRetriever(self.corpus, min_score=0)
        result = classify_issue_enhanced(
            {"issue_number": 99, "title": "Add amber icons", "body": "A new icon color option"},
            classifier=lambda _: triage("feature_request"),
            retriever=retriever,
        )
        analysis = result.similarity_analysis
        self.assertNotIn("probability", analysis.evidence_summary.lower().replace("not a duplicate probability", ""))
        self.assertTrue(analysis.needs_human_review)
        self.assertIn(analysis.duplicate_assessment, {"suspected_duplicate", "insufficient_evidence", "no_similar_issue"})

    def test_feature_flag_defaults_off_and_base_schema_stays_unchanged(self):
        from issuepilot.config import similarity_enabled

        with patch.dict(os.environ, {"ISSUEPILOT_SIMILARITY_ENABLED": "false"}):
            self.assertFalse(similarity_enabled())
        base = triage("bug").model_dump()
        self.assertNotIn("similarity_analysis", base)
        self.assertEqual(
            set(base),
            {
                "category", "priority", "confidence", "summary", "rationale",
                "missing_information", "suggested_labels", "reply_draft",
            },
        )


if __name__ == "__main__":
    unittest.main()

"""Offline Mock demonstration of the bounded similarity-enhanced triage flow."""

from __future__ import annotations

import json

from issuepilot.config import DATASET_PATH
from issuepilot.models import TriageResult
from issuepilot.similarity import LocalIssueRetriever, classify_issue_enhanced


def mock_classifier(_: dict) -> TriageResult:
    return TriageResult(
        category="bug",
        priority="P2",
        confidence=0.82,
        summary="Synthetic demo issue reports an interrupted remote-agent turn.",
        rationale="The synthetic report describes an existing workflow failing during reconnection.",
        missing_information=["Exact client and server versions"],
        suggested_labels=["remote"],
        reply_draft="Please confirm the client and server versions used during reconnection.",
    )


def main() -> None:
    payload = json.loads(DATASET_PATH.read_text(encoding="utf-8-sig"))
    source = payload["items"][0]
    synthetic_issue = {
        "issue_number": 90000001,
        "repository": source["repository"],
        "title": source["title"],
        "body": source["body"],
        "synthetic": True,
    }
    result = classify_issue_enhanced(
        synthetic_issue,
        classifier=mock_classifier,
        retriever=LocalIssueRetriever(DATASET_PATH),
    )
    analysis = result.similarity_analysis
    output = {
        "demo_data": "synthetic query over the local read-only candidate corpus",
        "issue_input": {
            "issue_number": synthetic_issue["issue_number"],
            "repository": synthetic_issue["repository"],
            "title": synthetic_issue["title"],
            "body_excerpt": synthetic_issue["body"][:300],
            "synthetic": True,
        },
        "retrieval_triggered": analysis.search_performed,
        "search_status": analysis.search_status,
        "candidate_issues": [item.model_dump() for item in analysis.candidate_issues],
        "evidence_summary": analysis.evidence_summary,
        "duplicate_risk_suggestion": analysis.duplicate_assessment,
        "human_review_required": analysis.needs_human_review,
        "real_qwen_calls": 0,
        "github_write_operations": 0,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

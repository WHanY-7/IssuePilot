"""Explicitly gated deterministic Worker entry point for isolated acceptance tests."""

import logging
import os
import sys
from pathlib import Path

if os.getenv("ISSUEPILOT_ACCEPTANCE_MOCK") != "enabled":
    raise SystemExit("Acceptance mock worker is disabled")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from issuepilot import worker
from issuepilot.models import TriageResult


def classify_without_network(issue: dict) -> TriageResult:
    return TriageResult(
        category="bug",
        priority="P2",
        confidence=0.9,
        summary="Acceptance mock completed without an external model call",
        rationale="Deterministic container acceptance result generated locally",
        missing_information=["Real model evaluation intentionally not executed"],
        suggested_labels=["bug"],
        reply_draft="This deterministic acceptance reply was generated without contacting an external model.",
    )


logging.getLogger("issuepilot.mock_worker").warning(
    "Acceptance mock worker enabled; no external model API will be called"
)
worker.classify_issue = classify_without_network
worker.main()

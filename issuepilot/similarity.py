"""Bounded, read-only similar-issue retrieval for optional triage enrichment.

Retrieval scores rank local candidates; they are not duplicate probabilities.
The module never performs network access or external writes.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from .config import DATASET_PATH
from .models import (
    EnhancedTriageResult,
    SimilarIssueCandidate,
    SimilarityAnalysis,
    TriageResult,
)
from .triage import classify_issue

MAX_CANDIDATES = 5
MAX_TOOL_CALLS = 1
MAX_FLOW_STEPS = 3
DEFAULT_TIMEOUT_SECONDS = 1.0
DEFAULT_MIN_SCORE = 0.12
SUSPECTED_DUPLICATE_SCORE = 0.55
RETRIEVAL_METHOD = "local_tfidf_cosine_v1"

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.+#-]*", re.IGNORECASE)
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "i", "in", "is", "it", "of", "on", "or", "that",
    "the", "this", "to", "was", "we", "when", "with", "you",
}


class SimilarityRetrievalError(RuntimeError):
    """A safe, user-reportable local retrieval failure."""


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN.findall(text) if token.lower() not in _STOP_WORDS]


def _weighted_terms(issue: dict) -> list[str]:
    # Title terms are intentionally weighted more strongly than body terms.
    title = _tokens(str(issue.get("title", ""))[:500])
    body = _tokens(str(issue.get("body", ""))[:7000])
    return title * 3 + body


def _validated_source_url(value: object, repository: str, issue_number: int) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    expected_path = f"/{repository}/issues/{issue_number}"
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or parsed.path != expected_path:
        raise SimilarityRetrievalError(f"invalid_source_url_for_issue_{issue_number}")
    return text


class LocalIssueRetriever:
    """Deterministic TF-IDF cosine retrieval over one fixed local JSON corpus."""

    def __init__(
        self,
        corpus_path: str | Path = DATASET_PATH,
        *,
        min_score: float = DEFAULT_MIN_SCORE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.corpus_path = Path(corpus_path)
        if not 0 <= min_score <= 1:
            raise ValueError("min_score must be between 0 and 1")
        if not 0 < timeout_seconds <= 10:
            raise ValueError("timeout_seconds must be between 0 and 10")
        self.min_score = min_score
        self.timeout_seconds = timeout_seconds
        self._items = self._load_items()
        self._document_terms = [_weighted_terms(item) for item in self._items]
        document_frequency: Counter[str] = Counter()
        for terms in self._document_terms:
            document_frequency.update(set(terms))
        count = len(self._items)
        self._idf = {
            term: math.log((1 + count) / (1 + frequency)) + 1
            for term, frequency in document_frequency.items()
        }

    @property
    def corpus_size(self) -> int:
        return len(self._items)

    def _load_items(self) -> list[dict]:
        try:
            payload = json.loads(self.corpus_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise SimilarityRetrievalError("candidate_corpus_unreadable") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise SimilarityRetrievalError("candidate_corpus_invalid_shape")
        seen: set[int] = set()
        items: list[dict] = []
        for raw in payload["items"]:
            if not isinstance(raw, dict):
                raise SimilarityRetrievalError("candidate_record_not_object")
            number = raw.get("issue_number")
            repository = str(raw.get("repository", "")).strip()
            title = str(raw.get("title", "")).strip()
            body = str(raw.get("body", ""))
            if not isinstance(number, int) or number <= 0 or not repository or not title:
                raise SimilarityRetrievalError("candidate_record_missing_required_fields")
            if number in seen:
                raise SimilarityRetrievalError(f"duplicate_candidate_issue_{number}")
            seen.add(number)
            items.append(
                {
                    "issue_number": number,
                    "repository": repository,
                    "title": title,
                    "body": body,
                    "source_url": _validated_source_url(raw.get("url"), repository, number),
                }
            )
        return items

    def _vector(self, terms: list[str]) -> dict[str, float]:
        counts = Counter(terms)
        maximum = max(counts.values(), default=1)
        return {
            term: (count / maximum) * self._idf.get(term, 0.0)
            for term, count in counts.items()
            if term in self._idf
        }

    @staticmethod
    def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
        if not left or not right:
            return 0.0
        common = left.keys() & right.keys()
        numerator = sum(left[term] * right[term] for term in common)
        left_norm = math.sqrt(sum(value * value for value in left.values()))
        right_norm = math.sqrt(sum(value * value for value in right.values()))
        return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0

    def search(self, issue: dict, *, top_k: int = MAX_CANDIDATES) -> list[SimilarIssueCandidate]:
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_CANDIDATES:
            raise ValueError(f"top_k must be between 1 and {MAX_CANDIDATES}")
        current_number = issue.get("issue_number")
        if not isinstance(current_number, int) or current_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        query_terms = _weighted_terms(issue)
        query_vector = self._vector(query_terms)
        if not query_vector:
            return []
        started = time.monotonic()
        scored: list[tuple[float, int, SimilarIssueCandidate]] = []
        for item, terms in zip(self._items, self._document_terms, strict=True):
            if time.monotonic() - started > self.timeout_seconds:
                raise SimilarityRetrievalError("retrieval_timeout")
            if item["issue_number"] == current_number:
                continue
            candidate_vector = self._vector(terms)
            score = self._cosine(query_vector, candidate_vector)
            if score < self.min_score:
                continue
            overlap = sorted(
                query_vector.keys() & candidate_vector.keys(),
                key=lambda term: (-(query_vector[term] * candidate_vector[term]), term),
            )[:8]
            candidate = SimilarIssueCandidate(
                issue_number=item["issue_number"],
                title=item["title"],
                source_url=item["source_url"],
                retrieval_score=round(score, 6),
                retrieval_basis=overlap,
            )
            scored.append((score, item["issue_number"], candidate))
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        return [entry[2] for entry in scored[:top_k]]


def should_search_for_similar_issues(triage: TriageResult) -> tuple[bool, str]:
    """A bounded policy driven by the model's existing structured category."""
    if triage.category in {"bug", "feature_request"}:
        return True, f"category_{triage.category}_is_duplicate_search_eligible"
    return False, f"category_{triage.category}_skips_duplicate_search"


def _assessment(candidates: list[SimilarIssueCandidate]) -> tuple[str, str]:
    if not candidates:
        return (
            "no_similar_issue",
            "No local candidate met the retrieval threshold; this does not prove that no duplicate exists.",
        )
    best = candidates[0]
    terms = ", ".join(best.retrieval_basis[:5]) or "shared weighted terms"
    if best.retrieval_score >= SUSPECTED_DUPLICATE_SCORE:
        assessment = "suspected_duplicate"
    else:
        assessment = "insufficient_evidence"
    return (
        assessment,
        f"Top local candidate is Issue #{best.issue_number} (retrieval score "
        f"{best.retrieval_score:.6f}; overlapping terms: {terms}). "
        "The score ranks text similarity and is not a duplicate probability.",
    )


def classify_issue_enhanced(
    issue: dict,
    *,
    classifier: Callable[[dict], TriageResult] = classify_issue,
    retriever: LocalIssueRetriever | None = None,
) -> EnhancedTriageResult:
    """Run at most three steps and one allowlisted local retrieval call."""
    triage = classifier(issue)
    search_needed, reason = should_search_for_similar_issues(triage)
    if not search_needed:
        analysis = SimilarityAnalysis(
            search_performed=False,
            search_status="skipped",
            candidate_issues=[],
            duplicate_assessment="insufficient_evidence",
            evidence_summary="Similarity retrieval was skipped by the bounded category policy.",
            decision_reason=reason,
            retrieval_method="not_run",
            tool_calls=0,
            flow_steps=2,
        )
        return EnhancedTriageResult(**triage.model_dump(), similarity_analysis=analysis)

    try:
        tool = retriever or LocalIssueRetriever(DATASET_PATH)
        candidates = tool.search(issue, top_k=MAX_CANDIDATES)
        assessment, evidence = _assessment(candidates)
        analysis = SimilarityAnalysis(
            search_performed=True,
            search_status="completed" if candidates else "no_candidates",
            candidate_issues=candidates,
            duplicate_assessment=assessment,
            evidence_summary=evidence,
            decision_reason=reason,
            retrieval_method=RETRIEVAL_METHOD,
            tool_calls=1,
            flow_steps=MAX_FLOW_STEPS,
        )
    except (SimilarityRetrievalError, OSError, ValueError) as exc:
        analysis = SimilarityAnalysis(
            search_performed=True,
            search_status="error",
            candidate_issues=[],
            duplicate_assessment="insufficient_evidence",
            evidence_summary=f"Local similarity retrieval failed safely: {type(exc).__name__}.",
            decision_reason=reason,
            retrieval_method=RETRIEVAL_METHOD,
            tool_calls=1,
            flow_steps=MAX_FLOW_STEPS,
        )
    return EnhancedTriageResult(**triage.model_dump(), similarity_analysis=analysis)

from typing import Literal
from pydantic import BaseModel, Field, ConfigDict


class TriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["bug", "feature_request", "question", "other"]
    priority: Literal["P0", "P1", "P2", "P3"]
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=4, max_length=500)
    rationale: str = Field(min_length=8, max_length=1500)
    missing_information: list[str] = Field(default_factory=list, max_length=6)
    suggested_labels: list[str] = Field(default_factory=list, max_length=6)
    reply_draft: str = Field(min_length=5, max_length=2000)


class SimilarIssueCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_number: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=500)
    source_url: str = Field(min_length=1, max_length=2000)
    retrieval_score: float = Field(ge=0, le=1)
    retrieval_basis: list[str] = Field(default_factory=list, max_length=8)


class SimilarityAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search_performed: bool
    search_status: Literal["skipped", "completed", "no_candidates", "error"]
    candidate_issues: list[SimilarIssueCandidate] = Field(default_factory=list, max_length=5)
    duplicate_assessment: Literal[
        "suspected_duplicate", "insufficient_evidence", "no_similar_issue"
    ]
    evidence_summary: str = Field(min_length=5, max_length=2000)
    needs_human_review: bool = True
    decision_reason: str = Field(min_length=5, max_length=500)
    retrieval_method: str = Field(default="not_run", min_length=3, max_length=100)
    tool_calls: int = Field(default=0, ge=0, le=1)
    flow_steps: int = Field(default=2, ge=1, le=3)


class EnhancedTriageResult(TriageResult):
    similarity_analysis: SimilarityAnalysis


class CreateTaskRequest(BaseModel):
    issue_number: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reviewer: str = Field(min_length=2, max_length=80)
    reason: str = Field(min_length=10, max_length=1000)
    reviewed_reply: str | None = Field(default=None, min_length=20, max_length=2000)

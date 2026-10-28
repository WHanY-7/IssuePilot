import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from typing import Literal
from .config import auth_secret
from . import db, dataset
from .models import CreateTaskRequest, ReviewRequest
from .github_writeback import (WritebackError, CONFIRMATION, SANDBOX_REPO,
    make_preview, publish_comment, validate_target_issue, approved_review_is_meaningful,
    writeback_enabled)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("issuepilot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.initialize()
    yield


app = FastAPI(title="IssuePilot", version="0.1.0", lifespan=lifespan)


bearer = HTTPBearer(auto_error=False)


def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    import hmac
    try:
        expected = auth_secret()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Server authentication not configured")
    if credentials is None or not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


# Long-running triage tasks are now executed by the separate SQLite-backed
# worker process (python -m issuepilot.worker), never by API BackgroundTasks.


@app.get("/api/health")
def health():
    return {"status":"ok","service":"IssuePilot","write_back_enabled":writeback_enabled()}


@app.get("/api/issues", dependencies=[Depends(authorize)])
def issues(limit: int = Query(default=20,ge=1,le=50)):
    items = dataset.get_issues()[:limit]
    return {"count":len(items),"issues":[{"issue_number":x["issue_number"],"title":x.get("title"),"sample_group":x.get("sample_group"),"url":x.get("url")} for x in items]}


@app.post("/api/tasks", status_code=202, dependencies=[Depends(authorize)])
def create_task(req: CreateTaskRequest):
    issue = dataset.get_issue(req.issue_number)
    if issue is None:
        raise HTTPException(status_code=404, detail="Issue not present in local sample")
    try:
        record, created = db.create_task(issue, req.idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id":record["task_id"],"state":record["state"],"created":created}


@app.get("/api/tasks/{task_id}", dependencies=[Depends(authorize)])
def get_task(task_id: str):
    task = db.get_task(task_id)
    if task is None: raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/api/tasks/{task_id}/audit", dependencies=[Depends(authorize)])
def audit(task_id: str):
    if db.get_task(task_id) is None: raise HTTPException(status_code=404, detail="Task not found")
    return {"events":db.list_events(task_id)}


@app.post("/api/tasks/{task_id}/review", dependencies=[Depends(authorize)])
def review(task_id: str, req: ReviewRequest):
    if db.get_task(task_id) is None: raise HTTPException(status_code=404, detail="Task not found")
    if req.decision == "approve" and not req.reviewed_reply:
        raise HTTPException(status_code=422, detail="Approval requires an explicitly reviewed reply")
    if req.decision == "reject" and req.reviewed_reply is not None:
        raise HTTPException(status_code=422, detail="Rejected review must not contain publishable text")
    if not db.review_task(task_id,req.decision,req.reviewer,req.reason,req.reviewed_reply):
        raise HTTPException(status_code=409, detail="Task is not awaiting human review")
    return {"task_id":task_id,"state":"approved" if req.decision=="approve" else "rejected", "github_write_performed":False}


@app.post("/api/tasks/{task_id}/retry", status_code=202, dependencies=[Depends(authorize)])
def retry(task_id: str):
    if db.get_task(task_id) is None: raise HTTPException(status_code=404, detail="Task not found")
    if not db.retry_task(task_id):
        raise HTTPException(status_code=409, detail="Retry not allowed for current state or attempt count")
    return {"task_id":task_id,"state":"pending"}


# GitHub writeback is opt-in and restricted to the owner's sandbox repository.
# Reviewing a task never triggers this endpoint. There is no write to the
# original microsoft/vscode repository.
class GitHubSyncRequest(BaseModel):
    target_issue_number: int = Field(gt=0)
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["WRITE_TO_ISSUEPILOT_SANDBOX"]


@app.get("/api/tasks/{task_id}/github-preview", dependencies=[Depends(authorize)])
def github_preview(task_id: str, target_issue_number: int = Query(gt=0)):
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        return make_preview(task, db.list_events(task_id), target_issue_number)
    except WritebackError as exc:
        raise HTTPException(status_code=exc.http_code, detail=exc.code) from exc


@app.get("/api/tasks/{task_id}/github-status", dependencies=[Depends(authorize)])
def github_status(task_id: str):
    if db.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"writeback": db.get_writeback(task_id)}


@app.post("/api/tasks/{task_id}/github-sync", dependencies=[Depends(authorize)])
def github_sync(task_id: str, req: GitHubSyncRequest):
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    events = db.list_events(task_id)
    try:
        preview = make_preview(task, events, req.target_issue_number)
        if not approved_review_is_meaningful(events):
            raise WritebackError("review_reason_too_short_for_external_action", 409)
        if req.body_sha256 != preview["body_sha256"]:
            raise WritebackError("preview_changed", 409)
        # Permission and target checks are read-only and happen before claim.
        validate_target_issue(req.target_issue_number)
        if not db.claim_writeback(task_id, SANDBOX_REPO, req.target_issue_number, req.body_sha256):
            raise WritebackError("writeback_already_claimed_or_task_not_approved", 409)
        try:
            result = publish_comment(task, req.target_issue_number, preview["body"])
        except WritebackError as exc:
            db.uncertain_writeback(task_id, exc.code)
            raise
        except Exception:
            db.uncertain_writeback(task_id, "unexpected_external_error")
            log.exception("Writeback failed task_id=%s", task_id)
            raise WritebackError("external_status_uncertain", 503)
        if not db.complete_writeback(task_id, result["comment_id"], result["comment_url"]):
            db.uncertain_writeback(task_id, "receipt_persist_failed")
            raise WritebackError("receipt_persist_failed", 503)
        return {"task_id":task_id,"status":"posted","comment_url":result["comment_url"],"reconciled":result["reconciled"]}
    except WritebackError as exc:
        raise HTTPException(status_code=exc.http_code, detail=exc.code) from exc

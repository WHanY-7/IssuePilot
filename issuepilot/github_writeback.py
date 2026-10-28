"""Fail-closed GitHub sandbox-only writeback for human-reviewed IssuePilot tasks.

This module does not mutate the source repository. It can post one comment
only to WHanY-7/IssuePilot-Sandbox after a separate explicit confirmation.
"""
import hashlib
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SANDBOX_REPO = "WHanY-7/IssuePilot-Sandbox"
CONFIRMATION = "WRITE_TO_ISSUEPILOT_SANDBOX"


class WritebackError(Exception):
    def __init__(self, code: str, http_code: int = 409):
        super().__init__(code)
        self.code = code
        self.http_code = http_code


def writeback_enabled() -> bool:
    """Report whether sandbox writeback has been explicitly configured."""
    repo = os.getenv("ISSUEPILOT_GITHUB_TEST_REPO", "").strip()
    token_configured = bool(os.getenv("ISSUEPILOT_GITHUB_TOKEN", "").strip())
    return repo.lower() == SANDBOX_REPO.lower() and token_configured


def require_test_repo() -> str:
    repo = os.getenv("ISSUEPILOT_GITHUB_TEST_REPO", "").strip()
    if repo.lower() != SANDBOX_REPO.lower():
        raise WritebackError("sandbox_repo_not_configured", 503)
    return SANDBOX_REPO


def _review_detail(events: list[dict]) -> dict:
    for event in reversed(events):
        if event.get("event") == "human_review":
            try:
                result = json.loads(event.get("detail", ""))
                return result if isinstance(result, dict) else {}
            except (TypeError, ValueError):
                return {}
    return {}


def approved_review_is_meaningful(events: list[dict]) -> bool:
    detail = _review_detail(events)
    return (
        detail.get("decision") == "approve"
        and len(str(detail.get("reason", "")).strip()) >= 10
        and len(str(detail.get("reviewer", "")).strip()) >= 2
    )


def build_comment(task: dict) -> str:
    triage = task.get("triage") or {}
    issue = task.get("issue") or {}
    source = str(issue.get("url") or "")
    if not source.startswith("https://github.com/microsoft/vscode/issues/"):
        raise WritebackError("unexpected_issue_source", 422)
    reviewed_reply = str(task.get("reviewed_reply") or "").strip()
    if len(reviewed_reply) < 20:
        raise WritebackError("missing_human_reviewed_reply", 409)
    marker = f"<!-- issuepilot-task:{task['task_id']} -->"
    return (
        f"{marker}\n"
        "## IssuePilot controlled triage test\n\n"
        "**This is a sandbox copy of a reviewed AI suggestion, not an official decision.**\n\n"
        f"Source public issue: {source}\n\n"
        "### Human-reviewed reply (sandbox test only)\n\n"
        f"{reviewed_reply}\n\n"
        "_IssuePilot will not edit the source repository._"
    )


def make_preview(task: dict, events: list[dict], target_issue_number: int) -> dict:
    repo = require_test_repo()
    if task.get("state") != "approved":
        raise WritebackError("task_not_approved")
    if target_issue_number <= 0:
        raise WritebackError("invalid_target_issue", 422)
    comment = build_comment(task)
    digest = hashlib.sha256(comment.encode("utf-8")).hexdigest()
    return {
        "destination": f"https://github.com/{repo}/issues/{target_issue_number}",
        "source": task["issue"].get("url"),
        "body": comment,
        "body_sha256": digest,
        "human_review_reason_sufficient": approved_review_is_meaningful(events),
        "github_write_performed": False,
    }


def _request_json(method: str, path: str, token: str, payload=None):
    url = "https://api.github.com" + path
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "IssuePilot-Sandbox-Writeback/0.1",
        "Authorization": f"Bearer {token}",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        # Do not log upstream error bodies: they may contain response details.
        if exc.code in (401, 403):
            raise WritebackError("github_permission_denied", 403) from exc
        if exc.code == 404:
            raise WritebackError("sandbox_issue_not_found", 404) from exc
        raise WritebackError(f"github_http_{exc.code}", 502) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise WritebackError("github_network_status_uncertain", 503) from exc


def validate_target_issue(target_issue_number: int) -> None:
    """Read-only preflight before we claim an irreversible writeback attempt."""
    require_test_repo()
    token = os.getenv("ISSUEPILOT_GITHUB_TOKEN", "").strip()
    if not token:
        raise WritebackError("github_token_not_configured", 503)
    target_path = "/repos/WHanY-7/IssuePilot-Sandbox/issues/" + str(target_issue_number)
    issue = _request_json("GET", target_path, token)
    if issue.get("pull_request"):
        raise WritebackError("target_is_pull_request", 422)
    if int(issue.get("number", -1)) != target_issue_number:
        raise WritebackError("target_issue_mismatch", 422)


def publish_comment(task: dict, target_issue_number: int, body: str) -> dict:
    """Only the test repo; marker scan is best-effort, not exactly-once guarantee."""
    require_test_repo()
    token = os.getenv("ISSUEPILOT_GITHUB_TOKEN", "").strip()
    if not token:
        raise WritebackError("github_token_not_configured", 503)
    target_path = "/repos/WHanY-7/IssuePilot-Sandbox/issues/" + str(target_issue_number)
    comments_path = target_path + "/comments"
    marker = f"<!-- issuepilot-task:{task['task_id']} -->"
    for page in range(1, 11):
        comments = _request_json("GET", comments_path + f"?per_page=100&page={page}", token)
        if not isinstance(comments, list):
            raise WritebackError("unexpected_comment_response", 502)
        for comment in comments:
            if marker in str(comment.get("body", "")):
                return {"comment_id": comment["id"], "comment_url": comment["html_url"], "reconciled": True}
        if len(comments) < 100:
            break
    else:
        # Do not post if we were unable to inspect all existing comments.
        raise WritebackError("comment_history_too_large", 409)
    created = _request_json("POST", comments_path, token, {"body": body})
    if "id" not in created or "html_url" not in created:
        raise WritebackError("post_result_unknown", 503)
    return {"comment_id": created["id"], "comment_url": created["html_url"], "reconciled": False}

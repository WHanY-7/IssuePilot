"""Real HTTP + SQLite state transitions, with mocked LLM (no paid API)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
_TEMP = tempfile.TemporaryDirectory(
    prefix="issuepilot-test-", dir=Path(__file__).resolve().parents[1] / "data"
)
root = Path(_TEMP.name)
(root / "issues_seed.json").write_text(json.dumps({"items":[
    {"repository":"microsoft/vscode","issue_number":123,"title":"Synthetic startup failure", "body":"Synthetic example: the demo program closes during startup after an upgrade and reports an exception.","sample_group":"bug","existing_labels":["bug"],"url":"https://github.com/microsoft/vscode/issues/123"},
    {"repository":"microsoft/vscode","issue_number":456,"title":"Synthetic keyboard option", "body":"Synthetic example: add a configurable keyboard shortcut to improve accessibility in the demo program.","sample_group":"feature_request","existing_labels":[],"url":"https://github.com/microsoft/vscode/issues/456"},
]}),encoding="utf-8")
os.environ["ISSUEPILOT_DB"] = str(root / "test_tasks.sqlite3")
os.environ["ISSUEPILOT_DATASET"] = str(root / "issues_seed.json")
os.environ["ISSUEPILOT_AUTH_SECRET"] = "unit-test-only!unit-test-only!"

from fastapi.testclient import TestClient
from issuepilot.api import app
from issuepilot import worker, db
from issuepilot.models import TriageResult

OK = TriageResult(category="bug",priority="P2",confidence=0.8,
  summary="Application crashes on Windows startup", rationale="Reported startup crash after upgrade",
  missing_information=["Exact app version", "Crash traceback"],
  suggested_labels=["bug"], reply_draft="Please provide your application version and traceback.")
AUTH = {"Authorization":"Bearer unit-test-only!unit-test-only!"}


class WorkflowTests(unittest.TestCase):
    def test_health_reports_sandbox_writeback_configuration(self):
        with TestClient(app) as c:
            with patch.dict(os.environ, {
                "ISSUEPILOT_GITHUB_TEST_REPO":"",
                "ISSUEPILOT_GITHUB_TOKEN":"",
            }):
                self.assertFalse(c.get("/api/health").json()["write_back_enabled"])
            with patch.dict(os.environ, {
                "ISSUEPILOT_GITHUB_TEST_REPO":"microsoft/vscode",
                "ISSUEPILOT_GITHUB_TOKEN":"test-placeholder-token",
            }):
                self.assertFalse(c.get("/api/health").json()["write_back_enabled"])
            with patch.dict(os.environ, {
                "ISSUEPILOT_GITHUB_TEST_REPO":"WHanY-7/IssuePilot-Sandbox",
                "ISSUEPILOT_GITHUB_TOKEN":"test-placeholder-token",
            }):
                self.assertTrue(c.get("/api/health").json()["write_back_enabled"])

    def test_auth_validations_and_happy_path(self):
        with TestClient(app) as c:
            self.assertEqual(c.get("/api/health").status_code,200)
            self.assertEqual(c.get("/api/issues").status_code,401)
            self.assertEqual(c.get("/api/issues",headers=AUTH).json()["count"],2)
            self.assertEqual(c.post("/api/tasks",headers=AUTH,json={"issue_number":123,"idempotency_key":"short"}).status_code,422)
            with patch("issuepilot.worker.classify_issue",return_value=OK) as mocked:
                resp = c.post("/api/tasks",headers=AUTH,json={"issue_number":123,"idempotency_key":"test-case-001"})
                self.assertEqual(resp.status_code,202)
                tid=resp.json()["task_id"]
                self.assertEqual(c.get(f"/api/tasks/{tid}",headers=AUTH).json()["state"],"pending")
                self.assertTrue(worker.run_once())
                self.assertEqual(c.get(f"/api/tasks/{tid}",headers=AUTH).json()["state"],"needs_review")
                dup = c.post("/api/tasks",headers=AUTH,json={"issue_number":123,"idempotency_key":"test-case-001"})
                self.assertFalse(dup.json()["created"])
                self.assertEqual(dup.json()["task_id"],tid)
                self.assertEqual(mocked.call_count,1)
            conflict = c.post("/api/tasks",headers=AUTH,json={"issue_number":456,"idempotency_key":"test-case-001"})
            self.assertEqual(conflict.status_code,409)
            approved=c.post(f"/api/tasks/{tid}/review",headers=AUTH,json={"decision":"approve","reviewer":"test-reviewer","reason":"Evidence reviewed and checked carefully", "reviewed_reply":"Please share the version and exact traceback so we can investigate."})
            self.assertEqual(approved.status_code,200)
            self.assertFalse(approved.json()["github_write_performed"])
            self.assertEqual(c.post(f"/api/tasks/{tid}/review",headers=AUTH,json={"decision":"reject","reviewer":"test-reviewer","reason":"Already approved; do not repeat review"}).status_code,409)
            logs=c.get(f"/api/tasks/{tid}/audit",headers=AUTH).json()["events"]
            self.assertEqual([r["event"] for r in logs],["task_created","analysis_started","analysis_completed","human_review"])

    def test_failure_and_limited_retry(self):
        with TestClient(app) as c:
            with patch("issuepilot.worker.classify_issue",side_effect=RuntimeError("model timeout")):
                r=c.post("/api/tasks",headers=AUTH,json={"issue_number":456,"idempotency_key":"test-case-002"})
            tid=r.json()["task_id"]
            with patch("issuepilot.worker.classify_issue",side_effect=RuntimeError("model timeout")):
                self.assertTrue(worker.run_once())
            self.assertEqual(c.get(f"/api/tasks/{tid}",headers=AUTH).json()["state"],"failed")
            with patch("issuepilot.worker.classify_issue",return_value=OK):
                ret=c.post(f"/api/tasks/{tid}/retry",headers=AUTH)
                self.assertEqual(ret.status_code,202)
                self.assertTrue(worker.run_once())
            task=c.get(f"/api/tasks/{tid}",headers=AUTH).json()
            self.assertEqual(task["state"],"needs_review")
            self.assertEqual(task["attempt"],2)
            self.assertEqual(c.post(f"/api/tasks/{tid}/retry",headers=AUTH).status_code,409)

    def test_rejection_cannot_carry_publishable_text(self):
        with TestClient(app) as c:
            response=c.post("/api/tasks",headers=AUTH,json={"issue_number":456,"idempotency_key":"reject-review-001"})
            tid=response.json()["task_id"]
            with patch("issuepilot.worker.classify_issue",return_value=OK):
                self.assertTrue(worker.run_once())
            invalid=c.post(f"/api/tasks/{tid}/review",headers=AUTH,json={
                "decision":"reject","reviewer":"test-reviewer","reason":"The suggestion is not supported by the issue evidence",
                "reviewed_reply":"This reply must not be publishable after a rejection."
            })
            self.assertEqual(invalid.status_code,422)
            rejected=c.post(f"/api/tasks/{tid}/review",headers=AUTH,json={
                "decision":"reject","reviewer":"test-reviewer","reason":"The suggestion is not supported by the issue evidence"
            })
            self.assertEqual(rejected.status_code,200)
            self.assertEqual(c.get(f"/api/tasks/{tid}",headers=AUTH).json()["state"],"rejected")

class SandboxWritebackTests(unittest.TestCase):
    """Mock-only tests. No GitHub access, no model API, no real mutations."""

    def test_approval_preview_and_one_time_sandbox_sync(self):
        with patch.dict(os.environ, {"ISSUEPILOT_GITHUB_TEST_REPO":"WHanY-7/IssuePilot-Sandbox"}):
            with TestClient(app) as c:
                with patch("issuepilot.worker.classify_issue",return_value=OK):
                    response=c.post("/api/tasks",headers=AUTH,json={"issue_number":123,"idempotency_key":"sync-demo-001"})
                    self.assertTrue(worker.run_once())
                self.assertEqual(response.status_code,202)
                task_id=response.json()["task_id"]
                noapproval=c.get(f"/api/tasks/{task_id}/github-preview?target_issue_number=2",headers=AUTH)
                self.assertEqual(noapproval.status_code,409)
                self.assertEqual(c.post(f"/api/tasks/{task_id}/review", headers=AUTH, json={
                    "decision":"approve", "reviewer":"local-test", "reason":"Evidence and draft were checked manually", "reviewed_reply":"Thank you for reporting this issue. Please provide the exact build and traceback; this message does not confirm root cause."
                }).status_code,200)
                preview=c.get(f"/api/tasks/{task_id}/github-preview?target_issue_number=2",headers=AUTH)
                self.assertEqual(preview.status_code,200)
                self.assertEqual(preview.json()["human_review_reason_sufficient"],True)
                self.assertEqual(preview.json()["github_write_performed"],False)
                self.assertIn("IssuePilot-Sandbox/issues/2",preview.json()["destination"])
                self.assertIn("issuepilot-task",preview.json()["body"])
                body={"target_issue_number":2,"body_sha256":preview.json()["body_sha256"],"confirmation":"WRITE_TO_ISSUEPILOT_SANDBOX"}
                wronghash={**body,"body_sha256":"0"*64}
                self.assertEqual(c.post(f"/api/tasks/{task_id}/github-sync",headers=AUTH,json=wronghash).status_code,409)
                with patch("issuepilot.api.validate_target_issue"), patch("issuepilot.api.publish_comment", return_value={"comment_id":88,"comment_url":"https://github.com/WHanY-7/IssuePilot-Sandbox/issues/2#issuecomment-88","reconciled":False}) as mocked:
                    good=c.post(f"/api/tasks/{task_id}/github-sync",headers=AUTH,json=body)
                    self.assertEqual(good.status_code,200)
                    self.assertEqual(good.json()["status"],"posted")
                    self.assertEqual(c.post(f"/api/tasks/{task_id}/github-sync",headers=AUTH,json=body).status_code,409)
                    self.assertEqual(mocked.call_count,1)
                current=c.get(f"/api/tasks/{task_id}/github-status",headers=AUTH).json()["writeback"]
                self.assertEqual(current["status"],"posted")
                self.assertEqual(current["target_issue_number"],2)
                events=c.get(f"/api/tasks/{task_id}/audit",headers=AUTH).json()["events"]
                self.assertIn("github_writeback_claimed",[e["event"] for e in events])
                self.assertIn("github_writeback_posted",[e["event"] for e in events])

    def test_human_edited_reply_is_frozen_and_used_in_preview(self):
        with patch.dict(os.environ, {"ISSUEPILOT_GITHUB_TEST_REPO":"WHanY-7/IssuePilot-Sandbox"}):
            with TestClient(app) as c:
                with patch("issuepilot.worker.classify_issue",return_value=OK):
                    response=c.post("/api/tasks",headers=AUTH,json={"issue_number":123,"idempotency_key":"edited-reply-001"})
                    self.assertTrue(worker.run_once())
                tid=response.json()["task_id"]
                no_reply=c.post(f"/api/tasks/{tid}/review",headers=AUTH,json={
                    "decision":"approve","reviewer":"qa-reviewer","reason":"Compared draft with the original report carefully"
                })
                self.assertEqual(no_reply.status_code,422)
                reviewed="Thanks for the report. Please provide version and traceback; we have not confirmed the root cause."
                r=c.post(f"/api/tasks/{tid}/review",headers=AUTH,json={
                    "decision":"approve","reviewer":"qa-reviewer","reason":"Removed unsupported root-cause claims and checked report", "reviewed_reply":reviewed
                })
                self.assertEqual(r.status_code,200)
                task=c.get(f"/api/tasks/{tid}",headers=AUTH).json()
                self.assertEqual(task["reviewed_reply"],reviewed)
                preview=c.get(f"/api/tasks/{tid}/github-preview?target_issue_number=1",headers=AUTH)
                self.assertEqual(preview.status_code,200)
                self.assertIn(reviewed,preview.json()["body"])
                self.assertNotIn("Suggested priority:",preview.json()["body"])
                self.assertNotIn("Reviewer-visible draft",preview.json()["body"])
                self.assertIn("reviewed_reply_sha256",c.get(f"/api/tasks/{tid}/audit",headers=AUTH).json()["events"][-1]["detail"])

    def test_legacy_approval_without_frozen_reply_is_not_publishable(self):
        from issuepilot import db
        with patch.dict(os.environ,{"ISSUEPILOT_GITHUB_TEST_REPO":"WHanY-7/IssuePilot-Sandbox"}):
            with TestClient(app) as c:
                with patch("issuepilot.worker.classify_issue",return_value=OK):
                    response=c.post("/api/tasks",headers=AUTH,json={"issue_number":123,"idempotency_key":"legacy-approval-001"})
                    self.assertTrue(worker.run_once())
                tid=response.json()["task_id"]
                # Simulate a task approved under the original schema.
                with db.connect() as con:
                    con.execute("UPDATE tasks SET state='approved', reviewed_reply=NULL WHERE task_id=?",(tid,))
                self.assertEqual(c.get(f"/api/tasks/{tid}/github-preview?target_issue_number=1",headers=AUTH).status_code,409)

    def test_short_review_reason_cannot_trigger_external_mutation(self):
        with patch.dict(os.environ, {"ISSUEPILOT_GITHUB_TEST_REPO":"WHanY-7/IssuePilot-Sandbox"}):
            with TestClient(app) as c:
                with patch("issuepilot.worker.classify_issue",return_value=OK):
                    response=c.post("/api/tasks",headers=AUTH,json={"issue_number":456,"idempotency_key":"sync-demo-002"})
                    self.assertTrue(worker.run_once())
                task_id=response.json()["task_id"]
                self.assertEqual(c.post(f"/api/tasks/{task_id}/review",headers=AUTH,json={"decision":"approve","reviewer":"local-test","reason":"11", "reviewed_reply":"Please share exact reproduction steps."}).status_code,422)
                self.assertEqual(c.get(f"/api/tasks/{task_id}",headers=AUTH).json()["state"],"needs_review")
                self.assertEqual(c.get(f"/api/tasks/{task_id}/github-preview?target_issue_number=2",headers=AUTH).status_code,409)
                with patch("issuepilot.api.publish_comment") as mock_call:
                    mock_call.assert_not_called()
                self.assertIsNone(c.get(f"/api/tasks/{task_id}/github-status",headers=AUTH).json()["writeback"])

    def test_repo_allowlist_and_uncertain_network_failure(self):
        with patch.dict(os.environ, {"ISSUEPILOT_GITHUB_TEST_REPO":"microsoft/vscode"}):
            with TestClient(app) as c:
                self.assertEqual(c.get("/api/tasks/nonexistent/github-preview?target_issue_number=1",headers=AUTH).status_code,404)
        with patch.dict(os.environ, {"ISSUEPILOT_GITHUB_TEST_REPO":"WHanY-7/IssuePilot-Sandbox"}):
            with TestClient(app) as c:
                with patch("issuepilot.worker.classify_issue",return_value=OK):
                    response=c.post("/api/tasks",headers=AUTH,json={"issue_number":456,"idempotency_key":"sync-demo-003"})
                    self.assertTrue(worker.run_once())
                task_id=response.json()["task_id"]
                self.assertEqual(c.post(f"/api/tasks/{task_id}/review",headers=AUTH,json={"decision":"approve","reviewer":"local-test","reason":"Human reviewer checked the draft carefully","reviewed_reply":"Thank you for the feature suggestion. Can you share a concrete example of the desired shortcut?"}).status_code,200)
                preview=c.get(f"/api/tasks/{task_id}/github-preview?target_issue_number=2",headers=AUTH).json()
                from issuepilot.github_writeback import WritebackError
                with patch("issuepilot.api.validate_target_issue"), patch("issuepilot.api.publish_comment",side_effect=WritebackError("github_network_status_uncertain",503)) as mock_call:
                    data={"target_issue_number":2,"body_sha256":preview["body_sha256"],"confirmation":"WRITE_TO_ISSUEPILOT_SANDBOX"}
                    self.assertEqual(c.post(f"/api/tasks/{task_id}/github-sync",headers=AUTH,json=data).status_code,503)
                    self.assertEqual(c.post(f"/api/tasks/{task_id}/github-sync",headers=AUTH,json=data).status_code,409)
                    self.assertEqual(mock_call.call_count,1)
                current=c.get(f"/api/tasks/{task_id}/github-status",headers=AUTH).json()["writeback"]
                self.assertEqual(current["status"],"manual_reconciliation_required")

    def test_github_preflight_uses_only_the_fixed_sandbox_path(self):
        from issuepilot.github_writeback import WritebackError, require_test_repo, validate_target_issue
        with patch.dict(os.environ, {
            "ISSUEPILOT_GITHUB_TEST_REPO":"microsoft/vscode",
            "ISSUEPILOT_GITHUB_TOKEN":"test-placeholder-token",
        }):
            with self.assertRaises(WritebackError):
                require_test_repo()
        with patch.dict(os.environ, {
            "ISSUEPILOT_GITHUB_TEST_REPO":"WHanY-7/IssuePilot-Sandbox",
            "ISSUEPILOT_GITHUB_TOKEN":"test-placeholder-token",
        }):
            with patch("issuepilot.github_writeback._request_json", return_value={"number":7}) as request:
                validate_target_issue(7)
            request.assert_called_once_with(
                "GET", "/repos/WHanY-7/IssuePilot-Sandbox/issues/7", "test-placeholder-token"
            )


class DurableWorkerTests(unittest.TestCase):
    def test_pending_survives_api_restart_and_is_claimed_once(self):
        with TestClient(app) as c:
            created=c.post("/api/tasks",headers=AUTH,json={"issue_number":123,"idempotency_key":"durable-on-restart-001"})
            self.assertEqual(created.status_code,202)
            tid=created.json()["task_id"]
            self.assertEqual(db.get_task(tid)["state"],"pending")
            db.initialize()  # API restart must never fail queued tasks
            self.assertEqual(db.get_task(tid)["state"],"pending")
            with patch("issuepilot.worker.classify_issue",return_value=OK) as mocked:
                self.assertTrue(worker.run_once())
                self.assertFalse(worker.run_once())
                self.assertEqual(mocked.call_count,1)
            self.assertEqual(db.get_task(tid)["state"],"needs_review")

    def test_expired_owner_cannot_overwrite_result_and_manual_retry_required(self):
        with TestClient(app) as c:
            resp=c.post("/api/tasks",headers=AUTH,json={"issue_number":123,"idempotency_key":"worker-expire-001"})
            tid=resp.json()["task_id"]
            claim=db.claim_next_task("test-dead-worker")
            self.assertEqual(claim["task_id"],tid)
            with db.connect() as con:
                con.execute("UPDATE tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",(tid,))
            self.assertFalse(db.fail_task(tid,"test-dead-worker","late_worker_error"))
            self.assertEqual(db.expire_leases(),1)
            self.assertEqual(db.get_task(tid)["state"],"failed")
            self.assertEqual(db.get_task(tid)["error_code"],"worker_lease_expired")
            self.assertFalse(db.complete_task(tid,"test-dead-worker",OK.model_dump()))
            self.assertEqual(c.post(f"/api/tasks/{tid}/retry",headers=AUTH).status_code,202)
            with patch("issuepilot.worker.classify_issue",return_value=OK):
                self.assertTrue(worker.run_once())
            self.assertEqual(db.get_task(tid)["attempt"],2)
            self.assertEqual(db.get_task(tid)["state"],"needs_review")

    def test_claim_only_once_even_with_two_clients(self):
        with TestClient(app) as c:
            tid=c.post("/api/tasks",headers=AUTH,json={"issue_number":456,"idempotency_key":"atomic-claim-001"}).json()["task_id"]
            first=db.claim_next_task("worker-one")
            self.assertEqual(first["task_id"],tid)
            self.assertIsNone(db.claim_next_task("worker-two"))
            self.assertFalse(db.heartbeat_task(tid,"worker-two"))
            with self.assertRaises(ValueError):
                db.heartbeat_task(tid,"worker-one",lease_seconds=29)
            self.assertFalse(db.complete_task(tid,"worker-two",OK.model_dump()))
            self.assertTrue(db.complete_task(tid,"worker-one",OK.model_dump()))


if __name__=="__main__":
    unittest.main()

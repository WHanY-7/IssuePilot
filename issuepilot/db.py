import json
import sqlite3
import hashlib
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from uuid import uuid4
from .config import DB_PATH


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize():
    with connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS tasks (
           task_id TEXT PRIMARY KEY,
           issue_number INTEGER NOT NULL,
           idempotency_key TEXT NOT NULL UNIQUE,
           state TEXT NOT NULL,
           attempt INTEGER NOT NULL DEFAULT 0,
           issue_snapshot TEXT NOT NULL,
           triage_json TEXT,
           error_code TEXT,
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS audit_events (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           task_id TEXT NOT NULL,
           event TEXT NOT NULL,
           detail TEXT NOT NULL,
           happened_at TEXT NOT NULL,
           FOREIGN KEY(task_id) REFERENCES tasks(task_id)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS github_writebacks (
           task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
           target_repo TEXT NOT NULL,
           target_issue_number INTEGER NOT NULL,
           body_sha256 TEXT NOT NULL,
           status TEXT NOT NULL,
           comment_id TEXT,
           comment_url TEXT,
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL
        )""")
        # Migrate existing local SQLite DBs in place. Legacy approvals keep NULL
        # reviewed_reply and are not eligible for external writeback.
        columns = {r[1] for r in con.execute("PRAGMA table_info(tasks)").fetchall()}
        if "reviewed_reply" not in columns:
            con.execute("ALTER TABLE tasks ADD COLUMN reviewed_reply TEXT")
        # Add lease fields without changing the existing writeback/review tables.
        for name in ("lease_owner", "lease_expires_at"):
            if name not in columns:
                con.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT")
        # Old version has no owner information. Its 'running' tasks are not
        # safe to resume automatically. Installer requires zero active jobs.
        legacy = con.execute("SELECT task_id FROM tasks WHERE state='running' AND lease_owner IS NULL").fetchall()
        for row in legacy:
            con.execute("UPDATE tasks SET state='failed',error_code='legacy_execution_interrupted',updated_at=? WHERE task_id=?", (now(), row["task_id"]))
            _audit(con, row["task_id"], "legacy_interrupted", "Manual retry required")


def _audit(con, task_id: str, event: str, detail: str = ""):
    con.execute("INSERT INTO audit_events(task_id,event,detail,happened_at) VALUES(?,?,?,?)", (task_id,event,detail,now()))


def row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    d["triage"] = json.loads(d.pop("triage_json")) if d.get("triage_json") else None
    d["issue"] = json.loads(d.pop("issue_snapshot"))
    return d


def get_task(task_id: str):
    with connect() as con:
        return row_to_dict(con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())


def list_events(task_id: str):
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT event,detail,happened_at FROM audit_events WHERE task_id=? ORDER BY id", (task_id,))]


def create_task(issue: dict, idempotency_key: str):
    with connect() as con:
        old = con.execute("SELECT * FROM tasks WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if old:
            if old["issue_number"] != issue["issue_number"]:
                raise ValueError("Idempotency key already used for another issue")
            return row_to_dict(old), False
        task_id = str(uuid4())
        t = now()
        con.execute("INSERT INTO tasks(task_id,issue_number,idempotency_key,state,attempt,issue_snapshot,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (task_id,issue["issue_number"],idempotency_key,"pending",0,json.dumps(issue,ensure_ascii=False),t,t))
        _audit(con, task_id, "task_created", f"issue={issue['issue_number']}")
        result = row_to_dict(con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())
        return result, True


def claim_next_task(owner: str, lease_seconds: int = 120):
    """Atomically claim one queued task; one process may claim it only once."""
    if not owner or not (30 <= lease_seconds <= 3600):
        raise ValueError("Invalid worker owner or lease")
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT task_id FROM tasks WHERE state='pending' ORDER BY created_at, task_id LIMIT 1").fetchone()
        if row is None:
            return None
        task_id = row["task_id"]
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        changed = con.execute(
            "UPDATE tasks SET state='running', attempt=attempt+1, error_code=NULL, lease_owner=?,lease_expires_at=?,updated_at=? WHERE task_id=? AND state='pending'",
            (owner, expiry, now(), task_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("Atomic task claim failed")
        _audit(con, task_id, "analysis_started", "worker_claimed")
        return row_to_dict(con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())


def heartbeat_task(task_id: str, owner: str, lease_seconds: int = 120) -> bool:
    if not owner or not (30 <= lease_seconds <= 3600):
        raise ValueError("Invalid worker owner or lease")
    with connect() as con:
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        return bool(con.execute(
            "UPDATE tasks SET lease_expires_at=? WHERE task_id=? AND state='running' AND lease_owner=? AND lease_expires_at>?",
            (expiry,task_id,owner,now()),
        ).rowcount)


def expire_leases() -> int:
    """Mark crashed/expired workers failed. Never automatically re-run an LLM."""
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        expired = con.execute("SELECT task_id FROM tasks WHERE state='running' AND lease_expires_at<=?",(now(),)).fetchall()
        for row in expired:
            con.execute("UPDATE tasks SET state='failed', error_code='worker_lease_expired', lease_owner=NULL, lease_expires_at=NULL,updated_at=? WHERE task_id=? AND state='running'",(now(),row["task_id"]))
            _audit(con,row["task_id"],"worker_lease_expired","Worker result uncertain; manual retry required")
        return len(expired)


def complete_task(task_id: str, owner: str, result: dict) -> bool:
    with connect() as con:
        changed = con.execute(
            "UPDATE tasks SET state='needs_review',triage_json=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE task_id=? AND state='running' AND lease_owner=? AND lease_expires_at>?",
            (json.dumps(result,ensure_ascii=False),now(),task_id,owner,now()),
        ).rowcount
        if changed: _audit(con,task_id,"analysis_completed","Pending human review")
        return bool(changed)


def fail_task(task_id: str, owner: str, error_code: str) -> bool:
    with connect() as con:
        changed = con.execute(
            "UPDATE tasks SET state='failed',error_code=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE task_id=? AND state='running' AND lease_owner=? AND lease_expires_at>?",
            (error_code,now(),task_id,owner,now()),
        ).rowcount
        if changed: _audit(con,task_id,"analysis_failed",error_code)
        return bool(changed)


def review_task(task_id: str, decision: str, reviewer: str, reason: str, reviewed_reply: str | None = None):
    target = "approved" if decision == "approve" else "rejected"
    final_reply = reviewed_reply.strip() if decision == "approve" and reviewed_reply else None
    with connect() as con:
        updated = con.execute(
            "UPDATE tasks SET state=?,reviewed_reply=?,updated_at=? WHERE task_id=? AND state='needs_review'",
            (target,final_reply,now(),task_id),
        ).rowcount
        if updated:
            detail = {"reviewer": reviewer, "decision": decision, "reason": reason}
            if final_reply:
                detail["reviewed_reply_sha256"] = hashlib.sha256(final_reply.encode("utf-8")).hexdigest()
            _audit(con, task_id, "human_review", json.dumps(detail, ensure_ascii=False))
        return bool(updated)


def retry_task(task_id: str) -> bool:
    with connect() as con:
        changed = con.execute(
            "UPDATE tasks SET state='pending',error_code=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE task_id=? AND state='failed' AND attempt<2",
            (now(),task_id),
        ).rowcount
        if changed: _audit(con,task_id,"manual_retry_requested")
        return bool(changed)


def get_writeback(task_id: str):
    with connect() as con:
        row = con.execute("SELECT * FROM github_writebacks WHERE task_id=?",(task_id,)).fetchone()
        return dict(row) if row else None


def claim_writeback(task_id: str, repo: str, target_issue: int, digest: str) -> bool:
    # SQLite uniqueness guarantees only one initial claim per task, even if
    # multiple FastAPI requests race. Never auto-retry a network-unknown POST.
    with connect() as con:
        row = con.execute("SELECT state FROM tasks WHERE task_id=?",(task_id,)).fetchone()
        if not row or row["state"] != "approved":
            return False
        try:
            timestamp=now()
            con.execute("INSERT INTO github_writebacks(task_id,target_repo,target_issue_number,body_sha256,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (task_id,repo,target_issue,digest,"posting",timestamp,timestamp))
        except sqlite3.IntegrityError:
            return False
        _audit(con, task_id, "github_writeback_claimed", f"target={repo}#{target_issue}")
        return True


def complete_writeback(task_id: str, comment_id: str, comment_url: str):
    with connect() as con:
        updated=con.execute("UPDATE github_writebacks SET status='posted',comment_id=?,comment_url=?,updated_at=? WHERE task_id=? AND status='posting'",
             (str(comment_id),comment_url,now(),task_id)).rowcount
        if updated:
            _audit(con,task_id,"github_writeback_posted",comment_url)
        return bool(updated)


def uncertain_writeback(task_id: str, code: str):
    with connect() as con:
        updated=con.execute("UPDATE github_writebacks SET status='manual_reconciliation_required',updated_at=? WHERE task_id=? AND status='posting'",(now(),task_id)).rowcount
        if updated:
            _audit(con, task_id, "github_writeback_uncertain", code)
        return bool(updated)

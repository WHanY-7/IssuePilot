"""Dedicated single-purpose polling worker for the local IssuePilot MVP.

Usage: python -m issuepilot.worker --poll-seconds 3
No GitHub writes are performed by this process.
"""
import argparse
import logging
import os
import signal
import threading
from uuid import uuid4

from . import db
from .config import similarity_enabled
from .similarity import classify_issue_enhanced
from .triage import classify_issue

log = logging.getLogger("issuepilot.worker")


def run_once() -> bool:
    """Claim and process at most one pending task. Useful for deterministic tests."""
    db.expire_leases()
    owner = f"{os.getpid()}-{uuid4().hex}"
    task = db.claim_next_task(owner)
    if task is None:
        return False
    task_id = task["task_id"]
    stop = threading.Event()

    def keepalive():
        while not stop.wait(10):
            try:
                if not db.heartbeat_task(task_id, owner):
                    log.error("Lost lease on task_id=%s",task_id)
                    return
            except Exception:
                log.exception("Worker heartbeat failed task_id=%s",task_id)

    thread = threading.Thread(target=keepalive,daemon=True,name="issuepilot-lease-heartbeat")
    thread.start()
    try:
        if similarity_enabled():
            result = classify_issue_enhanced(task["issue"], classifier=classify_issue)
        else:
            result = classify_issue(task["issue"])
        if not db.complete_task(task_id, owner, result.model_dump()):
            log.error("Discarding result: worker lease lost task_id=%s",task_id)
        else:
            log.info("Triage completed task_id=%s",task_id)
    except Exception as exc:
        code = str(exc) if str(exc) in {"missing_qwen_api_key","invalid_model_output"} else type(exc).__name__
        log.exception("Triage failed task_id=%s",task_id)
        db.fail_task(task_id,owner,code)
    finally:
        stop.set()
        thread.join(timeout=2)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-seconds",type=float,default=3)
    parser.add_argument("--once",action="store_true",help="Process at most one task and exit")
    args = parser.parse_args()
    if not 0.5 <= args.poll_seconds <= 60:
        parser.error("poll-seconds must be between 0.5 and 60")
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.initialize()
    log.info("Worker started; task analysis only, GitHub writeback disabled")
    stop_requested = threading.Event()

    def request_stop(signum, frame):
        log.info("Worker stop requested signal=%s", signum)
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    if args.once:
        run_once()
        return
    while not stop_requested.is_set():
        try:
            if not run_once():
                stop_requested.wait(args.poll_seconds)
        except KeyboardInterrupt:
            break
        except Exception:
            log.exception("Worker poll error; will retry")
            stop_requested.wait(args.poll_seconds)
    log.info("Worker stopped")


if __name__ == "__main__":
    main()

"""Role-specific container health checks using only the Python standard library."""

import json
import os
import sys
from pathlib import Path
from urllib.request import urlopen


def check_api() -> bool:
    with urlopen("http://127.0.0.1:8790/api/health", timeout=2) as response:
        if response.status != 200:
            return False
        payload = json.loads(response.read().decode("utf-8"))
        return payload.get("status") == "ok"


def check_worker() -> bool:
    own_pid = os.getpid()
    marker = os.getenv("ISSUEPILOT_WORKER_PROCESS_MARKER", "issuepilot.worker").encode("utf-8")
    for entry in Path("/proc").glob("[0-9]*/cmdline"):
        if entry.parent.name == str(own_pid):
            continue
        try:
            command = entry.read_bytes()
        except (OSError, PermissionError):
            continue
        if marker in command:
            return True
    return False


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"api", "worker"}:
        return 2
    try:
        healthy = check_api() if sys.argv[1] == "api" else check_worker()
    except (OSError, ValueError, json.JSONDecodeError):
        healthy = False
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())

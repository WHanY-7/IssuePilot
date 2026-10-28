import json
from .config import DATASET_PATH


def get_issues() -> list[dict]:
    if not DATASET_PATH.is_file():
        raise FileNotFoundError(f"Issue dataset missing: {DATASET_PATH}")
    payload = json.loads(DATASET_PATH.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("Dataset must be an object with an items array")
    items = payload["items"]
    if not all(isinstance(x, dict) and isinstance(x.get("issue_number"), int) for x in items):
        raise ValueError("Malformed issue entries")
    return items


def get_issue(issue_number: int) -> dict | None:
    return next((x for x in get_issues() if x["issue_number"] == issue_number), None)

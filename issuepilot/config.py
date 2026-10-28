import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
DATASET_PATH = Path(os.getenv("ISSUEPILOT_DATASET", str(ROOT / "data" / "issues_demo.json")))
DB_PATH = Path(os.getenv("ISSUEPILOT_DB", str(ROOT / "data" / "tasks.sqlite3")))


def similarity_enabled() -> bool:
    """Return the explicit opt-in state for the read-only similarity flow."""
    value = os.getenv("ISSUEPILOT_SIMILARITY_ENABLED", "false").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise RuntimeError("ISSUEPILOT_SIMILARITY_ENABLED must be true or false")


def auth_secret():
    value = os.getenv("ISSUEPILOT_AUTH_SECRET", "")
    if len(value) < 20:
        raise RuntimeError("ISSUEPILOT_AUTH_SECRET must contain at least 20 characters")
    return value

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Load simple KEY=value lines without overriding the process environment."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()

HOST = os.getenv("SMX_HOST", "127.0.0.1")
PORT = int(os.getenv("SMX_PORT", "8080"))
ENV = os.getenv("SMX_ENV", "development")
DATABASE = Path(os.getenv("SMX_DATABASE", "data/xerox.sqlite3"))
if not DATABASE.is_absolute():
    DATABASE = ROOT / DATABASE
STORAGE = Path(os.getenv("SMX_STORAGE", "data/private-documents"))
if not STORAGE.is_absolute():
    STORAGE = ROOT / STORAGE
MAX_REQUEST_BYTES = int(os.getenv("SMX_MAX_REQUEST_BYTES", str(12 * 1024 * 1024)))
PUBLIC_BASE_URL = os.getenv("SMX_PUBLIC_BASE_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
SECURE_COOKIES = os.getenv("SMX_SECURE_COOKIES", "false").lower() == "true"
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

"""Central config: .env loading, filesystem paths, database config, and watchlist.

Edit WATCHLIST below (or override via the WATCHLIST env var, comma-separated)
to change which tickers the pipeline scans. The pipeline is written so adding
or removing tickers here is the only change needed end-to-end.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*_args, **_kwargs) -> bool:
        return False

# Load .env from the project root regardless of current working directory
# (so this works the same from a notebook in /notebooks or a script in /src).
_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

# ── paths ─────────────────────────────────────────────────────────────────
DATA_DIR = _ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
DB_PATH = DATA_DIR / "db" / "bandarmology.sqlite"

for _d in (RAW_DIR, PROCESSED_DIR, DB_PATH.parent):
    _d.mkdir(parents=True, exist_ok=True)

# ── database ──────────────────────────────────────────────────────────────
# Konfigurasi Database untuk arsitektur dual-engine (SQLite & PostgreSQL)
DB_TYPE: str = os.getenv("DB_TYPE", "sqlite").lower()  # "sqlite" | "postgresql"
DB_HOST: str = os.getenv("DB_HOST", "localhost")
DB_PORT: int = int(os.getenv("DB_PORT", "5432"))
DB_NAME: str = os.getenv("DB_NAME", "bandarmology")
DB_USER: str = os.getenv("DB_USER", "")
DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
DATABASE_URL: str | None = os.getenv("DATABASE_URL")  # override semua di atas jika ada

# ── secrets ───────────────────────────────────────────────────────────────
def get_broker_api_token() -> str | None:
    """Read the latest broker API token from `.env` / process env.

    This is resolved at runtime so notebooks can pick up a newly added token
    without depending on the import-time value cached in `config`.

    `BROKER_API_TOKEN` is the public-facing name. `STOCKBIT_TOKEN` remains a
    backward-compatible fallback for existing local setups.
    """
    load_dotenv(_ROOT / ".env")
    token = (
        os.environ.get("BROKER_API_TOKEN", "").strip()
        or os.environ.get("STOCKBIT_TOKEN", "").strip()
    )
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token or None

BROKER_API_TOKEN = get_broker_api_token()

def set_broker_api_token(token: str) -> None:
    """Hot-swap token in-memory and env (dipanggil oleh skrip backfill)."""
    global BROKER_API_TOKEN
    BROKER_API_TOKEN = token
    os.environ["BROKER_API_TOKEN"] = token

# ── watchlist ─────────────────────────────────────────────────────────────
# Start small on purpose — this is a starting point you search/curate by hand.
# The pipeline doesn't care how big this list is; grow it whenever you like.
_DEFAULT_WATCHLIST = [
    "BBCA", "BBRI", "BMRI", "BBNI",   # big banks
    "TLKM", "ASII", "UNVR",            # blue chips
    "GOTO", "BREN", "ANTM",            # high-flow / volatile names
]

def get_watchlist() -> list[str]:
    """Watchlist from env (WATCHLIST=BBCA,BBRI,...) or the default above."""
    env_val = os.environ.get("WATCHLIST", "").strip()
    if env_val:
        return [t.strip().upper() for t in env_val.split(",") if t.strip()]
    return list(_DEFAULT_WATCHLIST)

WATCHLIST = get_watchlist()

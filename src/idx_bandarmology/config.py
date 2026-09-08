"""Central config: .env loading, filesystem paths, and DB connection.

PostgreSQL edition — set DATABASE_URL in .env or use the local defaults below.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*_args, **_kwargs) -> bool:
        return False

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

# ── paths ─────────────────────────────────────────────────────────────────
DATA_DIR = _ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"

for _d in (RAW_DIR, PROCESSED_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── PostgreSQL connection ───────────────────────────────────────────────────
def get_database_url() -> str:
    """Read DATABASE_URL from env. Fallback to local PostgreSQL defaults."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    # Default local PostgreSQL (Debian/Droidspaces)
    user = os.environ.get("DB_USER", "bandar").strip()
    password = os.environ.get("DB_PASSWORD", "bandar123").strip()
    host = os.environ.get("DB_HOST", "localhost").strip()
    port = os.environ.get("DB_PORT", "5432").strip()
    dbname = os.environ.get("DB_NAME", "bandarmology").strip()
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"

DATABASE_URL = get_database_url()

# ── secrets ───────────────────────────────────────────────────────────────
def get_broker_api_token() -> str | None:
    """Read the latest broker API token from `.env` / process env."""
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
    """Update broker API token in memory and os.environ."""
    global BROKER_API_TOKEN
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    BROKER_API_TOKEN = token
    os.environ["BROKER_API_TOKEN"] = token
    
# ── watchlist (legacy 10-ticker default) ──────────────────────────────────
_DEFAULT_WATCHLIST = [
    "BBCA", "BBRI", "BMRI", "BBNI",
    "TLKM", "ASII", "UNVR",
    "GOTO", "BREN", "ANTM",
]


def get_watchlist() -> list[str]:
    """Watchlist from env (WATCHLIST=BBCA,BBRI,...) or the default above."""
    env_val = os.environ.get("WATCHLIST", "").strip()
    if env_val:
        return [t.strip().upper() for t in env_val.split(",") if t.strip()]
    return list(_DEFAULT_WATCHLIST)


WATCHLIST = get_watchlist()

# ── universe mode ─────────────────────────────────────────────────────────
def get_universe_mode() -> str:
    """Default universe mode from env (UNIVERSE_MODE=idx80, all, lq45, ...).

    Options: watchlist, idx30, lq45, idx80, all, liquid, custom.
    """
    return os.environ.get("UNIVERSE_MODE", "watchlist").strip().lower()


UNIVERSE_MODE = get_universe_mode()

# ── broker API rate limit ─────────────────────────────────────────────────
def get_broker_rate_limit() -> float:
    """Requests per minute for Stockbit. Default 8.0 (conservative)."""
    try:
        return float(os.environ.get("BROKER_RATE_LIMIT", "8.0"))
    except ValueError:
        return 8.0


BROKER_RATE_LIMIT = get_broker_rate_limit()

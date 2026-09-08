"""PostgreSQL storage — SQLAlchemy edition (Optimized & Indexed).

Replaces raw psycopg2 connections with SQLAlchemy engine for full pandas
compatibility while keeping bulk-upsert performance via raw psycopg2 
connections from the SQLAlchemy pool with explicit date-filtering.
"""

from __future__ import annotations

from datetime import datetime, date, timezone
from typing import Sequence

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

from . import config

# SQLAlchemy engine dengan Connection Pool yang stabil untuk FastAPI
engine = create_engine(
    config.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=10,
    pool_recycle=1800,
)

# ── schema ───────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    date    DATE NOT NULL,
    ticker  VARCHAR(20) NOT NULL,
    open    NUMERIC,
    high    NUMERIC,
    low     NUMERIC,
    close   NUMERIC,
    volume  BIGINT,
    PRIMARY KEY (date, ticker)
);

CREATE TABLE IF NOT EXISTS broker_flow (
    date                DATE NOT NULL,
    ticker              VARCHAR(20) NOT NULL,
    bandar_signal       VARCHAR(50),
    bandar_signal_score NUMERIC,
    foreign_net_broker  NUMERIC,
    local_net_broker    NUMERIC,
    gov_net_broker      NUMERIC,
    foreign_net_flow    NUMERIC,
    domestic_net_flow   NUMERIC,
    total_value         NUMERIC,
    foreign_signal      VARCHAR(50),
    conclusion_broker   TEXT,
    conclusion_flow     TEXT,
    fetched_at          TIMESTAMP,
    PRIMARY KEY (date, ticker)
);

CREATE TABLE IF NOT EXISTS broker_activity (
    date             DATE NOT NULL,
    ticker           VARCHAR(20) NOT NULL,
    broker_code      VARCHAR(20) NOT NULL,
    participant_type VARCHAR(20),
    buy_value        NUMERIC,
    sell_value       NUMERIC,
    net_value        NUMERIC,
    buy_lot          NUMERIC,
    sell_lot         NUMERIC,
    frequency        NUMERIC,
    buy_avg_price    NUMERIC,
    sell_avg_price   NUMERIC,
    fetched_at       TIMESTAMP,
    PRIMARY KEY (date, ticker, broker_code)
);

CREATE TABLE IF NOT EXISTS runs (
    run_at   TIMESTAMP NOT NULL,
    tickers  TEXT,
    n_prices INTEGER,
    n_broker INTEGER,
    notes    TEXT
);

CREATE TABLE IF NOT EXISTS tickers (
    ticker      VARCHAR(20) PRIMARY KEY,
    name        VARCHAR(200),
    board       VARCHAR(50),
    sector      VARCHAR(100),
    is_active   BOOLEAN DEFAULT TRUE,
    updated_at  TIMESTAMP
);

-- Indeks komposit dan indeks tanggal tunggal untuk akselerasi query
CREATE INDEX IF NOT EXISTS idx_prices_ticker_date ON prices(ticker, date);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);
CREATE INDEX IF NOT EXISTS idx_broker_flow_ticker_date ON broker_flow(ticker, date);
CREATE INDEX IF NOT EXISTS idx_broker_flow_date ON broker_flow(date);
CREATE INDEX IF NOT EXISTS idx_broker_activity_ticker_date ON broker_activity(ticker, date);
CREATE INDEX IF NOT EXISTS idx_broker_activity_date ON broker_activity(date);
CREATE INDEX IF NOT EXISTS idx_broker_activity_broker ON broker_activity(broker_code);
CREATE INDEX IF NOT EXISTS idx_tickers_sector ON tickers(sector);
CREATE INDEX IF NOT EXISTS idx_tickers_board ON tickers(board);
"""


def init_db() -> None:
    """Create tables and indexes if they don't exist yet."""
    with engine.begin() as conn:
        for stmt in _SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))


def _clean_numeric_df(df: pd.DataFrame) -> pd.DataFrame:
    """Replace NaN/inf with None so PostgreSQL accepts them."""
    df = df.copy()
    for col in df.columns:
        if df[col].dtype.kind in "fc":
            df[col] = df[col].replace([float("inf"), float("-inf")], None)
            df[col] = df[col].where(df[col].notna(), None)
    return df


def _get_raw_conn():
    """Get a raw psycopg2 connection from the SQLAlchemy pool for bulk upserts."""
    return engine.raw_connection()


def upsert_prices(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    df = _clean_numeric_df(df)
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
    cols = ["date", "ticker", "open", "high", "low", "close", "volume"]
    rows = [tuple(row) for row in df[cols].values]

    raw_conn = _get_raw_conn()
    try:
        with raw_conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO prices (date, ticker, open, high, low, close, volume)
                VALUES %s
                ON CONFLICT (date, ticker) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
                """,
                rows,
                page_size=2000,
            )
        raw_conn.commit()
    finally:
        raw_conn.close()
    return len(df)


def upsert_broker_flow(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    df = _clean_numeric_df(df)
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
    cols = [
        "date", "ticker", "bandar_signal", "bandar_signal_score",
        "foreign_net_broker", "local_net_broker", "gov_net_broker",
        "foreign_net_flow", "domestic_net_flow", "total_value",
        "foreign_signal", "conclusion_broker", "conclusion_flow", "fetched_at",
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    rows = [tuple(row) for row in df[cols].values]

    raw_conn = _get_raw_conn()
    try:
        with raw_conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO broker_flow (
                    date, ticker, bandar_signal, bandar_signal_score,
                    foreign_net_broker, local_net_broker, gov_net_broker,
                    foreign_net_flow, domestic_net_flow, total_value,
                    foreign_signal, conclusion_broker, conclusion_flow, fetched_at
                )
                VALUES %s
                ON CONFLICT (date, ticker) DO UPDATE SET
                    bandar_signal = EXCLUDED.bandar_signal,
                    bandar_signal_score = EXCLUDED.bandar_signal_score,
                    foreign_net_broker = EXCLUDED.foreign_net_broker,
                    local_net_broker = EXCLUDED.local_net_broker,
                    gov_net_broker = EXCLUDED.gov_net_broker,
                    foreign_net_flow = EXCLUDED.foreign_net_flow,
                    domestic_net_flow = EXCLUDED.domestic_net_flow,
                    total_value = EXCLUDED.total_value,
                    foreign_signal = EXCLUDED.foreign_signal,
                    conclusion_broker = EXCLUDED.conclusion_broker,
                    conclusion_flow = EXCLUDED.conclusion_flow,
                    fetched_at = EXCLUDED.fetched_at
                """,
                rows,
                page_size=2000,
            )
        raw_conn.commit()
    finally:
        raw_conn.close()
    return len(df)


def upsert_broker_activity(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    df = _clean_numeric_df(df)
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
    cols = [
        "date", "ticker", "broker_code", "participant_type",
        "buy_value", "sell_value", "net_value",
        "buy_lot", "sell_lot", "frequency",
        "buy_avg_price", "sell_avg_price", "fetched_at",
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    rows = [tuple(row) for row in df[cols].values]

    raw_conn = _get_raw_conn()
    try:
        with raw_conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO broker_activity (
                    date, ticker, broker_code, participant_type,
                    buy_value, sell_value, net_value,
                    buy_lot, sell_lot, frequency,
                    buy_avg_price, sell_avg_price, fetched_at
                )
                VALUES %s
                ON CONFLICT (date, ticker, broker_code) DO UPDATE SET
                    participant_type = EXCLUDED.participant_type,
                    buy_value = EXCLUDED.buy_value,
                    sell_value = EXCLUDED.sell_value,
                    net_value = EXCLUDED.net_value,
                    buy_lot = EXCLUDED.buy_lot,
                    sell_lot = EXCLUDED.sell_lot,
                    frequency = EXCLUDED.frequency,
                    buy_avg_price = EXCLUDED.buy_avg_price,
                    sell_avg_price = EXCLUDED.sell_avg_price,
                    fetched_at = EXCLUDED.fetched_at
                """,
                rows,
                page_size=2000,
            )
        raw_conn.commit()
    finally:
        raw_conn.close()
    return len(df)


def log_run(tickers: list[str], n_prices: int, n_broker: int, notes: str = "") -> None:
    init_db()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO runs (run_at, tickers, n_prices, n_broker, notes)
                VALUES (:run_at, :tickers, :n_prices, :n_broker, :notes)
            """),
            {
                "run_at": datetime.now(timezone.utc),
                "tickers": ",".join(t.upper() for t in tickers),
                "n_prices": n_prices,
                "n_broker": n_broker,
                "notes": notes,
            },
        )


def read_prices(
    tickers: Sequence[str] | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> pd.DataFrame:
    """Read prices with optional ticker and date-range filtering pushed down to SQL."""
    init_db()
    clauses = []
    params: dict[str, object] = {}
    if tickers:
        clauses.append("ticker = ANY(:tickers)")
        params["tickers"] = [t.upper() for t in tickers]
    if start_date:
        clauses.append("date >= :start_date")
        params["start_date"] = str(start_date)
    if end_date:
        clauses.append("date <= :end_date")
        params["end_date"] = str(end_date)
    
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    q = f"SELECT * FROM prices{where} ORDER BY ticker, date ASC"
    with engine.connect() as conn:
        return pd.read_sql(text(q), conn, params=params, parse_dates=["date"])


def read_broker_flow(
    tickers: Sequence[str] | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> pd.DataFrame:
    """Read broker flow with optional ticker and date-range filtering."""
    init_db()
    clauses = []
    params: dict[str, object] = {}
    if tickers:
        clauses.append("ticker = ANY(:tickers)")
        params["tickers"] = [t.upper() for t in tickers]
    if start_date:
        clauses.append("date >= :start_date")
        params["start_date"] = str(start_date)
    if end_date:
        clauses.append("date <= :end_date")
        params["end_date"] = str(end_date)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    q = f"SELECT * FROM broker_flow{where} ORDER BY ticker, date ASC"
    with engine.connect() as conn:
        return pd.read_sql(text(q), conn, params=params, parse_dates=["date"])


def read_broker_activity(
    tickers: Sequence[str] | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> pd.DataFrame:
    """Read broker activity with optional ticker and date-range filtering."""
    init_db()
    clauses = []
    params: dict[str, object] = {}
    if tickers:
        clauses.append("ticker = ANY(:tickers)")
        params["tickers"] = [t.upper() for t in tickers]
    if start_date:
        clauses.append("date >= :start_date")
        params["start_date"] = str(start_date)
    if end_date:
        clauses.append("date <= :end_date")
        params["end_date"] = str(end_date)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    q = f"SELECT * FROM broker_activity{where} ORDER BY ticker, date ASC, net_value DESC"
    with engine.connect() as conn:
        return pd.read_sql(text(q), conn, params=params, parse_dates=["date"])


def read_runs() -> pd.DataFrame:
    init_db()
    with engine.connect() as conn:
        return pd.read_sql(
            text("SELECT * FROM runs ORDER BY run_at DESC LIMIT 50"),
            conn,
            parse_dates=["run_at"],
        )

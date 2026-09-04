"""Storage layer supporting dual-engine: SQLite (dev) and PostgreSQL (prod).

This module uses a factory pattern to return the appropriate StorageAdapter
based on the DB_TYPE environment variable. Both adapters ensure non-destructive
database initialization (using IF NOT EXISTS) and idempotent upserts.

For PostgreSQL, SQLAlchemy with connection pooling is used for scalability.
SQLite fallback uses standard sqlite3 for simplicity and backwards compatibility.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Protocol

import pandas as pd

from . import config

# --- Protocol Definition (Goal 7.3.1) ---
class StorageAdapter(Protocol):
    def init_db(self) -> None: ...
    def upsert_prices(self, df: pd.DataFrame) -> int: ...
    def upsert_broker_flow(self, df: pd.DataFrame) -> int: ...
    def upsert_broker_activity(self, df: pd.DataFrame) -> int: ...
    def log_run(self, tickers: list[str], n_prices: int, n_broker: int, n_activity: int = 0, notes: str = "") -> None: ...
    def read_prices(self, tickers: list[str] | None = None) -> pd.DataFrame: ...
    def read_broker_flow(self, tickers: list[str] | None = None) -> pd.DataFrame: ...
    def read_broker_activity(self, tickers: list[str] | None = None) -> pd.DataFrame: ...
    def read_runs(self) -> pd.DataFrame: ...

# --- SQLite Adapter (Goal 7.3.2) ---
class SQLiteAdapter:
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS prices (
        date    TEXT NOT NULL,
        ticker  TEXT NOT NULL,
        open    REAL,
        high    REAL,
        low     REAL,
        close   REAL,
        volume  REAL,
        PRIMARY KEY (date, ticker)
    );

    CREATE TABLE IF NOT EXISTS broker_flow (
        date                TEXT NOT NULL,
        ticker              TEXT NOT NULL,
        bandar_signal       TEXT,
        bandar_signal_score REAL,
        foreign_net_broker  REAL,
        local_net_broker    REAL,
        gov_net_broker      REAL,
        foreign_net_flow    REAL,
        domestic_net_flow   REAL,
        total_value         REAL,
        foreign_signal      TEXT,
        conclusion_broker   TEXT,
        conclusion_flow     TEXT,
        fetched_at          TEXT,
        PRIMARY KEY (date, ticker)
    );

    CREATE TABLE IF NOT EXISTS broker_activity (
        date             TEXT NOT NULL,
        ticker           TEXT NOT NULL,
        broker_code      TEXT NOT NULL,
        participant_type TEXT,
        buy_value        REAL,
        sell_value       REAL,
        net_value        REAL,
        buy_lot          REAL,
        sell_lot         REAL,
        frequency        REAL,
        buy_avg_price    REAL,
        sell_avg_price   REAL,
        fetched_at       TEXT,
        PRIMARY KEY (date, ticker, broker_code)
    );

    CREATE TABLE IF NOT EXISTS runs (
        run_at      TEXT NOT NULL,
        tickers     TEXT,
        n_prices    INTEGER,
        n_broker    INTEGER,
        n_activity  INTEGER DEFAULT 0,
        notes       TEXT
    );
    """

    @contextmanager
    def get_conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(config.DB_PATH)
        try:
            yield conn
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.get_conn() as conn:
            conn.executescript(self._SCHEMA)
            conn.commit()

    def upsert_prices(self, df: pd.DataFrame) -> int:
        if df.empty: return 0
        df = df.copy()
        df["date"] = df["date"].astype(str)
        with self.get_conn() as conn:
            rows = df[["date", "ticker", "open", "high", "low", "close", "volume"]].values.tolist()
            conn.executemany(
                """INSERT INTO prices (date, ticker, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(date, ticker) DO UPDATE SET
                     open=excluded.open, high=excluded.high, low=excluded.low,
                     close=excluded.close, volume=excluded.volume""",
                rows,
            )
            conn.commit()
        return len(df)

    def upsert_broker_flow(self, df: pd.DataFrame) -> int:
        if df.empty: return 0
        df = df.copy()
        df["date"] = df["date"].astype(str)
        cols = [
            "date", "ticker", "bandar_signal", "bandar_signal_score",
            "foreign_net_broker", "local_net_broker", "gov_net_broker",
            "foreign_net_flow", "domestic_net_flow", "total_value",
            "foreign_signal", "conclusion_broker", "conclusion_flow", "fetched_at",
        ]
        for c in cols:
            if c not in df.columns: df[c] = None
        with self.get_conn() as conn:
            rows = df[cols].values.tolist()
            placeholders = ", ".join("?" * len(cols))
            updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("date", "ticker"))
            conn.executemany(
                f"""INSERT INTO broker_flow ({', '.join(cols)}) VALUES ({placeholders})
                    ON CONFLICT(date, ticker) DO UPDATE SET {updates}""",
                rows,
            )
            conn.commit()
        return len(df)

    def upsert_broker_activity(self, df: pd.DataFrame) -> int:
        if df.empty: return 0
        df = df.copy()
        df["date"] = df["date"].astype(str)
        cols = [
            "date", "ticker", "broker_code", "participant_type",
            "buy_value", "sell_value", "net_value",
            "buy_lot", "sell_lot", "frequency",
            "buy_avg_price", "sell_avg_price", "fetched_at",
        ]
        for c in cols:
            if c not in df.columns: df[c] = None
        with self.get_conn() as conn:
            rows = df[cols].values.tolist()
            placeholders = ", ".join("?" * len(cols))
            updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("date", "ticker", "broker_code"))
            conn.executemany(
                f"""INSERT INTO broker_activity ({', '.join(cols)}) VALUES ({placeholders})
                    ON CONFLICT(date, ticker, broker_code) DO UPDATE SET {updates}""",
                rows,
            )
            conn.commit()
        return len(df)

    def log_run(self, tickers: list[str], n_prices: int, n_broker: int, n_activity: int = 0, notes: str = "") -> None:
        with self.get_conn() as conn:
            conn.execute(
                "INSERT INTO runs (run_at, tickers, n_prices, n_broker, n_activity, notes) VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), ",".join(tickers), n_prices, n_broker, n_activity, notes),
            )
            conn.commit()

    def read_prices(self, tickers: list[str] | None = None) -> pd.DataFrame:
        self.init_db()
        q = "SELECT * FROM prices"
        params = ()
        if tickers:
            q += f" WHERE ticker IN ({','.join('?' * len(tickers))})"
            params = tuple(t.upper() for t in tickers)
        with self.get_conn() as conn:
            df = pd.read_sql(q, conn, params=params, parse_dates=["date"])
        return df.sort_values(["ticker", "date"]).reset_index(drop=True)

    def read_broker_flow(self, tickers: list[str] | None = None) -> pd.DataFrame:
        self.init_db()
        q = "SELECT * FROM broker_flow"
        params = ()
        if tickers:
            q += f" WHERE ticker IN ({','.join('?' * len(tickers))})"
            params = tuple(t.upper() for t in tickers)
        with self.get_conn() as conn:
            df = pd.read_sql(q, conn, params=params, parse_dates=["date"])
        return df.sort_values(["ticker", "date"]).reset_index(drop=True)

    def read_broker_activity(self, tickers: list[str] | None = None) -> pd.DataFrame:
        self.init_db()
        q = "SELECT * FROM broker_activity"
        params = ()
        if tickers:
            q += f" WHERE ticker IN ({','.join('?' * len(tickers))})"
            params = tuple(t.upper() for t in tickers)
        with self.get_conn() as conn:
            df = pd.read_sql(q, conn, params=params, parse_dates=["date"])
        return df.sort_values(["ticker", "date", "net_value"], ascending=[True, True, False]).reset_index(drop=True)

    def read_runs(self) -> pd.DataFrame:
        self.init_db()
        with self.get_conn() as conn:
            return pd.read_sql("SELECT * FROM runs ORDER BY run_at DESC", conn)

# --- PostgreSQL Adapter (Goal 7.3.3) ---
class PostgreSQLAdapter:
    def __init__(self):
        from sqlalchemy import create_engine
        # DATABASE_URL overrides individual settings if available
        db_url = getattr(config, 'DATABASE_URL', None) or os.environ.get("DATABASE_URL")
        if not db_url:
            db_user = getattr(config, 'DB_USER', os.environ.get("DB_USER", ""))
            db_pass = getattr(config, 'DB_PASSWORD', os.environ.get("DB_PASSWORD", ""))
            db_host = getattr(config, 'DB_HOST', os.environ.get("DB_HOST", "localhost"))
            db_port = getattr(config, 'DB_PORT', os.environ.get("DB_PORT", "5432"))
            db_name = getattr(config, 'DB_NAME', os.environ.get("DB_NAME", "bandarmology"))
            # Ensure proper format even with missing user/pass
            auth = f"{db_user}:{db_pass}@" if db_user else ""
            db_url = f"postgresql://{auth}{db_host}:{db_port}/{db_name}"
        
        self.engine = create_engine(
            db_url, 
            pool_size=5, 
            max_overflow=10,
            pool_pre_ping=True
        )

    def init_db(self) -> None:
        from sqlalchemy import text
        schema = """
        CREATE TABLE IF NOT EXISTS prices (
            date    DATE NOT NULL,
            ticker  VARCHAR(10) NOT NULL,
            open    NUMERIC(18,4),
            high    NUMERIC(18,4),
            low     NUMERIC(18,4),
            close   NUMERIC(18,4),
            volume  BIGINT,
            PRIMARY KEY (date, ticker)
        );

        CREATE TABLE IF NOT EXISTS broker_flow (
            date                DATE NOT NULL,
            ticker              VARCHAR(10) NOT NULL,
            bandar_signal       VARCHAR(30),
            bandar_signal_score SMALLINT,
            foreign_net_broker  NUMERIC(18,2),
            local_net_broker    NUMERIC(18,2),
            gov_net_broker      NUMERIC(18,2),
            foreign_net_flow    NUMERIC(18,2),
            domestic_net_flow   NUMERIC(18,2),
            total_value         NUMERIC(18,2),
            foreign_signal      VARCHAR(30),
            conclusion_broker   TEXT,
            conclusion_flow     TEXT,
            fetched_at          TIMESTAMPTZ,
            PRIMARY KEY (date, ticker)
        );

        CREATE TABLE IF NOT EXISTS broker_activity (
            date             DATE NOT NULL,
            ticker           VARCHAR(10) NOT NULL,
            broker_code      VARCHAR(10) NOT NULL,
            participant_type VARCHAR(20),
            buy_value        NUMERIC(18,2),
            sell_value       NUMERIC(18,2),
            net_value        NUMERIC(18,2),
            buy_lot          NUMERIC(18,2),
            sell_lot         NUMERIC(18,2),
            frequency        INTEGER,
            buy_avg_price    NUMERIC(18,4),
            sell_avg_price   NUMERIC(18,4),
            fetched_at       TIMESTAMPTZ,
            PRIMARY KEY (date, ticker, broker_code)
        );

        CREATE TABLE IF NOT EXISTS runs (
            id          SERIAL PRIMARY KEY,
            run_at      TIMESTAMPTZ NOT NULL,
            tickers     TEXT,
            n_prices    INTEGER,
            n_broker    INTEGER,
            n_activity  INTEGER,
            notes       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker);
        CREATE INDEX IF NOT EXISTS idx_broker_flow_ticker ON broker_flow(ticker);
        CREATE INDEX IF NOT EXISTS idx_broker_flow_date ON broker_flow(date);
        CREATE INDEX IF NOT EXISTS idx_broker_activity_ticker ON broker_activity(ticker);
        CREATE INDEX IF NOT EXISTS idx_broker_activity_date ON broker_activity(date);
        CREATE INDEX IF NOT EXISTS idx_broker_activity_broker ON broker_activity(broker_code);
        CREATE INDEX IF NOT EXISTS idx_runs_run_at ON runs(run_at DESC);
        """
        with self.engine.begin() as conn:
            # Splitting script and filtering empty statements
            for statement in schema.split(';'):
                if statement.strip():
                    conn.execute(text(statement))

            # Optional: Check for TimescaleDB extension
            try:
                ext_check = conn.execute(text("SELECT extname FROM pg_extension WHERE extname = 'timescaledb'")).fetchone()
                if ext_check:
                    ts_script = """
                    DO $$
                    BEGIN
                        IF EXISTS (SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = 'prices') THEN
                            RAISE NOTICE 'prices already a hypertable';
                        ELSE
                            PERFORM create_hypertable('prices', by_range('date'), if_not_exists => TRUE);
                        END IF;
                    END $$;
                    """
                    conn.execute(text(ts_script))
            except Exception as e:
                # Silently ignore if lacking permissions or timescaledb objects
                pass

    def upsert_prices(self, df: pd.DataFrame) -> int:
        if df.empty: return 0
        from sqlalchemy import text
        df = df.copy()
        df['date'] = pd.to_datetime(df['date']).dt.date
        
        insert_stmt = text("""
            INSERT INTO prices (date, ticker, open, high, low, close, volume)
            VALUES (:date, :ticker, :open, :high, :low, :close, :volume)
            ON CONFLICT (date, ticker) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume
        """)
        
        with self.engine.begin() as conn:
            conn.execute(insert_stmt, df.to_dict('records'))
        return len(df)

    def upsert_broker_flow(self, df: pd.DataFrame) -> int:
        if df.empty: return 0
        from sqlalchemy import text
        df = df.copy()
        df['date'] = pd.to_datetime(df['date']).dt.date
        cols = [
            "date", "ticker", "bandar_signal", "bandar_signal_score",
            "foreign_net_broker", "local_net_broker", "gov_net_broker",
            "foreign_net_flow", "domestic_net_flow", "total_value",
            "foreign_signal", "conclusion_broker", "conclusion_flow", "fetched_at",
        ]
        for c in cols:
            if c not in df.columns: df[c] = None

        set_clause = ",\n".join([f"{col} = EXCLUDED.{col}" for col in cols if col not in ("date", "ticker")])
        values_clause = ", ".join([f":{col}" for col in cols])
        cols_clause = ", ".join(cols)

        insert_stmt = text(f"""
            INSERT INTO broker_flow ({cols_clause})
            VALUES ({values_clause})
            ON CONFLICT (date, ticker) DO UPDATE SET
                {set_clause}
        """)

        with self.engine.begin() as conn:
            conn.execute(insert_stmt, df[cols].to_dict('records'))
        return len(df)

    def upsert_broker_activity(self, df: pd.DataFrame) -> int:
        if df.empty: return 0
        from sqlalchemy import text
        df = df.copy()
        df['date'] = pd.to_datetime(df['date']).dt.date
        cols = [
            "date", "ticker", "broker_code", "participant_type",
            "buy_value", "sell_value", "net_value",
            "buy_lot", "sell_lot", "frequency",
            "buy_avg_price", "sell_avg_price", "fetched_at",
        ]
        for c in cols:
            if c not in df.columns: df[c] = None

        set_clause = ",\n".join([f"{col} = EXCLUDED.{col}" for col in cols if col not in ("date", "ticker", "broker_code")])
        values_clause = ", ".join([f":{col}" for col in cols])
        cols_clause = ", ".join(cols)

        insert_stmt = text(f"""
            INSERT INTO broker_activity ({cols_clause})
            VALUES ({values_clause})
            ON CONFLICT (date, ticker, broker_code) DO UPDATE SET
                {set_clause}
        """)

        with self.engine.begin() as conn:
            conn.execute(insert_stmt, df[cols].to_dict('records'))
        return len(df)

    def log_run(self, tickers: list[str], n_prices: int, n_broker: int, n_activity: int = 0, notes: str = "") -> None:
        from sqlalchemy import text
        insert_stmt = text("""
            INSERT INTO runs (run_at, tickers, n_prices, n_broker, n_activity, notes) 
            VALUES (:run_at, :tickers, :n_prices, :n_broker, :n_activity, :notes)
        """)
        with self.engine.begin() as conn:
            conn.execute(insert_stmt, {
                "run_at": datetime.now(timezone.utc),
                "tickers": ",".join(tickers),
                "n_prices": n_prices,
                "n_broker": n_broker,
                "n_activity": n_activity,
                "notes": notes
            })

    def read_prices(self, tickers: list[str] | None = None) -> pd.DataFrame:
        self.init_db()
        from sqlalchemy import text
        q = "SELECT * FROM prices"
        params = {}
        if tickers:
            q += " WHERE ticker IN :tickers"
            params["tickers"] = tuple(t.upper() for t in tickers)
        with self.engine.connect() as conn:
            df = pd.read_sql(text(q), conn, params=params, parse_dates=["date"])
        return df.sort_values(["ticker", "date"]).reset_index(drop=True)

    def read_broker_flow(self, tickers: list[str] | None = None) -> pd.DataFrame:
        self.init_db()
        from sqlalchemy import text
        q = "SELECT * FROM broker_flow"
        params = {}
        if tickers:
            q += " WHERE ticker IN :tickers"
            params["tickers"] = tuple(t.upper() for t in tickers)
        with self.engine.connect() as conn:
            df = pd.read_sql(text(q), conn, params=params, parse_dates=["date"])
        return df.sort_values(["ticker", "date"]).reset_index(drop=True)

    def read_broker_activity(self, tickers: list[str] | None = None) -> pd.DataFrame:
        self.init_db()
        from sqlalchemy import text
        q = "SELECT * FROM broker_activity"
        params = {}
        if tickers:
            q += " WHERE ticker IN :tickers"
            params["tickers"] = tuple(t.upper() for t in tickers)
        with self.engine.connect() as conn:
            df = pd.read_sql(text(q), conn, params=params, parse_dates=["date"])
        return df.sort_values(["ticker", "date", "net_value"], ascending=[True, True, False]).reset_index(drop=True)

    def read_runs(self) -> pd.DataFrame:
        self.init_db()
        from sqlalchemy import text
        with self.engine.connect() as conn:
            return pd.read_sql(text("SELECT * FROM runs ORDER BY run_at DESC"), conn)

# --- Factory Integration (Goal 7.3.4 & 7.3.5) ---
def get_storage() -> StorageAdapter:
    # Use environment variable directly if config module lacks it temporarily during transition
    db_type = getattr(config, 'DB_TYPE', os.environ.get("DB_TYPE", "sqlite")).lower()
    if db_type == "postgresql":
        return PostgreSQLAdapter()
    return SQLiteAdapter()

# Global instance for easy import in other modules
# (e.g., pipeline.py can keep using: from . import storage; storage.upsert_prices(...) )
storage = get_storage()

# Expose adapter engine for universe.py (since it expects storage.engine)
# Only available when using PostgreSQL
engine = getattr(storage, 'engine', None)

# --- Backward compatibility bindings for direct function calls ---
init_db = storage.init_db
upsert_prices = storage.upsert_prices
upsert_broker_flow = storage.upsert_broker_flow
upsert_broker_activity = storage.upsert_broker_activity
log_run = storage.log_run
read_prices = storage.read_prices
read_broker_flow = storage.read_broker_flow
read_broker_activity = storage.read_broker_activity
read_runs = storage.read_runs

"""Pipeline orchestrator — scrape -> clean -> store, one call to run it all."""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any
import pandas as pd

from . import broker_api, config, prices, storage, universe

def _broker_flow_rows(watchlist_results: dict) -> pd.DataFrame:
    today = datetime.now(timezone.utc).date().isoformat()
    fetched_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for sym, r in watchlist_results.items():
        if not r.get("available"):
            continue
        broker = r.get("broker") or {}
        fd = r.get("foreignDomestic") or {}
        rows.append({
            "date": broker.get("date") or fd.get("date") or today,
            "ticker": sym,
            "bandar_signal": broker.get("signal"),
            "bandar_signal_score": broker.get("signalScore"),
            "foreign_net_broker": broker.get("foreignNet"),
            "local_net_broker": broker.get("localNet"),
            "gov_net_broker": broker.get("govNet"),
            "foreign_net_flow": fd.get("netForeign"),
            "domestic_net_flow": fd.get("netDomestic"),
            "total_value": fd.get("totalValue"),
            "foreign_signal": fd.get("signal"),
            "conclusion_broker": broker.get("conclusion"),
            "conclusion_flow": fd.get("conclusion"),
            "fetched_at": fetched_at,
        })
    return pd.DataFrame(rows)

def _already_fetched_today(tickers: list[str], table: str = "broker_flow") -> list[str]:
    if not tickers:
        return []
    from sqlalchemy import text
    q = f"""
        SELECT DISTINCT ticker FROM {table} 
        WHERE date = CURRENT_DATE 
        AND ticker = ANY(:tickers)
    """
    with storage.engine.connect() as conn:
        df = pd.read_sql(text(q), conn, params={"tickers": [t.upper() for t in tickers]})
    return df["ticker"].str.upper().tolist() if not df.empty else []

def run(
    tickers: list[str] | None = None,
    universe_mode: str | None = None,
    price_period: str = "1y",
    fetch_broker_data: bool = True,
    resume: bool = True,
    broker_batch_size: int | None = 100,
) -> dict[str, Any]:
    t0 = time.monotonic()
    if date.today().weekday() >= 5:
        print("[pipeline] Weekend detected — market closed, skipping broker fetch.")
        return {"tickers": [], "mode": "weekend", "n_prices": 0, "n_broker": 0, "n_activity": 0, "elapsed_seconds": 0, "notes": "skipped: weekend"}

    if tickers:
        syms = [t.upper() for t in tickers if t]
        mode_label = "custom"
    elif universe_mode:
        syms = universe.get_universe(universe_mode)
        mode_label = universe_mode
    else:
        syms = [t.upper() for t in config.WATCHLIST]
        mode_label = "watchlist"

    storage.init_db()
    
    t1 = time.monotonic()
    print(f"[pipeline] fetching prices from IDX API for {len(syms)} tickers...")
    n_prices = prices.fetch_history_many(syms, period=price_period)
    t2 = time.monotonic()
    print(f"[pipeline]   -> {n_prices} price rows upserted in {t2-t1:.1f}s")

    n_broker = 0
    n_activity = 0
    skipped: list[str] = []

    if fetch_broker_data and broker_api.is_available():
        target_syms = syms
        if resume:
            skipped = _already_fetched_today(syms)
            if skipped:
                print(f"[pipeline] resume: skipping {len(skipped)} tickers already fetched recently")
                target_syms = [s for s in syms if s not in skipped]

        if target_syms:
            print(f"[pipeline] fetching broker/bandar data for {len(target_syms)} tickers...")
            t3 = time.monotonic()
            batch_size = broker_batch_size if broker_batch_size else 100
            
            for i in range(0, len(target_syms), batch_size):
                batch = target_syms[i:i + batch_size]
                print(f"\n[pipeline]   ▶ BATCH {i//batch_size + 1}/{(len(target_syms)-1)//batch_size + 1}: {len(batch)} tickers")
                
                batch_results = broker_api.fetch_watchlist(batch)
                batch_df = _broker_flow_rows(batch_results)
                
                if not batch_df.empty:
                    saved_broker = storage.upsert_broker_flow(batch_df)
                    n_broker += saved_broker
                    
                    start = batch_df["date"].min()
                    end = batch_df["date"].max()
                    valid_syms = [s for s in batch if s in batch_results and batch_results[s].get("available")]
                    
                    if valid_syms:
                        _hist_broker, activity_df = broker_api.fetch_historical_broker_data(valid_syms, start, end)
                        if not activity_df.empty:
                            n_activity += storage.upsert_broker_activity(activity_df)
                            
                    print(f"[pipeline]   🔄 Batch saved to DB! Cumulative: {n_broker} broker rows, {n_activity} activity rows")
            print(f"[pipeline]   -> Total {n_broker} broker_flow rows and {n_activity} activity rows upserted in {time.monotonic()-t3:.1f}s")
        else:
            print("[pipeline] all tickers already fetched recently (resume=True).")

    elapsed = time.monotonic() - t0
    notes = "ok" if (n_prices or n_broker) else "no data fetched"
    storage.log_run(syms, n_prices, n_broker, n_activity=n_activity, notes=f"{notes}; mode={mode_label}; skipped={len(skipped)}")

    return {"tickers": syms, "mode": mode_label, "n_prices": n_prices, "n_broker": n_broker, "n_activity": n_activity, "elapsed_seconds": round(elapsed, 1)}

def backfill_broker_history(
    tickers: list[str] | None = None,
    universe_mode: str | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    price_period: str = "1y",
    refresh_prices: bool = True,
) -> dict[str, Any]:
    t0 = time.monotonic()
    if not broker_api.is_available():
        raise RuntimeError("BROKER_API_TOKEN is not configured.")

    if tickers:
        syms = [t.upper() for t in tickers if t]
        mode_label = "custom"
    elif universe_mode:
        syms = universe.get_universe(universe_mode)
        mode_label = universe_mode
    else:
        syms = [t.upper() for t in config.WATCHLIST]
        mode_label = "watchlist"

    storage.init_db()
    n_prices = 0
    if refresh_prices:
        print(f"[pipeline] refreshing prices from IDX API for {len(syms)} tickers...")
        n_prices = prices.fetch_history_many(syms, period=price_period)

    print(f"[pipeline] backfilling broker/bandar history for {len(syms)} tickers from {start_date} to {end_date}...")
    broker_df, activity_df = broker_api.fetch_historical_broker_data(syms, start_date, end_date)
    n_broker = storage.upsert_broker_flow(broker_df)
    n_activity = storage.upsert_broker_activity(activity_df)
    
    elapsed = time.monotonic() - t0
    storage.log_run(syms, n_prices, n_broker, n_activity=n_activity, notes=f"backfill {start_date} to {end_date}")
    
    return {"tickers": syms, "mode": mode_label, "n_prices": n_prices, "n_broker": n_broker, "n_activity": n_activity, "elapsed_seconds": round(elapsed, 1)}

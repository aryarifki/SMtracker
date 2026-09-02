"""Pipeline orchestrator — scrape -> clean -> store, one call to run it all.

Enhanced for large universes:
  * Rate-limited broker fetching (safe for 900 tickers).
  * Batch processing with progress logging and runtime tracking.
  * Resume mode: skip tickers already stored for today.
  * Universe mode integration (watchlist, idx30, lq45, idx80, all, liquid).

This is the only module you typically need to call directly:

    from idx_bandarmology import pipeline
    pipeline.run(universe="idx80")          # fetch IDX80
    pipeline.run(universe="all")            # fetch all ~900 tickers (slow!)
    pipeline.backfill_broker_history(universe="lq45", start_date=..., end_date=...)

Each run:
  1. Resolves universe into concrete ticker list.
  2. Pulls daily OHLCV from yfinance for every ticker (price history).
  3. Pulls today's broker/bandar snapshot for every ticker (rate-limited).
  4. Cleans/flattens both into tidy tables.
  5. Upserts into PostgreSQL.
  6. Logs the run with timing metrics.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

from . import broker_api, config, prices, storage, universe


def _broker_flow_rows(watchlist_results: dict) -> pd.DataFrame:
    """Flatten broker_api.fetch_watchlist() output into one tidy DataFrame."""
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
    """Return tickers that already have a row for CURRENT_DATE."""
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
    broker_batch_size: int | None = None,
) -> dict[str, Any]:
    """Run the full pipeline once. Returns a summary dict with timing."""
    t0 = time.monotonic()

    # ── SKIP WEEKEND ──
    if date.today().weekday() >= 5:
        print("[pipeline] Weekend detected — market closed, skipping broker fetch.")
        return {
            "tickers": [],
            "mode": "weekend",
            "n_prices": 0,
            "n_broker": 0,
            "n_activity": 0,
            "elapsed_seconds": 0,
            "notes": "skipped: weekend (market closed)",
        }
    # ── end skip weekend ──

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
    print(f"[pipeline] universe mode={mode_label}, tickers={len(syms)}")

    # 1) prices
    t1 = time.monotonic()
    print(f"[pipeline] fetching prices from IDX API for {len(syms)} tickers...")
    
    # UPDATED: Penarikan harga sekarang menangani batch-commit sendiri dan mengembalikan angka
    n_prices = prices.fetch_history_many(syms, period=price_period)
    
    t2 = time.monotonic()
    print(f"[pipeline]   -> {n_prices} price rows upserted in {t2-t1:.1f}s")

    # 2) broker / bandar flow
    n_broker = 0
    n_activity = 0
    broker_results: dict = {}
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
            print(f"[pipeline]   estimated time: ~{len(target_syms) * 8 / 60:.0f} minutes at 8s interval")
            t3 = time.monotonic()

            if broker_batch_size and len(target_syms) > broker_batch_size:
                all_results: dict = {}
                for i in range(0, len(target_syms), broker_batch_size):
                    batch = target_syms[i:i + broker_batch_size]
                    print(f"[pipeline]   batch {i//broker_batch_size + 1}/{(len(target_syms)-1)//broker_batch_size + 1}: {len(batch)} tickers")
                    batch_results = broker_api.fetch_watchlist(batch, progress_every=max(1, len(batch)//5))
                    all_results.update(batch_results)
                    if i + broker_batch_size < len(target_syms):
                        pause = 10.0
                        print(f"[pipeline]   pausing {pause:.0f}s between batches...")
                        time.sleep(pause)
                broker_results = all_results
            else:
                broker_results = broker_api.fetch_watchlist(target_syms, progress_every=max(1, len(target_syms)//10))

            broker_df = _broker_flow_rows(broker_results)
            n_broker = storage.upsert_broker_flow(broker_df)
            t4 = time.monotonic()
            print(f"[pipeline]   -> {n_broker} broker_flow rows upserted in {t4-t3:.1f}s")

            if not broker_df.empty:
                start = broker_df["date"].min()
                end = broker_df["date"].max()
                print("[pipeline] fetching per-broker distribution rows...")
                t5 = time.monotonic()
                _, activity_df = broker_api.fetch_historical_broker_data(
                    [s for s in target_syms if s in broker_results and broker_results[s].get("available")],
                    start, end,
                )
                n_activity = storage.upsert_broker_activity(activity_df)
                t6 = time.monotonic()
                print(f"[pipeline]   -> {n_activity} broker_activity rows upserted in {t6-t5:.1f}s")
        else:
            print("[pipeline] all tickers already fetched recently (resume=True).")
    elif fetch_broker_data:
        print("[pipeline]   BROKER_API_TOKEN not set — skipping broker/bandar data")

    elapsed = time.monotonic() - t0
    notes = "ok" if (n_prices or n_broker) else "no data fetched"
    storage.log_run(syms, n_prices, n_broker, notes=f"{notes}; mode={mode_label}; elapsed={elapsed:.0f}s; skipped={len(skipped)}")

    result = {
        "tickers": syms,
        "mode": mode_label,
        "n_prices": n_prices,
        "n_broker": n_broker,
        "n_activity": n_activity,
        "elapsed_seconds": round(elapsed, 1),
        "broker_skipped": len(skipped),
        "broker_fetched": len(syms) - len(skipped),
        "notes": notes,
    }
    print(f"[pipeline] run complete in {elapsed:.1f}s: {result['n_prices']} prices, {result['n_broker']} broker rows")
    return result


def backfill_broker_history(
    tickers: list[str] | None = None,
    universe_mode: str | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    price_period: str = "1y",
    refresh_prices: bool = True,
) -> dict[str, Any]:
    """Backfill historical broker/bandar rows for event-study analysis."""
    t0 = time.monotonic()

    if not broker_api.is_available():
        raise RuntimeError("BROKER_API_TOKEN/STOCKBIT_TOKEN is not configured.")
    if start_date is None or end_date is None:
        raise ValueError("start_date and end_date are required.")

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
    print(f"[pipeline] backfill mode={mode_label}, tickers={len(syms)}, range={start_date} to {end_date}")

    n_prices = 0
    if refresh_prices:
        t1 = time.monotonic()
        print(f"[pipeline] refreshing prices from IDX API for {len(syms)} tickers...")
        
        # UPDATED: Penarikan harga sekarang menangani batch-commit sendiri dan mengembalikan angka
        n_prices = prices.fetch_history_many(syms, period=price_period)
        
        print(f"[pipeline]   -> {n_prices} price rows in {time.monotonic()-t1:.1f}s")

    t2 = time.monotonic()
    print(f"[pipeline] backfilling broker/bandar history...")
    
    n_broker, n_activity = broker_api.fetch_historical_broker_data(syms, start_date, end_date)
    
    t3 = time.monotonic()
    print(f"[pipeline]   -> {n_broker} broker_flow rows, {n_activity} activity rows safely saved to DB in {t3-t2:.1f}s")

    elapsed = time.monotonic() - t0
    storage.log_run(
        syms,
        n_prices,
        n_broker,
        notes=f"backfill {start_date} to {end_date}; mode={mode_label}; elapsed={elapsed:.0f}s; activity={n_activity}",
    )
    return {
        "tickers": syms,
        "mode": mode_label,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "n_prices": n_prices,
        "n_broker": n_broker,
        "n_activity": n_activity,
        "elapsed_seconds": round(elapsed, 1),
     }

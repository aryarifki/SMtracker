#!/usr/bin/env python3
"""ETL Script: Hitung Analitik Foreign Flow (HMM, VAR, Heatmap, Network) 
dan simpan ke tabel analytics_foreign_flow untuk Re-tracker."""

from __future__ import annotations

import argparse
import sys
import time
import json
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import text
from psycopg2.extras import execute_values

# ── path setup ─────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from idx_bandarmology import storage, universe as universe_mod
from idx_bandarmology.foreign_analytics import (
    compute_hmm_regime,
    compute_var_irf,
    compute_foreign_hhi,
    compute_broker_heatmap,
    compute_broker_network
)
import numpy as np

def upsert_foreign_analytics(ticker: str, lookback_days: int, payload: dict) -> None:
    """UPSERT hasil komputasi ke tabel analytics_foreign_flow."""
    raw_conn = storage._get_raw_conn()
    try:
        with raw_conn.cursor() as cur:
            row = (
                ticker,
                lookback_days,
                payload["latest_date"],
                json.dumps(payload["features"]),
                json.dumps(payload["timeseries"]),
                json.dumps(payload["models"]),
                payload["company"]["name"],
                payload["company"]["group"],
            )
            
            execute_values(
                cur,
                """
                INSERT INTO analytics_foreign_flow (
                    ticker, lookback_days, latest_date, features, timeseries, 
                    models, company_name, group_name
                )
                VALUES %s
                ON CONFLICT (ticker, lookback_days) DO UPDATE SET
                    latest_date = EXCLUDED.latest_date,
                    features = EXCLUDED.features,
                    timeseries = EXCLUDED.timeseries,
                    models = EXCLUDED.models,
                    company_name = EXCLUDED.company_name,
                    group_name = EXCLUDED.group_name,
                    updated_at = NOW()
                """,
                [row],
                page_size=100,
            )
        raw_conn.commit()
    finally:
        raw_conn.close()

def process_ticker(ticker: str, lookback_days: int):
    print(f"   [ETL] Menghitung {ticker} ({lookback_days}D)... ", end="", flush=True)
    t0 = time.monotonic()
    
    q_date = text("SELECT MAX(date) FROM broker_flow WHERE ticker = :ticker")
    with storage.engine.connect() as conn:
        latest_date_row = conn.execute(q_date, {"ticker": ticker}).fetchone()
    
    if not latest_date_row or not latest_date_row[0]:
        print("SKIP (No broker data)")
        return
        
    end_date = latest_date_row[0]
    start_date = end_date - timedelta(days=lookback_days * 2)
    
    df_macro = storage.read_broker_flow(tickers=[ticker], start_date=start_date, end_date=end_date)
    if len(df_macro) < 20:
        print(f"SKIP (Data < 20 days: {len(df_macro)} rows)")
        return
        
    df_macro = df_macro.sort_values("date").tail(lookback_days)
    
    dates = [str(d.date()) for d in df_macro["date"]]
    foreign_net = [float(x or 0) for x in df_macro["foreign_net_broker"]]
    
    closes = []
    last_close = 0.0
    df_prices = storage.read_prices(tickers=[ticker], start_date=start_date, end_date=end_date)
    price_map = {str(d.date()): float(c) for d, c in zip(df_prices["date"], df_prices["close"]) if c is not None}
    
    for d in dates:
        c = price_map.get(d)
        if c and c > 0:
            last_close = c
        closes.append(last_close)
        
    returns = [0.0]
    for i in range(1, len(closes)):
        prev = closes[i-1]
        returns.append(float((closes[i] - prev) / prev) if prev else 0.0)

    df_micro = storage.read_broker_activity(tickers=[ticker], start_date=start_date, end_date=end_date)
    df_micro = df_micro[df_micro["participant_type"] == "Asing"]
    
    micro_rows = [
        {"date": str(d.date()), "broker_code": b, "net_value": float(n or 0)}
        for d, b, n in zip(df_micro["date"], df_micro["broker_code"], df_micro["net_value"])
    ]
    
    broker_net_totals = {}
    for r in micro_rows:
        broker_net_totals[r["broker_code"]] = broker_net_totals.get(r["broker_code"], 0.0) + r["net_value"]
    hhi_score = compute_foreign_hhi(list(broker_net_totals.values()))

    q_emiten = text("SELECT name, sector FROM tickers WHERE ticker = :ticker")
    with storage.engine.connect() as conn:
        emiten_row = conn.execute(q_emiten, {"ticker": ticker}).fetchone()
    company_name = emiten_row.name if emiten_row else "Perusahaan Tidak Diketahui"
    group_name = emiten_row.sector if emiten_row else "Independen / Belum Terpetakan"

    regime_data = compute_hmm_regime(foreign_net, n_states=3)
    var_irf_data = compute_var_irf(foreign_net, returns, lags=2, horizon=10)
    heatmap_data = compute_broker_heatmap(micro_rows, top_n=15)
    network_data = compute_broker_network(micro_rows, lookback_days, min_active_ratio=0.2, corr_threshold=0.65)
    
    f_arr = np.array(foreign_net)
    std_val = float(np.std(f_arr))
    zscore = float((f_arr[-1] - np.mean(f_arr)) / std_val) if std_val > 0 else 0.0

    payload = {
        "ticker": ticker,
        "company": {"name": company_name, "group": group_name},
        "lookback_days": lookback_days,
        "latest_date": dates[-1],
        "features": {
            "foreign_hhi": round(hhi_score, 4),
            "foreign_zscore": round(zscore, 2),
        },
        "timeseries": {
            "dates": dates,
            "foreign_net": foreign_net,
            "close_prices": closes,
            "hmm_states": [int(x) for x in regime_data.get("states", [])]
        },
        "models": {
            "regime_probabilities": [[float(p) for p in row] for row in regime_data.get("probabilities", [])],
            "impulse_response": var_irf_data,
            "broker_heatmap": heatmap_data,
            "broker_network": network_data
        }
    }
    
    upsert_foreign_analytics(ticker, lookback_days, payload)
    
    elapsed = time.monotonic() - t0
    print(f"DONE ({elapsed:.2f}s)")

def main():
    parser = argparse.ArgumentParser(description="ETL Foreign Flow Analytics")
    parser.add_argument("--universe", default="watchlist", help="Universe ticker (misal: idx80, watchlist, all)")
    parser.add_argument("--window", type=int, nargs="+", default=[20, 60], help="Lookback window(s). Default: 20 60")
    
    args = parser.parse_args()
    
    print(f"🚀 Memulai ETL Foreign Flow Analytics")
    print(f"   Universe : {args.universe}")
    print(f"   Windows  : {args.window}")
    
    tickers = universe_mod.get_universe(args.universe)
    print(f"   Total Tickers: {len(tickers)}\n")
    
    for ticker in tickers:
        for lookback in args.window:
            try:
                process_ticker(ticker.upper().strip(), lookback)
            except Exception as e:
                print(f"ERROR ({e})")
                
    print("\n✅ ETL Selesai. Data siap dikonsumsi Re-tracker.")

if __name__ == "__main__":
    main()

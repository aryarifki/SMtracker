"""yfinance client — fast concurrent OHLCV history fetcher for IDX tickers.

IDX tickers on Yahoo Finance need a ``.JK`` suffix (e.g. ``BBCA.JK``). This
module hides that detail: pass plain tickers like ``"BBCA"`` everywhere else
in the repo.
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf


def _yf_ticker(ticker: str) -> str:
    t = ticker.upper().strip()
    return t if t.endswith(".JK") else f"{t}.JK"


def fetch_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Daily OHLCV for one ticker.

    Returns a tidy DataFrame with columns:
    ``date, ticker, open, high, low, close, volume``
    """
    cols = ["date", "ticker", "open", "high", "low", "close", "volume"]
    sym = ticker.upper().strip()
    try:
        df = yf.download(
            _yf_ticker(sym),
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
        )
    except Exception:
        return pd.DataFrame(columns=cols)

    if df is None or df.empty:
        return pd.DataFrame(columns=cols)

    # yfinance sometimes returns a MultiIndex column (Ticker level) even for
    # a single symbol — flatten it.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.reset_index()
    df = df.rename(columns={
        "Date": "date", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "volume",
    })
    df["ticker"] = sym
    df["date"] = pd.to_datetime(df["date"]).dt.date
    
    # Pastikan hanya kolom yang diperlukan yang dikembalikan
    return df[cols]


def fetch_history_many(
    tickers: list[str],
    period: str = "1y",
    interval: str = "1d",
    batch_size: int = 50, # Mempertahankan micro-batching agar RAM aman
) -> int:
    """Fetch multiple tickers concurrently, save to DB per batch, and return row count."""
    from . import storage
    
    if not tickers:
        return 0
        
    total_upserted = 0
    total_batches = (len(tickers) + batch_size - 1) // batch_size
    
    # ── MICRO-BATCHING UNTUK HINDARI OOM ──
    for i in range(0, len(tickers), batch_size):
        batch_num = i // batch_size + 1
        batch_tickers = tickers[i : i + batch_size]
        
        print(f"[prices] 🔄 Mengambil batch harga yfinance {batch_num}/{total_batches} ({len(batch_tickers)} saham)...")
        
        results: list[pd.DataFrame] = []
        for t in batch_tickers:
            df = fetch_history(t, period=period, interval=interval)
            if not df.empty:
                results.append(df)
                    
        if not results:
            continue
            
        final_df = pd.concat(results, ignore_index=True)
        
        # Simpan ke database per batch
        n_upserted = storage.upsert_prices(final_df)
        total_upserted += n_upserted
        
        print(f"[prices] ✅ Batch {batch_num} tersimpan! Total baris harga: {total_upserted}")
        
        # Kosongkan memori
        del results
        del final_df
        
    return total_upserted

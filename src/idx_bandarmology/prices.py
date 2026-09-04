"""yfinance client — daily OHLCV history for IDX tickers.

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
    (empty DataFrame with these columns if the download fails or is empty —
    callers don't need to special-case errors).
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
    return df[cols]


def fetch_history_many(tickers: list[str], period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Daily OHLCV for several tickers, concatenated into one tidy table."""
    frames = [fetch_history(t, period=period, interval=interval) for t in tickers]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["date", "ticker", "open", "high", "low", "close", "volume"])
    return pd.concat(frames, ignore_index=True)

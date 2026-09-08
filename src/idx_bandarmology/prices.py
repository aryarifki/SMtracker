"""IDX API client — fast concurrent OHLCV history fetcher.

This module fetches historical data directly from IDX endpoints using 
connection pooling and thread-safe session reuse.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_SESSION_LOCK = threading.Lock()
_SHARED_SESSION: requests.Session | None = None


def _get_idx_session() -> requests.Session:
    """Reuses a singleton Session with connection pooling and auto-retries."""
    global _SHARED_SESSION
    with _SESSION_LOCK:
        if _SHARED_SESSION is not None:
            return _SHARED_SESSION

        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.headers.update({
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9,id;q=0.8',
            'Referer': 'https://www.idx.co.id/',
            'X-Requested-With': 'XMLHttpRequest',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        })
        try:
            session.get("https://www.idx.co.id/id", timeout=10.0)
            session.get("https://www.idx.co.id/primary/home/GetIndexList", timeout=10.0)
        except Exception as e:
            print(f"[prices] Session warmup notice: {e}")
        
        _SHARED_SESSION = session
        return _SHARED_SESSION


def fetch_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Daily OHLCV for one ticker fetched directly from IDX."""
    cols = ["date", "ticker", "open", "high", "low", "close", "volume"]
    sym = ticker.upper().strip().replace(".JK", "")
    session = _get_idx_session()
    url = f"https://www.idx.co.id/primary/ListedCompany/GetTradingInfoSS?code={sym}&start=0&length=1000"
    
    try:
        resp = session.get(url, timeout=12.0)
        resp.raise_for_status()
        data = resp.json()
        
        rows = []
        for item in data.get("replies", []):
            rows.append({
                "date": pd.to_datetime(item.get("Date")).date(),
                "ticker": sym,
                "open": float(item.get("OpenPrice", 0)),
                "high": float(item.get("High", 0)),
                "low": float(item.get("Low", 0)),
                "close": float(item.get("Close", 0)),
                "volume": int(item.get("Volume", 0)),
            })
            
        if rows:
            df = pd.DataFrame(rows)[cols]
            return df.sort_values("date").reset_index(drop=True)
            
    except Exception as exc:
        print(f"[prices] API IDX failed for {sym}: {type(exc).__name__}")
        
    return pd.DataFrame(columns=cols)


def fetch_history_many(
    tickers: list[str],
    period: str = "1y",
    interval: str = "1d",
    max_workers: int = 6,
) -> pd.DataFrame:
    """Fetch multiple tickers concurrently using ThreadPoolExecutor."""
    cols = ["date", "ticker", "open", "high", "low", "close", "volume"]
    if not tickers:
        return pd.DataFrame(columns=cols)
    
    results: list[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_history, t, period, interval): t for t in tickers}
        for future in as_completed(futures):
            try:
                df = future.result()
                if not df.empty:
                    results.append(df)
            except Exception as e:
                t = futures[future]
                print(f"[prices] Error fetching {t}: {e}")
                
    if not results:
        return pd.DataFrame(columns=cols)
    return pd.concat(results, ignore_index=True)

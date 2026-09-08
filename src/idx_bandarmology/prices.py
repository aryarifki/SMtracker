"""IDX API client — fast concurrent OHLCV history fetcher with WAF bypass.

This module fetches historical data directly from IDX endpoints using 
connection pooling, session warming (anti-WAF), and thread-safe session reuse.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_SESSION_LOCK = threading.Lock()
_SHARED_SESSION: requests.Session | None = None
_SESSION_READY = False


def _get_idx_session() -> requests.Session:
    """Reuses a singleton Session with connection pooling and WAF bypass."""
    global _SHARED_SESSION, _SESSION_READY
    with _SESSION_LOCK:
        if _SHARED_SESSION is not None and _SESSION_READY:
            return _SHARED_SESSION

        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=retry_strategy)
        session.mount("https://", adapter)
        
        # Base headers sesuai referensi idx_api_wrapper.py
        session.headers.update({
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9,id;q=0.8',
            'Referer': 'https://www.idx.co.id/',
            'Upgrade-Insecure-Requests': '1',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36'
        })
        
        # ── SESSION WARMING (Bypass WAF) ──
        try:
            # 1. Akses halaman utama untuk mendapatkan cookie WAF
            session.get("https://www.idx.co.id/id", timeout=15.0)
            time.sleep(1)
            
            # 2. Tambahkan header X-Requested-With setelah dapat cookie
            session.headers.update({
                'X-Requested-With': 'XMLHttpRequest'
            })
            
            # 3. Akses endpoint GetIndexList untuk "menyehatkan" sesi
            session.get("https://www.idx.co.id/primary/home/GetIndexList", timeout=15.0)
            time.sleep(1)
            
            _SESSION_READY = True
        except Exception as e:
            print(f"[prices] Session warmup notice: {e}")
        
        _SHARED_SESSION = session
        return _SHARED_SESSION


def fetch_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Daily OHLCV for one ticker fetched directly from IDX."""
    cols = ["date", "ticker", "open", "high", "low", "close", "volume"]
    sym = ticker.upper().strip().replace(".JK", "")
    session = _get_idx_session()
    
    max_retries = 3
    url = f"https://www.idx.co.id/primary/ListedCompany/GetTradingInfoSS?code={sym}&start=0&length=1000"
    
    for attempt in range(max_retries):
        try:
            resp = session.get(url, timeout=15.0)
            if resp.status_code != 200:
                print(f"[prices] HTTP {resp.status_code} on {sym} (attempt {attempt+1})")
                if attempt == max_retries - 1:
                    return pd.DataFrame(columns=cols)
                time.sleep(min(1000 * (2 ** attempt) / 1000, 15))
                continue
                
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
            else:
                return pd.DataFrame(columns=cols)
                
        except Exception as exc:
            print(f"[prices] API IDX failed for {sym} (attempt {attempt+1}): {type(exc).__name__}: {exc}")
            if attempt >= max_retries - 1:
                return pd.DataFrame(columns=cols)
            # Exponential backoff
            time.sleep(min(1000 * (2 ** attempt) / 1000, 15))
            
    return pd.DataFrame(columns=cols)


def fetch_history_many(
    tickers: list[str],
    period: str = "1y",
    interval: str = "1d",
    max_workers: int = 6,
    batch_size: int = 50,  # <--- TAMBAHKAN INI
) -> int:
    """Fetch multiple tickers concurrently, save to DB per batch, and return row count."""
    from . import storage
    
    cols = ["date", "ticker", "open", "high", "low", "close", "volume"]
    if not tickers:
        return 0
        
    total_upserted = 0
    
    # ── MICRO-BATCHING UNTUK HINDARI OOM ──
    for i in range(0, len(tickers), batch_size):
        batch_tickers = tickers[i : i + batch_size]
        results: list[pd.DataFrame] = []
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(fetch_history, t, period, interval): t for t in batch_tickers}
            for future in as_completed(futures):
                try:
                    df = future.result()
                    if not df.empty:
                        results.append(df)
                except Exception as e:
                    t = futures[future]
                    print(f"[prices] Error fetching {t}: {e}")
                    
        if not results:
            continue
            
        final_df = pd.concat(results, ignore_index=True)
        
        # Simpan ke database per batch
        n_upserted = storage.upsert_prices(final_df)
        total_upserted += n_upserted
        
        # Kosongkan memori
        del results
        del final_df
        
    return total_upserted
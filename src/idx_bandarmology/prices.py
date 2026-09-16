"""IDX API client — fast concurrent OHLCV history fetcher with WAF bypass & robust error handling."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from curl_cffi import requests as cffi_requests  # Menggunakan curl_cffi anti-WAF

# Headers standar untuk meniru browser
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Referer": "https://www.idx.co.id/",
}


def fetch_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Daily OHLCV for one ticker fetched directly from IDX."""
    cols = ["date", "ticker", "open", "high", "low", "close", "volume"]
    sym = ticker.upper().strip().replace(".JK", "")
    
    max_retries = 3
    url = f"https://www.idx.co.id/primary/ListedCompany/GetTradingInfoSS?code={sym}&start=0&length=1000"
    
    for attempt in range(max_retries):
        try:
            # Kunci sukses: impersonate="chrome" agar WAF IDX mengira ini browser asli
            resp = cffi_requests.get(url, headers=_HEADERS, impersonate="chrome", timeout=15.0)
            
            if resp.status_code != 200:
                print(f"[prices] HTTP {resp.status_code} on {sym} (attempt {attempt+1})")
                if attempt == max_retries - 1:
                    return pd.DataFrame(columns=cols)
                time.sleep(min(1000 * (2 ** attempt) / 1000, 15))
                continue
                
            # Cek apakah response benar-benar JSON
            try:
                data = resp.json()
            except ValueError:
                # Server mengembalikan HTML (misal saat maintenance), bukan JSON
                print(f"[prices] Response bukan JSON untuk {sym} (kemungkinan server maintenance).")
                return pd.DataFrame(columns=cols)
            
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
            time.sleep(min(1000 * (2 ** attempt) / 1000, 15))
            
    return pd.DataFrame(columns=cols)


def fetch_history_many(
    tickers: list[str],
    period: str = "1y",
    interval: str = "1d",
    max_workers: int = 3,       # Diturunkan jadi 3 agar tidak kena ban DDoS
    batch_size: int = 50,
) -> int:
    """Fetch multiple tickers concurrently, save to DB per batch, and return row count."""
    from . import storage
    
    cols = ["date", "ticker", "open", "high", "low", "close", "volume"]
    if not tickers:
        return 0
        
    total_upserted = 0
    total_batches = (len(tickers) + batch_size - 1) // batch_size
    
    # ── MICRO-BATCHING UNTUK HINDARI OOM ──
    for i in range(0, len(tickers), batch_size):
        batch_num = i // batch_size + 1
        batch_tickers = tickers[i : i + batch_size]
        
        print(f"[prices] 🔄 Mengambil batch harga {batch_num}/{total_batches} ({len(batch_tickers)} saham)...")
        
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
        
        print(f"[prices] ✅ Batch {batch_num} tersimpan! Total baris harga: {total_upserted}")
        
        # Kosongkan memori
        del results
        del final_df
        
    return total_upserted

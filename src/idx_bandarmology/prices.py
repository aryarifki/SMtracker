"""IDX API client — daily OHLCV history for IDX tickers.

This module replaces yfinance and fetches historical data directly
from the IDX endpoints using session cookies to bypass blocks.
"""

from __future__ import annotations

import random
import time
import pandas as pd
import requests

def _get_idx_session() -> requests.Session:
    """Mengadopsi logika ensureSession() dari BaseClient idx-api untuk menembus WAF."""
    session = requests.Session()
    
    session.headers.update({
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9,id;q=0.8',
        'Referer': 'https://www.idx.co.id/',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36'
    })
    
    try:
        session.get("https://www.idx.co.id/id", timeout=15.0)
        time.sleep(1.0)
        session.headers.update({'X-Requested-With': 'XMLHttpRequest'})
        session.get("https://www.idx.co.id/primary/home/GetIndexList", timeout=15.0)
    except Exception as e:
        print(f"[prices] Gagal menginisialisasi sesi IDX: {e}")
        
    return session

_SESSION = None

def _ensure_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _get_idx_session()
    return _SESSION


def fetch_history(ticker: str, period: str = "1y", interval: str = "1d") -> tuple[pd.DataFrame, int]:
    """Daily OHLCV for one ticker fetched directly from IDX."""
    global _SESSION
    cols = ["date", "ticker", "open", "high", "low", "close", "volume"]
    sym = ticker.upper().strip()
    
    session = _ensure_session()
    url = f"https://www.idx.co.id/primary/ListedCompany/GetTradingInfoSS?code={sym}&start=0&length=1000"
    
    last_status = 200
    max_retries = 5 
    
    for attempt in range(max_retries):
        try:
            resp = session.get(url, timeout=15.0)
            last_status = resp.status_code
            
            if not resp.ok:
                raise requests.exceptions.HTTPError(f"Server returned {last_status}")
                
            data = resp.json()
            rows = []
            
            for item in data.get("replies", []) or []:
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
                return df.sort_values("date").reset_index(drop=True), 200
                
            return pd.DataFrame(columns=cols), 200
            
        except Exception as exc:
            last_status = 403 if "403" in str(exc) else (429 if "429" in str(exc) else 500)
            
            if attempt >= max_retries - 1:
                print(f"[prices] API IDX gagal mutlak untuk {sym} setelah {max_retries} kali percobaan.")
                break
            
            delay_sec = min(1.0 * (2 ** attempt), 15.0)
            print(f"[prices] {sym} tertahan (Status {last_status}). Retrying in {delay_sec}s (Attempt {attempt+1}/{max_retries})...")
            time.sleep(delay_sec)
            
            if last_status == 403:
                session = _get_idx_session()
                _SESSION = session
                
    return pd.DataFrame(columns=cols), last_status


def fetch_history_many(tickers: list[str], period: str = "1y", interval: str = "1d") -> int:
    """Daily OHLCV for several tickers, micro-batched and committed to DB.
    
    Returns the total number of rows upserted.
    """
    from . import storage
    from sqlalchemy import text
    
    # ── INCREMENTAL SKIP LOGIC ──
    print("[prices] Menganalisis database untuk melewati saham yang sudah ditarik harganya HARI INI...")
    try:
        # Pengecekan cerdas: Hanya skip jika data saham untuk TANGGAL HARI INI sudah tersimpan di DB
        q = text("""
            SELECT DISTINCT ticker 
            FROM prices 
            WHERE date = CURRENT_DATE 
            AND ticker = ANY(:t)
        """)
        with storage.engine.connect() as conn:
            df_exist = pd.read_sql(q, conn, params={"t": tickers})
        done_set = set(df_exist['ticker'])
    except Exception as e:
        print(f"[prices] Gagal mengecek database: {e}")
        done_set = set()
        
    target_tickers = [t for t in tickers if t not in done_set]
    total = len(target_tickers)
    skipped_count = len(tickers) - total
    
    if skipped_count > 0:
        print(f"[prices] ⏭️ Skipped {skipped_count} tickers (sudah ada di database).")
        
    if total == 0:
        print("[prices] Semua harga saham sudah lengkap di database.")
        return 0
        
    frames = []
    consecutive_403 = 0
    total_upserted = 0
    BATCH_SIZE = 300
    
    for i, t in enumerate(target_tickers):
        if consecutive_403 >= 3:
            print("\n[prices] 🚨 SIRKUIT BREAKER AKTIF: IP Anda diblokir permanen oleh Firewall IDX (Status 403).")
            print("[prices] ⏭️ Menghentikan penarikan harga saham...\n")
            break
            
        df, status = fetch_history(t, period=period, interval=interval)
        
        if status == 403:
            consecutive_403 += 1
        else:
            consecutive_403 = 0
            
        if not df.empty:
            frames.append(df)
            
        is_last_item = (i == total - 1)
        
        # ── MICRO-BATCHING DB COMMIT ──
        if len(frames) > 0 and ((i + 1) % BATCH_SIZE == 0 or is_last_item):
            batch_df = pd.concat(frames, ignore_index=True)
            saved = storage.upsert_prices(batch_df)
            total_upserted += saved
            print(f"[prices] 🔄 Batch saved! {len(frames)} tickers committed. Cumulative price rows saved: {total_upserted}")
            frames = [] # Kosongkan memori untuk batch berikutnya
            
        # ── LOGIKA JEDA (ANTI-BLOCK/STEALTH MODE) ──
        if not is_last_item and status != 403:
            time.sleep(random.uniform(0.3, 1.2))
            if (i + 1) % 50 == 0:
                print(f"[prices] Progress Harga IDX: {i+1}/{total} saham diproses. Beristirahat sejenak...")
                time.sleep(random.uniform(3.0, 6.0))
                
    return total_upserted

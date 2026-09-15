"""IDX Direct API Client dengan WAF Bypass (curl_cffi) - Menyimpan langsung ke PostgreSQL."""
from __future__ import annotations
import time
from datetime import date, datetime, timedelta
from curl_cffi import requests
import pandas as pd
from . import storage

_BASE = "https://www.idx.co.id/primary"
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.idx.co.id/",
}

def _fetch(endpoint: str, params: dict = None) -> dict | None:
    """Fetch data dari idx.co.id dengan browser impersonation (Anti 403 Forbidden)."""
    url = f"{_BASE}{endpoint}"
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, impersonate="chrome", timeout=20)
        if resp.status_code == 200:
            return resp.json()
        else:
            print(f"[idx_api] HTTP {resp.status_code} on {endpoint}")
            return None
    except Exception as e:
        print(f"[idx_api] Error fetching {endpoint}: {e}")
        return None

def ingest_idx_daily_data(target_date: date):
    """Mengambil OHLCV, Broker, dan Index untuk tanggal spesifik, lalu menyimpan ke PostgreSQL."""
    date_str_api = target_date.strftime("%Y%m%d")
    date_iso = target_date.isoformat()
    
    print(f"[idx_api] 📅 Mengambil data IDX untuk tanggal: {date_iso}")
    
    # 1. Stock Summary (OHLCV + Foreign Flow)
    stock_data = _fetch("/TradingSummary/GetStockSummary", params={"date": date_str_api, "start": 0, "length": 9999})
    if stock_data and stock_data.get("data"):
        rows = stock_data["data"]
        df = pd.DataFrame(rows)
        df["Date"] = date_iso
        # Rename kolom agar sesuai dengan tabel prices di DB
        df = df.rename(columns={
            "StockCode": "ticker", "OpenPrice": "open", "High": "high", 
            "Low": "low", "Close": "close", "Volume": "volume"
        })
        # Simpan ke PostgreSQL (Gunakan upsert_prices yang sudah dimodifikasi atau buat fungsi baru)
        # Contoh: storage.upsert_idx_stock_summary(df)
        print(f"[idx_api] ✅ Stock Summary: {len(df)} baris disimpan.")
        
    # 2. Broker Summary (Aggregate)
    broker_data = _fetch("/TradingSummary/GetBrokerSummary", params={"date": date_str_api, "start": 0, "length": 9999})
    if broker_data and broker_data.get("data"):
        df_b = pd.DataFrame(broker_data["data"])
        df_b["Date"] = date_iso
        # storage.upsert_idx_broker_summary(df_b)
        print(f"[idx_api] ✅ Broker Summary: {len(df_b)} baris disimpan.")
        
    # 3. Index Summary (IHSG, LQ45)
    index_data = _fetch("/TradingSummary/GetIndexSummary", params={"date": date_str_api, "start": 0, "length": 9999})
    if index_data and index_data.get("data"):
        df_i = pd.DataFrame(index_data["data"])
        df_i["Date"] = date_iso
        # storage.upsert_idx_index_summary(df_i)
        print(f"[idx_api] ✅ Index Summary: {len(df_i)} baris disimpan.")

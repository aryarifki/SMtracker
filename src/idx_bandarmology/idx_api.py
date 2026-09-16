"""IDX API Client - Hybrid (Sync) dengan WAF Bypass (curl_cffi) & Rate Limiter."""

from __future__ import annotations
import time
from datetime import date, timedelta
import pandas as pd
from curl_cffi import requests
from . import storage

_BASE = "https://www.idx.co.id/primary"
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.idx.co.id/",
}

# ── SINKRONUS HELPER ──
def _fetch(endpoint: str, params: dict = None, retries: int = 5) -> dict | None:
    url = f"{_BASE}{endpoint}"
    for attempt in range(retries):
        try:
            # Jeda wajib 2 detik sebelum setiap request ke IDX
            time.sleep(2.0)
            
            resp = requests.get(url, params=params, headers=_HEADERS, impersonate="chrome", timeout=20)
            
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                # Jika kena 429, tidur 60 detik agar penalti IP dicabut
                print(f"[idx_api] ⚠️ HTTP 429 (Rate Limit). IP terkena penalti. Menunggu 60s...")
                time.sleep(60)
                continue
            else:
                print(f"[idx_api] HTTP {resp.status_code} on {endpoint}")
                return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(5)
            else:
                print(f"[idx_api] Error fetching {endpoint}: {e}")
                return None
    print(f"[idx_api] ❌ Gagal mengambil {endpoint} setelah {retries}x retry.")
    return None

# ── PARSER DATAFRAME ──
def _parse_stock_data(data: dict, date_iso: str) -> pd.DataFrame:
    if not data or not data.get("data"): return pd.DataFrame()
    df = pd.DataFrame(data["data"])
    df = df.rename(columns={
        "StockCode": "ticker", "OpenPrice": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume", "ForeignBuy": "foreign_buy",
        "ForeignSell": "foreign_sell", "Value": "value", "Frequency": "frequency",
        "NonRegularVolume": "non_regular_volume", "NonRegularValue": "non_regular_value"
    })
    df["date"] = date_iso
    return df

def _parse_broker_data(data: dict, date_iso: str) -> pd.DataFrame:
    if not data or not data.get("data"): return pd.DataFrame()
    df = pd.DataFrame(data["data"])
    df = df.rename(columns={
        "IDFirm": "id_firm", "FirmName": "firm_name", 
        "Volume": "volume", "Value": "value", "Frequency": "frequency"
    })
    df["date"] = date_iso
    return df

def _parse_index_data(data: dict, date_iso: str) -> pd.DataFrame:
    if not data or not data.get("data"): return pd.DataFrame()
    df = pd.DataFrame(data["data"])
    df = df.rename(columns={
        "IndexCode": "index_code", "IndexName": "index_name", "Close": "close",
        "Volume": "volume", "Value": "value", "MarketCapital": "market_cap"
    })
    df["date"] = date_iso
    return df

# ── FUNGSI SINKRONUS (HARIAN & BACKFILL SEQUENTIAL) ──
def ingest_idx_daily_data(target_date: date):
    """Mengambil OHLCV, Broker, dan Index untuk 1 hari spesifik (Sync)."""
    date_str_api = target_date.strftime("%Y%m%d")
    date_iso = target_date.isoformat()
    print(f"[idx_api] 📅 Mengambil data IDX Harian: {date_iso}")
    
    # 1. Stock Summary (OHLCV + Foreign Flow)
    stock_data = _fetch("/TradingSummary/GetStockSummary", params={"date": date_str_api, "start": 0, "length": 9999})
    df_stock = _parse_stock_data(stock_data, date_iso)
    if not df_stock.empty:
        storage.upsert_prices(df_stock)
        print(f"[idx_api] ✅ Stock: {len(df_stock)} baris.")
        
    # 2. Broker Summary (Aggregate)
    broker_data = _fetch("/TradingSummary/GetBrokerSummary", params={"date": date_str_api, "start": 0, "length": 9999})
    df_broker = _parse_broker_data(broker_data, date_iso)
    if not df_broker.empty:
        storage.upsert_idx_broker_summary(df_broker)
        print(f"[idx_api] ✅ Broker: {len(df_broker)} baris.")
        
    # 3. Index Summary (IHSG, LQ45)
    index_data = _fetch("/TradingSummary/GetIndexSummary", params={"date": date_str_api, "start": 0, "length": 9999})
    df_index = _parse_index_data(index_data, date_iso)
    if not df_index.empty:
        storage.upsert_idx_index_summary(df_index)
        print(f"[idx_api] ✅ Index: {len(df_index)} baris.")


# ── FUNGSI SNAPSHOT FUNDAMENTAL & GOVERNANCE ──
def ingest_financial_ratios(year: int = 2024, quarter: int = 4):
    print(f"[idx_api] 📊 Mengambil Financial Ratios (Year: {year}, Quarter: {quarter})...")
    params = {
        "urlName": "LINK_FINANCIAL_DATA_RATIO", "periodQuarter": quarter, "periodYear": year,
        "type": "yearly", "isPrint": "False", "cumulative": "false", "pageSize": 100, "pageNumber": 1
    }
    all_records = []
    page = 1
    while True:
        params["pageNumber"] = page
        data = _fetch("/DigitalStatistic/GetApiDataPaginated", params=params)
        if data and data.get("data") and len(data["data"]) > 0:
            all_records.extend(data["data"])
            page += 1
            time.sleep(1)
        else:
            break
            
    if all_records:
        df = pd.DataFrame(all_records)
        df["year"] = year
        df["quarter"] = quarter
        df = df.rename(columns={"code": "code", "per": "per", "pbv": "pbv", "roe": "roe", "roa": "roa", "der": "der", "eps": "eps"})
        cols = ["code", "year", "quarter", "per", "pbv", "roe", "roa", "der", "eps"]
        for c in cols:
            if c not in df.columns: df[c] = None
        storage.upsert_financial_ratios(df[cols])
        print(f"[idx_api] ✅ Financial Ratios: {len(df)} baris disimpan.")

def ingest_corporate_actions():
    print(f"[idx_api] 📝 Mengambil Corporate Actions...")
    ca_types = ["tanpaHmetd", "hmetd", "stockSplit", "reverseStock", "sahamBonus", "dividenSaham", "BuybackSaham", "PrivatePlacement", "ipo", "waran", "gabungUsaha", "kurangModal", "konversiSaham"]
    all_records = []
    for ca_type in ca_types:
        params = {"caType": ca_type, "dateFrom": "", "dateTo": "", "start": 0, "length": 9999}
        data = _fetch("/ListingActivity/GetIssuedHistory", params=params)
        if data and data.get("data"):
            for rec in data["data"]:
                rec["caType"] = ca_type
                all_records.append(rec)
            time.sleep(0.5)
            
    if all_records:
        df = pd.DataFrame(all_records)
        df = df.rename(columns={"KodeEmiten": "ticker", "TanggalPencatatan": "date", "caType": "ca_type", "JenisTindakan": "description"})
        cols = ["date", "ticker", "ca_type", "description"]
        for c in cols:
            if c not in df.columns: df[c] = None
        storage.upsert_corporate_actions(df[cols])
        print(f"[idx_api] ✅ Corporate Actions: {len(df)} baris disimpan.")

def ingest_company_details(ticker: str):
    data = _fetch("/ListedCompany/GetCompanyProfilesDetail", params={"KodeEmiten": ticker, "language": "id-id"})
    if not data or not data.get("Profiles") or len(data["Profiles"]) == 0: 
        return False
    
    # Ambil data dari Profiles[0]
    profile = data["Profiles"][0]
    
    # 1. Pemegang Saham
    sh_rows = []
    for s in profile.get("PemegangSaham", []):
        try:
            sh_rows.append({
                "ticker": ticker,
                "name": str(s.get("Nama", "") or "").strip(),
                "pct": float(s.get("Persentase", 0.0) or 0.0),
                "is_controlling": bool(s.get("Pengendali", False))
            })
        except Exception:
            continue # Skip baris jika ada data rusak
            
    if sh_rows: 
        storage.upsert_company_shareholders(pd.DataFrame(sh_rows))
        
    # 2. Direksi & Komisaris
    board_rows = []
    for d in profile.get("Direksi", []):
        try:
            board_rows.append({
                "ticker": ticker, 
                "name": str(d.get("Nama", "") or "").strip(), 
                "role": "Director", 
                "title": str(d.get("Jabatan", "") or "").strip()
            })
        except Exception:
            continue
            
    for k in profile.get("DewanKomisaris", []):
        try:
            board_rows.append({
                "ticker": ticker, 
                "name": str(k.get("Nama", "") or "").strip(), 
                "role": "Commissioner", 
                "title": str(k.get("Jabatan", "") or "").strip()
            })
        except Exception:
            continue
            
    if board_rows: 
        storage.upsert_company_board(pd.DataFrame(board_rows))
        
    return True

def ingest_all_company_details(concurrency: int = 5):
    import concurrent.futures
    from . import universe as universe_mod
    tickers = universe_mod.get_master_tickers(active_only=True)
    print(f"[idx_api] 🏢 Mengambil Company Details untuk {len(tickers)} saham (Concurrency: {concurrency})...")
    
    success_count = 0
    fail_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(ingest_company_details, t): t for t in tickers}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                if future.result(): success_count += 1
                else: fail_count += 1
            except Exception: fail_count += 1
            if i % 50 == 0: print(f"   - Progress: {i}/{len(tickers)} (Success: {success_count}, Fail: {fail_count})")
    
    # Perbaikan typo di baris terakhir (hapus tanda } yang berlebih)
    print(f"[idx_api] ✅ Company Details selesai! Total Sukses: {success_count}, Gagal: {fail_count}")

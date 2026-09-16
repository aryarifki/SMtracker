"""IDX Direct API Client dengan WAF Bypass (curl_cffi) - Menyimpan langsung ke PostgreSQL."""

from __future__ import annotations
import time
from datetime import date, datetime, timedelta
import pandas as pd
from curl_cffi import requests
from . import storage

_BASE = "https://www.idx.co.id/primary"
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.idx.co.id/",
}

def _fetch(endpoint: str, params: dict = None, retries: int = 3) -> dict | None:
    """Fetch data dari idx.co.id dengan browser impersonation (Anti 403 Forbidden)."""
    url = f"{_BASE}{endpoint}"
    
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=_HEADERS, impersonate="chrome", timeout=20)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code in (429, 500, 502, 503, 504):
                print(f"[idx_api] HTTP {resp.status_code} on {endpoint}. Retrying in {2**attempt}s...")
                time.sleep(2 ** attempt)
            else:
                print(f"[idx_api] HTTP {resp.status_code} on {endpoint}")
                return None
        except Exception as e:
            print(f"[idx_api] Error fetching {endpoint} (attempt {attempt+1}): {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            return None
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
        
        # Rename kolom agar sesuai dengan tabel prices di DB
        df = df.rename(columns={
            "StockCode": "ticker", 
            "OpenPrice": "open", 
            "High": "high", 
            "Low": "low", 
            "Close": "close", 
            "Volume": "volume",
            "ForeignBuy": "foreign_buy",
            "ForeignSell": "foreign_sell",
            "Value": "value",
            "Frequency": "frequency",
            "NonRegularVolume": "non_regular_volume",
            "NonRegularValue": "non_regular_value"
        })
        df["date"] = date_iso
        
        # Simpan ke PostgreSQL
        storage.upsert_prices(df)
        print(f"[idx_api] ✅ Stock Summary: {len(df)} baris disimpan.")
    else:
        print(f"[idx_api] ⚠️ Stock Summary: Tidak ada data untuk {date_iso}.")
        
    # 2. Broker Summary (Aggregate)
    broker_data = _fetch("/TradingSummary/GetBrokerSummary", params={"date": date_str_api, "start": 0, "length": 9999})
    if broker_data and broker_data.get("data"):
        df_b = pd.DataFrame(broker_data["data"])
        df_b = df_b.rename(columns={
            "IDFirm": "id_firm",
            "FirmName": "firm_name",
            "Volume": "volume",
            "Value": "value",
            "Frequency": "frequency"
        })
        df_b["date"] = date_iso
        
        storage.upsert_idx_broker_summary(df_b)
        print(f"[idx_api] ✅ Broker Summary: {len(df_b)} baris disimpan.")
    else:
        print(f"[idx_api] ⚠️ Broker Summary: Tidak ada data untuk {date_iso}.")
        
    # 3. Index Summary (IHSG, LQ45)
    index_data = _fetch("/TradingSummary/GetIndexSummary", params={"date": date_str_api, "start": 0, "length": 9999})
    if index_data and index_data.get("data"):
        df_i = pd.DataFrame(index_data["data"])
        df_i = df_i.rename(columns={
            "IndexCode": "index_code",
            "IndexName": "index_name",
            "Close": "close",
            "Volume": "volume",
            "Value": "value",
            "MarketCapital": "market_cap"
        })
        df_i["date"] = date_iso
        
        storage.upsert_idx_index_summary(df_i)
        print(f"[idx_api] ✅ Index Summary: {len(df_i)} baris disimpan.")
    else:
        print(f"[idx_api] ⚠️ Index Summary: Tidak ada data untuk {date_iso}.")


# ── FUNGSI SNAPSHOT FUNDAMENTAL ──

def ingest_financial_ratios(year: int = 2024, quarter: int = 4):
    """Mengambil Financial Ratios dari IDX dan menyimpan ke PostgreSQL."""
    print(f"[idx_api] 📊 Mengambil Financial Ratios (Year: {year}, Quarter: {quarter})...")
    
    endpoint = "/DigitalStatistic/GetApiDataPaginated"
    params = {
        "urlName": "LINK_FINANCIAL_DATA_RATIO",
        "periodQuarter": quarter,
        "periodYear": year,
        "type": "yearly",
        "isPrint": "False",
        "cumulative": "false",
        "pageSize": 100,
        "pageNumber": 1
    }
    
    all_records = []
    page = 1
    
    while True:
        params["pageNumber"] = page
        data = _fetch(endpoint, params=params)
        
        if data and data.get("data") and len(data["data"]) > 0:
            all_records.extend(data["data"])
            print(f"   - Page {page}: {len(data['data'])} records fetched.")
            page += 1
            time.sleep(1) # Jeda ramah
        else:
            break
            
    if all_records:
        df = pd.DataFrame(all_records)
        df = df.rename(columns={
            "code": "code",
            "per": "per",
            "pbv": "pbv",
            "roe": "roe",
            "roa": "roa",
            "der": "der",
            "eps": "eps"
        })
        df["year"] = year
        df["quarter"] = quarter
        
        cols = ["code", "year", "quarter", "per", "pbv", "roe", "roa", "der", "eps"]
        for c in cols:
            if c not in df.columns:
                df[c] = None
                
        storage.upsert_financial_ratios(df[cols])
        print(f"[idx_api] ✅ Financial Ratios: {len(df)} baris disimpan.")
    else:
        print(f"[idx_api] ⚠️ Financial Ratios: Tidak ada data.")


def ingest_corporate_actions():
    """Mengambil seluruh Corporate Actions dari IDX dan menyimpan ke PostgreSQL."""
    print(f"[idx_api] 📝 Mengambil Corporate Actions...")
    
    ca_types = [
        "tanpaHmetd", "hmetd", "stockSplit", "reverseStock", "sahamBonus",
        "dividenSaham", "BuybackSaham", "PrivatePlacement", "ipo", "waran",
        "gabungUsaha", "kurangModal", "konversiSaham"
    ]
    
    endpoint = "/ListingActivity/GetIssuedHistory"
    all_records = []
    
    for ca_type in ca_types:
        params = {"caType": ca_type, "dateFrom": "", "dateTo": "", "start": 0, "length": 9999}
        data = _fetch(endpoint, params=params)
        
        if data and data.get("data"):
            for rec in data["data"]:
                rec["caType"] = ca_type
                all_records.append(rec)
            print(f"   - caType={ca_type}: {len(data['data'])} records.")
            time.sleep(0.5)
            
    if all_records:
        df = pd.DataFrame(all_records)
        df = df.rename(columns={
            "KodeEmiten": "ticker",
            "TanggalPencatatan": "date",
            "caType": "ca_type",
            "JenisTindakan": "description"
        })
        
        cols = ["date", "ticker", "ca_type", "description"]
        for c in cols:
            if c not in df.columns:
                df[c] = None
                
        storage.upsert_corporate_actions(df[cols])
        print(f"[idx_api] ✅ Corporate Actions: {len(df)} baris disimpan.")
    else:
        print(f"[idx_api] ⚠️ Corporate Actions: Tidak ada data.")

def ingest_company_details(ticker: str):
    """Mengambil detail governance (Direksi, Komisaris, Pemegang Saham) untuk 1 saham."""
    endpoint = "/ListedCompany/GetCompanyProfilesDetail"
    params = {"KodeEmiten": ticker, "language": "id-id"}
    
    data = _fetch(endpoint, params=params)
    
    if not data or not data.get("Profiles") or len(data["Profiles"]) == 0:
        print(f"[idx_api] ⚠️ Detail untuk {ticker} tidak ditemukan.")
        return False
        
    profile = data["Profiles"][0]
    
    # 1. Pemegang Saham
    shareholders = profile.get("PemegangSaham", [])
    if shareholders:
        sh_rows = []
        for s in shareholders:
            sh_rows.append({
                "ticker": ticker,
                "name": (s.get("Nama") or "").strip(),
                "pct": float(s.get("Persentase") or 0.0),
                "is_controlling": bool(s.get("Pengendali", False))
            })
        if sh_rows:
            df_sh = pd.DataFrame(sh_rows)
            storage.upsert_company_shareholders(df_sh)
            
    # 2. Direksi & Komisaris
    board_rows = []
    for d in profile.get("Direksi", []):
        board_rows.append({
            "ticker": ticker,
            "name": (d.get("Nama") or "").strip(),
            "role": "Director",
            "title": (d.get("Jabatan") or "").strip()
        })
    for k in profile.get("DewanKomisaris", []):
        board_rows.append({
            "ticker": ticker,
            "name": (k.get("Nama") or "").strip(),
            "role": "Commissioner",
            "title": (k.get("Jabatan") or "").strip()
        })
        
    if board_rows:
        df_b = pd.DataFrame(board_rows)
        storage.upsert_company_board(df_b)
        
    return True


def ingest_all_company_details(concurrency: int = 5):
    """Mengambil detail governance untuk SEMUA saham di tabel tickers secara concurrent."""
    from . import universe as universe_mod
    import concurrent.futures
    
    tickers = universe_mod.get_master_tickers(active_only=True)
    print(f"[idx_api] 🏢 Mengambil Company Details untuk {len(tickers)} saham (Concurrency: {concurrency})...")
    
    success_count = 0
    fail_count = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(ingest_company_details, t): t for t in tickers}
        
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            ticker = futures[future]
            try:
                result = future.result()
                if result:
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as exc:
                print(f"[idx_api] ❌ Error fetching details for {ticker}: {exc}")
                fail_count += 1
                
            if i % 50 == 0:
                print(f"   - Progress: {i}/{len(tickers)} (Success: {success_count}, Fail: {fail_count})")
                
    print(f"[idx_api] ✅ Company Details selesai! Total Sukses: {success_count}, Gagal: {fail_count}")

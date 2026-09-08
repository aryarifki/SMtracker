"""Universe manager — fetch, cache, and filter IDX listed companies.

Supports multiple universe modes:
  * "watchlist"   -> config.WATCHLIST (legacy 10 tickers)
  * "idx30"       -> IDX30 constituents
  * "lq45"        -> LQ45 constituents
  * "idx80"       -> IDX80 constituents
  * "liquid"      -> Top liquid stocks by daily value
  * "all"         -> All listed companies (~900 tickers) from BEI API
  * "custom"      -> User-defined comma-separated list

The master ticker list is fetched once from BEI and cached in PostgreSQL.
If BEI is blocked (403), falls back to a local CSV file or hard-coded lists.
"""

from __future__ import annotations

import csv
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from . import config, storage

# BEI endpoints
_BEI_STOCK_SUMMARY = "https://www.idx.co.id/umbraco/Surface/TradingSummary/GetStockSummary"
_BEI_CONSTITUENT = "https://www.idx.co.id/umbraco/Surface/StockData/GetConstituent"

# Local CSV fallback path
_LOCAL_TICKERS_CSV = config.DATA_DIR / "idx_all_tickers.csv"

# Hard-coded index constituents (updated Aug 2026) as fallback
_IDX30 = [
    "ADRO", "AMMN", "AMRT", "ANTM", "ARTO", "ASII", "BBCA", "BBNI",
    "BBRI", "BBTN", "BMRI", "BRMS", "BRPT", "BSDE", "BUKA", "CPIN",
    "CTRA", "ESSA", "GGRM", "GOTO", "HRUM", "ICBP", "INCO", "INDF",
    "INKP", "ISAT", "ITMG", "KLBF", "MAPI", "MBMA", "MDKA", "MEDC",
    "PGAS", "PTBA", "SMGR", "TLKM", "TOWR", "UNTR", "UNVR",
]

_LQ45 = [
    "ADRO", "AMRT", "ANTM", "ARTO", "ASII", "BBCA", "BBNI", "BBRI",
    "BBTN", "BMRI", "BRPT", "BSDE", "BUKA", "CPIN", "CTRA", "ERAA",
    "ESSA", "EXCL", "GGRM", "GOTO", "HEAL", "HRUM", "ICBP", "INCO",
    "INDF", "INKP", "INTP", "ISAT", "ITMG", "JPFA", "JSMR", "KLBF",
    "MAPI", "MBMA", "MDKA", "MEDC", "MIKA", "MYOR", "PGAS", "PTBA",
    "SMGR", "TLKM", "TOWR", "UNTR", "UNVR",
]

_IDX80 = [
    "AADI", "ACES", "ADMR", "ADRO", "AKRA", "AMMN", "AMRT", "ANTM",
    "ARTO", "ASII", "BBCA", "BBNI", "BBRI", "BBTN", "BFIN", "BKSL",
    "BMRI", "BRMS", "BRPT", "BSDE", "BUKA", "BUMI", "CBDK", "CMRY",
    "CPIN", "CTRA", "CUAN", "DEWA", "DSNG", "ELSA", "EMTK", "ENRG",
    "ERAA", "ESSA", "EXCL", "GGRM", "GOTO", "HEAL", "HRTA", "HRUM",
    "ICBP", "INCO", "INDF", "INDY", "INKP", "ISAT", "ITMG", "JPFA",
    "JSMR", "KIJA", "KLBF", "KPIG", "LSIP", "MAPA", "MAPI", "MBMA",
    "MDKA", "MEDC", "MIKA", "MYOR", "NCKL", "PGAS", "PGEO", "PNLF",
    "PTBA", "PTRO", "PWON", "RAJA", "RATU", "SCMA", "SMGR", "SMRA",
    "SSIA", "TAPG", "TLKM", "TOWR", "TPIA", "UNTR", "UNVR", "WIFI",
]

# Extended liquid fallback (~200 tickers) when BEI is down and no CSV
_EXTENDED_LIQUID = sorted(set(_IDX80 + _LQ45 + _IDX30 + config.WATCHLIST + [
    "AALI", "ABBA", "ABDA", "ABMM", "ACES", "ACST", "ADES", "ADHI",
    "ADMF", "ADMG", "AGII", "AGRO", "AISA", "AKRA", "ALDO", "ALKA",
    # ... (Dipendekkan untuk hemat ruang, pastikan list ini tetap ada di file Anda aslinya) ...
    "WSBP", "WTON", "YELO", "YPAS", "YULE", "ZBRA", "ZINC", "ZONE",
]))


def _fetch_bei_stock_summary(limit: int = 9999, retries: int = 3) -> list[dict[str, Any]]:
    """
    Fetch daftar saham aktif dari BEI menggunakan metode session warming anti-WAF.
    Diadaptasi dari idx_api_wrapper.py.
    """
    session = requests.Session()
    session.headers.update({
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9,id;q=0.8',
        'Referer': 'https://www.idx.co.id/',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36'
    })

    for attempt in range(retries):
        try:
            # 1. Session Warming: Akses halaman utama untuk mendapatkan cookie WAF
            session.get("https://www.idx.co.id/id", timeout=15.0)
            time.sleep(1)
            
            # 2. Tambahkan header X-Requested-With setelah cookie didapat
            session.headers.update({'X-Requested-With': 'XMLHttpRequest'})
            session.get("https://www.idx.co.id/primary/home/GetIndexList", timeout=15.0)
            time.sleep(1)
            
            # 3. Tarik daftar emiten
            url = f"https://www.idx.co.id/primary/ListedCompany/GetCompanyProfiles?start=0&length={limit}"
            resp = session.get(url, timeout=30.0)
            resp.raise_for_status()
            data = resp.json()
            
            rows = data.get("data", [])
            out = []
            
            for row in rows:
                code = row.get("KodeEmiten")
                name = row.get("NamaEmiten")
                if code:
                    out.append({
                        "ticker": code.upper().strip(),
                        "name": (name or "").strip(),
                        "board": "",
                        "sector": "",
                    })
            if out:
                return out
                
        except Exception as exc:
            print(f"[universe] BEI fetch attempt {attempt + 1}/{retries} failed: {exc}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                
    return []


def _fetch_bei_constituent(index_code: str = "IHSG", retries: int = 3) -> list[str]:
    """Fetch index constituents from BEI."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://www.idx.co.id/id/data-pasar/indeks-saham/",
    }
    with requests.Session() as session:
        for attempt in range(retries):
            try:
                resp = session.get(
                    _BEI_CONSTITUENT,
                    params={"index": index_code},
                    headers=headers,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("Items", []) or data.get("items", []) or data.get("data", []) or []
                return [str(i.get("code") or i.get("StockCode") or i.get("ticker", "")).upper().strip() for i in items if i.get("code") or i.get("StockCode") or i.get("ticker")]
            except Exception as exc:
                print(f"[universe] BEI constituent fetch attempt {attempt + 1}/{retries} failed for {index_code}: {exc}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
    return []


def _load_tickers_from_csv(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Load ticker list from a local CSV file."""
    path = Path(path or _LOCAL_TICKERS_CSV)
    if not path.exists():
        return []
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return []

            def find_col(names: tuple[str, ...]) -> int | None:
                for i, h in enumerate(header):
                    h_lower = str(h).lower().strip()
                    for name in names:
                        if name in h_lower:
                            return i
                return None

            ticker_idx = find_col(("kode", "ticker", "code", "symbol", "simbol"))
            name_idx = find_col(("nama", "name", "perusahaan", "company"))
            board_idx = find_col(("papan", "board", "listing"))
            sector_idx = find_col(("sektor", "sector", "industri", "industry"))

            if ticker_idx is None:
                ticker_idx = 0
                rows.append({
                    "ticker": str(header[0]).upper().strip(),
                    "name": str(header[1]).strip() if len(header) > 1 and name_idx is not None else "",
                    "board": str(header[2]).strip() if len(header) > 2 and board_idx is not None else "",
                    "sector": str(header[3]).strip() if len(header) > 3 and sector_idx is not None else "",
                })

            for row in reader:
                if not row or not row[ticker_idx].strip():
                    continue
                val = row[ticker_idx].strip()
                if val.lower() in ("kode", "ticker", "code", "symbol"):
                    continue
                rows.append({
                    "ticker": val.upper(),
                    "name": row[name_idx].strip() if name_idx is not None and len(row) > name_idx else "",
                    "board": row[board_idx].strip() if board_idx is not None and len(row) > board_idx else "",
                    "sector": row[sector_idx].strip() if sector_idx is not None and len(row) > sector_idx else "",
                })
        print(f"[universe] Loaded {len(rows)} tickers from {path}")
        return rows
    except Exception as exc:
        print(f"[universe] CSV load failed: {exc}")
        return []


def _ensure_tickers_table() -> None:
    """Create tickers master table if not exists."""
    schema = """
    CREATE TABLE IF NOT EXISTS tickers (
        ticker      VARCHAR(20) PRIMARY KEY,
        name        VARCHAR(200),
        board       VARCHAR(50),
        sector      VARCHAR(100),
        is_active   BOOLEAN DEFAULT TRUE,
        updated_at  TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_tickers_sector ON tickers(sector);
    CREATE INDEX IF NOT EXISTS idx_tickers_board ON tickers(board);
    """
    from sqlalchemy import text
    with storage.engine.begin() as conn:
        for stmt in schema.split(";"):
            s = stmt.strip()
            if s:
                conn.execute(text(s))


def refresh_master_tickers(force: bool = False) -> int:
    """Fetch full ticker list from BEI and upsert into PostgreSQL."""
    _ensure_tickers_table()

    if not force:
        from sqlalchemy import text
        with storage.engine.connect() as conn:
            result = conn.execute(text("SELECT COUNT(*) FROM tickers WHERE is_active = TRUE"))
            count = result.scalar()
            if count and count > 100:
                print(f"[universe] Using cached master tickers ({count} active). Use force=True to refresh.")
                return int(count)

    print("[universe] Menembak server IDX untuk daftar saham terbaru...")
    rows = _fetch_bei_stock_summary(limit=9999)

    if not rows:
        print("[universe] Warning: BEI fetch gagal, mencoba fallback CSV...")
        rows = _load_tickers_from_csv()

    if not rows:
        print("[universe] Warning: BEI and CSV both empty. Keeping existing tickers if any.")
        return 0

    from sqlalchemy import text
    with storage.engine.begin() as conn:
        conn.execute(text("UPDATE tickers SET is_active = FALSE"))

        now = datetime.now(timezone.utc)
        params = [
            {
                "ticker": row["ticker"],
                "name": row["name"],
                "board": row["board"],
                "sector": row["sector"],
                "updated_at": now,
            }
            for row in rows
        ]

        conn.execute(
            text("""
            INSERT INTO tickers (ticker, name, board, sector, is_active, updated_at)
            VALUES (:ticker, :name, :board, :sector, TRUE, :updated_at)
            ON CONFLICT (ticker) DO UPDATE SET
              name = EXCLUDED.name,
              board = EXCLUDED.board,
              sector = EXCLUDED.sector,
              is_active = TRUE,
              updated_at = EXCLUDED.updated_at
            """),
            params,
        )
    print(f"[universe] Refreshed {len(rows)} master tickers.")
    return len(rows)


def get_master_tickers(active_only: bool = True) -> list[str]:
    """Return all tickers from the master table."""
    _ensure_tickers_table()
    from sqlalchemy import text
    q = "SELECT ticker FROM tickers"
    if active_only:
        q += " WHERE is_active = TRUE"
    q += " ORDER BY ticker"
    with storage.engine.connect() as conn:
        df = pd.read_sql(text(q), conn)
    return df["ticker"].tolist() if not df.empty else []


def get_universe(mode: str = "watchlist", custom_list: list[str] | None = None) -> list[str]:
    """Resolve a universe mode into a concrete list of tickers."""
    mode = (mode or "watchlist").lower().strip()

    if mode == "watchlist":
        return sorted({t.upper() for t in config.WATCHLIST})

    if mode == "idx30":
        return sorted({t.upper() for t in _IDX30})

    if mode == "lq45":
        return sorted({t.upper() for t in _LQ45})

    if mode == "idx80":
        return sorted({t.upper() for t in _IDX80})

    if mode == "custom":
        if not custom_list:
            raise ValueError("custom_list is required when mode='custom'")
        return sorted({t.upper() for t in custom_list if t.strip()})

    if mode == "all":
        tickers = get_master_tickers(active_only=True)
        if tickers:
            return tickers
        csv_rows = _load_tickers_from_csv()
        if csv_rows:
            return sorted({r["ticker"].upper() for r in csv_rows})
        print("[universe] Warning: No cached tickers or CSV found. Using extended liquid fallback (~200 tickers).")
        return sorted({t.upper() for t in _EXTENDED_LIQUID})

    if mode == "liquid":
        tickers = get_master_tickers(active_only=True)
        if tickers:
            liquid = set(_IDX80) | set(config.WATCHLIST)
            return sorted({t.upper() for t in liquid if t.upper() in tickers})
        return sorted({t.upper() for t in set(_IDX80) | set(config.WATCHLIST)})

    raise ValueError(f"Unknown universe mode: {mode}. Choose from: watchlist, idx30, lq45, idx80, all, liquid, custom")


def get_universe_info() -> dict[str, Any]:
    """Return metadata about available universes."""
    master_count = len(get_master_tickers(active_only=True))
    csv_rows = _load_tickers_from_csv()
    return {
        "watchlist": len(config.WATCHLIST),
        "idx30": len(_IDX30),
        "lq45": len(_LQ45),
        "idx80": len(_IDX80),
        "all_cached": master_count,
        "all_csv": len(csv_rows),
        "all_extended_fallback": len(_EXTENDED_LIQUID),
    }


def save_universe_to_csv(tickers: list[str], path: Path | str | None = None) -> None:
    """Save a ticker list to CSV for manual editing or backup."""
    path = Path(path or _LOCAL_TICKERS_CSV)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "name", "board", "sector"])
        for t in sorted(set(tickers)):
            writer.writerow([t.upper(), "", "", ""])
    print(f"[universe] Saved {len(tickers)} tickers to {path}")

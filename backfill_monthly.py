#!/usr/bin/env python3
"""Backfill broker data per bulan dengan async IDX (cepat) + Stockbit (detail) & auto-token renew."""

from __future__ import annotations

import argparse
import sys
import os
import time
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from sqlalchemy import text

# Injector Token
from dotenv import set_key, load_dotenv
from playwright.sync_api import sync_playwright

# ── path setup ─────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from idx_bandarmology import pipeline, storage, universe as universe_mod, idx_api
from idx_bandarmology.broker_api import set_rate_limit
try:
    from idx_bandarmology import config
except ImportError:
    config = None

# ── konfigurasi ────────────────────────────────────────────────────────────
_ENV_PATH = _ROOT / ".env"
_SESSION_DIR = _ROOT / "browser_session"
_DEBUG_DIR = _ROOT / "debug"
_DEBUG_DIR.mkdir(parents=True, exist_ok=True)

_PAUSE_BETWEEN_MONTHS = 15

# ── helper rentang bulan ────────────────────────────────────────────────────
def get_month_ranges(end_date: date | None = None, months_back: int = 12) -> list[tuple[date, date, str]]:
    if end_date is None:
        end_date = date.today()
    ranges = []
    for i in range(months_back):
        year, month = end_date.year, end_date.month - i
        while month <= 0:
            month += 12
            year -= 1
        start = date(year, month, 1)
        next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        end = next_month - timedelta(days=1)
        if end > date.today(): end = date.today()
        ranges.append((start, end, f"{year}-{month:02d}"))
    return ranges

def parse_month_args(arg: str, ranges: list[tuple[date, date, str]]) -> list[tuple[date, date, str]]:
    if arg == "all": return ranges
    if arg == "last6": return ranges[:6]
    if arg == "last3": return ranges[:3]
    selected = [s.strip() for s in arg.split(",")]
    filtered = [r for r in ranges if r[2] in selected]
    if not filtered:
        print(f"❌ Bulan '{arg}' tidak ditemukan.")
        sys.exit(1)
    return filtered

def estimate_time(n_tickers: int, n_days: int) -> str:
    total_seconds = n_tickers * n_days * 8
    return f"~{total_seconds / 3600:.1f} jam ({total_seconds/60:.0f} menit)"

# ── SISTEM PEMULIHAN TOKEN OTOMATIS ─────────────────────────────────────────
def auto_renew_token(force_clean_session: bool = False) -> bool:
    if force_clean_session:
        print("   🧹 Membersihkan sesi browser lama yang korup...")
        if _SESSION_DIR.exists():
            shutil.rmtree(_SESSION_DIR, ignore_errors=True)
        env_str = str(_ENV_PATH)
        os.system(f"sed -i '/BROKER_API_TOKEN/d' {env_str}")
        if "BROKER_API_TOKEN" in os.environ:
            del os.environ["BROKER_API_TOKEN"]
        if config is not None:
            config.set_broker_api_token("")

    print("\n   ⚠️ PERINGATAN: Akses API ditolak atau koneksi terputus (Mungkin Token Kedaluwarsa)!")
    print("   🤖 Mengaktifkan peramban darurat untuk mencuri token baru di latar belakang...")
    
    load_dotenv(_ENV_PATH)
    username = os.getenv("STOCKBIT_USERNAME")
    password = os.getenv("STOCKBIT_PASSWORD")
    
    if not username or not password:
        print("   ❌ Gagal: Kredensial Stockbit tidak ditemukan di .env")
        return False
        
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=_SESSION_DIR,
            headless=True,
            viewport={"width": 1280, "height": 720}
        )
        page = context.pages[0]
        captured_token = None

        def handle_request(request):
            nonlocal captured_token
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer ") and "undefined" not in auth and len(auth) > 30:
                captured_token = auth

        page.on("request", handle_request)
        
        try:
            page.goto("https://stockbit.com/#/stream", wait_until="domcontentloaded", timeout=30000)
            for _ in range(5):
                if captured_token: break
                page.wait_for_timeout(1000)
                
            if not captured_token:
                print("   🔄 Sesi tidak valid, mencoba login paksa...")
                page.goto("https://stockbit.com/login", wait_until="domcontentloaded")
                page.wait_for_selector("input", timeout=15000)
                page.locator('input[id="username"], input[type="text"], input[name="username"]').first.fill(username)
                page.locator('input[id="password"], input[type="password"], input[name="password"]').first.fill(password)
                page.locator('button[type="submit"], input[type="submit"], button:has-text("Log In")').first.click()
                
                print("\n   🚨 PERHATIAN: Silakan cek aplikasi Stockbit / HP Anda sekarang!")
                print("   ⏳ Menunggu Anda melakukan autentikasi perangkat (Batas waktu: 2 menit)...")
                
                for _ in range(60):
                    if captured_token: 
                        print("   ✅ Autentikasi sukses! Token baru berhasil ditangkap.")
                        break
                    page.wait_for_timeout(2000)
                
                if not captured_token:
                    print("\n   ❌ Waktu habis. Autentikasi tidak diselesaikan atau gagal.")
                    debug_path = _DEBUG_DIR / "debug_backfill_login.png"
                    page.screenshot(path=str(debug_path))
                    print(f"   📸 Screenshot kegagalan disimpan sebagai {debug_path.name}")
                    
        except Exception as e:
            print(f"   ❌ Gagal navigasi saat renew token: {e}")
            try:
                debug_path = _DEBUG_DIR / "debug_backfill_error.png"
                page.screenshot(path=str(debug_path))
                print(f"   📸 Screenshot error disimpan sebagai {debug_path.name}")
            except:
                pass
        finally:
            context.close()
            
    if captured_token:
        print(f"   ✅ Token darurat berhasil diamankan! ({captured_token[:15]}...)")
        set_key(dotenv_path=_ENV_PATH, key_to_set="BROKER_API_TOKEN", value_to_set=captured_token)
        os.environ["BROKER_API_TOKEN"] = captured_token
        if config is not None:
            config.set_broker_api_token(captured_token)
        return True
    
    print("   ❌ Gagal mendapatkan token darurat.")
    return False

# ── cek status langsung ke database ─────────────────────────────────────────
def check_db_status(universe_mode: str) -> None:
    print(f"📋 Backfill Progress (Cek Database)\n   Universe: {universe_mode.upper()}")
    syms = universe_mod.get_universe(universe_mode)
    
    q_broker = text("""
        SELECT MIN(date) as start_date, MAX(date) as end_date, COUNT(DISTINCT date) as trading_days
        FROM broker_flow WHERE ticker = ANY(:tickers)
    """)
    
    q_prices = text("""
        SELECT MIN(date) as start_date, MAX(date) as end_date, COUNT(DISTINCT date) as trading_days
        FROM prices WHERE ticker = ANY(:tickers)
    """)
    
    with storage.engine.connect() as conn:
        res_broker = conn.execute(q_broker, {"tickers": syms}).fetchone()
        res_prices = conn.execute(q_prices, {"tickers": syms}).fetchone()
        
    print("-" * 40)
    if res_prices and res_prices[2] > 0:
        print(f"   📈 Data Harga (IDX): {res_prices[0]} hingga {res_prices[1]} ({res_prices[2]} hari)")
    else:
        print("   ❌ Belum ada data Harga (IDX) tersimpan.")
        
    if res_broker and res_broker[2] > 0:
        print(f"   📊 Data Bandar (Stockbit): {res_broker[0]} hingga {res_broker[1]} ({res_broker[2]} hari)")
    else:
        print("   ❌ Belum ada data Bandar (Stockbit) tersimpan.")
    print("-" * 40)

# ── eksekusi utama ─────────────────────────────────────────────────────────
def run_backfill_month(
    month_label: str, start: date, end: date, universe_mode: str, 
    rate_limit: float, refresh_prices: bool,
) -> bool:
    print(f"\n{'='*60}")
    print(f"📅 Memproses: {month_label}  ({start}  →  {end})")
    print(f"{'='*60}")

    if start > end:
        print(f"   ⚠️  Range tidak valid. Skip.")
        return True

    syms = universe_mod.get_universe(universe_mode)
    
    n_days = (end - start).days + 1
    trading_days = sum(1 for i in range(n_days) if (start + timedelta(days=i)).weekday() < 5)
    print(f"   🎯 Target: {len(syms)} tickers | Hari kerja: ~{trading_days} | Estimasi Stockbit (jika dari nol): {estimate_time(len(syms), trading_days)}")

    # ── 1. AMBIL DATA IDX (ASYNC, SANGAT CEPAT) ──
    # Data harga (OHLCV), Broker Aggregate, dan Index langsung dari idx.co.id
    # Menggunakan concurrency=8, backfill 1 bulan hanya butuh ~2-3 menit.
    print("\n   🚀 [1/2] Mengambil data IDX (Harga & Broker Aggregate) via Async...")
    try:
        idx_api.run_async_backfill(start, end, concurrency=4)
    except Exception as e:
        print(f"   ❌ Gagal mengambil data IDX: {e}")

    # ── 2. AMBIL DATA STOCKBIT (BROKER-TO-BROKER) ──
    # Data detail distribusi broker dan sinyal bandar
    print("\n   📈 [2/2] Mengambil data Stockbit (Bandarmology)...")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            t0 = time.monotonic()
            set_rate_limit(rate_limit)

            # Karena data harga sudah diambil oleh idx_api, kita set refresh_prices=False
            # agar pipeline tidak menarik ulang data harga menggunakan metode lama.
            result = pipeline.backfill_broker_history(
                universe_mode=universe_mode, 
                start_date=start,
                end_date=end,
                refresh_prices=False, 
                price_period="1y",
            )

            elapsed = time.monotonic() - t0
            print(f"\n   ✅ Proses Stockbit selesai dalam {elapsed/60:.1f} menit")
            print(f"      📊 Baris baru disimpan -> Broker rows: {result['n_broker']:,} | Activity rows: {result.get('n_activity', 0):,}")

            if result['n_broker'] == 0 and result['n_activity'] == 0:
                print("   ℹ️ Tidak ada baris baru Stockbit (kemungkinan data bulan ini sudah lengkap di database).")
            
            return True

        except Exception as exc:
            error_msg = str(exc).lower()
            print(f"   ❌ GAGAL pada percobaan {attempt + 1}/{max_retries}: {exc}")
            
            is_token_issue = any(k in error_msg for k in ["401", "403", "unauthorized", "forbidden", "token", "timeout", "read", "connection"])
            force_clean = any(k in error_msg for k in ["401", "403", "unauthorized", "forbidden"])
            
            if is_token_issue and attempt < max_retries - 1:
                if auto_renew_token(force_clean_session=force_clean):
                    print("   🔁 Mencoba melanjutkan unduhan dengan token baru...")
                    time.sleep(2)
                    continue
                else:
                    break
            else:
                return False
    return False

def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill broker data per bulan (Hybrid IDX Async + Stockbit)")
    parser.add_argument("--universe", default="idx80", help="Universe yang akan di-backfill")
    parser.add_argument("--months", default="all", help='Bulan: "all", "last3", "2026-09", dll.')
    parser.add_argument("--rate-limit", type=float, default=8.0)
    parser.add_argument("--status", action="store_true", help="Cek tanggal maksimal data di database")
    # --no-refresh-prices tidak lagi diperlukan karena idx_api menangani harga secara async
    
    args = parser.parse_args()
    
    storage.init_db()
    
    if args.status:
        check_db_status(args.universe)
        return

    ranges = get_month_ranges(months_back=12)
    selected = parse_month_args(args.months, ranges)

    print(f"\n📅 Total bulan dipilih: {len(selected)}")
    for start, end, label in selected:
        print(f"   ⏳ {label}  ({start} ~ {end})")

    if not selected:
        return

    print(f"\n⏳ Bulan yang akan dikerjakan: {len(selected)}")
    confirm = input("Lanjutkan? [Y/n]: ").strip().lower()
    if confirm and confirm not in ("y", "yes", "ya"):
        print("Dibatalkan.")
        return

    for start, end, label in selected:
        ok = run_backfill_month(label, start, end, args.universe, args.rate_limit, False)
        if label != selected[-1][2] and ok:
            print(f"⏸️ Jeda {_PAUSE_BETWEEN_MONTHS} detik sebelum bulan berikutnya...")
            time.sleep(_PAUSE_BETWEEN_MONTHS)

if __name__ == "__main__":
    main()

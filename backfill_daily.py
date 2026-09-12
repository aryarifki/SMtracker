#!/usr/bin/env python3
"""Backfill broker data harian (dinamis N-hari ke belakang) dengan auto-token renew."""

from __future__ import annotations
import argparse
import sys
import os
import time
import shutil
from datetime import date, timedelta
from pathlib import Path

from dotenv import set_key, load_dotenv
from playwright.sync_api import sync_playwright

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from idx_bandarmology import prices, storage, universe as universe_mod, broker_api
from idx_bandarmology.broker_api import set_rate_limit
try:
    from idx_bandarmology import config
except ImportError:
    config = None

_ENV_PATH = _ROOT / ".env"
_SESSION_DIR = _ROOT / "browser_session"
_DEBUG_DIR = _ROOT / "debug"
_DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# --- FUNGSI PEMULIHAN TOKEN ---
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

    print("\n   ⚠️ PERINGATAN: Akses API ditolak (Mungkin Token Kedaluwarsa)!")
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
                    debug_path = _DEBUG_DIR / "debug_daily_login.png"
                    page.screenshot(path=str(debug_path))
                    
        except Exception as e:
            print(f"   ❌ Gagal navigasi saat renew token: {e}")
        finally:
            context.close()
            
    if captured_token:
        print(f"   ✅ Token darurat berhasil diamankan! ({captured_token[:15]}...)")
        set_key(dotenv_path=_ENV_PATH, key_to_set="BROKER_API_TOKEN", value_to_set=captured_token)
        os.environ["BROKER_API_TOKEN"] = captured_token
        if config is not None:
            config.set_broker_api_token(captured_token)
        return True
    
    return False

# --- LOGIKA EKSEKUSI HARIAN ---
def run_daily_backfill(universe_mode: str, rate_limit: float, refresh_prices: bool, days_back: int) -> bool:
    end = date.today()
    start = end - timedelta(days=days_back)

    print(f"\n{'='*60}")
    print(f"📅 Memproses Data Harian: {start}  →  {end}")
    print(f"{'='*60}")

    syms = universe_mod.get_universe(universe_mode)
    print(f"   🎯 Target: {len(syms)} tickers di universe '{universe_mode}'")

    max_retries = 3
    for attempt in range(max_retries):
        try:
            t0 = time.monotonic()
            set_rate_limit(rate_limit)

            # 1. Ambil data harga (IDX) jika diizinkan
            n_prices = 0
            if refresh_prices:
                print("[daily] Menarik data harga dari IDX...")
                n_prices = prices.fetch_history_many(syms, period="1y")
                print(f"[daily]   -> {n_prices} baris harga tersimpan.")

            # 2. Ambil data broker langsung ke endpoint history (Bypass watchlist)
            print("[daily] Menarik data broker dari Stockbit...")
            n_broker, n_activity = broker_api.fetch_historical_broker_data(
                syms, start, end, force=True # Force=True agar selalu eksekusi
            )

            elapsed = time.monotonic() - t0
            print(f"   ✅ Selesai dalam {elapsed/60:.1f} menit")
            print(f"      📊 Broker rows: {n_broker:,} | Activity rows: {n_activity:,}")
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
    parser = argparse.ArgumentParser(description="Backfill broker data harian")
    parser.add_argument("--universe", default="watchlist", help="Universe yang akan ditarik")
    parser.add_argument("--days", type=int, default=3, help="Jumlah hari ke belakang")
    parser.add_argument("--rate-limit", type=float, default=8.0)
    parser.add_argument("--no-refresh-prices", action="store_true")
    args = parser.parse_args()
    
    # ── SKIP WEEKEND UNTUK CRONJOB ──
    if date.today().weekday() >= 5:
        print("🗓️ Hari ini akhir pekan (Sabtu/Minggu). Bursa saham tutup. Skrip dihentikan.")
        sys.exit(0)
        
    storage.init_db()
    
    if not run_daily_backfill(args.universe, args.rate_limit, not args.no_refresh_prices, args.days):
        print("\n🛑 CRONJOB GAGAL: Backfill harian tidak berhasil setelah 3x percobaan.")
        sys.exit(1)

# ── BLOK EKSEKUSI UTAMA ──
if __name__ == "__main__":
    main()

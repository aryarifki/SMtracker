#!/usr/bin/env python3
"""Sinkronisasi harian otomatis dengan arsitektur Self-Healing, Retry & Live Progress."""

from __future__ import annotations

import sys
import os
import time
import shutil
import argparse
from datetime import datetime
from pathlib import Path

from dotenv import set_key, load_dotenv
from playwright.sync_api import sync_playwright

# ── path setup ─────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from idx_bandarmology import pipeline
from idx_bandarmology.broker_api import set_rate_limit, is_available
try:
    from idx_bandarmology import config
except ImportError:
    config = None

_ENV_PATH = _ROOT / ".env"
_SESSION_DIR = _ROOT / "browser_session"

def get_new_token(force_clean_session: bool = False) -> bool:
    if force_clean_session:
        print("\n🧹 Membersihkan sesi browser lama yang korup...")
        if _SESSION_DIR.exists():
            shutil.rmtree(_SESSION_DIR, ignore_errors=True)
        os.system("sed -i '/BROKER_API_TOKEN/d' /opt/SMtracker/.env")
        if "BROKER_API_TOKEN" in os.environ:
            del os.environ["BROKER_API_TOKEN"]

    print("🤖 Mengaktifkan peramban darurat untuk menangkap token baru di latar belakang...")
    
    load_dotenv(_ENV_PATH)
    username = os.getenv("STOCKBIT_USERNAME")
    password = os.getenv("STOCKBIT_PASSWORD")
    
    if not username or not password:
        print("❌ Gagal: Kredensial Stockbit (USERNAME/PASSWORD) tidak ditemukan di .env")
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
                print("🔄 Sesi tidak valid atau kosong, mencoba login paksa...")
                page.goto("https://stockbit.com/login", wait_until="domcontentloaded")
                page.wait_for_selector("input", timeout=15000)
                page.locator('input[id="username"], input[type="text"], input[name="username"]').first.fill(username)
                page.locator('input[id="password"], input[type="password"], input[name="password"]').first.fill(password)
                page.locator('button[type="submit"], input[type="submit"], button:has-text("Log In")').first.click()
                
                print("\n🚨 PERHATIAN: Silakan cek aplikasi Stockbit / HP Anda sekarang!")
                print("⏳ Menunggu Anda melakukan autentikasi perangkat (Batas waktu: 2 menit)...")
                
                for _ in range(60):
                    if captured_token: 
                        print("✅ Autentikasi sukses! Token baru berhasil ditangkap.")
                        break
                    page.wait_for_timeout(2000)
                
                if not captured_token:
                    print("\n❌ Waktu habis. Autentikasi tidak diselesaikan atau gagal.")
                    page.screenshot(path="/opt/SMtracker/debug_daily_login.png")
                    print("📸 Screenshot kegagalan disimpan sebagai debug_daily_login.png")
                    
        except Exception as e:
            print(f"❌ Gagal navigasi saat renew token: {e}")
            try:
                page.screenshot(path="/opt/SMtracker/debug_daily_error.png")
            except:
                pass
        finally:
            context.close()
            
    if captured_token:
        print(f"✅ Token berhasil diamankan! ({captured_token[:15]}...)")
        set_key(dotenv_path=_ENV_PATH, key_to_set="BROKER_API_TOKEN", value_to_set=captured_token)
        os.environ["BROKER_API_TOKEN"] = captured_token
        if config is not None:
            config.BROKER_API_TOKEN = captured_token
        return True
    
    return False


def main():
    parser = argparse.ArgumentParser(description="Sinkronisasi harian otomatis SMtracker")
    parser.add_argument("--force", action="store_true", help="Paksa tarik data ulang meskipun sudah ada di database hari ini")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"📅 Memulai Sinkronisasi Harian: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    
    # Batas kecepatan dipertahankan di angka 12 request per menit (1 req / 5 detik)
    set_rate_limit(12.0)
    
    resume_mode = not args.force
    if not resume_mode:
        print("🚀 MODE PAKSA (--force) AKTIF: Menarik ulang semua data broker hari ini.")
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if not is_available():
                raise RuntimeError("Token is not configured")
                
            result = pipeline.run(
                universe_mode="all",
                price_period="1y",
                fetch_broker_data=True,
                resume=resume_mode,
                broker_batch_size=50  # <-- Penambahan parameter untuk memaksa terminal melapor secara aktif
            )
            print(f"\n🎉 Eksekusi harian selesai! Tersimpan: {result['n_broker']} baris broker.")
            break 
            
        except Exception as exc:
            error_msg = str(exc).lower()
            print(f"\n❌ Terjadi kesalahan pada percobaan {attempt + 1}/{max_retries}: {exc}")
            
            is_token_issue = any(k in error_msg for k in ["401", "403", "unauthorized", "forbidden", "token", "timeout", "connection", "read", "not configured"])
            
            if is_token_issue and attempt < max_retries - 1:
                print("⚠️ Terdeteksi masalah Token/Jaringan.")
                force_clean = any(k in error_msg for k in ["401", "403", "unauthorized", "forbidden"])
                
                if get_new_token(force_clean_session=force_clean):
                    print("\n🔁 Mencoba mengulangi sinkronisasi dengan token baru...\n")
                    time.sleep(3)
                    continue
                else:
                    print("❌ Gagal mendapatkan token baru, menyerah.")
                    break
            else:
                print("❌ Error kritis atau jatah retry habis. Menghentikan skrip.")
                break

if __name__ == "__main__":
    main()

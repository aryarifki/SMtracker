#!/usr/bin/env python3
import os
import sys
import subprocess
from pathlib import Path
from dotenv import set_key, load_dotenv
from playwright.sync_api import sync_playwright

_ROOT = Path(__file__).resolve().parent
ENV_PATH = _ROOT / ".env"
SESSION_DIR = _ROOT / "browser_session" 

load_dotenv(ENV_PATH)

STOCKBIT_USERNAME = os.getenv("STOCKBIT_USERNAME")
STOCKBIT_PASSWORD = os.getenv("STOCKBIT_PASSWORD")

def get_bearer_token():
    print(f"Membuka peramban dengan sesi tersimpan di folder '{SESSION_DIR.name}'...")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR,
            headless=True,
            viewport={"width": 1280, "height": 720}
        )
        page = context.pages[0]
        captured_token = None

        def handle_request(request):
            nonlocal captured_token
            headers = request.headers
            if "authorization" in headers:
                auth = headers["authorization"]
                # PERBAIKAN 1: Filter ketat! Tolak "undefined" dan pastikan panjang token masuk akal (>30 karakter)
                if auth.startswith("Bearer ") and "undefined" not in auth and len(auth) > 30:
                    captured_token = auth

        page.on("request", handle_request)

        try:
            print("Mengecek apakah sesi lama masih aktif (bypass login)...")
            page.goto("https://stockbit.com/#/stream", wait_until="domcontentloaded", timeout=30000)
            
            for _ in range(5):
                if captured_token:
                    print("✅ Sesi lama masih aktif! Melewati halaman login.")
                    return captured_token
                page.wait_for_timeout(1000)

            print("Sesi kosong/expired. Membuka halaman login Stockbit...")
            page.goto("https://stockbit.com/login", wait_until="domcontentloaded")
            page.wait_for_selector("input", timeout=15000)

            print("Mengisi kredensial username & password...")
            page.locator('input[id="username"], input[type="text"], input[name="username"]').first.fill(STOCKBIT_USERNAME)
            page.locator('input[id="password"], input[type="password"], input[name="password"]').first.fill(STOCKBIT_PASSWORD)
            
            print("Mengklik tombol login...")
            page.locator('button[type="submit"], input[type="submit"], button:has-text("Log In")').first.click()
            
            print("\n=========================================================")
            print("⏳ MENUNGGU VERIFIKASI...")
            print("JIKA ADA NOTIFIKASI DI GOOGLE PIXEL 6 ANDA, KLIK 'YES, IT'S ME' SEKARANG!")
            print("Skrip akan menunggu maksimal 60 detik...")
            print("=========================================================")
            
            for i in range(30):
                if captured_token:
                    break
                page.wait_for_timeout(2000)
                if i > 0 and i % 5 == 0:
                    print(f"... masih menunggu persetujuan di HP ({i*2} detik berlalu)")
            
        except Exception as e:
            print(f"\n❌ Terjadi kesalahan saat menavigasi: {e}")
        finally:
            context.close()
            
        return captured_token

if __name__ == "__main__":
    if not STOCKBIT_USERNAME or not STOCKBIT_PASSWORD:
        print("❌ STOCKBIT_USERNAME dan STOCKBIT_PASSWORD belum diatur di .env")
        sys.exit(1)

    new_token = get_bearer_token()

    if new_token:
        print(f"\n✅ Token berhasil diamankan! (Dimulai dengan: {new_token[:15]}...)")
        set_key(dotenv_path=ENV_PATH, key_to_set="BROKER_API_TOKEN", value_to_set=new_token)
        
        print("🔄 Memulai proses pipeline backfill otomatis...")
        try:
            # PERBAIKAN 2: input="y\n" akan otomatis "mengetik" huruf Y saat ditanya "Lanjutkan? [Y/n]"
            subprocess.run(["python3", "backfill_monthly.py", "--universe", "all", "--rate-limit", "12.0"], input="y\n", text=True, check=True)
            print("🎉 Eksekusi harian selesai dengan sempurna!")
        except subprocess.CalledProcessError as e:
            print(f"❌ Pipeline backfill mengalami kegagalan eksekusi: {e}")
            sys.exit(1)
    else:
        print("\n❌ Gagal mendapatkan token. Waktu tunggu habis atau kredensial salah.")
        sys.exit(1)

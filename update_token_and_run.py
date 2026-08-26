#!/usr/bin/env python3
import os
import sys
from pathlib import Path
from dotenv import set_key, load_dotenv
from playwright.sync_api import sync_playwright

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src")) # Pastikan folder src terbaca

_ENV_PATH = _ROOT / ".env"
_SESSION_DIR = _ROOT / "browser_session" 
load_dotenv(_ENV_PATH)

STOCKBIT_USERNAME = os.getenv("STOCKBIT_USERNAME")
STOCKBIT_PASSWORD = os.getenv("STOCKBIT_PASSWORD")

def get_bearer_token():
    print(f"Membuka peramban dengan sesi tersimpan di folder '{_SESSION_DIR.name}'...")
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
            print("Mengecek sesi lama...")
            page.goto("https://stockbit.com/#/stream", wait_until="domcontentloaded", timeout=30000)
            for _ in range(5):
                if captured_token: return captured_token
                page.wait_for_timeout(1000)

            print("Sesi kosong. Login ulang...")
            page.goto("https://stockbit.com/login", wait_until="domcontentloaded")
            page.wait_for_selector("input", timeout=15000)
            page.locator('input[id="username"], input[type="text"], input[name="username"]').first.fill(STOCKBIT_USERNAME)
            page.locator('input[id="password"], input[type="password"], input[name="password"]').first.fill(STOCKBIT_PASSWORD)
            page.locator('button[type="submit"], input[type="submit"], button:has-text("Log In")').first.click()
            
            for _ in range(30):
                if captured_token: break
                page.wait_for_timeout(2000)
        except Exception as e:
            print(f"❌ Terjadi kesalahan: {e}")
        finally:
            context.close()
        return captured_token

if __name__ == "__main__":
    new_token = get_bearer_token()

    if new_token:
        print(f"✅ Token berhasil diamankan!")
        set_key(dotenv_path=_ENV_PATH, key_to_set="BROKER_API_TOKEN", value_to_set=new_token)
        
        # Panggil Pipeline Langsung Tanpa Subprocess
        os.environ["BROKER_API_TOKEN"] = new_token
        from idx_bandarmology import config, pipeline, broker_api
        
        config.BROKER_API_TOKEN = new_token
        broker_api.set_rate_limit(12.0) # Rate limit agresif
        
        print("🔄 Memulai sinkronisasi harian terbaru...")
        # Mode run() ini setara dengan tombol "Run latest pipeline to today" di dashboard Anda
        result = pipeline.run(universe_mode="all", price_period="3d")
        
        print(f"🎉 Eksekusi harian selesai! Tersimpan: {result['n_broker']} broker rows, {result.get('n_activity', 0)} activity rows.")
    else:
        print("❌ Gagal mendapatkan token.")
        sys.exit(1)

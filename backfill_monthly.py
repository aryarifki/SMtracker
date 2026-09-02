#!/usr/bin/env python3
"""Backfill broker data per bulan dengan progress tracking & auto-token renew."""

from __future__ import annotations

import argparse
import json
import sys
import os
import time
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

# Injector Token
from dotenv import set_key, load_dotenv
from playwright.sync_api import sync_playwright

# ── path setup ─────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from idx_bandarmology import pipeline, storage, universe as universe_mod
from idx_bandarmology.broker_api import set_rate_limit
# Mengimpor config agar kita bisa menimpa (hot-swap) token di memori secara langsung
try:
    from idx_bandarmology import config
except ImportError:
    config = None

# ── konfigurasi ────────────────────────────────────────────────────────────
_PROGRESS_FILE = _ROOT / "data" / "backfill_progress.json"
_PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
_ENV_PATH = _ROOT / ".env"
_SESSION_DIR = _ROOT / "browser_session"

_PAUSE_BETWEEN_MONTHS = 15

# ── helper progress ────────────────────────────────────────────────────────
def load_progress() -> dict:
    if _PROGRESS_FILE.exists():
        try:
            data = json.loads(_PROGRESS_FILE.read_text(encoding="utf-8"))
            if "completed" in data and isinstance(data["completed"], list):
                print("🔄 Melakukan migrasi format log progress lama (default ke idx80)...")
                return {
                    "started_at": data.get("started_at"),
                    "last_run": data.get("last_run"),
                    "universes": {"idx80": {"completed": data["completed"], "failed": data.get("failed", {})}}
                }
            return data
        except json.JSONDecodeError:
            pass
    return {"started_at": None, "last_run": None, "universes": {}}

def save_progress(p: dict) -> None:
    p["last_run"] = datetime.now().isoformat()
    _PROGRESS_FILE.write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")

def get_universe_progress(progress: dict, universe: str) -> dict:
    if "universes" not in progress:
        progress["universes"] = {}
    if universe not in progress["universes"]:
        progress["universes"][universe] = {"completed": [], "failed": {}}
    return progress["universes"][universe]

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
        os.system("sed -i '/BROKER_API_TOKEN/d' /opt/SMtracker/.env")
        if "BROKER_API_TOKEN" in os.environ:
            del os.environ["BROKER_API_TOKEN"]

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
                    page.screenshot(path="/opt/SMtracker/debug_backfill_login.png")
                    print("   📸 Screenshot kegagalan disimpan sebagai debug_backfill_login.png")
                    
        except Exception as e:
            print(f"   ❌ Gagal navigasi saat renew token: {e}")
            try:
                page.screenshot(path="/opt/SMtracker/debug_backfill_error.png")
                print("   📸 Screenshot error disimpan sebagai debug_backfill_error.png")
            except:
                pass
        finally:
            context.close()
            
    if captured_token:
        print(f"   ✅ Token darurat berhasil diamankan! ({captured_token[:15]}...)")
        set_key(dotenv_path=_ENV_PATH, key_to_set="BROKER_API_TOKEN", value_to_set=captured_token)
        os.environ["BROKER_API_TOKEN"] = captured_token
        if config is not None:
            config.BROKER_API_TOKEN = captured_token  # Hot-swap ke memori
        return True
    
    print("   ❌ Gagal mendapatkan token darurat.")
    return False

# ── eksekusi utama ─────────────────────────────────────────────────────────
def run_backfill_month(
    month_label: str, start: date, end: date, universe_mode: str, 
    rate_limit: float, refresh_prices: bool, progress: dict,
) -> bool:
    print(f"\n{'='*60}")
    print(f"📅 Memproses: {month_label}  ({start}  →  {end})")
    print(f"{'='*60}")

    uni_prog = get_universe_progress(progress, universe_mode)
    if month_label in uni_prog["completed"]:
        print(f"   ✅ Sudah selesai untuk universe '{universe_mode}'. Skip.")
        return True
    if start > end:
        print(f"   ⚠️  Range tidak valid. Skip.")
        return True

    syms = universe_mod.get_universe(universe_mode)
    if universe_mode == "all":
        idx80_prog = progress.get("universes", {}).get("idx80", {}).get("completed", [])
        if month_label in idx80_prog:
            print("   💡 Info: idx80 sudah ada untuk bulan ini. Menghapus idx80 dari daftar request...")
            syms_idx80 = universe_mod.get_universe("idx80")
            syms = [s for s in syms if s not in syms_idx80]

    n_days = (end - start).days + 1
    trading_days = sum(1 for i in range(n_days) if (start + timedelta(days=i)).weekday() < 5)
    print(f"   🎯 Target: {len(syms)} tickers | Hari kerja: ~{trading_days} | Estimasi: {estimate_time(len(syms), trading_days)}")

    max_retries = 3
    for attempt in range(max_retries):
        try:
            t0 = time.monotonic()
            set_rate_limit(rate_limit)

            result = pipeline.backfill_broker_history(
                universe_mode=universe_mode, 
                start_date=start,
                end_date=end,
                refresh_prices=refresh_prices,
                price_period="1y",
            )

            elapsed = time.monotonic() - t0
            print(f"   ✅ Selesai dalam {elapsed/60:.1f} menit")
            print(f"      📊 Broker rows: {result['n_broker']:,} | Activity rows: {result.get('n_activity', 0):,}")

            uni_prog["completed"].append(month_label)
            if month_label in uni_prog["failed"]:
                del uni_prog["failed"][month_label]
            save_progress(progress)
            return True

        except Exception as exc:
            error_msg = str(exc).lower()
            print(f"   ❌ GAGAL pada percobaan {attempt + 1}/{max_retries}: {exc}")
            
            # Cek apakah error disebabkan oleh koneksi terputus (timeout) atau token mati (401/403)
            is_token_issue = any(k in error_msg for k in ["401", "403", "unauthorized", "forbidden", "token", "timeout", "read", "connection"])
            force_clean = any(k in error_msg for k in ["401", "403", "unauthorized", "forbidden"])
            
            if is_token_issue and attempt < max_retries - 1:
                if auto_renew_token(force_clean_session=force_clean):
                    print("   🔁 Mencoba melanjutkan unduhan dengan token baru...")
                    time.sleep(2)
                    continue  # Ulangi loop attempt
                else:
                    break # Gagal renew token, menyerah.
            else:
                uni_prog["failed"][month_label] = str(exc)
                save_progress(progress)
                return False
    return False

def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill broker data per bulan")
    parser.add_argument("--universe", default="idx80", help="Universe yang akan di-backfill")
    parser.add_argument("--months", default="all", help='Bulan: "all", "last3", dsb.')
    parser.add_argument("--rate-limit", type=float, default=8.0)
    parser.add_argument("--no-refresh-prices", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--reset-progress", action="store_true")

    args = parser.parse_args()
    storage.init_db()
    progress = load_progress()

    if args.reset_progress:
        if _PROGRESS_FILE.exists(): _PROGRESS_FILE.unlink()
        print("🗑️  Progress di-reset.")
        return

    if args.status:
        print(f"📋 Backfill Progress\n   Dimulai: {progress.get('started_at') or 'Belum pernah'}")
        for uni, data in progress.get("universes", {}).items():
            print(f"\n   🔹 Universe: {uni.upper()}\n      ✅ Selesai: {len(data['completed'])} bulan")
            for m in data["completed"]: print(f"         - {m}")
        return

    if not progress["started_at"]:
        progress["started_at"] = datetime.now().isoformat()
        save_progress(progress)

    ranges = get_month_ranges(months_back=12)
    selected = parse_month_args(args.months, ranges)
    uni_prog = get_universe_progress(progress, args.universe)

    print(f"\n📅 Total bulan dipilih: {len(selected)}")
    for start, end, label in selected:
        print(f"   {'✅' if label in uni_prog['completed'] else '⏳'} {label}  ({start} ~ {end})")

    remaining = [r for r in selected if r[2] not in uni_prog["completed"]]
    if not remaining:
        print("\n🎉 Semua bulan sudah selesai!")
        return

    print(f"\n⏳ Bulan yang akan dikerjakan: {len(remaining)}")
    confirm = input("Lanjutkan? [Y/n]: ").strip().lower()
    if confirm and confirm not in ("y", "yes", "ya"):
        print("Dibatalkan.")
        return

    for start, end, label in remaining:
        ok = run_backfill_month(label, start, end, args.universe, args.rate_limit, not args.no_refresh_prices, progress)
        if label != remaining[-1][2] and ok:
            time.sleep(_PAUSE_BETWEEN_MONTHS)

if __name__ == "__main__":
    main()

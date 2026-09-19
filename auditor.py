#!/usr/bin/env python3
"""
Auditor Engine — Memonitor sinyal OPEN, menentukan WIN/LOSS/EXPIRED.
Data ini akan menjadi 'Training Set' untuk model XGBoost di masa depan.
"""

import sys
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from sqlalchemy import text

# ── PATH SETUP ──
_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from idx_bandarmology import storage

# Import modul Telegram Bot
import telegram_bot

MAX_HOLD_DAYS = 20 # Maksimal hari menahan saham (Trading Days)

def get_open_signals():
    """Ambil semua sinyal yang masih berstatus OPEN."""
    q = text("SELECT id, ticker, signal_date, entry_price, tp_price, sl_price FROM signals WHERE status = 'OPEN'")
    with storage.engine.connect() as conn:
        return conn.execute(q).fetchall()

def get_prices_since(ticker: str, start_date: date):
    """Ambil data harga harian (High, Low, Close) sejak tanggal sinyal diberikan."""
    q = text("""
        SELECT date, high, low, close 
        FROM prices 
        WHERE ticker = :ticker AND date >= :start_date 
        ORDER BY date ASC
    """)
    with storage.engine.connect() as conn:
        return conn.execute(q, {"ticker": ticker, "start_date": start_date}).fetchall()

def update_signal_status(sig_id: str, status: str, exit_price: float, exit_date: date, days_held: int):
    """Update status sinyal di database (WIN/LOSS/EXPIRED)."""
    q = text("""
        UPDATE signals 
        SET status = :status, exit_price = :exit_price, exit_date = :exit_date, days_held = :days_held, updated_at = CURRENT_TIMESTAMP
        WHERE id = :id
    """)
    with storage.engine.begin() as conn:
        conn.execute(q, {
            "id": sig_id, "status": status, "exit_price": exit_price, 
            "exit_date": exit_date, "days_held": days_held
        })

def audit_signals():
    print(f"\n{'='*58}")
    print(f"🤖 BandarAI Auditor Engine — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*58}")
    
    open_sigs = get_open_signals()
    if not open_sigs:
        print("ℹ️  Tidak ada sinyal OPEN untuk diaudit.")
        return

    print(f"🔍 Mengaudit {len(open_sigs)} sinyal OPEN...\n")
    
    wins, losses, expired = 0, 0, 0

    for sig in open_sigs:
        sig_id, ticker, sig_date, entry, tp, sl = sig
        sig_date = sig_date if isinstance(sig_date, date) else sig_date.date()
        
        prices = get_prices_since(ticker, sig_date)
        if not prices:
            continue

        days_held = len(prices) - 1 # Kurangi 1 karena hari pertama adalah hari entry
        
        # Cek apakah TP atau SL tersentuh
        hit_tp = False
        hit_sl = False
        exit_price = float(prices[-1].close) # Default exit = harga close terakhir
        exit_date = prices[-1].date

        for p in prices[1:]: # Skip hari pertama (entry day)
            if p.high >= tp:
                hit_tp = True
                exit_price = float(tp)
                exit_date = p.date
                break
            if p.low <= sl:
                hit_sl = True
                exit_price = float(sl)
                exit_date = p.date
                break

        if hit_tp:
            update_signal_status(sig_id, "WIN", exit_price, exit_date, days_held)
            
            # ── KIRIM NOTIFIKASI TELEGRAM ──
            try:
                msg = telegram_bot.format_audit_message("WIN", ticker, float(entry), exit_price, days_held)
                telegram_bot.send_message(msg)
            except Exception as tg_err:
                print(f"  ⚠️ [Telegram] Gagal kirim audit WIN {ticker}: {tg_err}")
                
            print(f"  ✅ WIN  | {ticker} | Entry: {entry:,.0f} | TP: {tp:,.0f} | {days_held}d")
            wins += 1
            
        elif hit_sl:
            update_signal_status(sig_id, "LOSS", exit_price, exit_date, days_held)
            
            # ── KIRIM NOTIFIKASI TELEGRAM ──
            try:
                msg = telegram_bot.format_audit_message("LOSS", ticker, float(entry), exit_price, days_held)
                telegram_bot.send_message(msg)
            except Exception as tg_err:
                print(f"  ⚠️ [Telegram] Gagal kirim audit LOSS {ticker}: {tg_err}")
                
            print(f"  ❌ LOSS | {ticker} | Entry: {entry:,.0f} | SL: {sl:,.0f} | {days_held}d")
            losses += 1
            
        elif days_held >= MAX_HOLD_DAYS:
            update_signal_status(sig_id, "EXPIRED", exit_price, exit_date, days_held)
            
            # ── KIRIM NOTIFIKASI TELEGRAM ──
            try:
                msg = telegram_bot.format_audit_message("EXPIRED", ticker, float(entry), exit_price, days_held)
                telegram_bot.send_message(msg)
            except Exception as tg_err:
                print(f"  ⚠️ [Telegram] Gagal kirim audit EXP {ticker}: {tg_err}")
                
            print(f"  ⏰ EXP  | {ticker} | Entry: {entry:,.0f} | Exit: {exit_price:,.0f} | {days_held}d")
            expired += 1

    print(f"\n📊 Audit Selesai: {wins}W / {losses}L / {expired} Exp")

if __name__ == "__main__":
    audit_signals()

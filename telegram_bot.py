"""Telegram Bot Notification Module for The Investowl System."""

import os
import requests
from datetime import datetime
from sqlalchemy import text
from idx_bandarmology import storage

BOT_TOKEN = "8924668232:AAHakfMgHCt8YgLDN2TivsL-FJnSEi3NVX8"
CHAT_ID = "-1004373620684"

def send_message(text: str):
    """Mengirim pesan teks ke Telegram."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"  ⚠️ [Telegram] Gagal kirim pesan: {e}")

def get_daily_stats() -> dict:
    """Menghitung statistik WIN/LOSS secara real-time dari tabel signals."""
    try:
        q = text("""
            SELECT 
                COUNT(CASE WHEN status = 'WIN' THEN 1 END) as wins,
                COUNT(CASE WHEN status = 'LOSS' THEN 1 END) as losses,
                COUNT(CASE WHEN status IN ('WIN', 'LOSS', 'EXPIRED') THEN 1 END) as total_closed
            FROM signals
            WHERE status IN ('WIN', 'LOSS', 'EXPIRED')
        """)
        with storage.engine.connect() as conn:
            row = conn.execute(q).fetchone()
            
        wins = row[0] if row else 0
        losses = row[1] if row else 0
        total_closed = row[2] if row else 0
        
        q_pnl = text("""
            SELECT 
                AVG(((exit_price - entry_price) / entry_price) * 100) as avg_ret,
                AVG(CASE WHEN exit_price >= tp_price THEN ((exit_price - entry_price) / entry_price) * 100 END) as avg_win,
                AVG(CASE WHEN exit_price <= sl_price THEN ((exit_price - entry_price) / entry_price) * 100 END) as avg_loss,
                MAX(((exit_price - entry_price) / entry_price) * 100) as best,
                MIN(((exit_price - entry_price) / entry_price) * 100) as worst
            FROM signals
            WHERE status IN ('WIN', 'LOSS', 'EXPIRED') AND entry_price > 0 AND exit_price IS NOT NULL
        """)
        with storage.engine.connect() as conn:
            pnl_row = conn.execute(q_pnl).fetchall()[0]

        wr = (wins / total_closed * 100) if total_closed > 0 else 0
        avg_ret = float(pnl_row[0]) if pnl_row and pnl_row[0] is not None else 0
        avg_win = float(pnl_row[1]) if pnl_row and pnl_row[1] is not None else 0
        avg_loss = float(pnl_row[2]) if pnl_row and pnl_row[2] is not None else 0
        best = float(pnl_row[3]) if pnl_row and pnl_row[3] is not None else 0
        worst = float(pnl_row[4]) if pnl_row and pnl_row[4] is not None else 0

        return {
            "wr": wr, "wins": wins, "losses": losses, "total": total_closed,
            "avg_ret": avg_ret, "avg_win": avg_win, "avg_loss": avg_loss,
            "best": best, "worst": worst
        }
    except Exception as e:
        print(f"  ⚠️ [Telegram] Gagal ambil stats: {e}")
        return {"wr": 0, "wins": 0, "losses": 0, "total": 0, "avg_ret": 0, "avg_win": 0, "avg_loss": 0, "best": 0, "worst": 0}

def format_signal_message(r: dict) -> str:
    """Merapikan format pesan sinyal BUY baru."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    wp = r.get('wp', 'N/A')
    sm_notes = r.get('smart_money_notes', 'Netral')
    sec_notes = r.get('sector_notes', 'Neutral')
    
    msg = f"""🟢 <b>Sinyal BUY Baru</b> — {r['ticker']}
📅 {datetime.now().strftime("%d %b %Y")}

💰 <b>Entry</b>: Rp {r['lp']:,.0f}
🎯 <b>TP</b>: Rp {r['tp']:,.0f} (+{r['tp_pct']:.1f}%)
🛑 <b>SL</b>: Rp {r['sl']:,.0f} (-{r['sl_pct']:.1f}%)

📊 <b>Composite Score</b>: {r['score']}/100
📈 <b>Wyckoff Phase</b>: {wp}
🔥 <b>Smart Money</b>: {sm_notes}
🏭 <b>Sektor</b>: {sec_notes}

⏱️ {ts}
🤖 <i>The Investowl Bot</i>"""
    return msg

def format_audit_message(status: str, ticker: str, entry: float, exit_price: float, days_held: int) -> str:
    """Merapikan format pesan hasil Audit (WIN/LOSS/EXPIRED) + Daily Track Record."""
    pnl = ((exit_price - entry) / entry) * 100 if entry > 0 else 0
    emoji = "✅" if status == "WIN" else "❌" if status == "LOSS" else "⏰"
    
    stats = get_daily_stats()
    wr_emoji = "🟢" if stats['wr'] >= 60 else "🔴" if stats['wr'] < 50 else "🟡"
    
    msg = f"""{emoji} <b>{status}</b> — {ticker}

Entry: Rp {entry:,.0f}
Exit: Rp {exit_price:,.0f}
P&L: {pnl:+.1f}%
Durasi: {days_held} hari trading

📊 <b>DAILY TRACK RECORD — {datetime.now().strftime("%d %b %Y")}</b>
{wr_emoji} <b>Win Rate: {stats['wr']:.1f}%</b>  ({stats['wins']}W / {stats['losses']}L)
📈 Avg Return: {stats['avg_ret']:+.2f}%
✅ Avg Win: +{stats['avg_win']:.1f}%
❌ Avg Loss: {stats['avg_loss']:.1f}%
🏆 Best Trade: +{stats['best']:.1f}%
💔 Worst Trade: {stats['worst']:.1f}%

🤖 <i>The Investowl Bot</i>"""
    return msg

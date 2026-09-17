import os
import sys
import json
import numpy as np
import pandas as pd
import warnings
import time
from datetime import datetime, timedelta
from pathlib import Path

# ── PATH SETUP ──
_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from idx_bandarmology import storage, config
from sqlalchemy import text

warnings.filterwarnings("ignore")

# ── KONFIGURASI ──
WATCHLIST = config.WATCHLIST
MIN_SCORE_TO_SIGNAL = getattr(config, "MIN_SCORE_TO_SIGNAL", 65)
MIN_PRICE_IDR = getattr(config, "MIN_PRICE_IDR", 50)
MIN_VOLUME_LOT = getattr(config, "MIN_VOLUME_LOT", 10000)

TICKER_BLACKLIST = {"TLKM", "ICBP", "BREN", "MIKA", "KLBF"}
TICKER_PENALTY = {"BSDE": -8, "CTRA": -8, "ASII": -6, "SMRA": -8, "AKRA": -8, "MYOR": -8}
TICKER_BONUS = {"MDKA": +8, "ADRO": +5, "PTBA": +3, "ULTJ": +3}

# ══════════════════════════════════════════════════════
#  DB LOADERS (Menggantikan yfinance)
# ══════════════════════════════════════════════════════

def load_price_from_db(ticker: str, days: int = 365) -> pd.DataFrame | None:
    """Ambil data OHLCV historis dari tabel prices di PostgreSQL."""
    try:
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        df = storage.read_prices(tickers=[ticker], start_date=start_date, end_date=end_date)
        
        if df is None or df.empty or len(df) < 20:
            return None
            
        df = df.rename(columns={
            "open": "open", "high": "high", "low": "low",
            "close": "close", "volume": "volume"
        })
        df = df[["date", "open", "high", "low", "close", "volume"]].dropna()
        df = df.sort_values("date").reset_index(drop=True)
        
        # Normalize volume ke lot jika ternyata dalam bentuk shares
        if df["volume"].median() > 5e8:
            df["volume"] = df["volume"] / 100
            
        return df
    except Exception:
        return None

def load_ihsg_from_db(days: int = 365) -> pd.DataFrame | None:
    """Ambil data IHSG dari tabel idx_index_summary. Fallback ke yfinance jika data < 50 hari."""
    try:
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        
        q = text("""
            SELECT date, close 
            FROM idx_index_summary 
            WHERE index_code = 'COMPOSITE' AND date >= :start AND date <= :end
            ORDER BY date ASC
        """)
        with storage.engine.connect() as conn:
            df = pd.read_sql(q, conn, params={"start": start_date, "end": end_date})
            
        if not df.empty and len(df) >= 50:
            return df
            
        # ── FALLBACK KE YFINANCE JIKA DATA DB KURANG DARI 50 HARI ──
        print("  ⚠️ [IHSG] Data di DB kurang dari 50 hari. Fallback ke yfinance...")
        import yfinance as yf
        raw = yf.download("^JKSE", period="1y", interval="1d", progress=False, auto_adjust=True)
        if raw is not None and not raw.empty:
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            # Reset index agar 'Date' menjadi kolom biasa
            raw = raw.reset_index()
            df_yf = raw.rename(columns={"Date": "date", "Close": "close"})[["date", "close"]].dropna()
            if len(df_yf) >= 50:
                return df_yf
                
        return None
    except Exception as e:
        print(f"  ❌ [Debug IHSG] Error mengambil IHSG: {e}")
        return None
        
def quick_fundamental_check_from_db(ticker: str) -> dict:
    """Cek PER & DER dari tabel financial_ratios."""
    try:
        q = text("""
            SELECT per, der 
            FROM financial_ratios 
            WHERE code = :ticker 
            ORDER BY year DESC, quarter DESC LIMIT 1
        """)
        with storage.engine.connect() as conn:
            row = conn.execute(q, {"ticker": ticker}).fetchone()
            
        if not row:
            return {"pass": True, "penalty": 0, "notes": "No Fund Data"}
            
        per = float(row[0]) if row[0] else 0
        der = float(row[1]) if row[1] else 0
        fails = 0
        notes = []
        
        if per > 60:
            fails += 1
            notes.append(f"PE tinggi ({per:.0f}x)")
        if per < 0:
            fails += 2
            notes.append("Rugi (PE negatif)")
        if der > 5.0:
            fails += 1
            notes.append(f"DER {der:.1f}x (berutang banyak)")
            
        penalty = min(15, fails * 7)
        return {"pass": fails <= 1, "penalty": penalty, "notes": " · ".join(notes) if notes else "OK"}
    except Exception:
        return {"pass": True, "penalty": 0, "notes": "Error"}

def get_smart_money_score(ticker: str) -> dict:
    """Ambil skor Bandarmology dari tabel broker_flow & analytics_foreign_flow."""
    try:
        # Kita tambahkan total_value ke query untuk menghitung rasio persentase
        q_broker = text("""
            SELECT bandar_signal, foreign_net_broker, total_value 
            FROM broker_flow 
            WHERE ticker = :ticker 
            ORDER BY date DESC LIMIT 1
        """)
        q_analytics = text("""
            SELECT features 
            FROM analytics_foreign_flow 
            WHERE ticker = :ticker 
            ORDER BY latest_date DESC LIMIT 1
        """)
        
        with storage.engine.connect() as conn:
            b_row = conn.execute(q_broker, {"ticker": ticker}).fetchone()
            a_row = conn.execute(q_analytics, {"ticker": ticker}).fetchone()
            
        sm_score = 50.0
        notes = []
        
        if b_row:
            sig = b_row[0] or "NEUTRAL"
            fnet = float(b_row[1] or 0)
            total_val = float(b_row[2] or 0)
            
            if sig in ("STRONG_ACCUMULATION",):
                sm_score += 25
                notes.append("Bandar Akumulasi Kuat 🔥")
            elif sig in ("ACCUMULATION", "NET_BUY"):
                sm_score += 15
                notes.append("Bandar Akumulasi")
            elif sig in ("STRONG_DISTRIBUTION",):
                sm_score -= 25
                notes.append("Bandar Distribusi 💀")
            elif sig in ("DISTRIBUTION", "NET_SELL"):
                sm_score -= 15
                notes.append("Bandar Distribusi")
                
            # ── PERBAIKAN: RASIO PERSENTASE ALIRAN ASING ──
            # Cegah pembagian dengan nol jika total_val = 0
            if total_val > 0:
                fnet_pct = (fnet / total_val) * 100
                
                # Threshold 1.5% (Berlaku untuk blue chip maupun saham kecil)
                if fnet_pct > 1.5:
                    sm_score += 15
                    notes.append(f"Asing Buy {fnet_pct:.1f}%")
                elif fnet_pct < -1.5:
                    sm_score -= 15
                    notes.append(f"Asing Sell {fnet_pct:.1f}%")
                
        if a_row and a_row[0]:
            features = a_row[0] if isinstance(a_row[0], dict) else json.loads(a_row[0])
            zscore = features.get("foreign_zscore", 0)
            if zscore > 2.0:
                sm_score += 15
                notes.append(f"Foreign Anomaly Z:{zscore:.1f}")
                
        return {
            "score": int(np.clip(round(sm_score), 0, 100)),
            "notes": " · ".join(notes) if notes else "Netral"
        }
    except Exception:
        return {"score": 50, "notes": "No SM Data"}
# ══════════════════════════════════════════════════════
#  TECHNICAL INDICATORS (Logika Asli Dipertahankan)
# ══════════════════════════════════════════════════════

def cmf(df, p=14):
    hl  = df["high"] - df["low"]
    clv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl.replace(0, np.nan)
    return (clv * df["volume"]).rolling(p).sum() / df["volume"].rolling(p).sum()

def obv(df):
    return (np.sign(df["close"].diff()).fillna(0) * df["volume"]).cumsum()

def mfi(df, p=14):
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    mf  = tp * df["volume"]
    pos = mf.where(tp > tp.shift(1), 0).rolling(p).sum()
    neg = mf.where(tp < tp.shift(1), 0).rolling(p).sum()
    return (100 - 100 / (1 + pos / neg.replace(0, np.nan))).fillna(50)

def atr(df, p=14):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(p).mean()

def rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return (100 - 100 / (1 + g / l.replace(0, np.nan))).fillna(50)

def wyckoff_phase(df, c, o):
    n   = len(df)
    p   = df["close"]
    vol = df["volume"]
    seg = max(n // 3, 5)
    tr  = (p.iloc[-seg:].mean() - p.iloc[:seg].mean()) / (p.iloc[:seg].mean() + 1e-9)
    r20 = (p.tail(20).max() - p.tail(20).min()) / (p.tail(20).mean() + 1e-9)
    vol_ma20   = vol.rolling(20).mean().iloc[-1]
    vol_ratio  = vol.tail(5).max() / (vol_ma20 + 1)
    vol_climax = vol_ratio >= 2.5
    ob_rising  = float(o.iloc[-1]) > float(o.iloc[-min(10, n-1)])
    price_pos  = (p.iloc[-1] - p.tail(20).min()) / ((p.tail(20).max() - p.tail(20).min()) + 1e-9)

    if tr < -0.06 and vol_climax and price_pos < 0.35:
        return "A", "Selling Climax", min(90, 60 + int(abs(tr)*200))
    if tr > 0.10 and ob_rising and price_pos > 0.75:
        return "E", "Markup", min(88, 55 + int(tr*100))
    if tr > 0.03 and ob_rising and vol_ratio >= 1.5 and price_pos > 0.65:
        return "D", "Sign of Strength", min(85, 50 + int(tr*100))

    p_min20 = p.tail(20).min()
    p_min10 = p.tail(10).min()
    broke   = p_min10 <= p_min20 * 1.002
    recov   = float(p.iloc[-1]) > float(p.tail(5).min()) * 1.015
    lookback = min(180, n)
    v_lb    = vol.iloc[-lookback:]
    v_ma_lb = v_lb.rolling(20).mean()
    vol_hist = (v_lb / v_ma_lb.replace(0, np.nan)).fillna(0)
    p_lb    = p.iloc[-lookback:]
    had_a   = bool((vol_hist >= 2.5).any() and
                   (p_lb.iloc[0] - p_lb.min()) / (p_lb.iloc[0] + 1) > 0.06)
    if broke and recov and vol_climax and tr < 0.05 and r20 < 0.15 and had_a:
        return "C", "Spring ⭐", min(85, 50 + int(vol_ratio*8))
    if r20 < 0.10 and abs(tr) < 0.05:
        return "B", "Building Cause", min(68, 35 + int((0.10-r20)*200))
    return "B", "Indeterminate", 40

def detect_vcp_grade(df) -> str:
    if len(df) < 60: return "NONE"
    close = df["close"]
    vol = df["volume"]
    lp = float(close.iloc[-1])
    ma50 = float(close.rolling(50).mean().iloc[-1]) if len(df) >= 50 else lp
    ma150 = float(close.rolling(150).mean().iloc[-1]) if len(df) >= 150 else lp
    ma200 = float(close.rolling(200).mean().iloc[-1]) if len(df) >= 200 else lp
    above = lp > ma50 and lp > ma150
    tscore = sum([lp > ma50, lp > ma150, lp > ma200])
    r15 = (df["high"].tail(15).max() - df["low"].tail(15).min()) / lp * 100
    r30 = (df["high"].iloc[-45:-15].max() - df["low"].iloc[-45:-15].min()) / lp * 100 if len(df)>=45 else r15*2
    r60 = (df["high"].iloc[-90:-45].max() - df["low"].iloc[-90:-45].min()) / lp * 100 if len(df)>=90 else r30*1.5
    contracting = r15 < r30 * 0.7 and r30 < r60 * 0.85
    vol_dry = float(vol.tail(10).mean()) < float(vol.tail(30).mean()) * 0.75
    tight = r15 < 8
    if above and contracting and vol_dry and tight and tscore >= 2: return "A"
    elif above and contracting and vol_dry and tscore >= 2: return "B"
    elif above and (contracting or vol_dry) and tscore >= 1: return "C"
    return "NONE"

# ══════════════════════════════════════════════════════
#  HARD GATES & SCORING ENGINE
# ══════════════════════════════════════════════════════

def check_hard_gates(df, wp: str, cmf_v: float, mfi_v: float, obv_s: pd.Series, vr: float, regime: dict = None) -> tuple:
    n = len(df)
    p = df["close"]

    if wp == "A": return False, "Phase A (Selling Climax) — bukan area entry"
    if wp == "E": return False, "Phase E (Markup lanjut) — terlambat masuk"

    market_bearish = (regime or {}).get("regime") in ("BEAR", "CRASH", "RISK_OFF")
    if len(p) >= 50 and not market_bearish:
        ma50 = float(p.rolling(50).mean().iloc[-1])
        lp   = float(p.iloc[-1])
        if wp == "B" and lp < ma50 * 0.98: return False, f"Phase B di bawah MA50 — downtrend"
        if wp in ("C",):
            ma20 = float(p.rolling(20).mean().iloc[-1]) if len(p) >= 20 else lp
            if lp < ma20 * 0.92: return False, f"Phase C terlalu jauh di bawah MA20"

    if wp == "B":
        if cmf_v < 0.05: return False, f"Phase B + CMF {cmf_v:+.3f} < 0.05 (inflow lemah)"
        if mfi_v > 75: return False, f"Phase B + MFI {mfi_v:.0f} > 75 (belum oversold)"
        obv_up_10 = float(obv_s.iloc[-1]) > float(obv_s.iloc[-min(10, n-1)])
        if not obv_up_10: return False, "Phase B + OBV tidak rising 10 hari"
        if vr < 1.0: return False, f"Phase B + volume {vr:.1f}x (perlu konfirmasi >= 1.0x)"

    rsi_v = float(rsi(p).iloc[-1])
    if wp == "B" and rsi_v > 75: return False, f"Phase B + RSI {rsi_v:.0f} > 65 (overbought)"
    if wp == "D" and rsi_v > 72: return False, f"Phase D + RSI {rsi_v:.0f} > 72 (terlalu extend)"
    if wp not in ("C",) and rsi_v < 20: return False, f"RSI {rsi_v:.0f} < 20 (extreme panic)"

    if vr < 0.70 and wp != "C": return False, f"Volume {vr:.1f}x terlalu lemah"
    return True, ""

def get_market_regime(ihsg_df) -> dict:
    if ihsg_df is None or len(ihsg_df) < 50:
        return {"regime":"UNKNOWN","multiplier":1.0,"ok":True,"desc":"IHSG unavailable"}
    p = ihsg_df["close"]
    n = len(p)
    ma50 = p.rolling(50).mean()
    ma200 = p.rolling(min(200,n)).mean()
    lp = float(p.iloc[-1])
    ret60 = (lp / float(p.iloc[-min(60,n-1)]) - 1) * 100
    peak = float(p.tail(252).max()) if n >= 252 else float(p.max())
    dd = (lp - peak) / peak * 100
    above_ma50 = lp > float(ma50.iloc[-1])
    above_ma200 = lp > float(ma200.iloc[-1])
    ma50_up = float(ma50.iloc[-1]) > float(ma50.iloc[-min(20,n-1)])

    if dd < -40 or ret60 < -28: return {"regime":"CRASH", "multiplier":0.65, "ok":True, "desc":f"IHSG crash ({dd:.1f}%)"}
    elif dd < -20 or ret60 < -15: return {"regime":"BEAR", "multiplier":0.85, "ok":True, "desc":f"IHSG bear ({dd:.1f}%)"}
    elif (dd < -10 and not above_ma50) or ret60 < -12: return {"regime":"RISK_OFF", "multiplier":0.75,"ok":True, "desc":f"IHSG risk-off"}
    elif dd < -5 and above_ma200 and ret60 < -3: return {"regime":"CORRECTION", "multiplier":0.90,"ok":True, "desc":f"IHSG koreksi"}
    elif above_ma50 and above_ma200 and ma50_up: return {"regime":"BULL", "multiplier":1.05,"ok":True, "desc":f"IHSG uptrend"}
    return {"regime":"MIXED", "multiplier":0.90,"ok":True, "desc":f"IHSG mixed"}

def calc_rs(df, ihsg_df) -> dict:
    if ihsg_df is None or df is None: return {"score":50,"interp":"—","rs20":100}
    try:
        merged = df[["close"]].rename(columns={"close":"stock"}).join(
            ihsg_df[["close"]].rename(columns={"close":"ihsg"}), how="inner")
        if len(merged) < 25: return {"score":50,"interp":"—","rs20":100}
        n = len(merged)
        sr = (float(merged["stock"].iloc[-1]) / float(merged["stock"].iloc[-min(20,n-1)]) - 1) * 100
        ir = (float(merged["ihsg"].iloc[-1])  / float(merged["ihsg"].iloc[-min(20,n-1)])  - 1) * 100
        rs20 = (1 + sr/100) / (1 + ir/100) * 100
        score = 50.0 + float(np.clip((rs20 - 100) * 1.5, -20, 20))
        score = int(np.clip(round(score), 0, 100))
        if rs20 > 108: interp = "OUTPERFORM ▲"
        elif rs20 < 93: interp = "UNDERPERFORM ▼"
        else: interp = "IN LINE →"
        return {"score":score, "interp":interp, "rs20":round(rs20,1)}
    except:
        return {"score":50,"interp":"—","rs20":100}

def compute_score_v4(df, ticker: str, ihsg_df, regime: dict) -> dict:
    n = len(df)
    p = min(14, max(7, n // 2))

    c_ = cmf(df, p=p)
    o_ = obv(df)
    m_ = mfi(df, p=p)
    a_ = atr(df, p=14)
    r_ = rsi(df["close"], p=14)
    wp, wn, wconf = wyckoff_phase(df, c_, o_)

    lp    = float(df["close"].iloc[-1])
    cmf_v = float(c_.iloc[-1]) if not pd.isna(c_.iloc[-1]) else 0.0
    mfi_v = float(m_.iloc[-1]) if not pd.isna(m_.iloc[-1]) else 50.0
    rsi_v = float(r_.iloc[-1]) if not pd.isna(r_.iloc[-1]) else 50.0
    atr_v = float(a_.iloc[-1]) if not pd.isna(a_.iloc[-1]) else lp * 0.02
    obv_up = float(o_.iloc[-1]) > float(o_.iloc[-min(10, n-1)])
    av    = float(df["volume"].tail(20).mean())
    vr    = float(df["volume"].iloc[-1]) / av if av > 0 else 1.0

    if atr_v == 0: return None

    passes, gate_reason = check_hard_gates(df, wp, cmf_v, mfi_v, o_, vr, regime)
    if not passes:
        return {"blocked": True, "reason": gate_reason, "wp": wp, "ticker": ticker}

    ts = 50.0
    ts += float(np.clip(cmf_v * 120, -24, 24))
    if mfi_v < 30: ts += 15
    elif mfi_v < 45: ts += 7
    elif mfi_v > 70: ts -= 15
    ts = int(np.clip(round(ts), 0, 100))

    phase_bonus = {"C": 25, "D": 15, "B": 0, "A": -30, "E": -30}.get(wp, 0)
    vcp_grade = detect_vcp_grade(df)
    vcp_s = {"A":90, "B":70, "C":40, "NONE":0}[vcp_grade]
    rs = calc_rs(df, ihsg_df)
    
    # Ambil data dari DB
    sm_data = get_smart_money_score(ticker)
    fund = quick_fundamental_check_from_db(ticker)
    
    ticker_adj = TICKER_PENALTY.get(ticker, 0) + TICKER_BONUS.get(ticker, 0)

    # Weighted Composite (Diperbarui)
    raw = int(np.clip(round(
        ts * 0.35 +          # Teknikal 35%
        sm_data["score"] * 0.35 +  # Smart Money 35% (Ditingkatkan!)
        vcp_s * 0.10 +      # VCP 10%
        rs["score"] * 0.15 + # RS 15%
        50 * 0.05           # Base 5%
    ), 0, 100))

    raw = raw + phase_bonus + ticker_adj - fund["penalty"]
    final = int(np.clip(round(raw * regime.get("multiplier", 1.0)), 0, 100))

    sl = max(round(lp - 1.5 * atr_v, 0), round(float(df["low"].tail(10).min()) * 0.97, 0))
    tp = round(lp + 2.5 * (lp - sl), 0)
    sl_pct = (lp - sl) / lp * 100
    tp_pct = (tp  - lp) / lp * 100

    return {
        "blocked": False, "score": final, "ts": ts,
        "sm_score": sm_data["score"],
        "vcp_grade": vcp_grade, "wp": wp, "wn": wn,
        "cmf_v": round(cmf_v, 4), "mfi_v": round(mfi_v, 1),
        "rsi_v": round(rsi_v, 1), "obv_dir": "Rising ▲" if obv_up else "Falling ▼",
        "vr": round(vr, 2), "lp": lp, "atr_v": round(atr_v, 0),
        "sl": sl, "sl_pct": round(sl_pct, 1), "tp": tp, "tp_pct": round(tp_pct, 1),
        "rs_interp": rs["interp"],
        "smart_money_notes": sm_data["notes"],
        "fund_ok": fund["pass"], "fund_penalty": fund["penalty"],
        "signal_type": "STRONG_BUY" if final >= 78 else "BUY",
        "session": "", "ticker": ticker
    }

# ══════════════════════════════════════════════════════
#  DATABASE INJECTION (ML DUMMY & ANALYTICS)
# ══════════════════════════════════════════════════════

def save_analytics_to_db(candidates):
    """Menyimpan seluruh kalkulasi hari ini ke tabel analytics_daily_signals."""
    if not candidates: return
    
    # Buat tabel jika belum ada
    with storage.engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS analytics_daily_signals (
                date DATE NOT NULL,
                ticker VARCHAR(20) NOT NULL,
                ml_win_prob NUMERIC,
                composite_score INT,
                technical_score INT,
                smart_money_score INT,
                fundamental_score INT,
                ml_label VARCHAR(20),
                features_snapshot JSONB,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (date, ticker)
            );
        """))

    raw_conn = storage._get_raw_conn()
    try:
        with raw_conn.cursor() as cur:
            today = datetime.now().strftime("%Y-%m-%d")

            for c in candidates:
                base_prob = float(c['score']) * 0.60 + float(c['sm_score']) * 0.40
                ml_win_prob = min(99.0, max(1.0, base_prob))

                if ml_win_prob >= 65: ml_label = "WIN"
                elif ml_win_prob >= 40: ml_label = "HOLD"
                else: ml_label = "LOSS"

                features = {
                    "cmf": c.get("cmf_v"),
                    "rsi": c.get("rsi_v"),
                    "mfi": c.get("mfi_v"),
                    "wyckoff_phase": c.get("wp"),
                    "vcp_grade": c.get("vcp_grade"),
                    "smart_money_notes": c.get("smart_money_notes")
                }

                query = """
                INSERT INTO analytics_daily_signals
                (date, ticker, ml_win_prob, composite_score, technical_score, smart_money_score, fundamental_score, ml_label, features_snapshot)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (date, ticker) DO UPDATE SET
                    ml_win_prob = EXCLUDED.ml_win_prob,
                    composite_score = EXCLUDED.composite_score,
                    technical_score = EXCLUDED.technical_score,
                    smart_money_score = EXCLUDED.smart_money_score,
                    fundamental_score = EXCLUDED.fundamental_score,
                    ml_label = EXCLUDED.ml_label,
                    features_snapshot = EXCLUDED.features_snapshot,
                    updated_at = CURRENT_TIMESTAMP;
                """
                cur.execute(query, (
                    today, c['ticker'], ml_win_prob, c['score'], 
                    c.get('ts', 50), c.get('sm_score', 50), 
                    100 - c.get('fund_penalty', 0), ml_label, json.dumps(features)
                ))

            raw_conn.commit()
        print(f"\n  💾 [DB] Berhasil menyimpan {len(candidates)} setup analitik ke PostgreSQL.")
    except Exception as e:
        print(f"\n  ❌ [DB Error] Gagal menyimpan analitik: {e}")
    finally:
        raw_conn.close()

# ══════════════════════════════════════════════════════
#  MAIN SCAN EXECUTION
# ══════════════════════════════════════════════════════

def _scan_tickers(tickers: list, session: str, ihsg_df, regime: dict, threshold: int) -> tuple:
    candidates, blocked_log = [], []
    for tk in tickers:
        if tk in TICKER_BLACKLIST: continue
        try:
            df = load_price_from_db(tk)
            if df is None: 
                print(f"  ⚠️ {tk}: Data harga di DB kosong/ tidak cukup.")
                continue
            
            lp = float(df["close"].iloc[-1])
            if lp < MIN_PRICE_IDR or float(df["volume"].iloc[-1]) < MIN_VOLUME_LOT: 
                print(f"  ⚠️ {tk}: Harga/Volume di bawah minimum.")
                continue

            r = compute_score_v4(df, tk, ihsg_df, regime)
            if r is None: continue
            if r.get("blocked"):
                blocked_log.append(f"  ⛔ {tk}: {r['reason']}")
                continue

            r["session"] = session
            if r["score"] < threshold: 
                print(f"  ℹ️ {tk}: Skor {r['score']} di bawah threshold ({threshold}).")
                continue
            
            candidates.append(r)
            print(f"  ✅ {tk}: {r['score']}/100 | {r['signal_type']} | SM:{r['sm_score']} | {r['smart_money_notes']}")
            time.sleep(0.05)

        except Exception as e:
            print(f"  ❌ {tk}: ERROR - {e}")
            continue

    return candidates, blocked_log

def scan_once(session: str = "DB_SCAN") -> list:
    print(f"\n{'='*58}")
    print(f"BandarAI Scanner v4.0 (DB Native) — {session}")
    print(f"{'='*58}")

    ihsg_df = load_ihsg_from_db()
    regime = get_market_regime(ihsg_df)
    print(f"🌏 {regime['regime']} — {regime['desc']}\n")

    if not regime["ok"]: return []

    candidates, blocked = _scan_tickers(WATCHLIST, session, ihsg_df, regime, MIN_SCORE_TO_SIGNAL)
    
    if candidates:
        save_analytics_to_db(candidates)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:5]

if __name__ == "__main__":
    res = scan_once()
    print(f"\n🎯 {len(res)} Sinyal ditemukan.")

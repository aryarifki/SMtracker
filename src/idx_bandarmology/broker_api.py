"""Broker-flow client for per-stock smart-money and flow data.

Authenticated via the ``BROKER_API_TOKEN`` secret in `.env`.

This module exposes:
  * Per-broker net buy/sell breakdowns (local, foreign, government).
  * Bandar detector accumulation/distribution states.
  * Foreign-vs-domestic transaction flow.
  * Multi-timeframe price performance used as a cross-check.

Every numeric field is normalized to a float or ``None`` — nothing is
fabricated when a field is missing. Each section also carries a short
English summary string for quick display in the dashboard.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

import requests

from . import config

_BASE = "https://exodus.stockbit.com"
_TIMEOUT = 15.0
_CACHE_TTL = 300.0  # 5 minutes — upstream data is end-of-day and refreshes slowly.
_cache: dict[str, tuple[float, Any]] = {}

# ── rate limiting state ──────────────────────────────────────────────────────
_rate_limit_sec: float = 0.0
_last_request_time: float = 0.0

def set_rate_limit(seconds: float) -> None:
    """Set maximum request rate in seconds (e.g., 8.0 for 1 request per 8s)."""
    global _rate_limit_sec
    _rate_limit_sec = max(0.0, seconds)

def _throttle() -> None:
    """Sleep if the time elapsed since the last request is less than the limit."""
    global _last_request_time
    if _rate_limit_sec <= 0:
        return
    elapsed = time.time() - _last_request_time
    if elapsed < _rate_limit_sec:
        time.sleep(_rate_limit_sec - elapsed)
    _last_request_time = time.time()


# ── transport ────────────────────────────────────────────────────────────────

def is_available() -> bool:
    """True when a broker API token is configured in `.env`."""
    return bool(config.get_broker_api_token())


def _get(path: str) -> Any:
    token = config.get_broker_api_token()
    if not token:
        raise RuntimeError(
            "BROKER_API_TOKEN not configured — add it to your .env "
            "(see .env.example)"
        )
    
    # Throttle before firing the actual request
    _throttle()
    
    resp = requests.get(
        _BASE + path,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _cached(key: str, fn: Callable[[], Any]) -> Any:
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


# ── parsing helpers ──────────────────────────────────────────────────────────

def _f(v: Any) -> Optional[float]:
    """Coerce to float; the upstream API often returns numbers as strings."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _raw(o: Any) -> Optional[float]:
    """Extract ``.value.raw`` / ``.raw`` from nested value dicts."""
    if isinstance(o, dict):
        if "raw" in o:
            return _f(o.get("raw"))
        if "value" in o:
            return _raw(o.get("value"))
    return _f(o)


def _rp(v: Optional[float]) -> str:
    """Human Rupiah (T = triliun, M = miliar, Jt = juta)."""
    if v is None:
        return "-"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e12:
        return f"{sign}Rp {a / 1e12:.2f} T"
    if a >= 1e9:
        return f"{sign}Rp {a / 1e9:.2f} M"
    if a >= 1e6:
        return f"{sign}Rp {a / 1e6:.2f} Jt"
    return f"{sign}Rp {a:.0f}"


def _sym(ticker: str) -> str:
    return ticker.upper().replace(".JK", "").strip()


# accdist label -> (signal code, score, readable English)
_ACC_MAP: dict[str, tuple[str, int, str]] = {
    "Big Acc": ("STRONG_ACCUMULATION", 2, "strong accumulation"),
    "Small Acc": ("ACCUMULATION", 1, "accumulation"),
    "Neutral": ("NEUTRAL", 0, "neutral"),
    "Small Dist": ("DISTRIBUTION", -1, "distribution"),
    "Big Dist": ("STRONG_DISTRIBUTION", -2, "strong distribution"),
}


def _accdist(label: Optional[str]) -> tuple[str, int, str]:
    return _ACC_MAP.get(label or "", ("NEUTRAL", 0, "neutral"))


def signal_methodology() -> dict[str, Any]:
    """Human-readable rules used to convert raw broker data into signals."""
    return {
        "overall_signal_rule": (
            "The overall bandar signal is chosen from the stronger of the 5-day average state "
            "(`avg5`) and the top-5-broker state (`top5`). The state with the larger absolute score wins."
        ),
        "state_mapping": [
            {"raw_label": "Big Acc", "signal": "STRONG_ACCUMULATION", "score": 2},
            {"raw_label": "Small Acc", "signal": "ACCUMULATION", "score": 1},
            {"raw_label": "Neutral", "signal": "NEUTRAL", "score": 0},
            {"raw_label": "Small Dist", "signal": "DISTRIBUTION", "score": -1},
            {"raw_label": "Big Dist", "signal": "STRONG_DISTRIBUTION", "score": -2},
        ],
        "foreign_flow_rule": (
            "Foreign flow is labeled ACCUMULATION when foreign net flow is positive and at least "
            "5% of total traded value, NET_BUY when positive but below 5%, DISTRIBUTION when negative "
            "and at most -5%, NET_SELL when negative but above -5%, and NEUTRAL when near zero."
        ),
    }


# ── section: broker summary + bandar detector ────────────────────────────────

def _broker_section_from_marketdetector(sym: str, md: dict[str, Any]) -> dict[str, Any]:
    bandar = md.get("bandar_detector") or {}
    bs = md.get("broker_summary") or {}

    def mk_buy(b: dict) -> dict:
        return {
            "code": b.get("netbs_broker_code"),
            "type": b.get("type"),
            "value": _f(b.get("bval")),
            "lot": _f(b.get("blot")),
            "freq": _f(b.get("freq")),
            "avgPrice": _f(b.get("netbs_buy_avg_price")),
        }

    def mk_sell(b: dict) -> dict:
        return {
            "code": b.get("netbs_broker_code"),
            "type": b.get("type"),
            "value": _f(b.get("sval")),
            "lot": _f(b.get("slot")),
            "freq": _f(b.get("freq")),
            "avgPrice": _f(b.get("netbs_sell_avg_price")),
        }

    buyers = [mk_buy(b) for b in (bs.get("brokers_buy") or [])]
    sellers = [mk_sell(b) for b in (bs.get("brokers_sell") or [])]
    buyers.sort(key=lambda x: x["value"] or 0, reverse=True)
    sellers.sort(key=lambda x: x["value"] or 0)  # most negative first

    def net_by_type(t: str) -> float:
        s = 0.0
        for b in buyers + sellers:
            if b["type"] == t and b["value"]:
                s += b["value"]
        return s

    foreign_net = net_by_type("Asing")
    local_net = net_by_type("Lokal")
    gov_net = net_by_type("Pemerintah")

    def acc(o: Any) -> Optional[dict]:
        if not isinstance(o, dict):
            return None
        return {
            "accdist": o.get("accdist"),
            "amount": _f(o.get("amount")),
            "percent": _f(o.get("percent")),
            "vol": _f(o.get("vol")),
        }

    bandar_out = {
        "brokerAccdist": bandar.get("broker_accdist"),
        "avg": acc(bandar.get("avg")),
        "avg5": acc(bandar.get("avg5")),
        "top1": acc(bandar.get("top1")),
        "top3": acc(bandar.get("top3")),
        "top5": acc(bandar.get("top5")),
        "top10": acc(bandar.get("top10")),
        "totalBuyer": bandar.get("total_buyer"),
        "totalSeller": bandar.get("total_seller"),
        "numberBrokerBuysell": bandar.get("number_broker_buysell"),
        "value": _f(bandar.get("value")),
        "volume": _f(bandar.get("volume")),
        "averagePrice": _f(bandar.get("average")),
    }

    # Overall bandar signal: blend the 5-day pattern (avg5) with the big-player
    # gauge (top5). Whichever is stronger in magnitude wins.
    sig5, score5, read5 = _accdist((bandar.get("avg5") or {}).get("accdist"))
    sigt, scoret, readt = _accdist((bandar.get("top5") or {}).get("accdist"))
    if abs(scoret) >= abs(score5):
        signal, readable, score = sigt, readt, scoret
    else:
        signal, readable, score = sig5, read5, score5

    top5 = bandar_out["top5"] or {}
    fnet_word = "net buying" if foreign_net > 0 else "net selling" if foreign_net < 0 else "balanced"
    conclusion = (
        f"Large players (top 5 brokers) are flagged as {readable} "
        f"({_rp(top5.get('amount'))}, {(top5.get('percent') or 0):.1f}% of traded value). "
        f"Foreign flow is {fnet_word} {_rp(abs(foreign_net))} today; local flow is {_rp(local_net)}. "
        f"{bandar_out['totalBuyer'] or 0} buying brokers versus {bandar_out['totalSeller'] or 0} selling brokers."
    )

    return {
        "available": True,
        "date": md.get("to") or md.get("from"),
        "signal": signal,
        "signalScore": score,
        "buyers": buyers[:12],
        "sellers": sellers[:12],
        "foreignNet": foreign_net,
        "localNet": local_net,
        "govNet": gov_net,
        "bandar": bandar_out,
        "conclusion": conclusion,
    }


def _broker_section(sym: str) -> dict[str, Any]:
    md = _get(f"/marketdetectors/{sym}").get("data", {}) or {}
    return _broker_section_from_marketdetector(sym, md)


def _md_range(sym: str, frm: str, to: str) -> dict[str, Any]:
    """Fetch Stockbit market detector data for a specific date/range."""
    return _get(f"/marketdetectors/{sym}?from={frm}&to={to}").get("data", {}) or {}


def fetch_broker_distribution(
    ticker: str,
    trade_date: str | date | datetime,
    end_date: str | date | datetime | None = None,
    investor_type: str = "INVESTOR_TYPE_ALL",
    market_board: str = "MARKET_TYPE_REGULER",
    data_type: str = "BROKER_DISTRIBUTION_DATA_TYPE_VALUE",
) -> dict[str, Any]:
    """Fetch broker-to-broker distribution graph from Stockbit order-trade API."""
    start_iso = _parse_date(trade_date).isoformat()
    end_iso = _parse_date(end_date or trade_date).isoformat()
    sym = _sym(ticker)
    path = (
        "/order-trade/broker/distribution"
        f"?date=&symbol={sym}"
        f"&investor_type={investor_type}"
        f"&market_board={market_board}"
        f"&data_type={data_type}"
        f"&from={start_iso}&to={end_iso}"
    )
    key = f"broker_distribution:{sym}:{start_iso}:{end_iso}:{investor_type}:{market_board}:{data_type}"
    resp = _cached(key, lambda: _get(path))
    return (resp or {}).get("data", {}) or {}


def _broker_activity_rows(sym: str, md: dict[str, Any], fetched_at: str) -> list[dict[str, Any]]:
    """Flatten Stockbit broker_summary into one row per broker."""
    bs = md.get("broker_summary") or {}
    rows: dict[str, dict[str, Any]] = {}
    row_date = md.get("to") or md.get("from")

    for b in bs.get("brokers_buy") or []:
        code = b.get("netbs_broker_code")
        if not code:
            continue
        row = rows.setdefault(code, {
            "date": row_date,
            "ticker": sym,
            "broker_code": code,
            "participant_type": b.get("type"),
            "buy_value": 0.0,
            "sell_value": 0.0,
            "net_value": 0.0,
            "buy_lot": 0.0,
            "sell_lot": 0.0,
            "frequency": 0.0,
            "buy_avg_price": None,
            "sell_avg_price": None,
            "fetched_at": fetched_at,
        })
        row["buy_value"] += _f(b.get("bval")) or 0.0
        row["buy_lot"] += _f(b.get("blot")) or 0.0
        row["frequency"] += _f(b.get("freq")) or 0.0
        row["buy_avg_price"] = _f(b.get("netbs_buy_avg_price"))

    for b in bs.get("brokers_sell") or []:
        code = b.get("netbs_broker_code")
        if not code:
            continue
        row = rows.setdefault(code, {
            "date": row_date,
            "ticker": sym,
            "broker_code": code,
            "participant_type": b.get("type"),
            "buy_value": 0.0,
            "sell_value": 0.0,
            "net_value": 0.0,
            "buy_lot": 0.0,
            "sell_lot": 0.0,
            "frequency": 0.0,
            "buy_avg_price": None,
            "sell_avg_price": None,
            "fetched_at": fetched_at,
        })
        if not row.get("participant_type"):
            row["participant_type"] = b.get("type")
        row["sell_value"] += abs(_f(b.get("sval")) or 0.0)
        row["sell_lot"] += abs(_f(b.get("slot")) or 0.0)
        row["frequency"] += _f(b.get("freq")) or 0.0
        row["sell_avg_price"] = _f(b.get("netbs_sell_avg_price"))

    out = []
    for row in rows.values():
        row["net_value"] = (row["buy_value"] or 0.0) - (row["sell_value"] or 0.0)
        out.append(row)
    return out


def _flow_row(sym: str, md: dict[str, Any], fallback_date: str, fetched_at: str) -> dict[str, Any] | None:
    bs = md.get("broker_summary") or {}
    if not (bs.get("brokers_buy") or bs.get("brokers_sell")):
        return None
    broker = _broker_section_from_marketdetector(sym, md)
    return {
        "date": broker.get("date") or fallback_date,
        "ticker": sym,
        "bandar_signal": broker.get("signal"),
        "bandar_signal_score": broker.get("signalScore"),
        "foreign_net_broker": broker.get("foreignNet"),
        "local_net_broker": broker.get("localNet"),
        "gov_net_broker": broker.get("govNet"),
        "foreign_net_flow": None,
        "domestic_net_flow": None,
        "total_value": (broker.get("bandar") or {}).get("value"),
        "foreign_signal": None,
        "conclusion_broker": broker.get("conclusion"),
        "conclusion_flow": None,
        "fetched_at": fetched_at,
    }


# ── section: foreign vs domestic flow ────────────────────────────────────────

def _foreign_domestic_section(sym: str) -> dict[str, Any]:
    fd = _get(f"/findata-view/foreign-domestic/v1/chart-data/{sym}").get("data", {}) or {}
    val = fd.get("value", {}) or {}
    summary = fd.get("summary", {}) or {}

    def vp(node: Any) -> dict:
        node = node or {}
        return {"value": _raw(node.get("value")), "pct": _raw(node.get("percentage"))}

    net_foreign = _raw((summary.get("net_foreign") or {}).get("value"))
    net_domestic = _raw((summary.get("net_domestic") or {}).get("value"))
    total = _raw(val.get("total"))

    out = {
        "available": True,
        "date": fd.get("last_updated") or fd.get("to"),
        "totalValue": total,
        "foreignBuy": vp(val.get("foreign_buy")),
        "foreignSell": vp(val.get("foreign_sell")),
        "domesticBuy": vp(val.get("domestic_buy")),
        "domesticSell": vp(val.get("domestic_sell")),
        "foreignTotalPct": _raw((val.get("foreign_total") or {}).get("percentage")),
        "domesticTotalPct": _raw((val.get("domestic_total") or {}).get("percentage")),
        "netForeign": net_foreign,
        "netDomestic": net_domestic,
    }

    netpct = (net_foreign / total * 100) if (net_foreign is not None and total) else None
    if net_foreign is None:
        word, signal = "unavailable", "NEUTRAL"
    elif net_foreign > 0:
        word = "net buying (foreign accumulation)"
        signal = "ACCUMULATION" if (netpct or 0) >= 5 else "NET_BUY"
    elif net_foreign < 0:
        word = "net selling (foreign distribution)"
        signal = "DISTRIBUTION" if (netpct or 0) <= -5 else "NET_SELL"
    else:
        word, signal = "balanced", "NEUTRAL"
    out["signal"] = signal
    out["conclusion"] = (
        f"Foreign flow is {word} {_rp(abs(net_foreign) if net_foreign is not None else None)}"
        + (f" ({netpct:+.1f}% of traded value {_rp(total)})" if netpct is not None else "")
        + f"; domestic net flow is {_rp(net_domestic)}."
    )
    return out


def _parse_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    import pandas as pd
    try:
        return pd.to_datetime(str(value)).date()
    except Exception:
        return datetime.utcnow().date()


def fetch_historical_broker_flow(
    tickers: list[str],
    start_date: str | date | datetime,
    end_date: str | date | datetime,
) -> pd.DataFrame:
    """Backfill daily broker/bandar snapshots from Stockbit marketdetectors.

    The live pipeline stores one latest snapshot per run. This function fills
    older signal dates by querying ``/marketdetectors/{ticker}?from=d&to=d``
    for each weekday in the requested range, then flattening the result to the
    same columns used by the ``broker_flow`` table.
    """
    import pandas as pd

    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start > end:
        start, end = end, start

    fetched_at = datetime.utcnow().isoformat()
    syms = [_sym(t) for t in tickers if t]

    dates: list[str] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            dates.append(current.isoformat())
        current += timedelta(days=1)

    def fetch_one(task: tuple[str, str]) -> dict[str, Any] | None:
        sym, iso = task
        try:
            md = _md_range(sym, iso, iso)
        except Exception:
            return None
        return _flow_row(sym, md, iso, fetched_at)

    from concurrent.futures import ThreadPoolExecutor

    tasks = [(sym, iso) for iso in dates for sym in syms]
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(tasks)))) as pool:
        rows = [row for row in pool.map(fetch_one, tasks) if row is not None]

    cols = [
        "date", "ticker", "bandar_signal", "bandar_signal_score",
        "foreign_net_broker", "local_net_broker", "gov_net_broker",
        "foreign_net_flow", "domestic_net_flow", "total_value",
        "foreign_signal", "conclusion_broker", "conclusion_flow", "fetched_at",
    ]
    return pd.DataFrame(rows, columns=cols)


def fetch_historical_broker_data(
    tickers: list[str],
    start_date: str | date | datetime,
    end_date: str | date | datetime,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Backfill daily flow rows and per-broker distribution rows."""
    import pandas as pd
    from concurrent.futures import ThreadPoolExecutor

    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start > end:
        start, end = end, start

    fetched_at = datetime.utcnow().isoformat()
    syms = [_sym(t) for t in tickers if t]
    dates: list[str] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            dates.append(current.isoformat())
        current += timedelta(days=1)

    errors: list[str] = []

    def fetch_one(task: tuple[str, str]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        sym, iso = task
        try:
            md = _md_range(sym, iso, iso)
        except Exception as exc:  # noqa: BLE001
            if len(errors) < 8:
                errors.append(f"{sym} {iso}: {type(exc).__name__}: {str(exc)[:140]}")
            return None, []
        flow = _flow_row(sym, md, iso, fetched_at)
        activity = _broker_activity_rows(sym, md, fetched_at) if flow else []
        return flow, activity

    tasks = [(sym, iso) for iso in dates for sym in syms]
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(tasks)))) as pool:
        results = list(pool.map(fetch_one, tasks))

    flow_rows = [flow for flow, _activity in results if flow is not None]
    activity_rows = [row for _flow, activity in results for row in activity]
    if errors and not flow_rows:
        print("[broker_api] historical broker fetch returned no rows. Sample errors:")
        for err in errors:
            print(f"[broker_api]   {err}")
    flow_cols = [
        "date", "ticker", "bandar_signal", "bandar_signal_score",
        "foreign_net_broker", "local_net_broker", "gov_net_broker",
        "foreign_net_flow", "domestic_net_flow", "total_value",
        "foreign_signal", "conclusion_broker", "conclusion_flow", "fetched_at",
    ]
    activity_cols = [
        "date", "ticker", "broker_code", "participant_type",
        "buy_value", "sell_value", "net_value",
        "buy_lot", "sell_lot", "frequency",
        "buy_avg_price", "sell_avg_price", "fetched_at",
    ]
    return pd.DataFrame(flow_rows, columns=flow_cols), pd.DataFrame(activity_rows, columns=activity_cols)


# ── section: price performance (quick cross-check; daily OHLC comes from yfinance) ──

def _price_performance_section(sym: str) -> dict[str, Any]:
    prices = _get(f"/company-price-feed/price-performance/{sym}").get("data", {}).get("prices", []) or []
    rows = []
    by_tf: dict[str, Optional[float]] = {}
    for p in prices:
        tf = p.get("timeframe")
        pct = _raw(p.get("percentage"))
        rows.append({
            "timeframe": tf,
            "close": _raw(p.get("close")),
            "high": _raw(p.get("high")),
            "low": _raw(p.get("low")),
            "pct": pct,
        })
        if tf:
            by_tf[tf] = pct

    m1, m3, y1 = by_tf.get("1M"), by_tf.get("3M"), by_tf.get("1Y")
    parts = []
    for label, v in (("1 month", m1), ("3 months", m3), ("1 year", y1)):
        if v is not None:
            parts.append(f"{label} {v:+.1f}%")
    mom = sum(1 for v in (m1, m3, y1) if v is not None and v > 0)
    neg = sum(1 for v in (m1, m3, y1) if v is not None and v < 0)
    if mom > neg:
        trend = "positive price momentum"
    elif neg > mom:
        trend = "negative price momentum"
    else:
        trend = "mixed price momentum"
    conclusion = f"{trend.capitalize()}" + (f" ({', '.join(parts)})." if parts else ".")

    return {"available": True, "prices": rows, "conclusion": conclusion}


# ── public API ───────────────────────────────────────────────────────────────

def fetch_analysis(ticker: str) -> dict[str, Any]:
    """Full per-stock broker analysis with per-section graceful degradation.

    Returns a dict with ``broker``, ``foreignDomestic`` and ``pricePerformance``
    sections, each independently marked ``available`` so one broken endpoint
    doesn't take down the whole pipeline run.
    """
    sym = _sym(ticker)

    def _build() -> dict[str, Any]:
        result: dict[str, Any] = {"ticker": sym, "available": True}

        def safe(name: str, fn: Callable[[], dict]) -> None:
            try:
                result[name] = fn()
            except Exception as exc:  # noqa: BLE001
                result[name] = {"available": False, "reason": str(exc)[:160]}

        safe("broker", lambda: _broker_section(sym))
        safe("foreignDomestic", lambda: _foreign_domestic_section(sym))
        safe("pricePerformance", lambda: _price_performance_section(sym))

        sections = [result.get("broker"), result.get("foreignDomestic"), result.get("pricePerformance")]
        if not any((s or {}).get("available") for s in sections):
            result["available"] = False

        result["summary"] = _overall_summary(sym, result)
        return result

    return _cached(f"analysis:{sym}", _build)


def _overall_summary(sym: str, r: dict[str, Any]) -> str:
    broker = r.get("broker") or {}
    fd = r.get("foreignDomestic") or {}
    pp = r.get("pricePerformance") or {}
    bits = []
    if broker.get("available"):
        sig = broker.get("signal", "NEUTRAL").replace("_", " ").lower()
        bits.append(f"bandar signal {sig}")
        fn = broker.get("foreignNet")
        if fn is not None:
            bits.append(f"foreign broker net {_rp(fn)}")
    if fd.get("available") and fd.get("netForeign") is not None:
        bits.append(f"foreign flow {_rp(fd.get('netForeign'))}")
    if pp.get("available"):
        for row in pp.get("prices", []):
            if row.get("timeframe") == "1M" and row.get("pct") is not None:
                bits.append(f"1-month price {row['pct']:+.1f}%")
                break
    if not bits:
        return f"Broker-flow data for {sym} is not available for the latest trading day."
    return f"{sym}: " + "; ".join(bits) + "."


def fetch_watchlist(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Run fetch_analysis for each symbol in the watchlist. Never raises."""
    out: dict[str, dict[str, Any]] = {}
    for s in symbols:
        try:
            out[_sym(s)] = fetch_analysis(s)
        except Exception as exc:  # noqa: BLE001
            out[_sym(s)] = {"ticker": _sym(s), "available": False, "reason": str(exc)[:160]}
    return out

"""Streamlit dashboard for IDX smart-money and broker-flow analysis."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from html import escape
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from idx_bandarmology import analysis, broker_api, pipeline, storage  # noqa: E402


PROFILE_META = {
    "smart_foreign": ("Foreign Smart Money", "Directional foreign institutions"),
    "local_institutional": ("Local Institutions", "Local institution-like accounts"),
    "market_maker": ("Market Makers", "Active on both sides; net position matters"),
    "bandar_gorengan": ("Speculative Operators", "Speculative operator profile"),
    "retail": ("Retail-Dominant", "Retail-heavy platforms"),
    "lainnya": ("Other Brokers", "Outside defined behavioral profiles"),
}
SMART_PROFILES = {"smart_foreign", "local_institutional"}
ACC_SIGNALS = {"STRONG_ACCUMULATION", "ACCUMULATION", "NET_BUY", "AKUMULASI_KUAT", "AKUMULASI"}
DIST_SIGNALS = {"STRONG_DISTRIBUTION", "DISTRIBUTION", "NET_SELL", "DISTRIBUSI_KUAT", "DISTRIBUSI"}


def fmt_signal(value: object) -> str:
    if value is None or pd.isna(value):
        return "-"
    mapping = {
        "AKUMULASI_KUAT": "Strong Accumulation",
        "AKUMULASI": "Accumulation",
        "DISTRIBUSI_KUAT": "Strong Distribution",
        "DISTRIBUSI": "Distribution",
        "NETRAL": "Neutral",
        "STRONG_ACCUMULATION": "Strong Accumulation",
        "ACCUMULATION": "Accumulation",
        "NET_BUY": "Net Buy",
        "STRONG_DISTRIBUTION": "Strong Distribution",
        "DISTRIBUTION": "Distribution",
        "NET_SELL": "Net Sell",
        "NEUTRAL": "Neutral",
    }
    text = str(value)
    return mapping.get(text, text.replace("_", " ").title())


def fmt_rp(value: object) -> str:
    if value is None or pd.isna(value):
        return "-"
    n = float(value)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1e12:
        return f"{sign}Rp {n / 1e12:.2f} T"
    if n >= 1e9:
        return f"{sign}Rp {n / 1e9:.2f} B"
    if n >= 1e6:
        return f"{sign}Rp {n / 1e6:.2f} M"
    return f"{sign}Rp {n:,.0f}"


def fmt_pct(value: object) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):+.2%}"


def participant_label(value: object) -> str:
    return {"Asing": "FOREIGN", "Lokal": "LOCAL", "Pemerintah": "GOV"}.get(str(value), str(value or "-"))


def english_text(value: object) -> object:
    if value is None or pd.isna(value):
        return value
    mapping = {
        "Asing": "Foreign",
        "Lokal": "Local",
        "Pemerintah": "Government",
        "AKUMULASI_KUAT": "Strong Accumulation",
        "AKUMULASI": "Accumulation",
        "DISTRIBUSI_KUAT": "Strong Distribution",
        "DISTRIBUSI": "Distribution",
        "NETRAL": "Neutral",
    }
    return mapping.get(str(value), value)


def signed_color(value: float) -> str:
    return "#0f9f6e" if value >= 0 else "#dc3545"


def score_tone(score: float) -> tuple[str, str]:
    if score < 40:
        return "negative", "#f43f5e"
    if score <= 70:
        return "warning", "#f59e0b"
    return "positive", "#10b981"


def price_at_or_before(price_df: pd.DataFrame, ts: pd.Timestamp) -> pd.Series | None:
    sub = price_df[price_df["date"] <= ts].sort_values("date")
    return None if sub.empty else sub.iloc[-1]


def return_to_date(price_df: pd.DataFrame, ts: pd.Timestamp, periods: int) -> float | None:
    sub = price_df[price_df["date"] <= ts].sort_values("date")
    if len(sub) <= periods:
        return None
    latest = float(sub.iloc[-1]["close"])
    base = float(sub.iloc[-periods - 1]["close"])
    return latest / base - 1 if base else None


def flow_row_at(flow_df: pd.DataFrame, ticker: str, ts: pd.Timestamp) -> dict[str, object]:
    sub = flow_df[(flow_df["ticker"] == ticker) & (flow_df["date"] <= ts)].sort_values("date")
    return {} if sub.empty else sub.iloc[-1].to_dict()


def latest_activity_date(activity_df: pd.DataFrame, ticker: str, ts: pd.Timestamp) -> pd.Timestamp | None:
    sub = activity_df[(activity_df["ticker"] == ticker) & (activity_df["date"] <= ts)]
    if sub.empty:
        return None
    return pd.Timestamp(sub["date"].max())


def profile_flow_from_activity(activity: pd.DataFrame) -> pd.DataFrame:
    if activity.empty:
        return pd.DataFrame()
    df = activity.copy()
    df["profile"] = df["broker_code"].map(analysis.broker_profile_of)
    broker_rows = (
        df.groupby(["profile", "broker_code", "participant_type"], dropna=False)
        .agg(net=("net_value", "sum"), buy=("buy_value", "sum"), sell=("sell_value", "sum"))
        .reset_index()
    )
    rows = []
    for profile, (label, desc) in PROFILE_META.items():
        members = broker_rows[broker_rows["profile"] == profile].copy()
        if members.empty:
            continue
        members["abs_net"] = members["net"].abs()
        rows.append(
            {
                "profile": profile,
                "label": label,
                "description": desc,
                "net": float(members["net"].sum()),
                "top_brokers": members.sort_values("abs_net", ascending=False)
                .head(6)[["broker_code", "participant_type", "net"]]
                .to_dict("records"),
            }
        )
    return pd.DataFrame(rows)


def smart_daily_from_activity(activity: pd.DataFrame) -> pd.DataFrame:
    if activity.empty:
        return pd.DataFrame()
    df = activity.copy()
    df["profile"] = df["broker_code"].map(analysis.broker_profile_of)
    df = df[df["profile"].isin(SMART_PROFILES)]
    if df.empty:
        return pd.DataFrame()
    daily = df.groupby("date")["net_value"].sum().reset_index(name="smart_net").sort_values("date")
    daily["cumulative_net"] = daily["smart_net"].cumsum()
    return daily


@st.cache_data(ttl=1800, show_spinner=False)
def cached_causality(ticker: str) -> dict[str, object] | None:
    return analysis.causality_foreign_vs_price(ticker, max_lags=5)


@st.cache_data(ttl=1800, show_spinner=False)
def cached_broker_scan(tickers: tuple[str, ...], horizon: int, min_events: int, min_net_value: float) -> pd.DataFrame:
    return analysis.broker_alpha_scan(
        list(tickers),
        horizon=horizon,
        min_events=min_events,
        min_net_value=min_net_value,
        group_by=("ticker", "broker_code"),
    )


@st.cache_data(ttl=1800, show_spinner=False)
def cached_broker_distribution_api(ticker: str, start_date: pd.Timestamp, end_date: pd.Timestamp) -> dict[str, object]:
    return broker_api.fetch_broker_distribution(ticker, start_date, end_date=end_date)


def label_component(signal: object) -> float:
    raw = str(signal or "").upper()
    if raw in {"AKUMULASI_KUAT", "STRONG_ACCUMULATION"}:
        return 100
    if raw in {"AKUMULASI", "ACCUMULATION", "NET_BUY"}:
        return 80
    if raw in {"NETRAL", "NEUTRAL"}:
        return 50
    if raw in {"DISTRIBUSI", "DISTRIBUTION", "NET_SELL"}:
        return 25
    if raw in {"DISTRIBUSI_KUAT", "STRONG_DISTRIBUTION"}:
        return 0
    return 40


def p_value_component(p_value: float | None) -> float:
    if p_value is None or pd.isna(p_value):
        return 50
    if p_value <= 0.01:
        return 100
    if p_value <= 0.05:
        return 80
    if p_value <= 0.10:
        return 55
    return 20


def foreign_component(value: float | None) -> float:
    if value is None or pd.isna(value):
        return 50
    if value > 0:
        return 100
    if value < 0:
        return 0
    return 50


def broker_win_component(scan_df: pd.DataFrame, ticker: str) -> tuple[float, str]:
    if scan_df.empty:
        return 50, "No broker validation sample"
    sub = scan_df[scan_df["ticker"] == ticker].copy() if "ticker" in scan_df.columns else scan_df.copy()
    if sub.empty:
        return 50, "No broker validation sample"
    sub = sub.sort_values(["significant", "p_value_one_sided", "mean_fwd_return"], ascending=[False, True, False])
    row = sub.iloc[0]
    win_rate = float(row.get("win_rate", 0.5))
    return max(0, min(100, win_rate * 100)), f"{row.get('broker_code', '-')} win rate {win_rate:.0%}"


def conviction_score(signal: object, foreign_5d: float | None, scan_df: pd.DataFrame, ticker: str) -> dict[str, object]:
    causality = cached_causality(ticker)
    p_value = None if not causality else float(causality.get("min_p_value", np.nan))
    p_score = p_value_component(p_value)
    s_score = label_component(signal)
    f_score = foreign_component(foreign_5d)
    w_score, w_note = broker_win_component(scan_df, ticker)
    score = (p_score * 0.30) + (s_score * 0.30) + (f_score * 0.20) + (w_score * 0.20)
    return {
        "score": round(float(score), 1),
        "p_value": p_value,
        "causality_component": p_score,
        "signal_component": s_score,
        "foreign_component": f_score,
        "broker_component": w_score,
        "broker_note": w_note,
    }


def contradiction_alerts(signal: object, ret_5d: float | None, ret_10d: float | None, foreign_5d: float | None, smart_cum: float | None) -> list[str]:
    raw = str(signal or "").upper()
    alerts = []
    if raw in DIST_SIGNALS and ((ret_5d is not None and ret_5d > 0) or (ret_10d is not None and ret_10d > 0)):
        alerts.append(
            "Distribution while price is still rising — potential unfinished distribution or new buyer absorption. Monitor volume."
        )
    if raw in ACC_SIGNALS and ret_5d is not None and ret_5d < 0:
        alerts.append("Accumulation signal with negative 5D return — accumulation may be early, failed, or absorbed by larger supply.")
    if foreign_5d is not None and foreign_5d < 0 and raw in ACC_SIGNALS:
        alerts.append("Aggregate accumulation conflicts with foreign net selling — check whether the move is driven by local brokers.")
    if smart_cum is not None and smart_cum < 0 and raw in ACC_SIGNALS:
        alerts.append("Signal is accumulation but smart-money cumulative flow is negative in the selected window.")
    return alerts


def broker_subtype(row: pd.Series) -> str:
    if participant_label(row.get("Type") or row.get("participant_type")) != "FOREIGN":
        return "-"
    net = abs(float(row.get("Net", row.get("net_value", 0)) or 0))
    freq = max(float(row.get("Freq", row.get("frequency", 0)) or 0), 1)
    avg_value = net / freq
    if avg_value >= 500_000_000 or (net >= 5_000_000_000 and freq <= 500):
        return "Institutional"
    if freq >= 2_000 or avg_value <= 100_000_000:
        return "Speculative"
    return "Mixed"


def render_metric_card(label: str, value: str, note: str = "", tone: str = "neutral", title: str = "") -> None:
    color = {"positive": "#0f9f6e", "negative": "#dc3545", "warning": "#b7791f"}.get(tone, "#94a3b8")
    st.markdown(
        f"""
        <div class="metric-card" title="{escape(title)}" style="--accent:{color}">
            <div class="metric-label">{escape(label)}</div>
            <div class="metric-value" style="color:{color}">{escape(value)}</div>
            <div class="metric-note">{escape(note)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_page_header(ticker: str, analysis_ts: pd.Timestamp, window_start: pd.Timestamp, activity_date: pd.Timestamp | None) -> None:
    data_date = activity_date.strftime("%Y-%m-%d") if activity_date is not None else "-"
    st.markdown(
        f"""
        <div class="page-header">
            <div>
                <div class="eyebrow">IDX Broker Flow Research</div>
                <div class="page-title">Smart Money Dashboard</div>
            </div>
            <div class="header-meta">
                <span>{escape(ticker)}</span>
                <span>Analysis {analysis_ts:%Y-%m-%d}</span>
                <span>Broker data {escape(data_date)}</span>
                <span>Window {window_start:%Y-%m-%d} to {analysis_ts:%Y-%m-%d}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_alerts(alerts: list[str]) -> None:
    if not alerts:
        return
    html = "".join(f"<div>{escape(item)}</div>" for item in alerts)
    st.markdown(f'<div class="alert-panel">{html}</div>', unsafe_allow_html=True)


def render_verdict(text: str) -> None:
    st.markdown(
        f"""
        <div class="verdict-panel">
            <div class="panel-kicker">Current read</div>
            <div class="verdict-text">{escape(text)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_profile_flow(profile_df: pd.DataFrame) -> None:
    if profile_df.empty:
        st.caption("No broker-profile flow for this date window.")
        return
    max_abs = max(float(profile_df["net"].abs().max()), 1.0)
    html = ['<div class="profile-panel">']
    for row in profile_df.sort_values("net", ascending=False).itertuples():
        net = float(row.net)
        width = max(3, min(100, abs(net) / max_abs * 100))
        color = signed_color(net)
        chips = []
        for broker in row.top_brokers:
            b_net = float(broker.get("net") or 0)
            chips.append(
                '<span class="broker-chip">'
                f'{escape(str(broker.get("broker_code", "-")))}'
                f'<span>{escape(participant_label(broker.get("participant_type")))}</span>'
                f'<b style="color:{signed_color(b_net)}">{escape(fmt_rp(b_net))}</b>'
                "</span>"
            )
        html.append(
            '<div class="profile-row">'
            '<div class="profile-head">'
            f"<div><b>{escape(row.label)}</b><small>{escape(row.description)}</small></div>"
            f'<strong style="color:{color}">{escape(fmt_rp(net))}</strong>'
            "</div>"
            '<div class="bar-track">'
            f'<div class="bar-fill" style="width:{width:.1f}%; background:{color};"></div>'
            "</div>"
            f'<div class="chip-row">{"".join(chips)}</div>'
            "</div>"
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)


def style_table(df: pd.DataFrame, money_cols: list[str] | None = None, pct_cols: list[str] | None = None):
    money_cols = money_cols or []
    pct_cols = pct_cols or []
    fmt = {col: fmt_rp for col in money_cols if col in df.columns}
    fmt.update({col: fmt_pct for col in pct_cols if col in df.columns})
    return df.style.format(fmt)


def plot_price_context(price_df: pd.DataFrame, broker_df: pd.DataFrame, ticker: str, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> plt.Figure:
    px = price_df[(price_df["date"] >= start_ts) & (price_df["date"] <= end_ts)].sort_values("date")
    br = broker_df[(broker_df["date"] >= start_ts) & (broker_df["date"] <= end_ts)].sort_values("date")
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10.8, 4.05), sharex=True, gridspec_kw={"height_ratios": [3.1, 0.95]})
    if px.empty:
        ax1.text(0.5, 0.5, "No price rows in selected broker window.", ha="center", va="center")
        ax1.set_axis_off()
        ax2.set_axis_off()
        return fig
    ax1.plot(px["date"], px["close"], color="#2563eb", linewidth=1.8, label="Close")
    ax1.axvline(start_ts, color="#b7791f", linewidth=1.0, linestyle="--", alpha=0.75, label="Broker window start")
    signal_dates = set()
    if not br.empty:
        overlay = px.merge(br[["date", "bandar_signal", "bandar_signal_score"]], on="date", how="inner")
        colors = overlay["bandar_signal_score"].map({2: "#0f9f6e", 1: "#65a30d", 0: "#94a3b8", -1: "#ea580c", -2: "#dc3545"}).fillna("#94a3b8")
        ax1.scatter(overlay["date"], overlay["close"], c=colors, s=34, zorder=4, label="Signal date")
        signal_dates = set(overlay[overlay["bandar_signal"].isin(ACC_SIGNALS)]["date"])
    volume_colors = ["#0f9f6e" if d in signal_dates else "#cbd5e1" for d in px["date"]]
    if "volume" in px.columns:
        ax2.bar(px["date"], px["volume"].fillna(0) / 1e6, color=volume_colors, width=0.8)
    ax1.set_title(f"{ticker} price, volume, and signal window")
    ax1.set_ylabel("Close")
    ax1.grid(alpha=0.18)
    ax1.legend(loc="upper left", fontsize=8)
    ax2.set_ylabel("Vol M")
    ax2.grid(axis="y", alpha=0.15)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def plot_broker_compare(activity: pd.DataFrame, broker_codes: list[str], mode: str) -> plt.Figure:
    fig, ax = plt.subplots(figsize=(10.8, 3.65))
    if activity.empty or not broker_codes:
        ax.text(0.5, 0.5, "Select broker codes to display flow.", ha="center", va="center")
        ax.set_axis_off()
        return fig
    sub = activity[activity["broker_code"].isin(broker_codes)].copy()
    if sub.empty:
        ax.text(0.5, 0.5, "No rows for selected broker codes.", ha="center", va="center")
        ax.set_axis_off()
        return fig
    pivot = sub.pivot_table(index="date", columns="broker_code", values="net_value", aggfunc="sum").sort_index()
    if mode == "Cumulative":
        pivot = pivot.cumsum()
    pivot = pivot / 1e9
    for code in pivot.columns:
        ax.plot(pivot.index, pivot[code], marker="o", linewidth=2, label=code)
    ax.axhline(0, color="#64748b", linewidth=0.9)
    ax.set_ylabel("Net value, Rp B")
    ax.set_title("Broker flow comparison")
    ax.grid(alpha=0.16)
    ax.legend(loc="best", fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def plot_smart_flow(daily: pd.DataFrame) -> plt.Figure:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10.8, 3.45), sharex=True, gridspec_kw={"height_ratios": [2, 0.9]})
    if daily.empty:
        ax1.text(0.5, 0.5, "No smart-money flow in selected window.", ha="center", va="center")
        ax1.set_axis_off()
        ax2.set_axis_off()
        return fig
    colors = np.where(daily["smart_net"] >= 0, "#0f9f6e", "#dc3545")
    ax1.bar(daily["date"], daily["smart_net"] / 1e9, color=colors, width=0.8)
    ax1.axhline(0, color="#64748b", linewidth=0.8)
    ax1.set_ylabel("Daily net, Rp B")
    ax1.grid(axis="y", alpha=0.15)
    ax2.plot(daily["date"], daily["cumulative_net"] / 1e9, color="#2563eb", linewidth=1.8)
    ax2.axhline(0, color="#64748b", linewidth=0.8)
    ax2.set_ylabel("Cumulative")
    ax2.grid(axis="y", alpha=0.15)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def plot_event_ribbon(event_table: pd.DataFrame, horizons: tuple[int, ...], show_individual: bool) -> plt.Figure:
    xs = [0, *horizons]
    fig, ax = plt.subplots(figsize=(10.8, 3.85))
    if event_table.empty:
        ax.text(0.5, 0.5, "No accumulation events in this window.", ha="center", va="center")
        ax.set_axis_off()
        return fig
    cols = [f"t_plus_{h}d" for h in xs]
    values = event_table[cols].apply(pd.to_numeric, errors="coerce")
    median = values.median()
    q25 = values.quantile(0.25)
    q75 = values.quantile(0.75)
    mean_plus_5 = values["t_plus_5d"].mean() if "t_plus_5d" in values.columns else values.iloc[:, -1].mean()
    color = "#0f9f6e" if mean_plus_5 >= 100 else "#dc3545"
    if show_individual:
        for row in values.itertuples(index=False):
            ax.plot(xs, list(row), color="#64748b", alpha=0.28, linewidth=1)
    ax.fill_between(xs, q25.values, q75.values, color=color, alpha=0.22, label="25-75 percentile")
    ax.plot(xs, median.values, color=color, linewidth=3, marker="o", label="Median path")
    ax.axhline(100, color="#94a3b8", linestyle="--", linewidth=0.9)
    ax.set_xticks(xs)
    ax.set_xticklabels(["Signal", *[f"+{h}d" for h in horizons]])
    ax.set_ylabel("Normalized price")
    ax.set_title("Event study ribbon, signal date = 100")
    ax.grid(alpha=0.16)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    return fig


def plotly_layout(fig: go.Figure, height: int, title: str | None = None) -> go.Figure:
    fig.update_layout(
        title=title,
        height=height,
        margin=dict(l=18, r=18, t=38 if title else 16, b=22),
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        font=dict(family="Inter, Arial, sans-serif", color="#334155", size=12),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(size=11)),
    )
    fig.update_xaxes(showgrid=False, linecolor="#d9e2ec", tickfont=dict(color="#64748b"))
    fig.update_yaxes(gridcolor="#edf2f7", linecolor="#d9e2ec", tickfont=dict(color="#64748b"))
    return fig


def interactive_price_context(price_df: pd.DataFrame, broker_df: pd.DataFrame, ticker: str, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> go.Figure:
    px = price_df[(price_df["date"] >= start_ts) & (price_df["date"] <= end_ts)].sort_values("date")
    br = broker_df[(broker_df["date"] >= start_ts) & (broker_df["date"] <= end_ts)].sort_values("date")
    fig = go.Figure()
    if px.empty:
        return plotly_layout(fig, 390, "No price rows in selected broker window")
    overlay = px.merge(br[["date", "bandar_signal", "bandar_signal_score"]], on="date", how="left") if not br.empty else px.copy()
    overlay["Signal"] = overlay.get("bandar_signal", pd.Series(index=overlay.index)).map(fmt_signal)
    signal_color = overlay.get("bandar_signal_score", pd.Series(index=overlay.index)).map(
        {2: "#0f9f6e", 1: "#65a30d", 0: "#94a3b8", -1: "#ea580c", -2: "#dc3545"}
    ).fillna("#94a3b8")
    signal_series = overlay["bandar_signal"] if "bandar_signal" in overlay.columns else pd.Series("", index=overlay.index)
    volume_color = np.where(signal_series.isin(ACC_SIGNALS), "#0f9f6e", "#cbd5e1")

    fig.add_bar(
        x=px["date"],
        y=px["volume"].fillna(0) / 1e6 if "volume" in px.columns else np.zeros(len(px)),
        name="Volume, M",
        marker_color=volume_color,
        opacity=0.36,
        yaxis="y2",
        hovertemplate="%{x|%Y-%m-%d}<br>Volume: %{y:.2f}M<extra></extra>",
    )
    fig.add_trace(
        go.Scatter(
            x=px["date"],
            y=px["close"],
            mode="lines",
            name="Close",
            line=dict(color="#2563eb", width=2),
            hovertemplate="%{x|%Y-%m-%d}<br>Close: Rp %{y:,.0f}<extra></extra>",
        )
    )
    if not br.empty:
        signal_rows = overlay[overlay["bandar_signal"].notna()].copy()
        fig.add_trace(
            go.Scatter(
                x=signal_rows["date"],
                y=signal_rows["close"],
                mode="markers",
                name="Signal",
                marker=dict(color=signal_color.loc[signal_rows.index], size=8, line=dict(width=1, color="#ffffff")),
                customdata=np.stack([signal_rows["Signal"], signal_rows["bandar_signal_score"].fillna(0)], axis=-1),
                hovertemplate="%{x|%Y-%m-%d}<br>Close: Rp %{y:,.0f}<br>Signal: %{customdata[0]}<br>Score: %{customdata[1]}<extra></extra>",
            )
        )
    fig.add_shape(
        type="line",
        x0=start_ts,
        x1=start_ts,
        y0=0,
        y1=1,
        yref="paper",
        line=dict(color="#b7791f", width=1, dash="dash"),
    )
    fig.update_layout(
        yaxis=dict(title="Close"),
        yaxis2=dict(title="Volume M", overlaying="y", side="right", showgrid=False),
        barmode="overlay",
    )
    return plotly_layout(fig, 405, f"{ticker} Price, Volume, and Signal Context")


def interactive_broker_compare(activity: pd.DataFrame, broker_codes: list[str], mode: str) -> go.Figure:
    fig = go.Figure()
    if activity.empty or not broker_codes:
        return plotly_layout(fig, 350, "Select broker codes to display flow")
    sub = activity[activity["broker_code"].isin(broker_codes)].copy()
    if sub.empty:
        return plotly_layout(fig, 350, "No rows for selected broker codes")
    pivot = sub.pivot_table(index="date", columns="broker_code", values="net_value", aggfunc="sum").sort_index()
    if mode == "Cumulative":
        pivot = pivot.cumsum()
    pivot = pivot / 1e9
    line_width = 2 if len(pivot.columns) <= 5 else 1.35
    marker_size = 6 if len(pivot.columns) <= 5 else 4
    for code in pivot.columns:
        fig.add_trace(
            go.Scatter(
                x=pivot.index,
                y=pivot[code],
                mode="lines+markers",
                name=code,
                line=dict(width=line_width),
                marker=dict(size=marker_size),
                hovertemplate=f"{code}<br>%{{x|%Y-%m-%d}}<br>Net: Rp %{{y:.2f}}B<extra></extra>",
            )
        )
    fig.add_hline(y=0, line_width=1, line_color="#94a3b8")
    y_title = "Cumulative Net Value, Rp B" if mode == "Cumulative" else "Daily Net Value, Rp B"
    fig.update_yaxes(title=y_title)
    title = "Broker Flow Comparison, Cumulative in Selected Window" if mode == "Cumulative" else "Broker Flow Comparison, Daily Net by Date"
    return plotly_layout(fig, 360, title)


def interactive_smart_flow(daily: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    if daily.empty:
        return plotly_layout(fig, 320, "No smart-money flow in selected window")
    colors = np.where(daily["smart_net"] >= 0, "#0f9f6e", "#dc3545")
    fig.add_bar(
        x=daily["date"],
        y=daily["smart_net"] / 1e9,
        name="Daily Net",
        marker_color=colors,
        hovertemplate="%{x|%Y-%m-%d}<br>Daily Net: Rp %{y:.2f}B<extra></extra>",
    )
    fig.add_trace(
        go.Scatter(
            x=daily["date"],
            y=daily["cumulative_net"] / 1e9,
            mode="lines+markers",
            name="Cumulative Net",
            yaxis="y2",
            line=dict(color="#2563eb", width=2),
            hovertemplate="%{x|%Y-%m-%d}<br>Cumulative: Rp %{y:.2f}B<extra></extra>",
        )
    )
    fig.add_hline(y=0, line_width=1, line_color="#94a3b8")
    fig.update_layout(
        yaxis=dict(title="Daily, Rp B"),
        yaxis2=dict(title="Cumulative, Rp B", overlaying="y", side="right", showgrid=False),
    )
    return plotly_layout(fig, 330, "Smart-Money Daily Flow")


def interactive_event_ribbon(event_table: pd.DataFrame, horizons: tuple[int, ...], show_individual: bool) -> go.Figure:
    xs = [0, *horizons]
    fig = go.Figure()
    if event_table.empty:
        return plotly_layout(fig, 360, "No accumulation events in this window")
    cols = [f"t_plus_{h}d" for h in xs]
    values = event_table[cols].apply(pd.to_numeric, errors="coerce")
    median = values.median()
    q25 = values.quantile(0.25)
    q75 = values.quantile(0.75)
    mean_plus_5 = values["t_plus_5d"].mean() if "t_plus_5d" in values.columns else values.iloc[:, -1].mean()
    color = "#0f9f6e" if mean_plus_5 >= 100 else "#dc3545"
    x_labels = ["Signal", *[f"+{h}D" for h in horizons]]
    if show_individual:
        for idx, row in event_table.iterrows():
            fig.add_trace(
                go.Scatter(
                    x=x_labels,
                    y=[row[col] for col in cols],
                    mode="lines",
                    line=dict(color="#94a3b8", width=1),
                    opacity=0.25,
                    showlegend=False,
                    hovertemplate=f"{row.get('ticker', '')} | {pd.Timestamp(row.get('signal_date')).date()}<br>%{{x}}: %{{y:.2f}}<extra></extra>",
                )
            )
    fig.add_trace(go.Scatter(x=x_labels, y=q75.values, mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip"))
    fig.add_trace(
        go.Scatter(
            x=x_labels,
            y=q25.values,
            mode="lines",
            fill="tonexty",
            fillcolor="rgba(15,159,110,0.18)" if color == "#0f9f6e" else "rgba(220,53,69,0.16)",
            line=dict(width=0),
            name="25-75 percentile",
            hovertemplate="%{x}<br>25th pct: %{y:.2f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x_labels,
            y=median.values,
            mode="lines+markers",
            name="Median Path",
            line=dict(color=color, width=3),
            hovertemplate="%{x}<br>Median: %{y:.2f}<extra></extra>",
        )
    )
    fig.add_hline(y=100, line_width=1, line_dash="dash", line_color="#94a3b8")
    fig.update_yaxes(title="Normalized Price")
    return plotly_layout(fig, 365, "Event Study Ribbon, Signal Date = 100")


def sparkline_values(activity: pd.DataFrame, broker_code: str, end_ts: pd.Timestamp, days: int = 5) -> str:
    sub = activity[(activity["broker_code"] == broker_code) & (activity["date"] <= end_ts)].sort_values("date").tail(days)
    if sub.empty:
        return "-----"
    chars = []
    for value in sub["net_value"].fillna(0):
        chars.append("+" if value > 0 else "-" if value < 0 else "0")
    return "".join(chars)


def top_broker_compact_table(top_buy: pd.DataFrame, top_sell: pd.DataFrame, activity: pd.DataFrame, end_ts: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for side, df in (("Buy", top_buy.head(3)), ("Sell", top_sell.head(3))):
        for row in df.itertuples():
            rows.append(
                {
                    "Side": side,
                    "Broker": row.broker_code,
                    "Type": participant_label(row.participant_type),
                    "Net on Analysis Date": row.net_value,
                    "5D Flow": sparkline_values(activity, row.broker_code, end_ts),
                }
            )
    return pd.DataFrame(rows)


def profile_compact_table(profile_df: pd.DataFrame) -> pd.DataFrame:
    if profile_df.empty:
        return pd.DataFrame()
    out = profile_df[["label", "net"]].copy()
    out = out.sort_values("net", ascending=False).head(6)
    out = out.rename(columns={"label": "Profile", "net": "Net"})
    return out.reset_index(drop=True)


def profile_broker_detail_table(activity: pd.DataFrame, profile_key: str | None = None) -> pd.DataFrame:
    if activity.empty:
        return pd.DataFrame()
    df = activity.copy()
    df["Profile Key"] = df["broker_code"].map(analysis.broker_profile_of)
    if profile_key:
        df = df[df["Profile Key"] == profile_key]
    if df.empty:
        return pd.DataFrame()
    grouped = (
        df.groupby(["Profile Key", "broker_code", "participant_type"], dropna=False)
        .agg(
            Buy=("buy_value", "sum"),
            Sell=("sell_value", "sum"),
            Net=("net_value", "sum"),
            Freq=("frequency", "sum"),
            Days=("date", "nunique"),
        )
        .reset_index()
    )
    grouped["Profile"] = grouped["Profile Key"].map(lambda key: PROFILE_META.get(key, (key, ""))[0])
    grouped["Broker"] = grouped["broker_code"]
    grouped["Type"] = grouped["participant_type"].map(participant_label)
    grouped["Avg Value / Tx"] = grouped.apply(lambda r: abs(float(r["Net"] or 0)) / max(float(r["Freq"] or 0), 1), axis=1)
    grouped = grouped.sort_values(["Profile", "Net"], ascending=[True, False])
    return grouped[["Profile", "Broker", "Type", "Buy", "Sell", "Net", "Freq", "Days", "Avg Value / Tx"]].reset_index(drop=True)


def participant_color(label: str) -> str:
    return {
        "FOREIGN": "#dc3545",
        "LOCAL": "#7c3aed",
        "GOV": "#0f9f6e",
    }.get(label, "#94a3b8")


def rgba_from_hex(hex_color: str, alpha: float) -> str:
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return f"rgba(148,163,184,{alpha})"
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def estimated_broker_paths(dist: pd.DataFrame, top_n: int = 8) -> pd.DataFrame:
    if dist.empty:
        return pd.DataFrame()
    buyers = dist[dist["net_value"] > 0].copy().sort_values("net_value", ascending=False).head(top_n)
    sellers = dist[dist["net_value"] < 0].copy().sort_values("net_value", ascending=True).head(top_n)
    if buyers.empty or sellers.empty:
        return pd.DataFrame()

    buyers["remaining"] = buyers["net_value"].astype(float)
    sellers["remaining"] = sellers["net_value"].abs().astype(float)
    edges: list[dict[str, object]] = []
    seller_idx = 0
    seller_rows = sellers.reset_index(drop=True)
    buyer_rows = buyers.reset_index(drop=True)

    for buyer_i in range(len(buyer_rows)):
        buyer_left = float(buyer_rows.loc[buyer_i, "remaining"])
        while buyer_left > 1e-9 and seller_idx < len(seller_rows):
            seller_left = float(seller_rows.loc[seller_idx, "remaining"])
            if seller_left <= 1e-9:
                seller_idx += 1
                continue
            matched = min(buyer_left, seller_left)
            edges.append(
                {
                    "buyer_code": buyer_rows.loc[buyer_i, "broker_code"],
                    "buyer_type": participant_label(buyer_rows.loc[buyer_i, "participant_type"]),
                    "seller_code": seller_rows.loc[seller_idx, "broker_code"],
                    "seller_type": participant_label(seller_rows.loc[seller_idx, "participant_type"]),
                    "matched_value": matched,
                }
            )
            buyer_left -= matched
            seller_rows.loc[seller_idx, "remaining"] = seller_left - matched
            if seller_rows.loc[seller_idx, "remaining"] <= 1e-9:
                seller_idx += 1
        buyer_rows.loc[buyer_i, "remaining"] = buyer_left
    return pd.DataFrame(edges)


def exact_broker_paths(distribution_data: dict[str, object], top_n: int = 8) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    by_value = (distribution_data or {}).get("by_value") or {}
    for buyer in (by_value.get("top_broker_buy") or [])[:top_n]:
        detail = buyer.get("detail") or {}
        for counterparty in buyer.get("distribute_to") or []:
            rows.append(
                {
                    "buyer_code": detail.get("code"),
                    "buyer_type": participant_label(detail.get("type")),
                    "seller_code": counterparty.get("code"),
                    "seller_type": participant_label(counterparty.get("type")),
                    "matched_value": float(counterparty.get("amount") or 0),
                }
            )
    return pd.DataFrame(rows)


def broker_distribution_sankey(
    dist: pd.DataFrame,
    trade_date: pd.Timestamp,
    distribution_data: dict[str, object] | None = None,
    top_n: int = 8,
) -> go.Figure:
    fig = go.Figure()
    paths = exact_broker_paths(distribution_data or {}, top_n=top_n)
    exact_mode = not paths.empty
    if paths.empty:
        paths = estimated_broker_paths(dist, top_n=top_n)
    if paths.empty:
        return plotly_layout(fig, 420, "Broker Distribution")

    buyer_nodes = []
    seller_nodes = []
    node_labels = []
    node_colors = []
    node_index: dict[str, int] = {}

    for side, code_col, type_col in (("B", "buyer_code", "buyer_type"), ("S", "seller_code", "seller_type")):
        source_df = paths[[code_col, type_col]].drop_duplicates().reset_index(drop=True)
        for row in source_df.itertuples(index=False):
            code = getattr(row, code_col)
            type_label = getattr(row, type_col)
            key = f"{side}:{code}"
            node_index[key] = len(node_labels)
            node_labels.append(code)
            node_colors.append(participant_color(type_label))
            if side == "B":
                buyer_nodes.append(key)
            else:
                seller_nodes.append(key)

    sources = []
    targets = []
    values = []
    link_colors = []
    custom = []
    for row in paths.itertuples(index=False):
        s_key = f"B:{row.buyer_code}"
        t_key = f"S:{row.seller_code}"
        sources.append(node_index[s_key])
        targets.append(node_index[t_key])
        values.append(float(row.matched_value) / 1e9)
        color = participant_color(row.buyer_type)
        link_colors.append(rgba_from_hex(color, 0.35))
        custom.append([row.buyer_code, row.seller_code, fmt_rp(row.matched_value)])

    fig.add_trace(
        go.Sankey(
            arrangement="snap",
            node=dict(
                pad=16,
                thickness=12,
                line=dict(color="#d9e2ec", width=0.5),
                label=node_labels,
                color=node_colors,
                hovertemplate="%{label}<extra></extra>",
            ),
            link=dict(
                source=sources,
                target=targets,
                value=values,
                color=link_colors,
                customdata=custom,
                hovertemplate="Buyer %{customdata[0]}<br>Seller %{customdata[1]}<br>Estimated matched value %{customdata[2]}<extra></extra>",
            ),
        )
    )
    fig.add_annotation(x=0.02, y=1.05, xref="paper", yref="paper", text="Buyers", showarrow=False, font=dict(color="#0f9f6e", size=12))
    fig.add_annotation(x=0.98, y=1.05, xref="paper", yref="paper", text="Sellers", showarrow=False, font=dict(color="#dc3545", size=12), xanchor="right")
    api_start = (distribution_data or {}).get("start_date")
    api_end = (distribution_data or {}).get("end_date")
    if api_start and api_end:
        date_label = f"{api_start} to {api_end}" if api_start != api_end else str(api_end)
    else:
        date_label = f"{trade_date:%Y-%m-%d}"
    title = (
        f"Broker Distribution, Exact API Counterparties on {date_label}"
        if exact_mode
        else f"Broker Distribution, Estimated Matching on {date_label}"
    )
    return plotly_layout(fig, 430, title)


def broker_summary_table(dist: pd.DataFrame, distribution_data: dict[str, object] | None = None, top_n: int = 10) -> pd.DataFrame:
    by_value = (distribution_data or {}).get("by_value") or {}
    if by_value.get("top_broker_buy") or by_value.get("top_broker_sell"):
        buy_rows = by_value.get("top_broker_buy") or []
        sell_rows = by_value.get("top_broker_sell") or []
        rows: list[dict[str, object]] = []
        max_len = max(len(buy_rows), len(sell_rows), 0)
        for i in range(min(max_len, top_n)):
            row: dict[str, object] = {}
            if i < len(buy_rows):
                b = buy_rows[i].get("detail") or {}
                row.update(
                    {
                        "Buy Broker": b.get("code", ""),
                        "Buy Type": participant_label(b.get("type")),
                        "Buy Value": b.get("amount"),
                        "Buy Lot": np.nan,
                        "Buy Avg": np.nan,
                    }
                )
            else:
                row.update({"Buy Broker": "", "Buy Type": "", "Buy Value": np.nan, "Buy Lot": np.nan, "Buy Avg": np.nan})
            if i < len(sell_rows):
                s = sell_rows[i].get("detail") or {}
                row.update(
                    {
                        "Sell Broker": s.get("code", ""),
                        "Sell Type": participant_label(s.get("type")),
                        "Sell Value": s.get("amount"),
                        "Sell Lot": np.nan,
                        "Sell Avg": np.nan,
                    }
                )
            else:
                row.update({"Sell Broker": "", "Sell Type": "", "Sell Value": np.nan, "Sell Lot": np.nan, "Sell Avg": np.nan})
            rows.append(row)
        return pd.DataFrame(rows)

    if dist.empty:
        return pd.DataFrame()
    buyers = dist[dist["net_value"] > 0].copy().sort_values("net_value", ascending=False).head(top_n).reset_index(drop=True)
    sellers = dist[dist["net_value"] < 0].copy().sort_values("net_value", ascending=True).head(top_n).reset_index(drop=True)
    rows: list[dict[str, object]] = []
    max_len = max(len(buyers), len(sellers))
    for i in range(max_len):
        row: dict[str, object] = {}
        if i < len(buyers):
            b = buyers.iloc[i]
            row.update(
                {
                    "Buy Broker": b["broker_code"],
                    "Buy Type": participant_label(b["participant_type"]),
                    "Buy Value": b["buy_value"],
                    "Buy Lot": b["buy_lot"],
                    "Buy Avg": b["buy_avg_price"],
                }
            )
        else:
            row.update({"Buy Broker": "", "Buy Type": "", "Buy Value": np.nan, "Buy Lot": np.nan, "Buy Avg": np.nan})
        if i < len(sellers):
            s = sellers.iloc[i]
            row.update(
                {
                    "Sell Broker": s["broker_code"],
                    "Sell Type": participant_label(s["participant_type"]),
                    "Sell Value": s["sell_value"],
                    "Sell Lot": s["sell_lot"],
                    "Sell Avg": s["sell_avg_price"],
                }
            )
        else:
            row.update({"Sell Broker": "", "Sell Type": "", "Sell Value": np.nan, "Sell Lot": np.nan, "Sell Avg": np.nan})
        rows.append(row)
    return pd.DataFrame(rows)


def build_screener(watchlist: list[str], as_of: pd.Timestamp, scan_df: pd.DataFrame, all_prices: pd.DataFrame, all_flow: pd.DataFrame, all_activity: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for ticker in watchlist:
        flow_row = flow_row_at(all_flow, ticker, as_of)
        act_date = latest_activity_date(all_activity, ticker, as_of)
        if not flow_row or act_date is None:
            continue
        px = all_prices[all_prices["ticker"] == ticker]
        flow_sub = all_flow[(all_flow["ticker"] == ticker) & (all_flow["date"] <= as_of)].sort_values("date")
        foreign_5d = float(flow_sub.tail(5)["foreign_net_broker"].fillna(0).sum()) if not flow_sub.empty else np.nan
        buyers, _ = analysis.top_net_broker_summary(ticker, trade_date=act_date, top_n=1)
        top_buyer = "-" if buyers.empty else str(buyers.iloc[0]["broker_code"])
        ret_5d = return_to_date(px, as_of, 5)
        conv = conviction_score(flow_row.get("bandar_signal"), foreign_5d, scan_df, ticker)
        rows.append(
            {
                "Ticker": ticker,
                "Signal": fmt_signal(flow_row.get("bandar_signal")),
                "Conviction Score": conv["score"],
                "Foreign Net (5D)": foreign_5d,
                "Top Buyer": top_buyer,
                "5D Return": ret_5d,
                "Data Date": pd.Timestamp(flow_row.get("date")).date(),
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("Conviction Score", ascending=False).reset_index(drop=True)


st.set_page_config(page_title="IDX Smart Money", layout="wide")

plt.rcParams.update(
    {
        "figure.facecolor": "#ffffff",
        "axes.facecolor": "#ffffff",
        "savefig.facecolor": "#ffffff",
        "axes.edgecolor": "#d9e2ec",
        "axes.labelcolor": "#64748b",
        "axes.titlecolor": "#111827",
        "xtick.color": "#64748b",
        "ytick.color": "#64748b",
        "grid.color": "#e5e7eb",
        "text.color": "#111827",
        "legend.facecolor": "#ffffff",
        "legend.edgecolor": "#d9e2ec",
        "legend.labelcolor": "#111827",
        "font.family": "sans-serif",
        "font.sans-serif": ["Inter", "Arial"],
    }
)

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    :root {
      --bg: #ffffff; --sidebar: #f6f8fb; --panel: #ffffff; --panel2: #f8fafc; --line: #d9e2ec;
      --muted: #64748b; --text: #334155; --strong: #111827;
      --green: #0f9f6e; --red: #dc3545; --blue: #2563eb; --amber: #b7791f;
    }
    html, body, [data-testid="stAppViewContainer"], [data-testid="stApp"] {
      background: var(--bg); color: var(--text); font-family: Inter, -apple-system, BlinkMacSystemFont, sans-serif;
    }
    .block-container { max-width: 1260px; padding: 0.75rem 1.05rem 1.3rem; }
    [data-testid="stSidebar"] { background: var(--sidebar); border-right: 1px solid var(--line); }
    [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 { font-size: 0.95rem !important; margin-bottom: 0.35rem !important; }
    div[data-testid="stVerticalBlock"] { gap: 0.46rem; }
    div[data-testid="column"] > div[data-testid="stVerticalBlock"] { gap: 0.42rem; }
    h1 { font-size: 1.32rem !important; font-weight: 700 !important; color: var(--strong); letter-spacing: 0; margin-bottom: 0 !important; }
    h2, h3 {
      font-size: 0.95rem !important; font-weight: 700 !important; color: var(--strong); letter-spacing: 0;
      margin-top: 0.38rem !important; margin-bottom: 0.28rem !important;
    }
    h3::after { content: ""; display: block; height: 1px; background: var(--line); margin-top: 0.32rem; opacity: 0.75; }
    [data-testid="stCaptionContainer"], small, .metric-note { color: var(--muted) !important; }
    .page-header {
      display: flex; justify-content: space-between; align-items: flex-end; gap: 18px;
      padding: 10px 13px; margin: 0 0 7px; background: #ffffff;
      border: 1px solid var(--line); border-radius: 8px;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }
    .eyebrow {
      color: var(--blue); font-size: 0.68rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.12em; margin-bottom: 4px;
    }
    .page-title { color: var(--strong); font-size: 1.12rem; font-weight: 700; letter-spacing: 0; line-height: 1.2; }
    .header-meta { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; max-width: 720px; }
    .header-meta span {
      color: var(--text); background: #f8fafc; border: 1px solid var(--line); border-radius: 999px;
      padding: 3px 8px; font-size: 0.7rem; font-weight: 600;
    }
    .stButton button {
      width: 100%; background: #ffffff; color: var(--text); border: 1px solid #cbd5e1;
      border-radius: 6px; height: 2.2rem; font-weight: 650; font-size: 0.82rem;
    }
    .stButton button:hover { border-color: var(--blue); color: var(--strong); background: #f8fafc; }
    .stSelectbox [data-baseweb="select"] > div, .stTextInput [data-baseweb="input"] > div,
    .stNumberInput [data-baseweb="input"] > div, .stDateInput [data-baseweb="input"] > div,
    .stMultiSelect [data-baseweb="select"] > div {
      background: #ffffff !important; border-color: #cbd5e1 !important; border-radius: 6px !important; color: var(--text) !important;
      min-height: 2.05rem !important;
    }
    label p { color: var(--muted) !important; font-size: 0.78rem !important; font-weight: 650 !important; }
    [data-testid="stDataFrame"] { border: 1px solid var(--line); border-radius: 7px; overflow: hidden; background: var(--panel); }
    .metric-card, .verdict-panel, .profile-panel, .alert-panel {
      background: var(--panel); border: 1px solid var(--line); border-radius: 7px;
    }
    .metric-card {
      padding: 8px 10px; min-height: 62px; border-left: 3px solid var(--accent, var(--line));
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }
    .metric-label, .panel-kicker {
      color: var(--muted); font-size: 0.61rem; font-weight: 750; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 4px;
    }
    .metric-value { font-size: 1.02rem; font-weight: 760; font-variant-numeric: tabular-nums; line-height: 1.15; }
    .metric-note { font-size: 0.68rem; margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .verdict-panel { padding: 9px 12px; margin: 6px 0 5px; background: #f8fafc; border-left: 3px solid var(--blue); }
    .verdict-text { line-height: 1.38; color: var(--text); font-size: 0.84rem; }
    .alert-panel { padding: 8px 11px; margin: 5px 0; color: #7c4a03; background: #fff8e6; border-color: #f2d18b; font-size: 0.82rem; }
    .profile-panel { padding: 9px 12px; background: #ffffff; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04); }
    .profile-row { padding: 7px 0; border-bottom: 1px solid var(--line); }
    .profile-row:last-child { border-bottom: 0; }
    .profile-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
    .profile-head b { color: var(--strong); font-size: 0.88rem; }
    .profile-head small { display: block; font-size: 0.72rem; margin-top: 2px; color: var(--muted); }
    .bar-track { height: 4px; background: #e5e7eb; border-radius: 999px; margin: 6px 0; overflow: hidden; }
    .bar-fill { height: 100%; border-radius: 999px; }
    .chip-row { display: flex; flex-wrap: wrap; gap: 6px; }
    .broker-chip {
      display: inline-flex; gap: 6px; align-items: center; padding: 2px 7px; background: #f8fafc;
      border: 1px solid var(--line); border-radius: 5px; font-size: 0.7rem; color: var(--text);
    }
    .broker-chip span { color: var(--muted); font-size: 0.58rem; border: 1px solid var(--line); border-radius: 3px; padding: 1px 4px; background: #ffffff; }
    div[data-testid="stTabs"] [role="tablist"] { gap: 4px; border-bottom: 1px solid var(--line); }
    div[data-testid="stTabs"] button {
      color: var(--muted); font-weight: 700; padding: 6px 10px; border-radius: 6px 6px 0 0;
      background: transparent; font-size: 0.86rem;
    }
    div[data-testid="stTabs"] button[aria-selected="true"] { color: var(--strong); background: #ffffff; border-bottom-color: var(--blue); border-bottom-width: 2px; }
    hr { border-color: var(--line); margin: 0.45rem 0; }
    @media (max-width: 640px) {
      .block-container { padding: 0.85rem 0.7rem; }
      .page-header { display: block; padding: 12px; }
      .header-meta { justify-content: flex-start; margin-top: 10px; }
      .metric-value { font-size: 1.05rem; }
      .metric-note { white-space: normal; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


all_broker = storage.read_broker_flow()
all_activity = storage.read_broker_activity()
all_prices = storage.read_prices()
if not all_broker.empty:
    all_broker["ticker"] = all_broker["ticker"].str.upper()
if not all_activity.empty:
    all_activity["ticker"] = all_activity["ticker"].str.upper()
if not all_prices.empty:
    all_prices["ticker"] = all_prices["ticker"].str.upper()

available_tickers = (
    sorted(set(all_broker["ticker"].unique()).intersection(set(all_activity["ticker"].unique())))
    if not all_broker.empty and not all_activity.empty
    else []
)

with st.sidebar:
    st.header("Controls")
    if not available_tickers:
        st.warning("No ticker has both broker-flow and broker-activity history yet.")
        st.stop()

    default_universe = ",".join(available_tickers)
    watchlist_input = st.text_input("Universe", value=default_universe)
    watchlist = [t.strip().upper() for t in watchlist_input.split(",") if t.strip()]
    watchlist = [t for t in watchlist if t in available_tickers]
    if not watchlist:
        st.warning("The selected universe has no broker history.")
        st.stop()

    selected_ticker = st.selectbox("Ticker", watchlist)
    ticker_dates = sorted(all_activity[all_activity["ticker"] == selected_ticker]["date"].dt.date.unique().tolist())
    latest_broker_date = max(ticker_dates) if ticker_dates else None
    ticker_price_dates = sorted(all_prices[all_prices["ticker"] == selected_ticker]["date"].dt.date.unique().tolist())
    latest_price_date = max(ticker_price_dates) if ticker_price_dates else None
    if latest_broker_date:
        st.caption(f"Latest broker data: {latest_broker_date}")
    if latest_price_date and latest_price_date != latest_broker_date:
        st.caption(f"Latest price data: {latest_price_date}")
    if latest_broker_date and latest_broker_date < date.today():
        st.warning("Today is not available until broker-flow data is fetched and stored.")
    analysis_date = st.selectbox("Analysis date", ticker_dates, index=len(ticker_dates) - 1)
    analysis_ts = pd.Timestamp(analysis_date)
    lookback_label = st.selectbox("Broker window", ["20 calendar days", "30 calendar days", "60 calendar days", "90 calendar days", "180 calendar days"], index=2)
    lookback_days = int(lookback_label.split()[0])
    horizon_label = st.selectbox("Validation horizon", ["1 trading day", "3 trading days", "5 trading days", "10 trading days"], index=3)
    horizon = int(horizon_label.split()[0])
    min_events = st.number_input("Min broker events", min_value=3, max_value=30, value=5, step=1)
    min_net_buy_b = st.number_input("Min net buy, Rp B", min_value=0.0, value=0.0, step=0.5)

    st.divider()
    if st.button("Run latest pipeline to today"):
        result = pipeline.run(watchlist)
        if result["n_broker"] == 0:
            st.error("No broker-flow rows were stored. Check whether the Stockbit/BROKER_API_TOKEN is still valid or whether the upstream endpoint has data.")
        else:
            st.success(f"Stored {result['n_broker']} flow rows and {result.get('n_activity', 0)} activity rows.")
            st.rerun()

    if latest_broker_date and latest_broker_date < date.today():
        missing_start = latest_broker_date + timedelta(days=1)
        if st.button(f"Fetch missing broker dates ({missing_start} to {date.today()})"):
            result = pipeline.backfill_broker_history(watchlist, missing_start, date.today(), refresh_prices=True)
            if result["n_broker"] == 0:
                st.error("No missing broker rows were stored. The broker API returned no usable rows; refresh the Stockbit token or try again after broker data is published.")
            else:
                st.success(f"Stored {result['n_broker']} flow rows and {result.get('n_activity', 0)} activity rows.")
                st.rerun()

    backfill_range = st.date_input("Historical backfill range", value=(date.today() - timedelta(days=90), date.today()))
    if st.button("Backfill broker history"):
        if isinstance(backfill_range, tuple) and len(backfill_range) == 2:
            result = pipeline.backfill_broker_history(watchlist, backfill_range[0], backfill_range[1], refresh_prices=True)
            if result["n_broker"] == 0:
                st.error("No broker rows were stored for that range. The broker API returned no usable rows; refresh the Stockbit token or check the selected dates.")
            else:
                st.success(f"Stored {result['n_broker']} flow rows and {result.get('n_activity', 0)} activity rows.")
                st.rerun()


window_start = analysis_ts - pd.Timedelta(days=lookback_days)
price_df = all_prices[all_prices["ticker"] == selected_ticker].copy()
broker_df = all_broker[all_broker["ticker"] == selected_ticker].copy()
activity_df = all_activity[all_activity["ticker"] == selected_ticker].copy()

price_window = price_df[(price_df["date"] >= window_start) & (price_df["date"] <= analysis_ts)].copy()
broker_window = broker_df[(broker_df["date"] >= window_start) & (broker_df["date"] <= analysis_ts)].copy()
activity_window = activity_df[(activity_df["date"] >= window_start) & (activity_df["date"] <= analysis_ts)].copy()

if broker_window.empty or activity_window.empty:
    st.warning("No broker history exists inside the selected date window.")
    st.stop()

px_row = price_at_or_before(price_df, analysis_ts)
signal_row = flow_row_at(broker_df, selected_ticker, analysis_ts)
activity_date = latest_activity_date(activity_df, selected_ticker, analysis_ts)
top_buy, top_sell = analysis.top_net_broker_summary(selected_ticker, trade_date=activity_date, top_n=6)
daily_smart = smart_daily_from_activity(activity_window)
profile_df = profile_flow_from_activity(activity_window)
scan_h = cached_broker_scan(tuple(watchlist), horizon, int(min_events), float(min_net_buy_b) * 1e9)
scan_10d = cached_broker_scan((selected_ticker,), 10, 5, 0.0)

close_value = float(px_row["close"]) if px_row is not None and pd.notna(px_row["close"]) else np.nan
ret_5d = return_to_date(price_df, analysis_ts, 5)
ret_10d = return_to_date(price_df, analysis_ts, 10)
foreign_5d = float(broker_window.sort_values("date").tail(5)["foreign_net_broker"].fillna(0).sum())
smart_cum = float(daily_smart["cumulative_net"].iloc[-1]) if not daily_smart.empty else np.nan
top_buyer = top_buy.iloc[0] if not top_buy.empty else None
top_seller = top_sell.iloc[0] if not top_sell.empty else None
conviction = conviction_score(signal_row.get("bandar_signal"), foreign_5d, scan_10d, selected_ticker)
score_value = float(conviction["score"])
score_tone_name, score_color = score_tone(score_value)
alerts = contradiction_alerts(signal_row.get("bandar_signal"), ret_5d, ret_10d, foreign_5d, smart_cum)

sig_10d = scan_10d[scan_10d["significant"].eq(True)].copy() if not scan_10d.empty else pd.DataFrame()
if sig_10d.empty:
    verdict = (
        f"{selected_ticker} shows {fmt_signal(signal_row.get('bandar_signal'))} with {fmt_pct(ret_5d)} over 5D and "
        f"{fmt_pct(ret_10d)} over 10D. The current read is directional, but broker-specific 10D validation is not yet statistically strong."
    )
else:
    best = sig_10d.sort_values(["p_value_one_sided", "mean_fwd_return"], ascending=[True, False]).iloc[0]
    verdict = (
        f"{selected_ticker} shows {fmt_signal(signal_row.get('bandar_signal'))}. Broker {best['broker_code']} is the strongest 10D validation: "
        f"{int(best['n_events'])} events, mean return {fmt_pct(best['mean_fwd_return'])}, "
        f"win rate {best['win_rate']:.0%}, p-value {best['p_value_one_sided']:.4f}."
    )

render_page_header(selected_ticker, analysis_ts, window_start, activity_date)

breakdown = (
    f"Granger p-value component: {conviction['causality_component']:.0f}/100 "
    f"(p={conviction['p_value'] if conviction['p_value'] is not None and pd.notna(conviction['p_value']) else 'n/a'}); "
    f"Signal component: {conviction['signal_component']:.0f}/100; "
    f"Foreign 5D component: {conviction['foreign_component']:.0f}/100; "
    f"Broker win-rate component: {conviction['broker_component']:.0f}/100 ({conviction['broker_note']})."
)

k1, k2, k3, k4, k5, k6 = st.columns(6)
with k1:
    render_metric_card("Conviction Score", f"{score_value:.1f}/100", "weighted model", score_tone_name, breakdown)
with k2:
    render_metric_card("Signal", fmt_signal(signal_row.get("bandar_signal")), "selected date")
with k3:
    render_metric_card("5D Return", fmt_pct(ret_5d), "price context", "positive" if (ret_5d or 0) >= 0 else "negative")
with k4:
    render_metric_card("Foreign Net 5D", fmt_rp(foreign_5d), "broker summary", "positive" if foreign_5d >= 0 else "negative")
with k5:
    render_metric_card("Top Buyer", str(top_buyer["broker_code"]) if top_buyer is not None else "-", fmt_rp(top_buyer["net_value"]) if top_buyer is not None else "", "positive")
with k6:
    render_metric_card("Smart Cumulative", fmt_rp(smart_cum), f"{len(daily_smart)} broker days", "positive" if (smart_cum or 0) >= 0 else "negative")

render_alerts(alerts)
render_verdict(verdict)

overview_tab, flow_tab, causality_tab, validation_tab, screener_tab, raw_tab = st.tabs(
    ["Overview", "Broker Flow", "Causality Insight", "Validation", "Screener", "Raw Tables"]
)

with overview_tab:
    left, right = st.columns([1.55, 0.95])
    with left:
        st.subheader("Price, Volume, and Signal Context")
        st.plotly_chart(
            interactive_price_context(price_df, broker_df, selected_ticker, window_start, analysis_ts),
            use_container_width=True,
            config={"displayModeBar": True, "scrollZoom": True},
        )
    with right:
        st.subheader("Top Brokers")
        broker_summary = top_broker_compact_table(top_buy, top_sell, activity_df, analysis_ts)
        if broker_summary.empty:
            st.caption("No broker rows for the selected date.")
        else:
            st.caption("This table shows broker net buy or sell on the selected analysis date only.")
            st.dataframe(style_table(broker_summary, money_cols=["Net on Analysis Date"]), use_container_width=True, hide_index=True, height=246)

        perf = analysis.price_performance_table(selected_ticker)
        if not perf.empty:
            st.caption("Price Performance")
            perf = perf[perf["timeframe"].isin(["1D", "1W", "1M", "3M", "6M", "YTD"])]
            perf_view = perf.rename(columns={"timeframe": "Period", "return": "Return"})
            st.dataframe(style_table(perf_view, pct_cols=["Return"]), use_container_width=True, hide_index=True, height=142)

    lower_left, lower_right = st.columns([1.1, 0.9])
    with lower_left:
        st.subheader("Smart-Money Daily Flow")
        st.plotly_chart(interactive_smart_flow(daily_smart), use_container_width=True, config={"displayModeBar": True, "scrollZoom": True})
    with lower_right:
        st.subheader("Profile Net Flow")
        profile_view = profile_compact_table(profile_df)
        if profile_view.empty:
            st.caption("No profile flow for this window.")
        else:
            st.dataframe(style_table(profile_view, money_cols=["Net"]), use_container_width=True, hide_index=True, height=246)
            with st.expander("Broker detail by profile", expanded=False):
                detail_view = profile_broker_detail_table(activity_window)
                st.dataframe(
                    style_table(detail_view, money_cols=["Buy", "Sell", "Net", "Avg Value / Tx"]),
                    use_container_width=True,
                    hide_index=True,
                    height=320,
                )

with flow_tab:
    st.subheader("Broker Drill-Down")
    broker_codes = sorted(activity_window["broker_code"].dropna().unique().tolist())
    ranked_codes = (
        activity_window.assign(abs_net=activity_window["net_value"].abs())
        .groupby("broker_code")["abs_net"]
        .sum()
        .sort_values(ascending=False)
        .index.tolist()
    )
    default_codes = ranked_codes[:3] if ranked_codes else broker_codes[:3]
    c1, c2, c3, c4 = st.columns([0.8, 0.9, 1.7, 0.9])
    with c1:
        compare_mode = st.toggle("Compare mode", value=True)
    with c2:
        max_brokers = st.selectbox("Max brokers", [3, 5, 8, 12, "All"], index=1)
    with c3:
        max_selections = None if max_brokers == "All" else int(max_brokers)
        default_selection = default_codes[: min(len(default_codes), max_selections or len(default_codes))]
        selected_brokers = st.multiselect("Broker codes", broker_codes, default=default_selection, max_selections=max_selections)
    with c4:
        flow_mode = st.selectbox("Flow mode", ["Cumulative", "Daily"])
    st.caption("Cumulative mode sums broker net flow across the selected broker window. Daily mode shows each date separately.")
    st.plotly_chart(interactive_broker_compare(activity_window, selected_brokers, flow_mode), use_container_width=True, config={"displayModeBar": True, "scrollZoom": True})

    left, right = st.columns([0.95, 1.05])
    with left:
        st.subheader("Broker Profile Flow")
        render_profile_flow(profile_df)
        profile_options = ["All Profiles"] + [row["label"] for _, row in profile_df.iterrows()] if not profile_df.empty else ["All Profiles"]
        selected_profile_label = st.selectbox("Profile detail", profile_options)
        selected_profile_key = None
        if selected_profile_label != "All Profiles" and not profile_df.empty:
            selected_profile_key = profile_df[profile_df["label"] == selected_profile_label]["profile"].iloc[0]
        detail_view = profile_broker_detail_table(activity_window, selected_profile_key)
        if detail_view.empty:
            st.caption("No broker detail for this profile.")
        else:
            st.dataframe(
                style_table(detail_view, money_cols=["Buy", "Sell", "Net", "Avg Value / Tx"]),
                use_container_width=True,
                hide_index=True,
                height=360,
            )
    with right:
        st.subheader("Broker Distribution")
        available_dist_dates = sorted(activity_window["date"].dt.date.unique().tolist())
        d1, d2 = st.columns([0.8, 1.2])
        with d1:
            distribution_mode = st.selectbox("Distribution mode", ["Single day", "Date range"])
        with d2:
            if distribution_mode == "Single day":
                dist_date = st.selectbox("Distribution date", available_dist_dates, index=len(available_dist_dates) - 1)
                dist_start = pd.Timestamp(dist_date)
                dist_end = pd.Timestamp(dist_date)
            else:
                default_start = available_dist_dates[max(0, len(available_dist_dates) - 5)]
                range_value = st.date_input(
                    "Distribution range",
                    value=(default_start, available_dist_dates[-1]),
                    min_value=available_dist_dates[0],
                    max_value=available_dist_dates[-1],
                )
                if isinstance(range_value, tuple) and len(range_value) == 2:
                    dist_start = pd.Timestamp(range_value[0])
                    dist_end = pd.Timestamp(range_value[1])
                else:
                    dist_start = pd.Timestamp(available_dist_dates[-1])
                    dist_end = pd.Timestamp(available_dist_dates[-1])

        dist = activity_window[
            (activity_window["date"] >= dist_start) & (activity_window["date"] <= dist_end)
        ].copy()
        if not dist.empty:
            dist = (
                dist.groupby(["broker_code", "participant_type"], dropna=False)
                .agg(
                    buy_value=("buy_value", "sum"),
                    sell_value=("sell_value", "sum"),
                    net_value=("net_value", "sum"),
                    frequency=("frequency", "sum"),
                    buy_lot=("buy_lot", "sum"),
                    sell_lot=("sell_lot", "sum"),
                    buy_avg_price=("buy_avg_price", "mean"),
                    sell_avg_price=("sell_avg_price", "mean"),
                )
                .reset_index()
            )
        if dist.empty:
            st.caption("No distribution rows for this date range.")
        else:
            distribution_api = {}
            try:
                distribution_api = cached_broker_distribution_api(selected_ticker, dist_start, dist_end)
            except Exception as exc:  # noqa: BLE001
                distribution_api = {}
                st.caption(f"Live distribution API unavailable for this date: {type(exc).__name__}. Falling back to estimated matching.")
            if distribution_api:
                st.caption("The flow chart below uses broker-to-broker distribution edges returned by the live API.")
            else:
                st.caption("Exact broker-to-broker counterparties are unavailable. The flow chart below falls back to estimated same-day matching based on broker net buy and sell totals.")
            st.plotly_chart(
                broker_distribution_sankey(dist, dist_end, distribution_data=distribution_api, top_n=8),
                use_container_width=True,
                config={"displayModeBar": True, "scrollZoom": True},
            )
            st.caption("Broker Summary")
            summary_view = broker_summary_table(dist, distribution_data=distribution_api, top_n=10)
            st.dataframe(
                summary_view.style.format(
                    {
                        "Buy Value": fmt_rp,
                        "Sell Value": fmt_rp,
                        "Buy Lot": lambda v: "-" if pd.isna(v) else f"{float(v):,.1f}K" if abs(float(v)) >= 1_000 else f"{float(v):,.0f}",
                        "Sell Lot": lambda v: "-" if pd.isna(v) else f"{float(v):,.1f}K" if abs(float(v)) >= 1_000 else f"{float(v):,.0f}",
                        "Buy Avg": lambda v: "-" if pd.isna(v) else f"{float(v):,.0f}",
                        "Sell Avg": lambda v: "-" if pd.isna(v) else f"{float(v):,.0f}",
                    }
                ),
                use_container_width=True,
                hide_index=True,
                height=320,
            )
            st.caption("Detailed broker rows")
            dist_view = dist[["broker_code", "participant_type", "buy_value", "sell_value", "net_value", "frequency"]].rename(
                columns={
                    "broker_code": "Broker",
                    "participant_type": "Type",
                    "buy_value": "Buy",
                    "sell_value": "Sell",
                    "net_value": "Net",
                    "frequency": "Freq",
                }
            )
            dist_view["Type"] = dist_view["Type"].map(participant_label)
            dist_view["Avg Value / Tx"] = dist_view.apply(lambda r: abs(float(r["Net"] or 0)) / max(float(r["Freq"] or 0), 1), axis=1)
            dist_view["Sub-type"] = dist_view.apply(broker_subtype, axis=1)
            st.dataframe(style_table(dist_view, money_cols=["Buy", "Sell", "Net", "Avg Value / Tx"]), use_container_width=True, hide_index=True)

with causality_tab:
    st.subheader("Causality Insight")
    foreign_causality = cached_causality(selected_ticker)
    c1, c2, c3 = st.columns(3)
    with c1:
        if foreign_causality:
            render_metric_card(
                "Foreign Flow Granger",
                "Significant" if foreign_causality["is_significant"] else "Not Significant",
                f"p={foreign_causality['min_p_value']:.4f}, lag {foreign_causality['best_lag']}",
                "positive" if foreign_causality["is_significant"] else "warning",
            )
        else:
            render_metric_card("Foreign Flow Granger", "Unavailable", "insufficient observations", "warning")
    with c2:
        render_metric_card("Conviction Model", f"{score_value:.1f}/100", "hover score card for formula", score_tone_name)
    with c3:
        render_metric_card("Broker Validation", conviction["broker_note"], "historical forward returns")

    left, right = st.columns(2)
    with left:
        st.subheader("Participant Type")
        part_causality = analysis.causality_by_participant(selected_ticker, max_lags=5)
        if part_causality.empty:
            st.caption("Insufficient participant history.")
        else:
            part_view = part_causality.rename(
                columns={"participant_type": "Participant", "best_lag": "Lag", "p_value": "P Value", "significant": "Significant"}
            )
            part_view["Participant"] = part_view["Participant"].map(english_text)
            st.dataframe(part_view.style.format({"P Value": "{:.4f}"}), use_container_width=True, hide_index=True)
    with right:
        st.subheader("Top Broker Causality")
        broker_causality = analysis.causality_by_broker(selected_ticker, top_n=15, max_lags=5)
        if broker_causality.empty:
            st.caption("Insufficient broker history.")
        else:
            broker_view = broker_causality.rename(
                columns={"broker_code": "Broker", "best_lag": "Lag", "p_value": "P Value", "significant": "Significant"}
            )
            st.dataframe(broker_view.style.format({"P Value": "{:.4f}"}), use_container_width=True, hide_index=True)

with validation_tab:
    st.subheader("Broker-Specific Return Validation")
    if scan_h.empty:
        st.caption("No broker passes the current validation settings.")
    else:
        view = scan_h[
            [
                "ticker",
                "broker_code",
                "n_events",
                "mean_fwd_return",
                "median_fwd_return",
                "win_rate",
                "avg_net_value",
                "total_net_value",
                "p_value_one_sided",
                "significant",
            ]
        ].rename(
            columns={
                "ticker": "Ticker",
                "broker_code": "Broker",
                "n_events": "Events",
                "mean_fwd_return": "Mean Return",
                "median_fwd_return": "Median Return",
                "win_rate": "Win Rate",
                "avg_net_value": "Avg Net Buy",
                "total_net_value": "Total Net Buy",
                "p_value_one_sided": "P Value",
                "significant": "Significant",
            }
        )
        st.dataframe(style_table(view, money_cols=["Avg Net Buy", "Total Net Buy"], pct_cols=["Mean Return", "Median Return", "Win Rate"]), use_container_width=True, hide_index=True)

    st.subheader("Accumulation Event Study")
    show_individual = st.toggle("Show individual event paths", value=False)
    event_table = analysis.event_study_table(
        tickers=[selected_ticker],
        horizons=(1, 3, 5, 10),
        lookback_days=lookback_days,
        signals=list(ACC_SIGNALS),
    )
    st.plotly_chart(
        interactive_event_ribbon(event_table, horizons=(1, 3, 5, 10), show_individual=show_individual),
        use_container_width=True,
        config={"displayModeBar": True, "scrollZoom": True},
    )
    if not event_table.empty:
        event_view = event_table.rename(
            columns={
                "ticker": "Ticker",
                "signal_date": "Signal Date",
                "bandar_signal": "Signal",
                "bandar_signal_score": "Signal Score",
                "t_plus_0d": "Signal Day",
                "t_plus_1d": "+1D",
                "t_plus_3d": "+3D",
                "t_plus_5d": "+5D",
                "t_plus_10d": "+10D",
            }
        )
        event_view["Signal"] = event_view["Signal"].map(fmt_signal)
        st.dataframe(event_view, use_container_width=True, hide_index=True)

with screener_tab:
    st.subheader("Multi-Ticker Screener")
    only_acc = st.toggle("Show only Accumulation / Strong Accumulation", value=True)
    screener = build_screener(watchlist, analysis_ts, scan_h, all_prices, all_broker, all_activity)
    if only_acc and not screener.empty:
        screener = screener[screener["Signal"].isin(["Accumulation", "Strong Accumulation", "Net Buy"])]
    if screener.empty:
        st.caption("No tickers match the current screener filter.")
    else:
        st.dataframe(
            style_table(screener, money_cols=["Foreign Net (5D)"], pct_cols=["5D Return"]),
            use_container_width=True,
            hide_index=True,
        )

with raw_tab:
    st.subheader("Broker-Flow Rows")
    flow_view = broker_window[
        ["date", "bandar_signal", "bandar_signal_score", "foreign_net_broker", "local_net_broker", "total_value"]
    ].rename(
        columns={
            "date": "Date",
            "bandar_signal": "Signal",
            "bandar_signal_score": "Score",
            "foreign_net_broker": "Foreign Net",
            "local_net_broker": "Local Net",
            "total_value": "Value",
        }
    )
    flow_view["Signal"] = flow_view["Signal"].map(fmt_signal)
    st.dataframe(style_table(flow_view, money_cols=["Foreign Net", "Local Net", "Value"]), use_container_width=True, hide_index=True)

    st.subheader("Broker Activity Rows")
    activity_view = activity_window[
        ["date", "broker_code", "participant_type", "buy_value", "sell_value", "net_value", "frequency"]
    ].rename(
        columns={
            "date": "Date",
            "broker_code": "Broker",
            "participant_type": "Type",
            "buy_value": "Buy",
            "sell_value": "Sell",
            "net_value": "Net",
            "frequency": "Freq",
        }
    )
    activity_view["Type"] = activity_view["Type"].map(participant_label)
    st.dataframe(style_table(activity_view, money_cols=["Buy", "Sell", "Net"]), use_container_width=True, hide_index=True)

st.caption(f"Database: {storage.config.DB_PATH}")

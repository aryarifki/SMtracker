"""Descriptive analysis — correlations and quick plots for the feature table.

Designed to be used from a notebook:

    from idx_bandarmology import features, analysis
    feat = features.build_feature_table()
    analysis.correlation_table(feat)
    analysis.plot_signal_vs_forward_return(feat, horizon=5)
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from math import erfc, sqrt

from . import storage

_SIGNAL_COLS = ["bandar_signal_score", "foreign_net_broker", "foreign_net_flow_pct"]
_RETURN_COLS = [
    "back_return_1d", "back_return_3d", "back_return_5d", "back_return_10d",
    "fwd_return_1d", "fwd_return_3d", "fwd_return_5d", "fwd_return_10d",
]

_BROKER_PROFILES: dict[str, str] = {}
for _codes, _profile in (
    (("AK", "BK", "MS", "GR", "LG", "KZ", "CS", "DX"), "smart_foreign"),
    (("SQ", "MG", "EP", "DR", "BZ"), "bandar_gorengan"),
    (("XA", "AZ", "KI", "YO", "ZP"), "retail"),
    (("CC", "NI", "OD", "TP", "IF"), "local_institutional"),
    (("YU", "RX", "PD"), "market_maker"),
):
    for _code in _codes:
        _BROKER_PROFILES[_code] = _profile

_PROFILE_META: dict[str, tuple[str, str]] = {
    "smart_foreign": ("Foreign Smart Money", "Directional foreign institutions with higher conviction"),
    "local_institutional": ("Local Institutions", "Local funds and institution-like accounts"),
    "market_maker": ("Market Makers", "Often active on both sides; net position matters"),
    "bandar_gorengan": ("Speculative Operators", "Potential pump-and-dump style participants"),
    "retail": ("Retail-Dominant", "Retail-heavy platforms, often late or contrarian"),
    "lainnya": ("Other Brokers", "Brokers outside the defined behavioral profiles"),
}
_SMART_PROFILES = ("smart_foreign", "local_institutional")


def broker_profile_of(code: str | None) -> str:
    return _BROKER_PROFILES.get((code or "").upper(), "lainnya")


def _forward_return_frame(tickers: list[str] | None = None, horizons: tuple[int, ...] = (1, 3, 5, 10)) -> pd.DataFrame:
    price_df = storage.read_prices(tickers)
    if price_df.empty:
        return pd.DataFrame()
    df = price_df.sort_values(["ticker", "date"]).copy()
    for h in horizons:
        df[f"fwd_return_{h}d"] = df.groupby("ticker")["close"].shift(-h) / df["close"] - 1.0
    return df[["ticker", "date", "close", *[f"fwd_return_{h}d" for h in horizons]]]


def broker_profile_flow_table(ticker: str, lookback_days: int = 30) -> pd.DataFrame:
    """Net broker flow grouped by behavioral profile."""
    activity = storage.read_broker_activity([ticker])
    if activity.empty:
        return pd.DataFrame()
    cutoff = activity["date"].max() - pd.Timedelta(days=lookback_days)
    sub = activity[activity["date"] >= cutoff].copy()
    if sub.empty:
        return pd.DataFrame()
    sub["profile"] = sub["broker_code"].map(broker_profile_of)

    broker_rows = (
        sub.groupby(["profile", "broker_code", "participant_type"], dropna=False)
        .agg(
            net=("net_value", "sum"),
            buy=("buy_value", "sum"),
            sell=("sell_value", "sum"),
            days=("date", "nunique"),
        )
        .reset_index()
    )
    totals = (
        broker_rows.groupby("profile", dropna=False)
        .agg(net=("net", "sum"), buy=("buy", "sum"), sell=("sell", "sum"), days=("days", "max"))
        .reset_index()
    )
    rows = []
    for profile in _PROFILE_META:
        total = totals[totals["profile"] == profile]
        members = broker_rows[broker_rows["profile"] == profile].copy()
        if total.empty and members.empty:
            continue
        label, characteristic = _PROFILE_META[profile]
        members["abs_net"] = members["net"].abs()
        top_members = members.sort_values("abs_net", ascending=False).head(6)
        rows.append({
            "profile": profile,
            "label": label,
            "characteristic": characteristic,
            "net": float(total["net"].iloc[0]) if not total.empty else 0.0,
            "buy": float(total["buy"].iloc[0]) if not total.empty else 0.0,
            "sell": float(total["sell"].iloc[0]) if not total.empty else 0.0,
            "top_brokers": top_members[["broker_code", "participant_type", "net"]].to_dict("records"),
        })
    return pd.DataFrame(rows)


def smart_money_daily_flow(ticker: str, lookback_days: int = 30) -> pd.DataFrame:
    """Daily smart-money net flow and cumulative net flow."""
    activity = storage.read_broker_activity([ticker])
    if activity.empty:
        return pd.DataFrame()
    cutoff = activity["date"].max() - pd.Timedelta(days=lookback_days)
    sub = activity[activity["date"] >= cutoff].copy()
    if sub.empty:
        return pd.DataFrame()
    sub["profile"] = sub["broker_code"].map(broker_profile_of)
    smart = sub[sub["profile"].isin(_SMART_PROFILES)]
    if smart.empty:
        return pd.DataFrame()
    daily = smart.groupby("date")["net_value"].sum().reset_index(name="smart_net").sort_values("date")
    daily["cumulative_net"] = daily["smart_net"].cumsum()
    return daily.reset_index(drop=True)


def plot_smart_money_daily_flow(ticker: str, lookback_days: int = 30) -> plt.Figure:
    daily = smart_money_daily_flow(ticker, lookback_days=lookback_days)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 5.5), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    if daily.empty:
        ax1.text(0.5, 0.5, "No smart-money daily flow available.", ha="center", va="center")
        ax1.set_axis_off()
        ax2.set_axis_off()
        return fig
    colors = np.where(daily["smart_net"] >= 0, "#10b981", "#e11d48")
    ax1.bar(daily["date"], daily["smart_net"] / 1e9, color=colors, width=0.8)
    ax1.axhline(0, color="#64748b", linewidth=0.8)
    ax1.set_ylabel("Daily net (Rp B)")
    ax1.set_title(f"{ticker.upper()} - smart-money daily flow")
    ax1.grid(axis="y", alpha=0.15)

    ax2.plot(daily["date"], daily["cumulative_net"] / 1e9, color="#e11d48", linewidth=1.8)
    ax2.axhline(0, color="#64748b", linewidth=0.8)
    ax2.set_ylabel("Cumulative (Rp B)")
    ax2.grid(axis="y", alpha=0.15)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def price_performance_table(ticker: str) -> pd.DataFrame:
    """Multi-timeframe close-to-close returns from stored prices."""
    price_df = storage.read_prices([ticker]).sort_values("date")
    if price_df.empty:
        return pd.DataFrame()
    latest = price_df.iloc[-1]
    rows = []
    windows = [
        ("1D", 1), ("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126),
        ("1Y", 252), ("3Y", 756), ("5Y", 1260), ("10Y", 2520),
    ]
    for label, periods in windows:
        if len(price_df) > periods:
            base = price_df.iloc[-periods - 1]["close"]
            ret = latest["close"] / base - 1 if base else np.nan
        else:
            ret = np.nan
        rows.append({"timeframe": label, "return": ret})

    ytd = np.nan
    same_year = price_df[price_df["date"].dt.year == latest["date"].year]
    if len(same_year) > 1:
        first = same_year.iloc[0]["close"]
        ytd = latest["close"] / first - 1 if first else np.nan
    rows.insert(5, {"timeframe": "YTD", "return": ytd})
    return pd.DataFrame(rows)


def latest_signal_components(ticker: str) -> dict[str, object]:
    """Latest aggregate signal and headline net values."""
    flow = storage.read_broker_flow([ticker]).sort_values("date")
    if flow.empty:
        return {}
    row = flow.iloc[-1].to_dict()
    return row


def top_net_broker_summary(ticker: str, trade_date: pd.Timestamp | str | None = None, top_n: int = 6) -> tuple[pd.DataFrame, pd.DataFrame]:
    activity = storage.read_broker_activity([ticker])
    if activity.empty:
        return pd.DataFrame(), pd.DataFrame()
    if trade_date is None:
        trade_date = activity["date"].max()
    trade_date = pd.to_datetime(trade_date)
    sub = activity[(activity["date"] == trade_date) & (activity["ticker"] == ticker.upper())].copy()
    if sub.empty:
        return pd.DataFrame(), pd.DataFrame()
    buyers = sub[sub["net_value"] > 0].sort_values("net_value", ascending=False).head(top_n)
    sellers = sub[sub["net_value"] < 0].sort_values("net_value", ascending=True).head(top_n)
    return buyers.reset_index(drop=True), sellers.reset_index(drop=True)


def broker_distribution_table(ticker: str, trade_date: pd.Timestamp | str | None = None, top_n: int = 10) -> pd.DataFrame:
    """Per-broker buy/sell/net rows for one ticker/date."""
    activity = storage.read_broker_activity([ticker])
    if activity.empty:
        return pd.DataFrame()
    if trade_date is None:
        trade_date = activity["date"].max()
    trade_date = pd.to_datetime(trade_date)
    sub = activity[(activity["ticker"] == ticker.upper()) & (activity["date"] == trade_date)].copy()
    if sub.empty:
        return pd.DataFrame()
    sub["gross_value"] = sub["buy_value"].fillna(0) + sub["sell_value"].fillna(0)
    return sub.sort_values("gross_value", ascending=False).head(top_n).reset_index(drop=True)


def plot_broker_flow(ticker: str, broker_codes: list[str] | None = None, lookback_days: int = 90) -> plt.Figure:
    """Daily cumulative net flow by broker, overlaid with close price."""
    ticker = ticker.upper()
    activity = storage.read_broker_activity([ticker])
    price_df = storage.read_prices([ticker])
    fig, ax = plt.subplots(figsize=(10, 5))
    if activity.empty:
        ax.text(0.5, 0.5, "No per-broker activity rows available. Run historical backfill first.", ha="center", va="center")
        ax.set_axis_off()
        return fig

    cutoff = activity["date"].max() - pd.Timedelta(days=lookback_days)
    sub = activity[(activity["date"] >= cutoff) & (activity["net_value"].notna())].copy()
    if broker_codes:
        sub = sub[sub["broker_code"].isin([b.upper() for b in broker_codes])]
    else:
        top = (
            sub.assign(abs_net=lambda d: d["net_value"].abs())
            .groupby("broker_code")["abs_net"].sum()
            .sort_values(ascending=False)
            .head(5)
            .index.tolist()
        )
        sub = sub[sub["broker_code"].isin(top)]

    if sub.empty:
        ax.text(0.5, 0.5, "No broker rows match the selected filters.", ha="center", va="center")
        ax.set_axis_off()
        return fig

    pivot = sub.pivot_table(index="date", columns="broker_code", values="net_value", aggfunc="sum").sort_index()
    pivot = pivot.cumsum() / 1e9
    for code in pivot.columns:
        ax.plot(pivot.index, pivot[code], marker="o", linewidth=1.8, label=code)
    ax.axhline(0, color="#666", linestyle="--", linewidth=0.9)
    ax.set_ylabel("Cumulative net value (Rp B)")
    ax.set_title(f"{ticker} - broker flow by daily net accumulation")
    ax.grid(alpha=0.15)

    if not price_df.empty:
        px = price_df[price_df["date"] >= cutoff].sort_values("date")
        if not px.empty:
            ax2 = ax.twinx()
            ax2.plot(px["date"], px["close"], color="#111827", linestyle="--", linewidth=1.5, alpha=0.65, label="Close")
            ax2.set_ylabel("Close price")
    ax.legend(loc="best", fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def plot_broker_distribution(ticker: str, trade_date: pd.Timestamp | str | None = None, top_n: int = 12) -> plt.Figure:
    """Buyer/seller distribution for one ticker/date."""
    dist = broker_distribution_table(ticker, trade_date=trade_date, top_n=top_n)
    fig, ax = plt.subplots(figsize=(10, 5))
    if dist.empty:
        ax.text(0.5, 0.5, "No broker distribution rows available.", ha="center", va="center")
        ax.set_axis_off()
        return fig
    dist = dist.sort_values("net_value")
    colors = np.where(dist["net_value"] >= 0, "#10b981", "#ef4444")
    ax.barh(dist["broker_code"], dist["net_value"] / 1e9, color=colors, alpha=0.9)
    ax.axvline(0, color="#666", linewidth=0.9)
    title_date = pd.to_datetime(dist["date"].iloc[0]).strftime("%Y-%m-%d")
    ax.set_title(f"{ticker.upper()} - broker distribution on {title_date}")
    ax.set_xlabel("Net value (Rp B), buy positive / sell negative")
    ax.grid(axis="x", alpha=0.15)
    fig.tight_layout()
    return fig


def broker_alpha_scan(
    tickers: list[str] | None = None,
    horizon: int = 5,
    min_events: int = 5,
    min_net_value: float = 0.0,
    group_by: tuple[str, ...] = ("ticker", "broker_code"),
) -> pd.DataFrame:
    """Find broker/ticker combinations where repeated net-buy events precede gains.

    Uses a one-sided normal approximation on the mean forward return. This is a
    screening statistic, not proof of causality.
    """
    activity = storage.read_broker_activity(tickers)
    returns = _forward_return_frame(tickers, horizons=(horizon,))
    target_col = f"fwd_return_{horizon}d"
    if activity.empty or returns.empty or target_col not in returns.columns:
        return pd.DataFrame()

    sub = activity[(activity["net_value"].fillna(0) > min_net_value)].copy()
    sub = sub.merge(returns[["ticker", "date", "close", target_col]], on=["ticker", "date"], how="left")
    sub = sub.dropna(subset=[target_col])
    if sub.empty:
        return pd.DataFrame()

    rows = []
    for keys, g in sub.groupby(list(group_by)):
        if not isinstance(keys, tuple):
            keys = (keys,)
        n = len(g)
        if n < min_events:
            continue
        vals = g[target_col].astype(float)
        mean = vals.mean()
        median = vals.median()
        win_rate = (vals > 0).mean()
        std = vals.std(ddof=1)
        t_stat = mean / (std / np.sqrt(n)) if std and std > 0 else np.nan
        p_value = 0.5 * erfc(float(t_stat) / sqrt(2)) if pd.notna(t_stat) else np.nan
        row = {col: key for col, key in zip(group_by, keys)}
        row.update({
            "n_events": n,
            "mean_fwd_return": mean,
            "median_fwd_return": median,
            "win_rate": win_rate,
            "avg_net_value": g["net_value"].mean(),
            "total_net_value": g["net_value"].sum(),
            "t_stat": t_stat,
            "p_value_one_sided": p_value,
            "significant": bool(n >= 5 and pd.notna(p_value) and p_value < 0.05 and mean > 0),
            "status": "ok" if n >= 5 else "need >=5 events",
        })
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    return (
        pd.DataFrame(rows)
        .sort_values(["significant", "p_value_one_sided", "mean_fwd_return"], ascending=[False, True, False])
        .reset_index(drop=True)
    )


def correlation_table(feat: pd.DataFrame) -> pd.DataFrame:
    """Pearson correlation between smart-money signals and return columns.

    Reads like: "if bandar_signal_score is high today, is back_return_5d or
    fwd_return_5d also higher, on average, across the whole watchlist &
    history we've collected?"
    """
    cols = [c for c in _SIGNAL_COLS + _RETURN_COLS if c in feat.columns]
    sub = feat[cols].apply(pd.to_numeric, errors="coerce")
    corr = sub.corr(method="pearson")
    return corr.loc[
        [c for c in _SIGNAL_COLS if c in corr.index],
        [c for c in _RETURN_COLS if c in corr.columns],
    ]


def correlation_by_ticker(feat: pd.DataFrame, signal_col: str = "bandar_signal_score",
                           target_col: str = "back_return_5d") -> pd.DataFrame:
    """Same correlation, but broken out per ticker — smart money's effect can
    differ a lot by stock (e.g. matters more for less-liquid names)."""
    empty = pd.DataFrame(columns=["ticker", "n_obs", "corr", "status"])
    if signal_col not in feat.columns or target_col not in feat.columns:
        return empty
    rows = []
    for tk, g in feat.groupby("ticker"):
        sub = g[[signal_col, target_col]].apply(pd.to_numeric, errors="coerce").dropna()
        n_obs = len(sub)
        corr = sub[signal_col].corr(sub[target_col]) if n_obs >= 2 else float("nan")
        status = "ok" if n_obs >= 5 else "need >=5 rows for a stable correlation"
        rows.append({"ticker": tk, "n_obs": n_obs, "corr": corr, "status": status})
    if not rows:
        return empty
    return pd.DataFrame(rows).sort_values(["n_obs", "corr"], ascending=[False, False]).reset_index(drop=True)


def summary_by_signal(feat: pd.DataFrame, target_col: str = "back_return_5d") -> pd.DataFrame:
    """Mean/median return grouped by bandar_signal bucket.

    With the default `back_return_5d`, this answers: "when a stock shows
    AKUMULASI today, how strong has its last 5-day return been?"
    """
    if "bandar_signal" not in feat.columns or target_col not in feat.columns:
        return pd.DataFrame()
    g = feat.dropna(subset=[target_col]).groupby("bandar_signal")[target_col]
    out = g.agg(n="count", mean_return="mean", median_return="median", std="std").reset_index()
    order = [
        "STRONG_DISTRIBUTION", "DISTRIBUTION", "NET_SELL", "NEUTRAL", "NET_BUY",
        "ACCUMULATION", "STRONG_ACCUMULATION",
        "DISTRIBUSI_KUAT", "DISTRIBUSI", "NETRAL", "AKUMULASI", "AKUMULASI_KUAT",
    ]
    out["_order"] = out["bandar_signal"].apply(lambda s: order.index(s) if s in order else 99)
    return out.sort_values("_order").drop(columns="_order").reset_index(drop=True)


def plot_signal_vs_return(feat: pd.DataFrame, horizon: int = 5,
                          signal_col: str = "bandar_signal_score",
                          direction: str = "back") -> plt.Figure:
    """Scatter + regression line: signal today vs historical or forward return."""
    prefix = "back" if direction == "back" else "fwd"
    target_col = f"{prefix}_return_{horizon}d"
    sub = feat[[signal_col, target_col, "ticker"]].dropna()
    fig, ax = plt.subplots(figsize=(7, 5))
    sns.regplot(data=sub, x=signal_col, y=target_col, ax=ax,
                scatter_kws={"alpha": 0.5, "s": 25}, line_kws={"color": "crimson"})
    ax.set_xlabel("Smart-money signal score (today)")
    ax.set_ylabel(f"{'Historical' if prefix == 'back' else 'Forward'} return, {horizon}d")
    ax.set_title(f"Smart money signal vs {horizon}-day {'historical' if prefix == 'back' else 'forward'} return")
    ax.axhline(0, color="gray", linewidth=0.8, linestyle="--")
    fig.tight_layout()
    return fig


def plot_signal_vs_forward_return(feat: pd.DataFrame, horizon: int = 5,
                                  signal_col: str = "bandar_signal_score") -> plt.Figure:
    """Backward-compatible wrapper for the old forward-return plot."""
    return plot_signal_vs_return(feat, horizon=horizon, signal_col=signal_col, direction="fwd")


def plot_signal_bucket_returns(feat: pd.DataFrame, target_col: str = "back_return_5d") -> plt.Figure:
    """Boxplot of returns, one box per bandar_signal bucket."""
    order = [
        "STRONG_DISTRIBUTION", "DISTRIBUTION", "NET_SELL", "NEUTRAL", "NET_BUY",
        "ACCUMULATION", "STRONG_ACCUMULATION",
        "DISTRIBUSI_KUAT", "DISTRIBUSI", "NETRAL", "AKUMULASI", "AKUMULASI_KUAT",
    ]
    sub = feat.dropna(subset=[target_col, "bandar_signal"]).copy()
    present = [o for o in order if o in sub["bandar_signal"].unique()]
    fig, ax = plt.subplots(figsize=(8, 5))
    sns.boxplot(data=sub, x="bandar_signal", y=target_col, order=present, hue="bandar_signal",
                palette="RdYlGn", legend=False, ax=ax)
    ax.axhline(0, color="gray", linewidth=0.8, linestyle="--")
    ax.set_xlabel("Smart-money signal")
    ax.set_ylabel(target_col)
    ax.set_title("Return distribution by smart-money signal")
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
    fig.tight_layout()
    return fig


def plot_price_with_signal(feat: pd.DataFrame, ticker: str) -> plt.Figure:
    """Full price history for one ticker, with colored markers on signal dates."""
    price_df = storage.read_prices([ticker])
    broker_df = storage.read_broker_flow([ticker])
    if price_df.empty:
        sub = feat[feat["ticker"] == ticker].dropna(subset=["close"]).sort_values("date").copy()
    else:
        sub = price_df.sort_values("date").copy()
        if not broker_df.empty:
            broker_cols = ["date", "bandar_signal", "bandar_signal_score"]
            sub = sub.merge(broker_df[broker_cols], on="date", how="left")
    color_map = {
        "STRONG_ACCUMULATION": "#1a9850", "ACCUMULATION": "#91cf60", "NET_BUY": "#91cf60",
        "NEUTRAL": "#999999",
        "DISTRIBUTION": "#fc8d59", "STRONG_DISTRIBUTION": "#d73027", "NET_SELL": "#fc8d59",
        "AKUMULASI_KUAT": "#1a9850", "AKUMULASI": "#91cf60",
        "NETRAL": "#999999", "DISTRIBUSI": "#fc8d59", "DISTRIBUSI_KUAT": "#d73027",
    }
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(sub["date"], sub["close"], color="#444", linewidth=1.2, label="Close")
    if "bandar_signal" in sub.columns:
        colors = sub["bandar_signal"].map(color_map).fillna("#cccccc")
        has_signal = sub["bandar_signal"].notna()
        ax.scatter(sub.loc[has_signal, "date"], sub.loc[has_signal, "close"],
                   c=colors[has_signal], s=40, zorder=3, label="Smart-money signal")
    ax.set_title(f"{ticker} - price and smart-money signal")
    ax.set_ylabel("Close price (Rp)")
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def plot_price_signal_panel(ticker: str) -> plt.Figure:
    """Repository-friendly chart: full price history plus signal-score bars."""
    price_df = storage.read_prices([ticker]).sort_values("date")
    broker_df = storage.read_broker_flow([ticker]).sort_values("date")
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )

    if not price_df.empty:
        ax1.plot(price_df["date"], price_df["close"], color="#1f2937", linewidth=1.8, label="Close")

    if not broker_df.empty and not price_df.empty:
        overlay = price_df.merge(
            broker_df[["date", "bandar_signal", "bandar_signal_score"]],
            on="date",
            how="inner",
        )
        color_map = {
            2: "#15803d",
            1: "#84cc16",
            0: "#9ca3af",
            -1: "#fb923c",
            -2: "#dc2626",
        }
        if not overlay.empty:
            colors = overlay["bandar_signal_score"].map(color_map).fillna("#9ca3af")
            ax1.scatter(overlay["date"], overlay["close"], c=colors, s=55, zorder=4, label="Signal date")
            ax2.bar(overlay["date"], overlay["bandar_signal_score"], color=colors, width=2.5)

    ax1.set_title(f"{ticker} - price with smart-money signal overlays")
    ax1.set_ylabel("Close price (Rp)")
    ax1.legend(loc="upper left")
    ax1.grid(alpha=0.15)

    ax2.axhline(0, color="#666", linewidth=0.9, linestyle="--")
    ax2.set_ylabel("Signal")
    ax2.set_ylim(-2.5, 2.5)
    ax2.set_yticks([-2, -1, 0, 1, 2])
    ax2.set_yticklabels(["Strong Dist.", "Dist.", "Neutral", "Acc.", "Strong Acc."])
    ax2.grid(alpha=0.15)

    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def signal_outcome_table(feat: pd.DataFrame, ticker: str, horizons: tuple[int, ...] = (1, 3, 5, 10)) -> pd.DataFrame:
    """One row per signal date showing what happened after the signal."""
    cols = ["ticker", "date", "close", "bandar_signal", "bandar_signal_score"]
    cols += [f"back_return_{h}d" for h in horizons if f"back_return_{h}d" in feat.columns]
    cols += [f"fwd_return_{h}d" for h in horizons if f"fwd_return_{h}d" in feat.columns]
    sub = feat[feat["ticker"] == ticker][cols].sort_values("date").copy()
    return sub.reset_index(drop=True)


def plot_signal_outcomes(feat: pd.DataFrame, ticker: str, horizon: int = 5) -> plt.Figure:
    """Bar chart of realized returns around each signal date for one ticker."""
    target_back = f"back_return_{horizon}d"
    target_fwd = f"fwd_return_{horizon}d"
    sub = signal_outcome_table(feat, ticker, horizons=(horizon,))

    fig, ax = plt.subplots(figsize=(10, 4))
    if sub.empty:
        ax.text(0.5, 0.5, "No signal rows available.", ha="center", va="center")
        ax.set_axis_off()
        return fig

    x = range(len(sub))
    width = 0.38
    if target_back in sub.columns:
        ax.bar([i - width / 2 for i in x], sub[target_back].fillna(0), width=width, color="#94a3b8", label=f"Historical {horizon}d")
    if target_fwd in sub.columns:
        vals = sub[target_fwd]
        ax.bar([i + width / 2 for i in x], vals.fillna(0), width=width, color="#2563eb", label=f"Forward {horizon}d")
        for i, val in enumerate(vals):
            if pd.isna(val):
                ax.text(i + width / 2, 0, "NA", ha="center", va="bottom", fontsize=8, color="#666")

    labels = [f"{d:%Y-%m-%d}\n{str(s).replace('_', ' ')}" for d, s in zip(sub["date"], sub["bandar_signal"])]
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=0, ha="center")
    ax.axhline(0, color="#666", linewidth=0.9, linestyle="--")
    ax.set_ylabel("Return")
    ax.set_title(f"{ticker} - returns around each signal date")
    ax.legend()
    fig.tight_layout()
    return fig


def event_study_table(
    tickers: list[str] | None = None,
    horizons: tuple[int, ...] = (1, 3, 5, 10),
    lookback_days: int | None = 90,
    signals: list[str] | tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Normalized price paths after each signal date, rebased to 100 at t=0."""
    price_df = storage.read_prices(tickers).sort_values(["ticker", "date"]).reset_index(drop=True)
    broker_df = storage.read_broker_flow(tickers).sort_values(["ticker", "date"]).reset_index(drop=True)
    if price_df.empty or broker_df.empty:
        return pd.DataFrame()

    if lookback_days is not None and not broker_df.empty:
        cutoff = broker_df["date"].max() - pd.Timedelta(days=lookback_days)
        broker_df = broker_df[broker_df["date"] >= cutoff].copy()

    if signals:
        broker_df = broker_df[broker_df["bandar_signal"].isin(list(signals))].copy()

    rows = []
    horizon_cols = [0, *horizons]
    for ticker, ticker_broker in broker_df.groupby("ticker"):
        ticker_prices = price_df[price_df["ticker"] == ticker].reset_index(drop=True)
        for signal_row in ticker_broker.itertuples():
            matched = ticker_prices.index[ticker_prices["date"] == signal_row.date]
            if len(matched) == 0:
                continue
            start_idx = int(matched[0])
            start_close = float(ticker_prices.loc[start_idx, "close"])
            row = {
                "ticker": ticker,
                "signal_date": signal_row.date,
                "bandar_signal": getattr(signal_row, "bandar_signal", None),
                "bandar_signal_score": getattr(signal_row, "bandar_signal_score", None),
            }
            for h in horizon_cols:
                idx = start_idx + h
                col = f"t_plus_{h}d"
                if h == 0:
                    row[col] = 100.0
                elif idx < len(ticker_prices):
                    row[col] = float(ticker_prices.loc[idx, "close"]) / start_close * 100.0
                else:
                    row[col] = np.nan
            rows.append(row)
    return pd.DataFrame(rows)


def plot_event_study(
    tickers: list[str] | None = None,
    horizons: tuple[int, ...] = (1, 3, 5, 10),
    lookback_days: int | None = 90,
    aggregate: bool = False,
    signals: list[str] | tuple[str, ...] | None = None,
) -> plt.Figure:
    """Event-study chart with signal date rebased to 100."""
    df = event_study_table(
        tickers=tickers,
        horizons=horizons,
        lookback_days=lookback_days,
        signals=signals,
    )
    fig, ax = plt.subplots(figsize=(10, 5))
    if df.empty:
        ax.text(0.5, 0.5, "No signal events available.", ha="center", va="center")
        ax.set_axis_off()
        return fig

    xs = [0, *horizons]
    color_map = {
        "STRONG_ACCUMULATION": "#15803d",
        "ACCUMULATION": "#84cc16",
        "NET_BUY": "#65a30d",
        "NEUTRAL": "#9ca3af",
        "NET_SELL": "#fb923c",
        "DISTRIBUTION": "#f97316",
        "STRONG_DISTRIBUTION": "#dc2626",
        "AKUMULASI_KUAT": "#15803d",
        "AKUMULASI": "#84cc16",
        "NETRAL": "#9ca3af",
        "DISTRIBUSI": "#f97316",
        "DISTRIBUSI_KUAT": "#dc2626",
    }

    if aggregate:
        for signal_name, group in df.groupby("bandar_signal"):
            ys = [group[f"t_plus_{h}d"].mean() for h in xs]
            color = color_map.get(signal_name, "#2563eb")
            ax.plot(xs, ys, marker="o", linewidth=2.5, color=color, alpha=0.95, label=str(signal_name).replace("_", " "))
    else:
        for row in df.itertuples():
            ys = [getattr(row, f"t_plus_{h}d") for h in xs]
            label = f"{row.ticker} | {row.signal_date:%Y-%m-%d} | {str(row.bandar_signal).replace('_', ' ')}"
            color = color_map.get(getattr(row, "bandar_signal", None), "#2563eb")
            ax.plot(xs, ys, marker="o", linewidth=2, color=color, alpha=0.85, label=label)

        if len(df) > 1:
            mean_vals = []
            for h in xs:
                mean_vals.append(df[f"t_plus_{h}d"].mean())
            ax.plot(xs, mean_vals, color="#111827", linewidth=3, linestyle="--", label="Average path")

    ax.axhline(100, color="#666", linewidth=0.9, linestyle="--")
    ax.set_xticks(xs)
    ax.set_xticklabels(["Signal", *[f"+{h}d" for h in horizons]])
    ax.set_ylabel("Normalized price (signal date = 100)")
    chart_scope = "selected events" if tickers else "all stored events"
    ax.set_title(f"Event study after signal dates ({chart_scope})")
    ax.grid(alpha=0.15)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    return fig



def _make_stationary(series: pd.Series) -> pd.Series:
    """Check stationarity using ADF test and apply first difference if non-stationary (p > 0.05)."""
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning)
    try:
        from statsmodels.tsa.stattools import adfuller
        if series.std() == 0 or len(series.dropna()) < 10:
            return series
        p_val = adfuller(series.dropna())[1]
        if p_val > 0.05:
            return series.diff()
    except ImportError:
        pass
    except Exception:
        pass
    return series


def causality_foreign_vs_price(ticker: str, max_lags: int = 5) -> dict[str, object] | None:
    """Test if foreign net flow Granger-causes price returns."""
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning)
    try:
        from statsmodels.tsa.stattools import grangercausalitytests
    except ImportError:
        return None
        
    prices = storage.read_prices([ticker]).sort_values("date")
    flow = storage.read_broker_flow([ticker]).sort_values("date")
    
    if prices.empty or flow.empty:
        return None
        
    df = pd.merge(prices[["date", "close"]], flow[["date", "foreign_net_broker"]], on="date", how="inner")
    df["return"] = _make_stationary(df["close"].pct_change())
    df["foreign_net_broker"] = _make_stationary(df["foreign_net_broker"])
    df = df.dropna(subset=["return", "foreign_net_broker"])
    
    if len(df) < max_lags * 3 + 2:
        return None
        
    data = df[["return", "foreign_net_broker"]].values
    try:
        results = grangercausalitytests(data, maxlag=max_lags)
        p_values = {lag: results[lag][0]["ssr_ftest"][1] for lag in range(1, max_lags + 1)}
        min_p = min(p_values.values())
        best_lag = [lag for lag, p in p_values.items() if p == min_p][0]
        
        return {
            "ticker": ticker,
            "best_lag": best_lag,
            "min_p_value": min_p,
            "is_significant": bool(min_p < 0.05),
            "n_obs": len(df)
        }
    except Exception:
        return None


def causality_by_participant(ticker: str, max_lags: int = 5) -> pd.DataFrame:
    """Test which participant types (Asing, Lokal, Pemerintah) Granger-cause price returns."""
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning)
    try:
        from statsmodels.tsa.stattools import grangercausalitytests
    except ImportError:
        return pd.DataFrame()

    prices = storage.read_prices([ticker]).sort_values("date")
    activity = storage.read_broker_activity([ticker]).sort_values("date")
    
    if prices.empty or activity.empty:
        return pd.DataFrame()
        
    prices["return"] = prices["close"].pct_change()
    # Group by date and participant_type
    grouped = activity.groupby(["date", "participant_type"])["net_value"].sum().reset_index()
    
    results_list = []
    for ptype in grouped["participant_type"].dropna().unique():
        sub = grouped[grouped["participant_type"] == ptype]
        df = pd.merge(prices[["date", "return"]], sub[["date", "net_value"]], on="date", how="inner")
        df["return"] = _make_stationary(df["return"])
        df["net_value"] = _make_stationary(df["net_value"])
        df = df.dropna()
        if len(df) < max_lags * 3 + 2:
            continue
            
        data = df[["return", "net_value"]].values
        try:
            res = grangercausalitytests(data, maxlag=max_lags)
            p_vals = {lag: res[lag][0]["ssr_ftest"][1] for lag in range(1, max_lags + 1)}
            min_p = min(p_vals.values())
            best_lag = [lag for lag, p in p_vals.items() if p == min_p][0]
            results_list.append({
                "participant_type": ptype,
                "best_lag": best_lag,
                "p_value": min_p,
                "significant": bool(min_p < 0.05)
            })
        except Exception:
            pass
            
    if not results_list:
        return pd.DataFrame()
    return pd.DataFrame(results_list).sort_values("p_value")


def causality_by_broker(ticker: str, top_n: int = 20, max_lags: int = 5) -> pd.DataFrame:
    """Test which specific brokers Granger-cause price returns."""
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning)
    try:
        from statsmodels.tsa.stattools import grangercausalitytests
    except ImportError:
        return pd.DataFrame()

    prices = storage.read_prices([ticker]).sort_values("date")
    activity = storage.read_broker_activity([ticker]).sort_values("date")
    
    if prices.empty or activity.empty:
        return pd.DataFrame()
        
    prices["return"] = prices["close"].pct_change()
    
    # Filter top brokers by absolute net value to save computation
    top_brokers = activity.assign(abs_net=activity["net_value"].abs()).groupby("broker_code")["abs_net"].sum().sort_values(ascending=False).head(top_n).index

    grouped = activity[activity["broker_code"].isin(top_brokers)].groupby(["date", "broker_code"])["net_value"].sum().reset_index()
    
    results_list = []
    for broker in grouped["broker_code"].unique():
        sub = grouped[grouped["broker_code"] == broker]
        df = pd.merge(prices[["date", "return"]], sub[["date", "net_value"]], on="date", how="inner")
        df["return"] = _make_stationary(df["return"])
        df["net_value"] = _make_stationary(df["net_value"])
        df = df.dropna()
        # Ensure we have enough data and variation
        if len(df) < max_lags * 3 + 2 or df["net_value"].std() == 0:
            continue
            
        data = df[["return", "net_value"]].values
        try:
            res = grangercausalitytests(data, maxlag=max_lags)
            p_vals = {lag: res[lag][0]["ssr_ftest"][1] for lag in range(1, max_lags + 1)}
            min_p = min(p_vals.values())
            best_lag = [lag for lag, p in p_vals.items() if p == min_p][0]
            results_list.append({
                "broker_code": broker,
                "best_lag": best_lag,
                "p_value": min_p,
                "significant": bool(min_p < 0.05)
            })
        except Exception:
            pass
            
    if not results_list:
        return pd.DataFrame()
    return pd.DataFrame(results_list).sort_values("p_value")

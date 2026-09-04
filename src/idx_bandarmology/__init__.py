"""idx_bandarmology — simple end-to-end bandarmology pipeline for IDX stocks.

Modules
-------
config        : .env loading, watchlist, paths, database config
broker_api    : broker-flow client and bandar detector parser (rate-limited)
prices        : yfinance client — OHLCV history for IDX tickers
storage       : Dual-engine read/write adapters (SQLite for dev, PostgreSQL+SQLAlchemy for prod)
pipeline      : orchestrates scrape -> clean -> store, with batching & timing
universe      : IDX listed companies manager (watchlist, idx30, lq45, idx80, all)
features      : turns raw broker/price tables into a single tidy feature table
analysis      : descriptive stats, correlations, plots
modeling      : regression + simple ML to test the "smart money -> price" hypothesis
"""

from . import config  # noqa: F401
from . import universe  # noqa: F401

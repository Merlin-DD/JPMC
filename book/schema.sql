CREATE TABLE IF NOT EXISTS positions (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    currency TEXT,
    shares REAL,
    financing_spread_bps REAL,
    sector TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT,
    ts TEXT,
    close REAL,
    is_stale INTEGER DEFAULT 0,
    PRIMARY KEY (ticker, ts)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    currency TEXT,
    ts TEXT,
    usd_per_unit REAL,
    is_stale INTEGER DEFAULT 0,
    PRIMARY KEY (currency, ts)
);

CREATE TABLE IF NOT EXISTS pnl_attribution (
    ticker TEXT,
    asof_date TEXT,
    equity_delta_pnl REAL,
    fx_pnl REAL,
    financing_pnl REAL,
    cross_pnl REAL,
    total_pnl REAL,
    recon_diff REAL,
    recon_ok INTEGER,
    PRIMARY KEY (ticker, asof_date)
);

CREATE TABLE IF NOT EXISTS commentary (
    asof_date TEXT PRIMARY KEY,
    text TEXT,
    generated_at TEXT,
    source TEXT
);

CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

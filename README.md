# synth_pnl — Synthetic Equity Swap Book

A small P&L attribution desk tool. It tracks a synthetic book of eight equity
swap positions across four venues and four currencies, decomposes each day's
P&L into four explicit legs, reconciles the decomposition against an
independently computed market-value change, and renders the result as a
live-refreshing dashboard with model-generated desk commentary.

The book:

| Ticker | Name | Currency | Venue |
| --- | --- | --- | --- |
| `AAPL` | Apple Inc | USD | NYSE |
| `MSFT` | Microsoft Corp | USD | NYSE |
| `0700.HK` | Tencent Holdings | HKD | HKEX |
| `0005.HK` | HSBC Holdings | HKD | HKEX |
| `7203.T` | Toyota Motor Corp | JPY | TSE |
| `6758.T` | Sony Group Corp | JPY | TSE |
| `SAP.DE` | SAP SE | EUR | XETRA |
| `SIE.DE` | Siemens AG | EUR | XETRA |

Everything is reported in USD.

---

## The attribution model

A position's day-over-day P&L is decomposed into four legs. `P` is the price in
local currency, `FX` is **USD per unit of local currency** (so a JPY position
carries an FX around 0.0067 and a USD position exactly 1.0), and `prev`/`t` are
consecutive market dates for that position.

```
equity_delta_pnl = shares × (P_t − P_prev) × FX_prev
fx_pnl           = shares × P_prev × (FX_t − FX_prev)
financing_pnl    = −(shares × P_prev × FX_prev) × (ref_rate + spread_bps/10000) × (days/360)
cross_pnl        = shares × (P_t − P_prev) × (FX_t − FX_prev)

total_pnl        = equity_delta_pnl + fx_pnl + financing_pnl + cross_pnl
```

### Why `cross_pnl` is its own leg

`cross_pnl` is the second-order term — the part of the move that is neither
purely price nor purely FX but the interaction of the two. Many desks quietly
absorb it into the equity or FX leg. Keeping it explicit is what makes the
decomposition **exact** rather than approximate. Writing `dP = P_t − P_prev` and
`dF = FX_t − FX_prev`:

```
equity + fx + cross = shares × (dP·FX_prev + P_prev·dF + dP·dF)
                    = shares × (P_t·FX_t − P_prev·FX_prev)
                    = change in USD market value
```

The three market legs sum precisely to the change in USD market value, with no
residual to explain away.

### Reconciliation

Every row is checked against a figure computed without reference to the four
legs — the raw change in USD market value plus the financing accrual:

```
recon_diff = total_pnl − [ (shares × P_t × FX_t − shares × P_prev × FX_prev) + financing_pnl ]
recon_ok   = |recon_diff| ≤ 0.01 USD
```

The identity above means this is exact in real arithmetic; the 0.01 USD
tolerance absorbs float64 rounding only. In practice residuals land around
1e-11. A row where `recon_ok` is false is flagged `BREAK` on the dashboard.

### Risk measures

- **Annualized volatility** — the standard deviation of daily book returns,
  scaled by √252. A return needs a denominator, so each day's P&L is divided by
  the book's **gross exposure at the prior mark**; P&L alone is a currency
  amount whose standard deviation scales with book size rather than describing
  risk. Days on which any venue was shut are excluded from the sample.
- **Gross / net exposure** — gross sums absolute USD market values, net sums
  signed ones. For a long-only book the two are equal; they diverge the moment
  a short is added.
- **Concentration** — the largest position's share of gross exposure, and the
  top-3 share.

---

## Architecture

```
                         scripts/refresh_prices.py
                         (local only — seed CSVs)
                                    │
                                    ▼
  yfinance ──► book/ingest.py ──► SQLite ◄── book/management/commands/seed.py
                    ▲            (book.sqlite3)
                    │                 │
        book/scheduler.py             ├──► book/attribution.py  (pure maths, no I/O)
        two daemon threads            │         ▲
        ├─ refresh   (60s)            │         │
        │   fetch_snapshot            └──► compute_attribution ──► pnl_attribution
        │   compute_attribution       │
        └─ commentary (900s)          ├──► book/commentary.py ──► Anthropic API ──► commentary
            generate_commentary       │         └─ rule-based fallback
                                      │
                                      └──► book/dashboard.py ─┐
                                           book/risk.py ──────┼──► Django views ──► HTML + /api/summary
                                                              │
                                                       (reads only — never
                                                        calls an external API)
```

### Data flow

1. **Ingestion** (`book/ingest.py`) — one batched `yf.download` per cycle covering
   every position ticker and FX cross. Each row stores `bar_ts` (when the market
   bar printed, tz-aware UTC) and `fetched_at` (when we polled) separately, so
   "the poller is alive" and "the data is current" stay distinguishable. A symbol
   missing from a response carries its last stored mark forward with
   `is_stale = 1`.
2. **Storage** — SQLite, WAL mode, accessed through raw parameterized SQL in
   `book/db.py`. There are no Django ORM models; `book/schema.sql` is the schema
   of record.
3. **Attribution** (`book/attribution.py`) — pure functions, no Django and no
   database, so the maths is unit-testable in isolation.
   `compute_attribution` is the plumbing that reads marks, pairs consecutive
   dates, and writes `pnl_attribution`.
4. **Views** (`book/views.py`, `book/dashboard.py`, `book/risk.py`) — read models
   only. The request path never calls yfinance or the Anthropic API; it reads
   cached rows. A small JS poll hits `/api/summary` on the same interval as the
   refresh loop.
5. **Commentary** (`book/commentary.py`) — asks Claude for 2–3 factual sentences
   about the day's attribution table, with a deterministic rule-based fallback.
   Written by the scheduler and the management command; the view only ever
   renders the cached row.

### Module map

| File | Responsibility |
| --- | --- |
| `book/schema.sql` | Table definitions (the schema of record) |
| `book/db.py` | Connection + raw SQL helpers, `system_state` accessors |
| `book/venues.py` | Exchange calendars, currency→venue map, `market_status()` |
| `book/marks.py` | Price/FX mark resolution, as-of FX lookup |
| `book/ingest.py` | `fetch_snapshot()` — batched yfinance poll |
| `book/attribution.py` | The four legs + reconciliation (pure) |
| `book/dashboard.py` | Read model for `/` and `/api/summary` |
| `book/risk.py` | Read model for `/risk` |
| `book/commentary.py` | Claude call + rule-based fallback |
| `book/scheduler.py` | Two daemon threads (refresh, commentary) |
| `scripts/refresh_prices.py` | Regenerates the committed seed CSVs (local only) |

### Routes

| Route | Purpose |
| --- | --- |
| `/` | Attribution dashboard — KPI strip, desk commentary, per-position table |
| `/risk` | Volatility, exposure by currency/sector, concentration, stacked-leg chart |
| `/api/summary` | JSON read model backing the auto-refresh poll |
| `/healthz` | Liveness probe — touches neither the database nor any external API |

---

## Running locally

Python 3.12, Django 6.1 (pinned in `requirements.txt`). With your environment
active and dependencies installed from `requirements.txt`:

```bash
python manage.py seed && python manage.py compute_attribution && python manage.py generate_commentary
```

```bash
DEBUG=True python manage.py runserver 8000
```

Then open `http://127.0.0.1:8000/`.

`seed` loads the committed CSVs in `data/`, so the app renders without network
access. To pull fresh market data before seeding:

```bash
python scripts/refresh_prices.py
```

To inspect the state of the database at any point:

```bash
python manage.py diagnose
```

### Management commands

| Command | What it does |
| --- | --- |
| `seed` | Creates tables from `schema.sql`, loads `data/*.csv`, initializes `system_state`. Idempotent; rebuilds a table whose columns no longer match the schema. |
| `compute_attribution [--since YYYY-MM-DD]` | Recomputes `pnl_attribution`. No argument is a full rebuild; `--since` is the incremental path the scheduler uses. |
| `generate_commentary [--asof-date YYYY-MM-DD] [--force]` | Writes desk commentary for the newest attributed date. Skips dates that already have model commentary; upgrades a rule-based row when the model becomes reachable. |
| `diagnose` | Row counts, `bar_ts` range, staleness split, newest-bar age, all `system_state` rows. |

### Configuration

All via environment variables (`.env` is loaded locally via python-dotenv).

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | `""` | Django secret key |
| `DEBUG` | `False` | Debug mode |
| `ALLOWED_HOSTS` | `""` | Comma-separated; `127.0.0.1` and `localhost` are always added |
| `DB_PATH` | `./book.sqlite3` | SQLite file location |
| `REFRESH_SECONDS` | `60` | Market-data refresh cadence |
| `COMMENTARY_SECONDS` | `900` | Commentary cadence |
| `ANTHROPIC_API_KEY` | *(unset)* | When unset, commentary uses the rule-based fallback |

### The background scheduler

Two daemon threads start from `BookConfig.ready()`, but only under a real server
process: every `manage.py` subcommand is blocked except `runserver`, and under
`runserver` only in the autoreloader's child (`RUN_MAIN=true`), so the parent
watcher does not start a second copy. `runserver --noreload` therefore does not
run the scheduler — use the reloading `runserver` or gunicorn.

### Tests

```bash
python -m pytest tests/ -v
```

`tests/test_attribution.py` covers the attribution maths: each leg in isolation,
the cross term, financing sign and ACT/360 scaling, reconciliation across a
mixed-currency book, and the USD case where FX and cross legs must be exactly
zero.

### Fonts

`static/css/desk.css` references five self-hosted `.woff2` files that are **not
committed**. Under `DEBUG=True` the browser falls back to the system stack and
everything works. `collectstatic` (and therefore any deploy) will **fail** until
they are added — WhiteNoise's manifest storage errors on unresolvable `url()`
references. See `static/fonts/README.md` for filenames and sources.

---

## Assumptions and Limitations

This is a demonstration tool. The following are deliberate simplifications, and
several would need to change before any figure here could be relied on.

**The positions are synthetic.** No real book exists. Share counts (50–2000) and
financing spreads (30–120 bps) are generated by a seeded RNG in
`scripts/refresh_prices.py`. Only the price and FX series are real.

**The 3.5% reference rate is an assumption.** `REF_RATE = 0.035` in
`book/attribution.py` is a flat, hardcoded funding rate applied to every
currency. A real book would source a term structure per currency; there is no
curve, no tenor, and no currency differentiation here.

**Financing accrues ACT/360 on actual calendar days.** Funding does not stop
because an exchange is shut, so a Friday→Monday roll accrues **three** days of
financing and a holiday-extended weekend accrues four. This is deliberate, not
an off-by-one.

**Prices come from a delayed public feed.** `yfinance` is an unofficial client
for a consumer endpoint — delayed, occasionally gappy, subject to change without
notice, and with no delivery guarantee or SLA. It is not a market data vendor.
Do not treat these marks as tradeable prices.

**Market-status logic ignores exchange holidays.** `market_status()` derives
open/closed purely from the current UTC time against each venue's nominal
Mon–Fri session window. A venue reports "open" on a public holiday, and the
HKEX/TSE intraday lunch breaks are not modelled. It is a rough liveness signal
for the UI, not a trading calendar.

**USD is the base currency.** Every figure is reported in USD, and the USD FX
row is a synthetic peg at exactly 1.0 rather than a fetched rate. There is no
support for reporting in another base.

**Volatility is indicative only.** It is computed on roughly two dozen daily
observations — currently **20**, after excluding 3 dates where a venue holiday
left the book incompletely marked. That is far too short a sample for a stable
estimate: the standard error on a 20-observation volatility is on the order of
15% of the estimate itself. Treat it as a sanity check on order of magnitude,
not a risk number.

### Further caveats

- **FX marks are dated by the UTC day of the print** (an end-of-day fixing
  convention), and joined to equity marks **as-of** — the most recent FX rate at
  or before the equity date. When FX ingestion lags price ingestion the rate
  used can be several days stale; `compute_attribution` reports the worst lag
  and warns above four days.
- **The book is long-only**, so gross and net exposure are identical. The code
  computes them separately and they diverge correctly if a short is added, but
  that path is untested against real data.
- **Single-process SQLite.** WAL mode plus a 5s busy timeout handles the
  scheduler's two threads alongside request traffic, but this will not survive
  horizontal scaling. One web process only.
- **Commentary is model-generated.** The system prompt confines it to the
  supplied attribution table and forbids speculation, advice, and forecasting,
  but it is not a substitute for reading the numbers. The panel tags each entry
  as model-generated or rule-based.
- **Attribution needs two consecutive marks**, so the first date a position
  appears produces no row.

"""P&L attribution maths for the synth_pnl book.

Pure functions only — no Django, no database, no I/O. Everything here is
a deterministic function of the numbers passed in, so it can be unit
tested directly and reused from anywhere (management command, scheduler,
future API layer).

Conventions
-----------
FX is quoted as **USD per unit of local currency** (so a JPY position
carries an FX around 0.0067, and a USD position carries exactly 1.0).
Every value returned is in USD.

A single-period move is decomposed into four legs::

    equity_delta_pnl = shares * (P_t - P_prev) * FX_prev
    fx_pnl           = shares * P_prev * (FX_t - FX_prev)
    financing_pnl    = -(shares * P_prev * FX_prev) * (ref + spread) * (days/360)
    cross_pnl        = shares * (P_t - P_prev) * (FX_t - FX_prev)

`cross_pnl` is the second-order term — the part of the move that is
neither purely price nor purely FX but the interaction of the two. It is
kept as its own explicit leg and is never folded into the equity or FX
legs, even though many desks quietly absorb it into one of them. Keeping
it separate is what makes the four legs sum *exactly* to the change in
USD market value (see `reconcile`).
"""

from dataclasses import dataclass

# Flat funding curve for the synthetic book — a real desk would source a
# term structure per currency; one constant is enough here.
REF_RATE = 0.035

# ACT/360: financing accrues over actual calendar days, on a 360-day year.
DAY_COUNT_BASIS = 360

BPS_PER_UNIT = 10_000

# Reconciliation tolerance in USD. The decomposition is exact in real
# arithmetic, so this only has to absorb float64 rounding.
RECON_TOLERANCE = 0.01


@dataclass(frozen=True)
class Attribution:
    """The four legs, their total, and the reconciliation result."""

    equity_delta_pnl: float
    fx_pnl: float
    financing_pnl: float
    cross_pnl: float
    total_pnl: float
    recon_diff: float
    recon_ok: bool


def market_value_usd(shares: float, price: float, fx: float) -> float:
    """Position value in USD at a point in time."""
    return shares * price * fx


def equity_delta_pnl(shares: float, price_prev: float, price_t: float, fx_prev: float) -> float:
    """Price move, held at the *previous* FX rate."""
    return shares * (price_t - price_prev) * fx_prev


def fx_pnl(shares: float, price_prev: float, fx_prev: float, fx_t: float) -> float:
    """FX move, applied to the *previous* local-currency notional."""
    return shares * price_prev * (fx_t - fx_prev)


def financing_pnl(
    shares: float,
    price_prev: float,
    fx_prev: float,
    spread_bps: float,
    days: int = 1,
    ref_rate: float = REF_RATE,
) -> float:
    """Cost of carrying the position, ACT/360.

    Negative for a long position: you pay to fund it.

    Day-count convention: ACT/360 accrues over *actual calendar days*,
    not trading days. Funding does not stop because the exchange is
    shut — you are still borrowing over the weekend — so a Friday->Monday
    roll costs three days of financing, and a holiday-extended weekend
    costs four. `days` is that actual gap; it defaults to 1 for the
    ordinary within-week case.
    """
    notional_usd = market_value_usd(shares, price_prev, fx_prev)
    rate = ref_rate + spread_bps / BPS_PER_UNIT
    return -notional_usd * rate * (days / DAY_COUNT_BASIS)


def cross_pnl(
    shares: float, price_prev: float, price_t: float, fx_prev: float, fx_t: float
) -> float:
    """Second-order price x FX interaction. Its own leg, always."""
    return shares * (price_t - price_prev) * (fx_t - fx_prev)


def reconcile(
    total_pnl: float,
    shares: float,
    price_prev: float,
    price_t: float,
    fx_prev: float,
    fx_t: float,
    financing: float,
) -> tuple[float, bool]:
    """Check the legs against an independently computed figure.

    The independent figure never touches the four legs: it is the raw
    change in USD market value plus the financing accrual. If the
    decomposition is right the two agree to within float noise.

    Returns ``(recon_diff, recon_ok)``.
    """
    mv_change = market_value_usd(shares, price_t, fx_t) - market_value_usd(
        shares, price_prev, fx_prev
    )
    independent_total = mv_change + financing
    diff = total_pnl - independent_total
    return diff, abs(diff) <= RECON_TOLERANCE


def attribute(
    shares: float,
    price_prev: float,
    price_t: float,
    fx_prev: float,
    fx_t: float,
    spread_bps: float,
    days: int = 1,
    ref_rate: float = REF_RATE,
) -> Attribution:
    """Full single-position, single-period attribution."""
    equity = equity_delta_pnl(shares, price_prev, price_t, fx_prev)
    fx = fx_pnl(shares, price_prev, fx_prev, fx_t)
    financing = financing_pnl(shares, price_prev, fx_prev, spread_bps, days, ref_rate)
    cross = cross_pnl(shares, price_prev, price_t, fx_prev, fx_t)

    total = equity + fx + financing + cross
    diff, ok = reconcile(total, shares, price_prev, price_t, fx_prev, fx_t, financing)

    return Attribution(
        equity_delta_pnl=equity,
        fx_pnl=fx,
        financing_pnl=financing,
        cross_pnl=cross,
        total_pnl=total,
        recon_diff=diff,
        recon_ok=ok,
    )

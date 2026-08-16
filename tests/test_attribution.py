"""Unit tests for the pure attribution maths in book/attribution.py.

No Django, no database — these run against plain numbers.
"""

import pytest

from book.attribution import (
    RECON_TOLERANCE,
    REF_RATE,
    attribute,
    financing_pnl,
    market_value_usd,
)

# A representative multi-currency book: (ticker, shares, price_prev,
# price_t, fx_prev, fx_t, spread_bps). FX is USD per unit of local.
MIXED_BOOK = [
    ("AAPL", 1200, 313.39, 316.22, 1.0, 1.0, 45),
    ("MSFT", 800, 502.10, 498.75, 1.0, 1.0, 60),
    ("0700.HK", 1500, 402.60, 409.80, 0.12751, 0.12762, 85),
    ("0005.HK", 2000, 98.45, 97.90, 0.12751, 0.12762, 70),
    ("7203.T", 900, 2810.00, 2845.50, 0.006712, 0.006688, 55),
    ("6758.T", 400, 3390.00, 3362.00, 0.006712, 0.006688, 95),
    ("SAP.DE", 650, 241.80, 244.35, 1.0842, 1.0871, 30),
    ("SIE.DE", 300, 187.25, 185.40, 1.0842, 1.0871, 120),
]


def test_pure_price_move_only_hits_equity_leg():
    """Price moves, FX flat -> equity leg carries it all, cross vanishes."""
    result = attribute(
        shares=100,
        price_prev=50.0,
        price_t=55.0,
        fx_prev=0.9,
        fx_t=0.9,
        spread_bps=50,
    )

    # 100 * (55 - 50) * 0.9
    assert result.equity_delta_pnl == pytest.approx(450.0)
    assert result.fx_pnl == 0.0
    assert result.cross_pnl == 0.0
    assert result.recon_ok


def test_pure_fx_move_only_hits_fx_leg():
    """FX moves, price flat -> fx leg carries it all, cross vanishes."""
    result = attribute(
        shares=100,
        price_prev=50.0,
        price_t=50.0,
        fx_prev=0.90,
        fx_t=0.95,
        spread_bps=50,
    )

    # 100 * 50 * (0.95 - 0.90)
    assert result.fx_pnl == pytest.approx(250.0)
    assert result.equity_delta_pnl == 0.0
    assert result.cross_pnl == 0.0
    assert result.recon_ok


def test_both_moving_produces_nonzero_cross_leg():
    """The interaction term is real and must not be silently absorbed."""
    result = attribute(
        shares=100,
        price_prev=50.0,
        price_t=55.0,
        fx_prev=0.90,
        fx_t=0.95,
        spread_bps=50,
    )

    # 100 * (55 - 50) * (0.95 - 0.90)
    assert result.cross_pnl == pytest.approx(25.0)
    assert result.cross_pnl != 0.0
    assert result.equity_delta_pnl != 0.0
    assert result.fx_pnl != 0.0
    assert result.recon_ok


@pytest.mark.parametrize("spread_bps", [0, 30, 75, 120])
@pytest.mark.parametrize("price_t", [45.0, 50.0, 55.0])
def test_financing_is_always_negative_for_a_long(spread_bps, price_t):
    """You pay to carry a long, whichever way the market went."""
    result = attribute(
        shares=100,
        price_prev=50.0,
        price_t=price_t,
        fx_prev=0.9,
        fx_t=0.92,
        spread_bps=spread_bps,
    )
    assert result.financing_pnl < 0.0


def test_financing_accrues_act_360_over_actual_days():
    """A Fri->Mon roll accrues three days, not one."""
    one_day = financing_pnl(100, 50.0, 0.9, spread_bps=50, days=1)
    three_days = financing_pnl(100, 50.0, 0.9, spread_bps=50, days=3)

    assert three_days == pytest.approx(3 * one_day)
    # -(100 * 50 * 0.9) * (0.035 + 0.005) * (1/360)
    assert one_day == pytest.approx(-(4500.0) * (REF_RATE + 0.005) / 360)


def test_reconciliation_passes_across_a_mixed_book():
    """Every leg set ties back to MV change + financing, position by
    position and in aggregate."""
    total_legs = 0.0
    total_independent = 0.0

    for ticker, shares, p_prev, p_t, fx_prev, fx_t, spread in MIXED_BOOK:
        result = attribute(
            shares=shares,
            price_prev=p_prev,
            price_t=p_t,
            fx_prev=fx_prev,
            fx_t=fx_t,
            spread_bps=spread,
        )

        assert result.recon_ok, f"{ticker} failed recon: diff={result.recon_diff}"
        assert abs(result.recon_diff) <= RECON_TOLERANCE

        # The four legs must sum to the reported total.
        assert result.total_pnl == pytest.approx(
            result.equity_delta_pnl
            + result.fx_pnl
            + result.financing_pnl
            + result.cross_pnl
        )

        mv_change = market_value_usd(shares, p_t, fx_t) - market_value_usd(
            shares, p_prev, fx_prev
        )
        total_legs += result.total_pnl
        total_independent += mv_change + result.financing_pnl

    assert total_legs == pytest.approx(total_independent, abs=RECON_TOLERANCE)


def test_usd_positions_have_no_fx_or_cross_pnl():
    """With FX pinned at 1.0 there is no currency exposure to attribute."""
    for ticker, shares, p_prev, p_t, _fx_prev, _fx_t, spread in MIXED_BOOK:
        result = attribute(
            shares=shares,
            price_prev=p_prev,
            price_t=p_t,
            fx_prev=1.0,
            fx_t=1.0,
            spread_bps=spread,
        )

        assert result.fx_pnl == 0.0, f"{ticker} leaked fx pnl"
        assert result.cross_pnl == 0.0, f"{ticker} leaked cross pnl"
        # Everything real lands in the equity leg.
        assert result.equity_delta_pnl == pytest.approx(shares * (p_t - p_prev))
        assert result.recon_ok

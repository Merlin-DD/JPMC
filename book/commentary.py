"""Desk commentary for a day's P&L attribution.

Two paths, and the caller cannot tell which one ran except by the `source`
column: `generate()` asks Claude for a short factual summary, and falls
back to a deterministic rule-based sentence whenever the model is
unavailable. It never raises — a dashboard that renders no commentary is
better than a dashboard that 500s, and a broken commentary path must not
take ingestion down with it.

The view never calls this. Commentary is written to the `commentary`
table by the management command and the scheduler; `/` only ever reads
the cached row.
"""

import logging
import os
from datetime import datetime, timezone

from book.db import execute, fetch_all, fetch_one

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 300

# 2-3 factual sentences. Deliberately written at normal volume — current
# models follow a plainly-stated constraint, and stacking CRITICAL/MUST
# on every line makes the real constraints harder to pick out.
SYSTEM_PROMPT = (
    "You are a desk analyst writing end-of-day commentary on a synthetic equity "
    "swap book. Given the day's P&L attribution table, write 2-3 sentences of "
    "plain factual commentary.\n\n"
    "Say which leg dominated the day's P&L and which positions contributed most. "
    "Every figure you cite must appear in the table.\n\n"
    "Confine yourself to what the table shows. Do not attribute the moves to "
    "market events, company news, or macro conditions — none of that is in your "
    "data. Do not give trading advice or recommendations, and do not forecast. "
    "All figures are USD."
)

LATEST_ASOF = "SELECT MAX(asof_date) AS asof_date FROM pnl_attribution"

ATTRIBUTION_FOR_DATE = """
    SELECT a.ticker, p.name, p.currency, p.sector,
           a.equity_delta_pnl, a.fx_pnl, a.financing_pnl, a.cross_pnl, a.total_pnl
    FROM pnl_attribution a
    JOIN positions p ON p.ticker = a.ticker
    WHERE a.asof_date = ?
    ORDER BY ABS(a.total_pnl) DESC
"""

EXISTING_COMMENTARY = "SELECT source, text, generated_at FROM commentary WHERE asof_date = ?"

COUNT_FOR_DATE = "SELECT COUNT(*) AS n FROM pnl_attribution WHERE asof_date = ?"

UPSERT_COMMENTARY = """
    INSERT INTO commentary (asof_date, text, generated_at, source)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(asof_date) DO UPDATE SET
        text = excluded.text,
        generated_at = excluded.generated_at,
        source = excluded.source
"""

LEGS = (
    ("equity_delta_pnl", "equity delta"),
    ("fx_pnl", "FX"),
    ("financing_pnl", "financing"),
    ("cross_pnl", "cross"),
)


def _abs_usd(value: float) -> str:
    """Magnitude only, thousands-separated, 2dp.

    Prose gets direction words ("contributed" / "detracted") rather than
    the table's accounting parentheses — "a loss of (36.21)" reads badly
    in a sentence, and the sign is never left to formatting alone.
    """
    return f"{abs(value):,.2f}"


def build_table(rows) -> str:
    """The compact table handed to the model.

    Fixed-width text rather than JSON: fewer tokens for the same content,
    and the column headers name the legs so the model doesn't have to
    infer what `cross` means.
    """
    header = (
        f"{'TICKER':<9} {'NAME':<24} {'CCY':<4} "
        f"{'EQUITY':>12} {'FX':>10} {'FINANCING':>11} {'CROSS':>9} {'TOTAL':>12}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row['ticker']:<9} {row['name'][:24]:<24} {row['currency']:<4} "
            f"{row['equity_delta_pnl']:>12,.2f} {row['fx_pnl']:>10,.2f} "
            f"{row['financing_pnl']:>11,.2f} {row['cross_pnl']:>9,.2f} "
            f"{row['total_pnl']:>12,.2f}"
        )

    totals = {column: sum(r[column] for r in rows) for column, _ in LEGS}
    book_total = sum(r["total_pnl"] for r in rows)
    lines.append("-" * len(header))
    lines.append(
        f"{'BOOK':<9} {'':<24} {'USD':<4} "
        f"{totals['equity_delta_pnl']:>12,.2f} {totals['fx_pnl']:>10,.2f} "
        f"{totals['financing_pnl']:>11,.2f} {totals['cross_pnl']:>9,.2f} "
        f"{book_total:>12,.2f}"
    )
    return "\n".join(lines)


def fallback_text(asof_date: str, rows) -> str:
    """Deterministic rule-based commentary.

    Same two facts the model is asked for — the dominant leg and the
    largest contributing position — so a fallback day reads like a
    terser version of a normal day rather than an error message.
    """
    if not rows:
        return f"No attribution rows for {asof_date}."

    totals = {column: sum(r[column] for r in rows) for column, _ in LEGS}
    book_total = sum(r["total_pnl"] for r in rows)

    leg_column, leg_label = max(LEGS, key=lambda leg: abs(totals[leg[0]]))
    leg_value = totals[leg_column]
    leg_verb = "contributing" if leg_value >= 0 else "detracting"

    top = max(rows, key=lambda r: abs(r["total_pnl"]))
    top_verb = "contributed" if top["total_pnl"] >= 0 else "detracted"

    book_verb = "gained" if book_total >= 0 else "lost"

    return (
        f"The book {book_verb} {_abs_usd(book_total)} USD on {asof_date} across "
        f"{len(rows)} positions. The {leg_label} leg dominated, {leg_verb} "
        f"{_abs_usd(leg_value)} USD. {top['ticker']} was the largest single "
        f"position by absolute impact, having {top_verb} {_abs_usd(top['total_pnl'])} USD."
    )


def _call_claude(table: str) -> str:
    """One Messages API call. Raises on any failure — `generate` catches."""
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        # This is short factual summarisation, not reasoning work: thinking
        # off with low effort keeps latency and spend down, and the task
        # is well inside what the model does without deliberation.
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": table}],
    )

    text = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    if not text:
        raise ValueError(f"model returned no text (stop_reason={response.stop_reason})")
    return text


def generate(conn, asof_date: str) -> dict:
    """Write commentary for one market date. Never raises.

    The write is skipped when the text and source it produces are already
    what the table holds. That is what lets a rule-based row settle: with
    no model available the fallback sentence is a pure function of the
    day's rows, so an unchanged book produces an identical sentence and
    `generated_at` stops advancing. It is a no-op guard on the write, not
    on the work — the model is still attempted every cycle, so a fallback
    row is replaced the moment the model becomes reachable.

    Returns a summary dict: `action` is "generated" or "unchanged",
    `source` is "claude" or "fallback", and `error` carries why the model
    path was skipped or failed.
    """
    rows = fetch_all(conn, ATTRIBUTION_FOR_DATE, (asof_date,))
    existing = fetch_one(conn, EXISTING_COMMENTARY, (asof_date,))

    if not rows:
        source = "fallback"
        text = fallback_text(asof_date, rows)
        error = "no attribution rows for this date"
    else:
        table = build_table(rows)
        error = None
        source = "claude"
        text = None

        if not os.environ.get("ANTHROPIC_API_KEY"):
            error = "ANTHROPIC_API_KEY is not set"
            logger.info("commentary: %s, using rule-based fallback", error)
        else:
            try:
                text = _call_claude(table)
            except Exception as exc:  # noqa: BLE001 - must never propagate
                error = f"{type(exc).__name__}: {exc}"
                logger.exception("commentary: model call failed, using rule-based fallback")

        if text is None:
            source = "fallback"
            text = fallback_text(asof_date, rows)

    if existing is not None and existing["source"] == source and existing["text"] == text:
        return {
            "asof_date": asof_date,
            "action": "unchanged",
            "source": source,
            "text": text,
            "generated_at": existing["generated_at"],
            "error": error,
            "positions": len(rows),
        }

    # Minted only when something is actually written, so an unchanged row
    # keeps the timestamp of the last real change.
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    execute(conn, UPSERT_COMMENTARY, (asof_date, text, generated_at, source))
    return {
        "asof_date": asof_date,
        "action": "generated",
        "source": source,
        "text": text,
        "generated_at": generated_at,
        "error": error,
        "positions": len(rows),
    }

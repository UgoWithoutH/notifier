"""Linear waterfall decomposition of THIS MONTH's gross return (%),
ANNUALIZED by a simple x12 (not compounding), the sibling of
shared/xirr_waterfall.py's since-inception XIRR waterfall.

Added 2026-09-14 per explicit user request: alongside the existing
since-inception "XIRR"/"XIRR Intérêts"/"XIRR Cash drag"/"XIRR Bonus"/"XIRR
Taxes"/"XIRR Frais" pie-chart block, each platform now ALSO reports a
"Rendements % brut" figure for the calendar month of the run - split into
"Intérêts brut %" / "Cash drag brut %" / "Bonus brut %" / "Frais brut %" /
"Taxes brut %" shares that sum EXACTLY to "Rendements % brut".

UPDATED 2026-09-14 (same day): annualized via a plain x12 multiplier per
explicit user request, rather than left as the raw one-month figure. This
is a SIMPLE (linear) annualization - "if every month looked like this one,
12 of them in a row" - NOT a compounding one: it does not account for
reinvestment/compounding the way XIRR's own annualization does (XIRR
solves `(1 + rate)^t` over real elapsed time). The two are deliberately
different scales/methodologies and shouldn't be expected to match exactly
even for an account with a stable monthly return - "Rendements % brut"
answers "at this month's pace, annualized linearly", while "XIRR" answers
"the real annualized, compounding, money-weighted return since inception".

Why this does NOT need shared/xirr_waterfall.py's Newton-Raphson/bisection
machinery: unlike XIRR, a single calendar month's SIMPLE return (gain
divided by that month's average balance) is perfectly LINEAR in its
component gains/losses:
    (a + b + c + d + e) / avg_balance == a/avg_balance + b/avg_balance + ...
and multiplying a sum by a constant (x12) distributes over it the same
way, so each step's share is just that step's own EUR delta divided by
the month's average TOTAL balance (invested + non-invested/idle cash),
times 12 - no re-solving, no reconciliation gap to fold anywhere, shares
sum EXACTLY to the total by construction (floating-point rounding aside).

Same "walk from a 0% baseline in a fixed order" mental model as the XIRR
waterfall, for consistency across the two blocks:
    Intérêts -> Cash drag -> Bonus -> Frais -> Taxes
using GROSS interest (not net) at the "Intérêts" step, and a positive
step delta whenever it should be interpreted as "increasing the month's
gross return" - each caller already computes cash drag's EUR equivalent
for its existing "Cash drag brut" figure (`cash_drag_brut_value *
avg_total_balance`, where `cash_drag_brut_value = cash_weight *
monthly_yield_rate_brut`, see any *_diversification.py's run()); the
"Intérêts" step here should add that same amount back (GROSS, as-if-fully-
invested interest) and the "Cash drag" step subtracts it right after -
mirroring shared/xirr_waterfall.py's own "Intérêts -> Cash drag" pairing -
so "Cash drag brut %" comes out NEGATIVE (a real cost, consistent with
"XIRR Cash drag"'s own sign), not a separately-signed positive magnitude.
"""

DEFAULT_TOLERANCE = 0.0001
MONTHS_PER_YEAR = 12


def compute_monthly_yield_shares(
    avg_total_balance: float,
    steps: list[tuple[str, float]],
    log=None,
    log_context: str = "",
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, float | None]:
    """Decompose this month's simple gross return into one additive,
    ANNUALIZED (x12, non-compounding - see module docstring) share per
    step (an ORDERED list of (name, delta_eur) pairs), each share being
    `(delta_eur / avg_total_balance) * 12`.

    Returns {step_name: share_or_None} - None for every step if
    `avg_total_balance` isn't strictly positive (soft-fail, same
    convention as compute_waterfall_xirr_shares - a None means "couldn't
    be computed this run", not "was computed as zero").

    Since this is linear (unlike XIRR), shares always sum EXACTLY to
    `(sum(delta for _, delta in steps) / avg_total_balance) * 12` - the
    `tolerance` check is purely a floating-point sanity net (logged as a
    warning if it ever fires, which would indicate a bug in this
    function, not real-world data noise).
    """
    if avg_total_balance <= 0:
        return {name: None for name, _ in steps}

    shares = {name: (delta / avg_total_balance) * MONTHS_PER_YEAR for name, delta in steps}

    if log is not None:
        total = (sum(delta for _, delta in steps) / avg_total_balance) * MONTHS_PER_YEAR
        gap = abs(sum(shares.values()) - total)
        if gap > tolerance:
            log.warning(
                "Monthly yield waterfall decomposition%s: sum of shares (%.4f pts) != total (%.4f pts), gap %.4f pts.",
                f" ({log_context})" if log_context else "", sum(shares.values()) * 100, total * 100, gap * 100,
            )

    return shares
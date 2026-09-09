"""Waterfall (successive-palier) decomposition of the XIRR gap, walked from
a TRUE 0%-return baseline up to the real XIRR - so the shares sum EXACTLY
to the real XIRR (not to an arbitrary "all factors neutralized" gap).

Added 2026-09-09 as an ALTERNATIVE to shared/xirr_shapley.py's Shapley-value
decomposition, for side-by-side comparison only - NOT wired into any
*_diversification.py script (see _compare_shapley_vs_waterfall.py).

Unlike shared/xirr_shapley.py (which neutralizes factors FROM the real
value - its "all neutralized" reference point is generally NOT a 0% return,
because e.g. cancelling "Intérêts" via `-net_interest` while also having a
separate "Taxes" term double-counts the tax, and "Cash drag" is a
hypothetical missed-earnings amount that was never really in the account),
this walks UP from a literal 0% baseline (get back exactly the net capital
deposited, no more no less) by ADDING each step's real delta in a FIXED
order, so that:
    share(step_k) = XIRR(cumulative through step_k) - XIRR(cumulative through step_{k-1})
telescopes to `sum(shares) == XIRR_real - 0% == XIRR_real` PROVIDED the
steps' deltas reconstruct `base_value` exactly by the last step (checked at
runtime below). E.g. for the "intérêts -> cash drag -> bonus -> frais ->
taxes" order: use GROSS interest (not net) at the "Intérêts" step, then
SUBTRACT the missed-earnings amount at "Cash drag" (real cost of idle cash
pulling the ideal gross interest back down to what was actually earned),
then add bonus, subtract fees, subtract withholding tax last - each euro is
counted exactly once.
"""

from datetime import date

from shared.xirr import compute_xirr

DEFAULT_TOLERANCE = 0.0001
DEFAULT_VALUE_TOLERANCE = 0.01  # 1 cent - steps must reconstruct base_value almost exactly


def compute_waterfall_xirr_shares(
    base_cashflows: list[tuple[date, float]],
    end_date: date,
    base_value: float,
    steps: list[tuple[str, float]],
    log=None,
    log_context: str = "",
    tolerance: float = DEFAULT_TOLERANCE,
    value_tolerance: float = DEFAULT_VALUE_TOLERANCE,
) -> dict[str, float | None]:
    """Decompose XIRR into one additive share per step, walking `steps`
    (an ORDERED list of (name, delta) pairs) from a true 0%-return baseline.

    Each step's delta is added on top of the running cumulative value -
    unlike shared/xirr_shapley.py's `factor_deltas`, these are NOT
    "neutralize from the real value" adjustments; they're real amounts
    that build UP from 0 to `base_value`. The last step's cumulative value
    should equal `base_value` (a mismatch beyond `value_tolerance` logs a
    warning - it means the steps don't fully/uniquely explain the gain,
    same double-counting bug this replaces).

    Returns {step_name: waterfall_share_or_None}, same soft-fail convention
    as compute_shapley_xirr_shares (None only if some step's XIRR couldn't
    be solved).
    """
    zero_return_value = -sum(amount for _, amount in base_cashflows)
    cumulative = zero_return_value
    xirr_prev = compute_xirr(base_cashflows + [(end_date, cumulative)])

    shares: dict[str, float | None] = {}
    ok = True
    for name, delta in steps:
        cumulative += delta
        xirr_next = compute_xirr(base_cashflows + [(end_date, cumulative)])
        if xirr_prev is None or xirr_next is None:
            shares[name] = None
            ok = False
        else:
            shares[name] = xirr_next - xirr_prev
        xirr_prev = xirr_next

    if log is not None:
        value_gap = abs(cumulative - base_value)
        if value_gap > value_tolerance:
            log.warning(
                "Waterfall XIRR decomposition%s: steps reconstruct %.2f EUR but base_value is "
                "%.2f EUR - gap %.2f EUR (some euro is double-counted or missing).",
                f" ({log_context})" if log_context else "", cumulative, base_value, value_gap,
            )
        real_xirr = compute_xirr(base_cashflows + [(end_date, base_value)])
        computed = [v for v in shares.values() if v is not None]
        if ok and real_xirr is not None and len(computed) == len(steps):
            gap = abs(sum(computed) - real_xirr)
            if gap > tolerance:
                log.warning(
                    "Waterfall XIRR decomposition%s: sum of shares (%.4f pts) != XIRR real "
                    "(%.4f pts), gap %.4f pts.",
                    f" ({log_context})" if log_context else "",
                    sum(computed) * 100, real_xirr * 100, gap * 100,
                )

    return shares

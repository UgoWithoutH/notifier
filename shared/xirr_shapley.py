"""Shapley-value decomposition of the XIRR gap into additive per-factor shares.

Added 2026-09-09: replaces the old "cancel one factor at a time" counterfactual
XIRR contributions used across every *_diversification.py script (XIRR Bonus /
XIRR Cash drag / XIRR Taxes / XIRR Frais / XIRR Intérêts). The old method
computed each share independently as `xirr_real - xirr_with_only_this_factor_
cancelled`, which does NOT sum back to `xirr_real - xirr_with_every_factor_
cancelled` because XIRR is a non-linear function of its cashflows (cancelling
two factors together isn't the same as summing the effect of cancelling them
one at a time - a classic interaction/non-additivity issue). The Shapley
value from cooperative game theory is the unique additive decomposition that
DOES sum back exactly - the "efficiency" property:
    sum_i Shapley(i) == XIRR real - XIRR with every factor neutralized
(verified at runtime below via a tolerance-gated warning log, not just
asserted in a comment).

Each "factor" is modelled as a single signed adjustment to add to the
account's final ("as if withdrawn/sold today") value in order to neutralize
it - e.g. -lifetime_bonus_received, +missed_earnings (cash drag),
+lifetime_taxes_withheld, +lifetime_fees_paid, -lifetime_net_interest. This
is exactly the same per-factor counterfactual logic every script already
had; only the combinatorics of COMBINING factors together is new (2**n
cashflow-subsets to evaluate instead of n independent single-factor ones -
n<=5 everywhere in this repo today, so at most 32 compute_xirr() calls, each
memoized per unique subset so identical subsets are never recomputed).
"""

from datetime import date
from itertools import combinations
from math import factorial

from shared.xirr import compute_xirr

DEFAULT_TOLERANCE = 0.0001  # 0.01 point, per the requested guard-rail


def xirr_for_subset(
    base_cashflows: list[tuple[date, float]],
    end_date: date,
    base_value: float,
    factor_deltas: dict[str, float],
    factors_removed,
) -> float | None:
    """Counterfactual XIRR with every factor in `factors_removed` neutralized
    simultaneously - each replaced by its already-existing signed adjustment
    from `factor_deltas` (reused as-is from each platform's own counterfactual
    logic), all added together onto `base_value` before recomputing XIRR on
    top of the same `base_cashflows` used for the real XIRR."""
    adjusted_value = base_value + sum(factor_deltas[f] for f in factors_removed)
    return compute_xirr(base_cashflows + [(end_date, adjusted_value)])


def compute_shapley_xirr_shares(
    base_cashflows: list[tuple[date, float]],
    end_date: date,
    base_value: float,
    factor_deltas: dict[str, float],
    log=None,
    log_context: str = "",
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, float | None]:
    """Decompose the XIRR gap between the real account (nothing neutralized)
    and the same account with every factor in `factor_deltas` neutralized
    simultaneously, into one additive Shapley share per factor.

    `factor_deltas` maps a factor name (e.g. "XIRR Bonus") to the signed
    adjustment to add to `base_value` to neutralize that single factor -
    unchanged from each platform's pre-existing counterfactual logic.

    Returns {factor_name: shapley_value_or_None}. A factor's share is None
    only if some required subset's XIRR couldn't be solved (compute_xirr()
    returned None for it) - same soft-fail convention used everywhere else
    in this codebase (an existing Sheet cell is left untouched rather than
    overwritten with a wrong/0 value).
    """
    factors = list(factor_deltas.keys())
    n = len(factors)
    if n == 0:
        return {}

    full = frozenset(factors)
    cache: dict[frozenset, float | None] = {}

    def value_of(removed: frozenset) -> float | None:
        if removed not in cache:
            cache[removed] = xirr_for_subset(base_cashflows, end_date, base_value, factor_deltas, removed)
        return cache[removed]

    real_xirr = value_of(frozenset())
    all_neutralized_xirr = value_of(full)
    if real_xirr is None:
        return {f: None for f in factors}

    shares: dict[str, float | None] = {}
    for i in factors:
        others = [f for f in factors if f != i]
        total = 0.0
        ok = True
        for k in range(len(others) + 1):
            weight = factorial(k) * factorial(n - k - 1) / factorial(n)
            for combo in combinations(others, k):
                kept = frozenset(combo)  # S: factors already "present" (not neutralized), not containing i
                v_with_i = value_of(full - (kept | {i}))   # xirr_for_subset(complement of S union {i})
                v_without_i = value_of(full - kept)         # xirr_for_subset(complement of S)
                if v_with_i is None or v_without_i is None:
                    ok = False
                    continue
                total += weight * (v_with_i - v_without_i)
        shares[i] = total if ok else None

    computed = [v for v in shares.values() if v is not None]
    if log is not None and all_neutralized_xirr is not None and len(computed) == n:
        # Efficiency check: sum of shares must equal the real XIRR minus the
        # XIRR with every factor neutralized (see module docstring).
        expected_total = real_xirr - all_neutralized_xirr
        gap = abs(sum(computed) - expected_total)
        if gap > tolerance:
            log.warning(
                "Shapley XIRR decomposition%s: efficiency check failed - sum of shares "
                "(%.4f pts) != XIRR real - XIRR all-neutralized (%.4f pts), gap %.4f pts.",
                f" ({log_context})" if log_context else "",
                sum(computed) * 100, expected_total * 100, gap * 100,
            )

    return shares

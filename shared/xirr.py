"""Generic XIRR (annualized money-weighted rate of return) calculator.

No external numeric dependency (no numpy/scipy) - solves
`sum(amount / (1 + rate) ** ((date - date0).days / 365)) == 0` via
Newton-Raphson, falling back to bisection if Newton doesn't converge
(e.g. a bad initial guess for an unusual cashflow shape).

Usable by any *_diversification.py script that can reconstruct a
platform's real external cashflows (deposits into the platform as
negative amounts, withdrawals out of it as positive amounts) plus a
final "as if sold/withdrawn today" positive cashflow equal to the
current total account value.
"""

from datetime import date


def compute_xirr(cashflows: list[tuple[date, float]], guess: float = 0.1) -> float | None:
    """Compute XIRR for a list of (date, amount) cashflows.

    Returns the annualized rate as a decimal fraction (e.g. 0.1159 for
    11.59%), or None if it can't be computed (fewer than 2 cashflows, no
    sign change between them - XIRR is mathematically undefined without
    both an outflow and an inflow - or no root found).
    """
    if len(cashflows) < 2:
        return None
    if not any(amount < 0 for _, amount in cashflows) or not any(amount > 0 for _, amount in cashflows):
        return None

    ordered = sorted(cashflows, key=lambda cf: cf[0])
    date0 = ordered[0][0]
    # Precompute each cashflow's offset in years from the first one, so the
    # NPV/derivative helpers below don't recompute it every call.
    years = [((d - date0).days / 365.0, amount) for d, amount in ordered]

    def npv(rate: float) -> float:
        return sum(amount / (1 + rate) ** t for t, amount in years)

    def npv_derivative(rate: float) -> float:
        return sum(-t * amount / (1 + rate) ** (t + 1) for t, amount in years)

    rate = guess
    for _ in range(100):
        f = npv(rate)
        fprime = npv_derivative(rate)
        if fprime == 0:
            break
        new_rate = rate - f / fprime
        if new_rate <= -1:
            # Keep the rate in the mathematically valid (1 + rate) > 0
            # domain - halve the distance to -1 instead of overshooting.
            new_rate = (rate - 1) / 2
        if abs(new_rate - rate) < 1e-9:
            return new_rate
        rate = new_rate

    # Newton didn't converge (unusual cashflow shape/bad guess) - fall back
    # to bisection over a wide, sane rate range.
    low, high = -0.9999, 10.0
    f_low, f_high = npv(low), npv(high)
    if f_low * f_high > 0:
        return None
    for _ in range(200):
        mid = (low + high) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-6:
            return mid
        if (f_low < 0) != (f_mid < 0):
            high, f_high = mid, f_mid
        else:
            low, f_low = mid, f_mid
    return (low + high) / 2

"""Throwaway comparison: Shapley vs waterfall XIRR decomposition on the same
sample cashflow extract, as suggested in the 2026-09-09 discussion about
non-additive XIRR attribution.

The 5 real components (Bonus, Cash drag, Taxes, Frais, Intérêts) satisfy the
real accounting identity:
    total_value_as_of = total_deposited_net + gross_interest + bonus - fees - withholding_tax
where gross_interest = net_interest_actually_earned + missed_earnings (the
extra interest that would have been earned with zero idle cash / cash drag).

Run: python _compare_shapley_vs_waterfall.py
"""

from datetime import date

from shared.xirr import compute_xirr
from shared.xirr_waterfall import compute_waterfall_xirr_shares

# Sample extract: 10,000 EUR deposited 2 years ago, worth 11,500 EUR today.
end_date = date(2026, 9, 9)
base_cashflows = [(date(2024, 9, 9), -10000.0)]
total_deposited_net = 10000.0

bonus = 100.0
fees = 20.0
withholding_tax = 130.0
net_interest_actually_earned = 900.0
missed_earnings = 150.0  # extra interest that would've been earned with no idle cash
gross_interest = net_interest_actually_earned + missed_earnings

# The account only ever really received net_interest_actually_earned (missed_earnings
# was NEVER credited - it's the hypothetical "if fully deployed" ideal, not real money).
total_value_as_of = total_deposited_net + net_interest_actually_earned + bonus - fees - withholding_tax
print(f"total_value_as_of (reconstructed) = {total_value_as_of:.2f} EUR\n")

real_xirr = compute_xirr(base_cashflows + [(end_date, total_value_as_of)])
print(f"XIRR real: {real_xirr * 100:.4f} pts\n")

# Friend's order: Intérêts (gross) -> Cash drag (subtract missed earnings) -> Bonus -> Frais -> Taxes.
steps = [
    ("XIRR Intérêts", gross_interest),
    ("XIRR Cash drag", -missed_earnings),
    ("XIRR Bonus", bonus),
    ("XIRR Frais", -fees),
    ("XIRR Taxes", -withholding_tax),
]
waterfall = compute_waterfall_xirr_shares(base_cashflows, end_date, total_value_as_of, steps)

header = f"{'Step':<18} {'Waterfall share':>16}"
print(header)
print("-" * len(header))
for name, _ in steps:
    print(f"{name:<18} {waterfall[name] * 100:>15.4f}%")
print("-" * len(header))
total = sum(waterfall.values())
print(f"{'SUM':<18} {total * 100:>15.4f}%")
print(f"{'XIRR real':<18} {real_xirr * 100:>15.4f}%")
print(f"\nMatch: {abs(total - real_xirr) < 1e-9} (sum of the 5 shares == XIRR real, exactly)")


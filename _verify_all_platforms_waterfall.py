"""Verify, for every *_diversification.py call site (23 total across 15
platforms), that the waterfall steps used in production sum EXACTLY back to
the real XIRR - using synthetic numbers that respect each platform's actual
accounting identity (total_value = total_deposited_net + gross_interest +
bonus - fees - tax), reproducing the exact `steps = [...]` construction found
in each file.
"""
from datetime import date

from shared.xirr import compute_xirr
from shared.xirr_waterfall import compute_waterfall_xirr_shares

DEPOSIT_DATE = date(2024, 1, 1)
END_DATE = date(2026, 9, 9)

TOLERANCE = 1e-4  # 0.01 percentage point


def check(label, total_deposited_net, steps, total_value_as_of):
    base_cashflows = [(DEPOSIT_DATE, -total_deposited_net)]
    real_xirr = compute_xirr(base_cashflows + [(END_DATE, total_value_as_of)])
    shares = compute_waterfall_xirr_shares(base_cashflows, END_DATE, total_value_as_of, steps, log_context=label)
    total_shares = sum(v for v in shares.values() if v is not None)
    ok = real_xirr is not None and abs(total_shares - real_xirr) < TOLERANCE
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label:45s} real={real_xirr*100:8.4f}%  sum(shares)={total_shares*100:8.4f}%  detail={ {k: round(v*100,4) for k, v in shares.items() if v is not None} }")
    return ok


results = []

# ---- Afranga (2 call sites, same shape: Intérêts/Cash drag/Bonus/Taxes) ----
gross_interest, missed_earnings, bonus, tax = 900.0, 150.0, 100.0, 130.0
deposited = 10000.0
total_value = deposited + gross_interest + bonus - tax
steps = [
    ("XIRR Intérêts", gross_interest + missed_earnings),
    ("XIRR Cash drag", -missed_earnings),
    ("XIRR Bonus", bonus),
    ("XIRR Taxes", -tax),
]
results.append(check("Afranga (both call sites)", deposited, steps, total_value))

# ---- Bienpreter / Bricks / Debitum (same 4-step shape) ----
for name in ("Bienpreter", "Bricks", "Debitum"):
    results.append(check(name, deposited, steps, total_value))

# ---- Go & Grow (Interets/Bonus/Frais, no cash drag/taxes) ----
gross_interest, bonus, fees = 500.0, 50.0, 20.0
deposited = 8000.0
total_value = deposited + gross_interest + bonus - fees
steps = [
    ("XIRR Intérêts", gross_interest),
    ("XIRR Bonus", bonus),
    ("XIRR Frais", -fees),
]
results.append(check("Go & Grow", deposited, steps, total_value))

# ---- Income Marketplace / Iuvo / Loanch (x2) / Swaper full (Interets/Cash drag/Bonus) ----
gross_interest, missed_earnings, bonus = 700.0, 120.0, 80.0
deposited = 9000.0
total_value = deposited + gross_interest + bonus
steps = [
    ("XIRR Intérêts", gross_interest + missed_earnings),
    ("XIRR Cash drag", -missed_earnings),
    ("XIRR Bonus", bonus),
]
for name in ("Income Marketplace", "Iuvo", "Loanch (compute_xirr_block_as_of)", "Loanch (run)", "Swaper (run, full)"):
    results.append(check(name, deposited, steps, total_value))

# ---- Lande fallback (Interets/Bonus only, no cash drag) ----
gross_interest, bonus = 400.0, 30.0
deposited = 5000.0
total_value = deposited + gross_interest + bonus
steps = [
    ("XIRR Intérêts", gross_interest),
    ("XIRR Bonus", bonus),
]
results.append(check("Lande (no cash drag fallback)", deposited, steps, total_value))

# ---- Lande full (Interets/Cash drag/Bonus) ----
gross_interest, missed_earnings, bonus = 400.0, 60.0, 30.0
deposited = 5000.0
total_value = deposited + gross_interest + bonus
steps = [
    ("XIRR Intérêts", gross_interest + missed_earnings),
    ("XIRR Cash drag", -missed_earnings),
    ("XIRR Bonus", bonus),
]
results.append(check("Lande (full)", deposited, steps, total_value))

# ---- Lendermarket / Monefit full / PeerBerry (x2) (Interets/Cash drag/Bonus/Frais) ----
gross_interest, missed_earnings, bonus, fees = 850.0, 140.0, 90.0, 25.0
deposited = 11000.0
total_value = deposited + gross_interest + bonus - fees
steps = [
    ("XIRR Intérêts", gross_interest + missed_earnings),
    ("XIRR Cash drag", -missed_earnings),
    ("XIRR Bonus", bonus),
    ("XIRR Frais", -fees),
]
results.append(check("Lendermarket", deposited, steps, total_value))
results.append(check("Monefit (full)", deposited, steps, total_value))

# PeerBerry stores its fee variable ALREADY negative-signed and adds it directly.
neg_fees = -fees
steps_peerberry = [
    ("XIRR Intérêts", gross_interest + missed_earnings),
    ("XIRR Cash drag", -missed_earnings),
    ("XIRR Bonus", bonus),
    ("XIRR Frais", neg_fees),
]
results.append(check("PeerBerry (as-of)", deposited, steps_peerberry, total_value))
results.append(check("PeerBerry (run)", deposited, steps_peerberry, total_value))

# ---- Mintos (x2) / Nectaro full (Interets/Cash drag/Bonus/Taxes) ----
gross_interest, missed_earnings, bonus, tax = 950.0, 160.0, 70.0, 47.5
deposited = 12000.0
total_value = deposited + gross_interest + bonus - tax
steps = [
    ("XIRR Intérêts", gross_interest + missed_earnings),
    ("XIRR Cash drag", -missed_earnings),
    ("XIRR Bonus", bonus),
    ("XIRR Taxes", -tax),
]
results.append(check("Mintos (as-of)", deposited, steps, total_value))
results.append(check("Mintos (run)", deposited, steps, total_value))
results.append(check("Nectaro (full)", deposited, steps, total_value))

# ---- Monefit fallback (Interets/Bonus/Frais, no cash drag) ----
gross_interest, bonus, fees = 300.0, 40.0, 15.0
deposited = 6000.0
total_value = deposited + gross_interest + bonus - fees
steps = [
    ("XIRR Intérêts", gross_interest),
    ("XIRR Bonus", bonus),
    ("XIRR Frais", -fees),
]
results.append(check("Monefit (no cash drag fallback)", deposited, steps, total_value))

# ---- Nectaro fallback (Interets/Bonus/Taxes, no cash drag) ----
gross_interest, bonus, tax = 350.0, 25.0, 17.5
deposited = 6500.0
total_value = deposited + gross_interest + bonus - tax
steps = [
    ("XIRR Intérêts", gross_interest),
    ("XIRR Bonus", bonus),
    ("XIRR Taxes", -tax),
]
results.append(check("Nectaro (no cash drag fallback)", deposited, steps, total_value))

# ---- Swaper fallback (Interets/Cash drag only, no bonus) ----
gross_interest, missed_earnings = 200.0, 35.0
deposited = 4000.0
total_value = deposited + gross_interest
steps = [
    ("XIRR Intérêts", gross_interest + missed_earnings),
    ("XIRR Cash drag", -missed_earnings),
]
results.append(check("Swaper (as-of, no bonus fallback)", deposited, steps, total_value))

print()
print(f"{sum(results)}/{len(results)} call-site shapes passed.")

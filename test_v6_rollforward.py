"""
Independent verification of carry_rollforward() -- the table that explains whichever
waterfall is in use.

The point of this table is that it can never contradict the numbers it explains, so the
checks here are about agreement: the carry it shows must be the carry the waterfall
actually took, its cumulative columns must be running totals of its own rows, and its
hurdle balance must behave the way that mechanic claims (falling to zero exactly when
carry starts). Each expectation is recomputed here by hand rather than by calling the
function under test.

Run: python3 test_v6_rollforward.py
"""
from datetime import date

from finance_engine import (
    apply_carry_waterfall,
    apply_carry_waterfall_declining_balance,
    carry_rollforward,
)

FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


forecast_rows = [
    {"year": 1, "date": date(2027, 1, 1), "gross_distribution": 3_000_000.0},
    {"year": 2, "date": date(2028, 1, 1), "gross_distribution": 9_000_000.0},
    {"year": 3, "date": date(2029, 1, 1), "gross_distribution": 5_000_000.0},
]
paid_in = 10_000_000.0
dists_to_date = 0.0
hurdle = 0.08
carry = 0.20

# ===========================================================================
print("\n--- Declining-balance style ---")
# ===========================================================================
wf = apply_carry_waterfall_declining_balance(paid_in, dists_to_date, forecast_rows, hurdle, carry)
rf = carry_rollforward(wf, paid_in, dists_to_date, hurdle, style="declining")

check("one row per forecast year", len(rf) == len(forecast_rows), f"{len(rf)} vs {len(forecast_rows)}")

# The roll-forward must reproduce the waterfall's own carry, year by year.
for i, (a, b) in enumerate(zip(rf, wf), start=1):
    check(f"year {i} carry matches the waterfall exactly",
          abs(a["carry_in_year"] - b["gp_carry"]) < 1e-9, (a["carry_in_year"], b["gp_carry"]))

# Hand-rolled balance, independent of the function under test.
bal = paid_in
manual = []
for r in forecast_rows:
    opening = bal
    pref = opening * hurdle
    applied = min(opening + pref, r["gross_distribution"])
    bal = opening + pref - applied
    manual.append((opening, pref, applied, bal))
for i, (m, a) in enumerate(zip(manual, rf), start=1):
    check(f"year {i} opening balance", abs(a["hurdle_balance_opening"] - m[0]) < 1e-6, (a["hurdle_balance_opening"], m[0]))
    check(f"year {i} preferred accrued", abs(a["preferred_accrued"] - m[1]) < 1e-6, (a["preferred_accrued"], m[1]))
    check(f"year {i} applied to capital + pref", abs(a["applied_to_capital_and_pref"] - m[2]) < 1e-6, (a["applied_to_capital_and_pref"], m[2]))
    check(f"year {i} closing balance", abs(a["hurdle_balance_closing"] - m[3]) < 1e-6, (a["hurdle_balance_closing"], m[3]))
    print(f"  year {i}: opening={a['hurdle_balance_opening']:,.0f} pref={a['preferred_accrued']:,.0f} "
          f"applied={a['applied_to_capital_and_pref']:,.0f} closing={a['hurdle_balance_closing']:,.0f} "
          f"carry={a['carry_in_year']:,.0f}")

# Cumulative columns must be running totals of this table's own rows.
run_d = run_p = run_c = 0.0
for i, a in enumerate(rf, start=1):
    run_d += a["distribution"]
    run_p += a["preferred_accrued"]
    run_c += a["carry_in_year"]
    check(f"year {i} cumulative distributions is a running total",
          abs(a["cumulative_distributions"] - run_d) < 1e-6)
    check(f"year {i} cumulative preferred is a running total",
          abs(a["cumulative_preferred"] - run_p) < 1e-6)
    check(f"year {i} cumulative carry entitlement is a running total",
          abs(a["carry_entitlement_cumulative"] - run_c) < 1e-6)

# The mechanic's central claim: no carry while the balance is open, carry once it closes.
for i, a in enumerate(rf, start=1):
    if not a["hurdle_cleared"]:
        check(f"year {i} takes no carry while the hurdle balance is open",
              a["carry_in_year"] == 0.0, a["carry_in_year"])
first_clear = next((i for i, a in enumerate(rf, start=1) if a["hurdle_cleared"]), None)
check("the hurdle clears somewhere in this scenario", first_clear is not None)
check("carry is zero before the clearing year and non-zero from it",
      first_clear is not None
      and all(rf[i]["carry_in_year"] == 0.0 for i in range(first_clear - 1))
      and rf[first_clear - 1]["carry_in_year"] > 0,
      f"first cleared year={first_clear}")

# ===========================================================================
print("\n--- Compounded-threshold style ---")
# ===========================================================================
cf_dates = [date(2022, 1, 1), date(2023, 1, 1)]
calls = [6_000_000.0, 4_000_000.0]
wf_c = apply_carry_waterfall(cf_dates, calls, dists_to_date, forecast_rows, hurdle, carry)
rf_c = carry_rollforward(wf_c, paid_in, dists_to_date, hurdle, style="compounded")

for i, (a, b) in enumerate(zip(rf_c, wf_c), start=1):
    check(f"year {i} carry matches the compounded waterfall exactly",
          abs(a["carry_in_year"] - b["gp_carry"]) < 1e-9, (a["carry_in_year"], b["gp_carry"]))

# Under this mechanic the "balance" is the shortfall to the threshold, so it must equal
# threshold minus cumulative distributions, floored at zero -- recomputed here directly.
cum = 0.0
for i, (a, b) in enumerate(zip(rf_c, wf_c), start=1):
    expected_open = max(0.0, b["hurdle_threshold"] - cum)
    check(f"year {i} shortfall to threshold", abs(a["hurdle_balance_opening"] - expected_open) < 1e-6,
          (a["hurdle_balance_opening"], expected_open))
    cum += b["gross_distribution"]
    expected_close = max(0.0, b["hurdle_threshold"] - cum)
    check(f"year {i} shortfall after the year's distribution",
          abs(a["hurdle_balance_closing"] - expected_close) < 1e-6,
          (a["hurdle_balance_closing"], expected_close))
    print(f"  year {i}: shortfall {a['hurdle_balance_opening']:,.0f} -> {a['hurdle_balance_closing']:,.0f}  "
          f"carry={a['carry_in_year']:,.0f}")

check("no preferred-accrual column is invented for the compounded mechanic",
      all(a["preferred_accrued"] == 0.0 for a in rf_c))

# ===========================================================================
print("\n--- Edge cases ---")
# ===========================================================================
check("no forecast rows -> empty table, no crash",
      carry_rollforward([], paid_in, dists_to_date, hurdle) == [])

# Distributions already received are credited: nothing is owed, so carry starts at once.
wf_paid = apply_carry_waterfall_declining_balance(10_000_000.0, 10_000_000.0, forecast_rows, hurdle, carry)
rf_paid = carry_rollforward(wf_paid, 10_000_000.0, 10_000_000.0, hurdle, style="declining")
check("capital already returned -> hurdle opens at zero and carry starts in year 1",
      rf_paid[0]["hurdle_balance_opening"] == 0.0 and rf_paid[0]["carry_in_year"] > 0,
      (rf_paid[0]["hurdle_balance_opening"], rf_paid[0]["carry_in_year"]))

print("\n=== SUMMARY ===")
if FAILS:
    print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")

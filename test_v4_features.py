"""
Independent verification of Round-3 addition: the two-tier ("step-down") management fee.
  1. remaining_cost_basis_by_year() -- per-year remaining invested cost basis
  2. crossover_fee_schedule() -- the {year: fee $} schedule built from that basis
  3. forecast_from_portfolio(..., fee_override_by_year=...) -- confirms the override actually
     substitutes for the flat mgmt_fee_rate calculation, dollar for dollar, and that the
     fee_ratio / distribution / ending_nav mechanics downstream are unaffected otherwise.

Each check recomputes the expected number by hand (a fresh loop/derivation, not by calling
the function under test) and diffs against the real output. Run: python3 test_v4_features.py
"""
from datetime import date

from finance_engine import (
    crossover_fee_schedule,
    forecast_from_portfolio,
    remaining_cost_basis_by_year,
)

FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ===========================================================================
print("\n--- remaining_cost_basis_by_year ---")
# ===========================================================================
# Company A: cost 10M, full exit year 2.
# Company B: cost 6M, 50% exit year 3, remaining 100% exit year 5.
# Company C: cost 4M, full exit year 4.
companies = [
    {"cost": 10_000_000.0, "exit_year_1": 2, "exit_pct_1": 1.0, "exit_year_2": 2, "exit_pct_2": 0.0},
    {"cost": 6_000_000.0, "exit_year_1": 3, "exit_pct_1": 0.5, "exit_year_2": 5, "exit_pct_2": 1.0},
    {"cost": 4_000_000.0, "exit_year_1": 4, "exit_pct_1": 1.0, "exit_year_2": 4, "exit_pct_2": 0.0},
]
result = remaining_cost_basis_by_year(companies, max_year=5)

# Manual, independent re-derivation (a company's full cost counts THROUGH its exit year,
# then drops/reduces starting the year after):
expected = {
    1: 10_000_000.0 + 6_000_000.0 + 4_000_000.0,   # 20M -- nothing has exited yet
    2: 10_000_000.0 + 6_000_000.0 + 4_000_000.0,   # 20M -- A's exit year itself still counts A in full
    3: 0.0 + 6_000_000.0 + 4_000_000.0,            # 10M -- A dropped out after year 2; B's exit year, still full 6M
    4: 0.0 + 3_000_000.0 + 4_000_000.0,            # 7M -- B down to 3M after its 50% exit; C's exit year, still full 4M
    5: 0.0 + 3_000_000.0 + 0.0,                    # 3M -- C dropped out after year 4; B's second exit year, still 3M
}
for t in range(1, 6):
    check(f"year {t} remaining cost basis matches", abs(result[t] - expected[t]) < 1e-6,
          (result[t], expected[t]))
    print(f"  year {t}: remaining_cost_basis = {result[t]:,.0f} (expected {expected[t]:,.0f})")

# Empty companies list -> no years, no crash.
check("empty companies -> empty schedule", remaining_cost_basis_by_year([], max_year=3) == {1: 0.0, 2: 0.0, 3: 0.0})


# ===========================================================================
print("\n--- crossover_fee_schedule ---")
# ===========================================================================
total_commitment = 30_000_000.0
crossover_year = 3
fee_rate_initial = 0.015
fee_rate_post = 0.010

schedule = crossover_fee_schedule(total_commitment, result, crossover_year, fee_rate_initial, fee_rate_post)

expected_fees = {
    1: fee_rate_initial * total_commitment,          # pre-crossover: flat on commitment
    2: fee_rate_initial * total_commitment,
    3: fee_rate_post * expected[3],                  # crossover year itself: already on remaining cost
    4: fee_rate_post * expected[4],
    5: fee_rate_post * expected[5],
}
for t in range(1, 6):
    check(f"year {t} fee matches hand calc", abs(schedule[t] - expected_fees[t]) < 1e-6,
          (schedule[t], expected_fees[t]))
    print(f"  year {t}: fee = ${schedule[t]:,.0f} (expected ${expected_fees[t]:,.0f})")

# Sanity: fee steps down in $ terms once remaining cost has shrunk past the flat-commitment fee.
check("post-crossover fee eventually drops below the flat pre-crossover fee",
      schedule[5] < schedule[1], (schedule[5], schedule[1]))
# 0% rates -> all zero, no crash.
zero_schedule = crossover_fee_schedule(total_commitment, result, crossover_year, 0.0, 0.0)
check("0% rates -> all-zero fee schedule", all(v == 0.0 for v in zero_schedule.values()))


# ===========================================================================
print("\n--- forecast_from_portfolio with fee_override_by_year ---")
# ===========================================================================
# Single company, current_value=100, expected_return=10%, full exit at year 2.
# fee_override supplies $5 (year 1) and $3 (year 2) -- deliberately different from what
# mgmt_fee_rate would produce, to prove the override is what actually gets used.
single_company = [{
    "name": "Solo Co", "current_value": 100.0, "expected_return": 0.10,
    "exit_year_1": 2, "exit_pct_1": 1.0, "exit_year_2": 2, "exit_pct_2": 0.0,
}]
override = {1: 5.0, 2: 3.0}
rows = forecast_from_portfolio(single_company, mgmt_fee_rate=0.99, as_of=date(2026, 1, 1),
                                fee_override_by_year=override)

# Manual, independent re-derivation:
# Year 1: grown = 100 * 1.10 = 110. Override fee = 5 (NOT 0.99*110). fee_ratio = (110-5)/110.
#         No exit this year -> distribution 0, net_val = 110 * fee_ratio = 105 exactly.
grown_1 = 100.0 * 1.10
fee_1 = override[1]
net_val_1 = grown_1 - fee_1  # since fee_ratio = (grown-fee)/grown, net_val = grown*fee_ratio = grown-fee
# Year 2: grown = net_val_1 * 1.10. Override fee = 3. fee_ratio = (grown_2-3)/grown_2.
#         Full exit -> distribution = grown_2 - fee_2 exactly (100% of post-fee value).
grown_2 = net_val_1 * 1.10
fee_2 = override[2]
dist_2 = grown_2 - fee_2

check("year 1 mgmt_fee uses override, not mgmt_fee_rate", abs(rows[0]["mgmt_fee"] - 5.0) < 1e-9,
      rows[0]["mgmt_fee"])
check("year 1 ending_nav matches hand calc", abs(rows[0]["ending_nav"] - net_val_1) < 1e-6,
      (rows[0]["ending_nav"], net_val_1))
check("year 1 gross_distribution is zero (no exit yet)", rows[0]["gross_distribution"] == 0.0)
check("year 2 mgmt_fee uses override, not mgmt_fee_rate", abs(rows[1]["mgmt_fee"] - 3.0) < 1e-9,
      rows[1]["mgmt_fee"])
check("year 2 gross_distribution matches hand calc (full exit, post-fee)",
      abs(rows[1]["gross_distribution"] - dist_2) < 1e-6, (rows[1]["gross_distribution"], dist_2))
check("year 2 ending_nav is zero (fully exited)", rows[1]["ending_nav"] == 0.0)

print(f"  year 1: fee=${rows[0]['mgmt_fee']:,.2f} ending_nav=${rows[0]['ending_nav']:,.2f}")
print(f"  year 2: fee=${rows[1]['mgmt_fee']:,.2f} distribution=${rows[1]['gross_distribution']:,.2f}")

# Backward compatibility: omitting fee_override_by_year (None, the default) must reproduce
# the pre-existing flat-rate behavior exactly.
rows_no_override = forecast_from_portfolio(single_company, mgmt_fee_rate=0.05, as_of=date(2026, 1, 1))
manual_grown_1 = 100.0 * 1.10
manual_fee_1 = manual_grown_1 * 0.05
check("no fee_override_by_year -> falls back to flat mgmt_fee_rate (backward compatible)",
      abs(rows_no_override[0]["mgmt_fee"] - manual_fee_1) < 1e-6,
      (rows_no_override[0]["mgmt_fee"], manual_fee_1))

# A year NOT present in fee_override_by_year falls back to the flat rate for that year only.
partial_override = {1: 5.0}  # year 2 intentionally absent
rows_partial = forecast_from_portfolio(single_company, mgmt_fee_rate=0.05, as_of=date(2026, 1, 1),
                                        fee_override_by_year=partial_override)
manual_grown_2_partial = (110.0 - 5.0) * 1.10
manual_fee_2_partial = manual_grown_2_partial * 0.05
check("year 1 uses the partial override", abs(rows_partial[0]["mgmt_fee"] - 5.0) < 1e-9)
check("year 2 (absent from override) falls back to flat mgmt_fee_rate",
      abs(rows_partial[1]["mgmt_fee"] - manual_fee_2_partial) < 1e-6,
      (rows_partial[1]["mgmt_fee"], manual_fee_2_partial))


# ===========================================================================
print("\n=== SUMMARY ===")
if FAILS:
    print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
    raise SystemExit(1)
else:
    print("ALL CHECKS PASSED")

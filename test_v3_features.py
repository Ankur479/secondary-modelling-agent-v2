"""
Independent verification of Round-2 additions:
  1. apply_carry_waterfall_declining_balance() -- alternate European waterfall
  2. cash_flow_duration() -- weighted-average distribution timing

Each check recomputes the expected number by hand (a fresh loop, not by calling the
function under test) and diffs against the real output. Run: python3 test_v3_features.py
"""
from datetime import date

from finance_engine import apply_carry_waterfall_declining_balance, cash_flow_duration

FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ===========================================================================
print("\n--- Declining-balance waterfall ---")
# ===========================================================================
# Hand-computable scenario: capital base $10M, hurdle 8%, carry 20%, no catch-up.
# Year 1 distribution $3M (all applied to capital+pref, hurdle stays open).
# Year 2 distribution $9M (clears the remaining hurdle balance, carry kicks in on
# whatever crosses the line).
# Year 3 distribution $5M (hurdle already clear from year 2 on, straight 80/20 split).
forecast_rows = [
    {"year": 1, "date": date(2027, 1, 1), "gross_distribution": 3_000_000.0},
    {"year": 2, "date": date(2028, 1, 1), "gross_distribution": 9_000_000.0},
    {"year": 3, "date": date(2029, 1, 1), "gross_distribution": 5_000_000.0},
]
paid_in_to_date = 10_000_000.0
distributions_to_date = 0.0
hurdle_rate = 0.08
carry_rate = 0.20

result = apply_carry_waterfall_declining_balance(paid_in_to_date, distributions_to_date,
                                                   forecast_rows, hurdle_rate, carry_rate)

# Manual, independent re-derivation:
capital_base = 10_000_000.0
hb = capital_base
cum_dist = 0.0
cum_pref = 0.0
cum_carry = 0.0
manual_rows = []
for row in forecast_rows:
    g = row["gross_distribution"]
    pref = hb * hurdle_rate
    applied = min(hb + pref, g)
    hb = hb + pref - applied
    cum_dist += g
    cum_pref += pref
    if hb <= 1e-6:
        target = carry_rate * max(0.0, cum_dist - capital_base - cum_pref)
    else:
        target = cum_carry
    gp = target - cum_carry
    cum_carry = target
    lp = g - gp
    manual_rows.append({"hurdle_balance": hb, "lp": lp, "gp": gp})

for i, (r, m) in enumerate(zip(result, manual_rows), start=1):
    check(f"year {i} hurdle balance matches", abs(r["hurdle_threshold"] - m["hurdle_balance"]) < 1e-6,
          (r["hurdle_threshold"], m["hurdle_balance"]))
    check(f"year {i} LP distribution matches", abs(r["lp_distribution"] - m["lp"]) < 1e-6,
          (r["lp_distribution"], m["lp"]))
    check(f"year {i} GP carry matches", abs(r["gp_carry"] - m["gp"]) < 1e-6,
          (r["gp_carry"], m["gp"]))
    print(f"  year {i}: hurdle_balance={r['hurdle_threshold']:,.0f}  "
          f"lp_dist={r['lp_distribution']:,.0f}  gp_carry={r['gp_carry']:,.0f}")

# Sanity: total LP + GP must equal total gross distributed, every year.
for i, r in enumerate(result, start=1):
    total_g = forecast_rows[i - 1]["gross_distribution"]
    check(f"year {i} LP+GP == gross distribution",
          abs((r["lp_distribution"] + r["gp_carry"]) - total_g) < 1e-6)

# Year 1: hurdle shouldn't have cleared yet (10M capital + 800k pref = 10.8M owed,
# only 3M distributed) -> zero carry.
check("year 1 carry is zero (hurdle nowhere near cleared)", result[0]["gp_carry"] == 0.0)
# Year 3: hurdle already cleared in year 2, so year 3 should be a straight 80/20 split
# of that year's $5M distribution (no capital/pref left to apply).
check("year 3 is a straight 80/20 split once hurdle is clear",
      abs(result[2]["lp_distribution"] - 5_000_000.0 * 0.8) < 1e-6 and
      abs(result[2]["gp_carry"] - 5_000_000.0 * 0.2) < 1e-6,
      (result[2]["lp_distribution"], result[2]["gp_carry"]))

# Zero hurdle_rate, zero carry_rate edge case: LP should get 100% every year, no errors.
result_zero = apply_carry_waterfall_declining_balance(10_000_000.0, 0.0, forecast_rows, 0.0, 0.0)
check("0% hurdle / 0% carry -> LP gets everything",
      all(r["gp_carry"] == 0.0 for r in result_zero) and
      all(abs(r["lp_distribution"] - forecast_rows[i]["gross_distribution"]) < 1e-6
          for i, r in enumerate(result_zero)))


# ===========================================================================
print("\n--- Cash-flow duration ---")
# ===========================================================================
as_of = date(2026, 1, 1)
dur_rows = [
    {"year": 1, "date": date(2027, 1, 1), "gross_distribution": 1_000_000.0},   # 1.0 yr out
    {"year": 2, "date": date(2028, 1, 1), "gross_distribution": 1_000_000.0},   # 2.0 yr out
    {"year": 3, "date": date(2029, 1, 1), "gross_distribution": 2_000_000.0},   # 3.0 yr out (approx, leap-adjusted)
]
dur = cash_flow_duration(dur_rows, "gross_distribution", as_of)
# Manual: weighted = 1M*1 + 1M*2 + 2M*3(approx) ; total = 4M
manual_weighted = sum(r["gross_distribution"] * (r["date"] - as_of).days / 365.0 for r in dur_rows)
manual_total = sum(r["gross_distribution"] for r in dur_rows)
manual_dur = manual_weighted / manual_total
check("duration matches manual weighted-average calc", abs(dur - manual_dur) < 1e-9, (dur, manual_dur))
# Sanity bounds: duration must lie between the earliest and latest cash flow's time.
min_t = min((r["date"] - as_of).days / 365.0 for r in dur_rows)
max_t = max((r["date"] - as_of).days / 365.0 for r in dur_rows)
check("duration lies within [min_t, max_t]", min_t <= dur <= max_t, (min_t, dur, max_t))
print(f"  duration = {dur:.3f} yrs (bounds [{min_t:.3f}, {max_t:.3f}])")

# All-zero distributions -> 0.0, not a division error.
dur_zero = cash_flow_duration([{"year": 1, "date": date(2027, 1, 1), "gross_distribution": 0.0}],
                               "gross_distribution", as_of)
check("all-zero distributions -> duration 0.0, no crash", dur_zero == 0.0)


# ===========================================================================
print("\n=== SUMMARY ===")
if FAILS:
    print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
    raise SystemExit(1)
else:
    print("ALL CHECKS PASSED")

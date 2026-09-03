"""
Independent verification of the 3 new Phase-1 features added to finance_engine.py:
  1. Fund vs LP scaling (done at the app-entry-point level, not inside finance_engine.py)
  2. Unfunded commitment generating its own return (build_unfunded_returns +
     the unfunded_returns param on secondary_pricing / leverage_overlay)
  3. Bottom-up EV/EBITDA company model (ebitda_exit_value + implied_annual_return)

Each check recomputes the expected number by an independent path (manual arithmetic
or a from-scratch loop) rather than re-calling the function under test, then diffs
against the real function's output. Run: python3 test_v2_features.py
"""
import csv
from datetime import date

from finance_engine import (
    fund_metrics_to_date, xirr, build_unfunded_returns, secondary_pricing,
    leverage_overlay, ebitda_exit_value, implied_annual_return, forecast_from_portfolio,
    reported_vs_market_value, apply_carry_waterfall,
)

FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ===========================================================================
print("\n--- 1. Fund vs LP scaling ---")
# ===========================================================================
with open("sample_fund_cashflows.csv") as f:
    rows = list(csv.DictReader(f))
cf_dates = [date.fromisoformat(r["Date"]) for r in rows]
calls_fund = [float(r["Capital_Call"]) for r in rows]
dists_fund = [float(r["Distribution"]) for r in rows]
as_of = date(2026, 9, 2)
nav_fund = 62_000_000.0

fund_commitment = 50_000_000.0
lp_commitment = 2_500_000.0
lp_pct = lp_commitment / fund_commitment
check("lp_pct == 5%", abs(lp_pct - 0.05) < 1e-12)

# App-level scaling: multiply every fund-level $ figure by lp_pct once, at entry.
calls_lp = [c * lp_pct for c in calls_fund]
dists_lp = [d * lp_pct for d in dists_fund]
nav_lp = nav_fund * lp_pct

td_fund = fund_metrics_to_date(cf_dates, calls_fund, dists_fund, nav_fund, as_of)
td_lp = fund_metrics_to_date(cf_dates, calls_lp, dists_lp, nav_lp, as_of)

check("LP paid_in = fund paid_in x lp_pct",
      abs(td_lp.paid_in - td_fund.paid_in * lp_pct) < 1e-6,
      f"{td_lp.paid_in} vs {td_fund.paid_in * lp_pct}")
check("LP distributions = fund distributions x lp_pct",
      abs(td_lp.distributions - td_fund.distributions * lp_pct) < 1e-6)
check("LP nav = fund nav x lp_pct", abs(td_lp.nav - nav_lp) < 1e-6)
# Ratios are scale-invariant -- DPI/RVPI/TVPI/IRR must be IDENTICAL fund vs LP.
check("DPI scale-invariant", abs(td_lp.dpi - td_fund.dpi) < 1e-12, f"{td_lp.dpi} vs {td_fund.dpi}")
check("RVPI scale-invariant", abs(td_lp.rvpi - td_fund.rvpi) < 1e-12)
check("TVPI scale-invariant", abs(td_lp.tvpi - td_fund.tvpi) < 1e-12)
check("IRR scale-invariant", abs(td_lp.irr - td_fund.irr) < 1e-9, f"{td_lp.irr} vs {td_fund.irr}")
print(f"  fund: paid_in=${td_fund.paid_in:,.0f} irr={td_fund.irr*100:.2f}%   "
      f"LP: paid_in=${td_lp.paid_in:,.0f} irr={td_lp.irr*100:.2f}%")


# ===========================================================================
print("\n--- 2. Unfunded commitment generates its own return ---")
# ===========================================================================
forecast_rows_5y = [{"year": y, "date": date(2026 + y, 9, 2), "gross_distribution": 1_000_000.0}
                     for y in range(1, 6)]
unfunded_calls = {1: 100_000.0, 2: 25_000.0, 3: 0.0, 4: 0.0, 5: 0.0}
returns_by_year, excluded = build_unfunded_returns(unfunded_calls, hold_years=3, moic=1.5,
                                                     forecast_rows=forecast_rows_5y)
# Manual expectation: year-1 call (100k) matures year 4 -> 150k. year-2 call (25k)
# matures year 5 -> 37.5k. Nothing excluded (both within the 5-year horizon).
check("year-4 return = 150,000", abs(returns_by_year[4] - 150_000.0) < 1e-6, returns_by_year)
check("year-5 return = 37,500", abs(returns_by_year[5] - 37_500.0) < 1e-6, returns_by_year)
check("year-1/2/3 returns are 0", returns_by_year[1] == 0 and returns_by_year[2] == 0 and returns_by_year[3] == 0)
check("nothing excluded", excluded == 0.0)

# A call maturing beyond the horizon should be excluded, not silently dropped into
# the last year.
unfunded_calls_late = {5: 100_000.0}  # matures year 8, horizon is 5 years
returns_late, excluded_late = build_unfunded_returns(unfunded_calls_late, hold_years=3, moic=1.5,
                                                       forecast_rows=forecast_rows_5y)
check("late-maturing call is excluded, not dumped in year 5",
      returns_late[5] == 0.0 and abs(excluded_late - 150_000.0) < 1e-6,
      (returns_late, excluded_late))

# secondary_pricing / leverage_overlay wiring: with unfunded_returns injected,
# total value received (and hence MOIC) should rise by exactly total_returns,
# and the IRR cash-flow stream should carry the return at the right date.
pricing_no_ret = secondary_pricing(10_000_000.0, forecast_rows_5y, as_of, [0.0],
                                    "gross_distribution", unfunded_calls)[0]
pricing_with_ret = secondary_pricing(10_000_000.0, forecast_rows_5y, as_of, [0.0],
                                      "gross_distribution", unfunded_calls, returns_by_year)[0]
expected_total_dist_delta = sum(returns_by_year.values())
actual_moic_dist_no_ret = pricing_no_ret["moic"] * pricing_no_ret["total_invested"]
actual_moic_dist_with_ret = pricing_with_ret["moic"] * pricing_with_ret["total_invested"]
check("total value received rises by exactly total unfunded returns",
      abs((actual_moic_dist_with_ret - actual_moic_dist_no_ret) - expected_total_dist_delta) < 1e-6,
      (actual_moic_dist_with_ret - actual_moic_dist_no_ret, expected_total_dist_delta))
check("unfunded_returns total is reported on the pricing row",
      abs(pricing_with_ret["unfunded_returns"] - expected_total_dist_delta) < 1e-6)
check("total_invested unaffected by returns (only calls count as invested)",
      pricing_with_ret["total_invested"] == pricing_no_ret["total_invested"])
check("IRR with returns >= IRR without (strictly more cash, same price/timing)",
      pricing_with_ret["irr"] > pricing_no_ret["irr"])

# Manual IRR cross-check: rebuild the cash-flow stream by hand and xirr it directly.
manual_flows = [(as_of, -10_000_000.0)] + [
    (r["date"], r["gross_distribution"] - unfunded_calls.get(r["year"], 0.0)
     + returns_by_year.get(r["year"], 0.0))
    for r in forecast_rows_5y
]
manual_irr = xirr(manual_flows)
check("secondary_pricing IRR matches independently-built XIRR",
      abs(manual_irr - pricing_with_ret["irr"]) < 1e-9, (manual_irr, pricing_with_ret["irr"]))

# leverage_overlay: at 0% leverage, levered IRR/MOIC must still match the unlevered
# scenario with returns included (regression + the new param shouldn't break the
# zero-leverage identity).
lev0 = leverage_overlay(pricing_with_ret, forecast_rows_5y, as_of, "gross_distribution",
                         unfunded_calls, leverage_pct=0.0, interest_rate=0.065,
                         unfunded_returns=returns_by_year)
check("0% leverage + unfunded returns still matches unlevered exactly",
      abs(lev0["levered_irr"] - pricing_with_ret["irr"]) < 1e-9 and
      abs(lev0["levered_moic"] - pricing_with_ret["moic"]) < 1e-9)


# ===========================================================================
print("\n--- 3. Bottom-up EV/EBITDA company model ---")
# ===========================================================================
result = ebitda_exit_value(
    entry_revenue=100.0, entry_ebitda_margin=0.25, entry_ev_multiple=10.0,
    entry_net_debt_ebitda=3.0, revenue_growth=0.08, fcf_conversion=0.50,
    exit_year=3, exit_ev_multiple=11.0, fund_ownership_pct=0.70,
)
# Manual recomputation, independent loop:
entry_ebitda = 100.0 * 0.25
entry_ev = entry_ebitda * 10.0
entry_nd = entry_ebitda * 3.0
check("entry EBITDA/EV/Net Debt", abs(result["entry_ebitda"] - entry_ebitda) < 1e-9 and
      abs(result["entry_ev"] - entry_ev) < 1e-9 and abs(result["entry_net_debt"] - entry_nd) < 1e-9)

rev, nd = 100.0, entry_nd
for t in range(1, 4):
    rev *= 1.08
    ebitda = rev * 0.25
    fcf = ebitda * 0.50
    nd = max(0.0, nd - fcf)
exit_ebitda_manual = ebitda
exit_ev_manual = exit_ebitda_manual * 11.0
exit_equity_manual = exit_ev_manual - nd
exit_proceeds_manual = exit_equity_manual * 0.70

check("exit EBITDA matches manual 3-yr compounding", abs(result["exit_ebitda"] - exit_ebitda_manual) < 1e-6)
check("exit net debt matches manual paydown", abs(result["exit_net_debt"] - nd) < 1e-6)
check("exit enterprise value = exit EBITDA x exit multiple",
      abs(result["exit_enterprise_value"] - exit_ev_manual) < 1e-6)
check("exit equity value = EV - net debt", abs(result["exit_equity_value"] - exit_equity_manual) < 1e-6)
check("exit proceeds to fund = equity value x ownership%",
      abs(result["exit_proceeds_to_fund"] - exit_proceeds_manual) < 1e-6,
      (result["exit_proceeds_to_fund"], exit_proceeds_manual))
print(f"  entry equity=${result['entry_equity_value']:.2f}mm  exit proceeds to fund=${result['exit_proceeds_to_fund']:.2f}mm")

# implied_annual_return: compounding current_value at the implied rate for
# `years` periods must land exactly back on exit_value.
current_mv = 12.0  # e.g. buyer's diligence-adjusted market value, $mm
r_implied = implied_annual_return(current_mv, result["exit_proceeds_to_fund"], years=3)
reconstructed = current_mv * (1 + r_implied) ** 3
check("implied_annual_return round-trips exactly",
      abs(reconstructed - result["exit_proceeds_to_fund"]) < 1e-6,
      (reconstructed, result["exit_proceeds_to_fund"]))
print(f"  implied annual return = {r_implied*100:.2f}%")

# Full integration: feed the implied return into forecast_from_portfolio (the
# same function the simple Expected-Return% mode already uses) with a single
# lump exit at year 3, and confirm year-3 gross_distribution is in the right
# ballpark (slightly below the pure compounded value, since mgmt fees shave a
# bit off every year before the exit is realized).
companies = [{
    "name": "Company A", "current_value": current_mv * 1_000_000,
    "expected_return": r_implied, "exit_year_1": 3, "exit_pct_1": 1.0,
    "exit_year_2": 3, "exit_pct_2": 0.0,
}]
fc = forecast_from_portfolio(companies, mgmt_fee_rate=0.02, as_of=date(2026, 9, 2))
pure_compounded_exit = current_mv * 1_000_000 * (1 + r_implied) ** 3
year3_dist = fc[2]["gross_distribution"]
check("year-3 distribution is below the pure-compounded exit value (fees shave it down)",
      0 < year3_dist < pure_compounded_exit,
      (year3_dist, pure_compounded_exit))
check("year-3 distribution is within 10% of the pure-compounded value (3 yrs of a 2% fee)",
      abs(year3_dist - pure_compounded_exit) / pure_compounded_exit < 0.10,
      (year3_dist, pure_compounded_exit))
print(f"  pure compounded exit=${pure_compounded_exit:,.0f}  actual (fee-adjusted) year-3 dist=${year3_dist:,.0f}")


# ===========================================================================
print("\n=== SUMMARY ===")
if FAILS:
    print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
    raise SystemExit(1)
else:
    print("ALL CHECKS PASSED")

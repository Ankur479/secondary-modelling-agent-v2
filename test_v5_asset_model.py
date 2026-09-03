"""
Verification of the bottom-up asset model against the source Excel workbook.

The expected values below are not hand-typed guesses -- they were read out of the
recalculated workbook itself (LibreOffice-evaluated, cell by cell), so this file
pins asset_model_build()/asset_model_returns() to what the Excel actually computes
for all five assets, line item by line item. If the port ever drifts, this fails.

Run: python3 test_v5_asset_model.py
"""
from finance_engine import asset_model_build, asset_model_returns

FAILS = []
TOL = 1e-6


def check(name, got, expected, tol=TOL):
    if expected is None:
        ok = got is None
    else:
        ok = got is not None and abs(got - expected) < tol
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + ("" if ok else f"   got={got!r} expected={expected!r}"))
    if not ok:
        FAILS.append(name)


# The Excel's projection columns and (flat) per-year operating assumptions.
YEARS = list(range(2026, 2034))
GROWTH = [0.08] * 8
MARGIN = [0.25] * 8
CONVERSION = [0.5] * 8

# Per-asset inputs exactly as the workbook has them, with the workbook's own
# computed outputs as the expected values.
ASSETS = [
    {
        "name": "Asset A", "entry_revenue": 98.1, "exit_year": 2028, "lp_cost": 77.0,
        "reported_value": 188.0, "hold_years": 6, "prior": 238.1,
        "expected": {
            "entry_ebitda": 24.525, "entry_ev": 269.775, "entry_net_debt": 73.575,
            "entry_equity": 196.2, "exit_ebitda": 30.8944368,
            "exit_ev": 370.7332416, "net_debt_at_exit": 30.5813016,
            "exit_equity": 340.15194, "proceeds": 238.106358,
            "gross_moic": 3.09229036363636, "multiple_on_rv": 1.26652318085106,
            "gross_irr": 0.207016974895866, "variance": 0.00635800000003428,
        },
        # Full year-by-year schedule for A, to prove the roll-forward itself, not
        # just the endpoints.
        "revenue": [105.948, 114.42384, 123.5777472, 0, 0, 0, 0, 0],
        "ebitda": [26.487, 28.60596, 30.8944368, 0, 0, 0, 0, 0],
        "fcf": [13.2435, 14.30298, 15.4472184, 0, 0, 0, 0, 0],
        "nd_begin": [73.575, 60.3315, 46.02852, 30.5813016, 0, 0, 0, 0],
        "nd_end": [60.3315, 46.02852, 30.5813016, 0, 0, 0, 0, 0],
    },
    {
        "name": "Asset B", "entry_revenue": 68.8, "exit_year": 2030, "lp_cost": 115.0,
        "reported_value": 132.0, "hold_years": 7, "prior": 214.2,
        "expected": {
            "entry_ebitda": 17.2, "entry_ev": 189.2, "entry_net_debt": 51.6,
            "entry_equity": 137.6, "exit_ebitda": 25.27244292096,
            "exit_ev": 303.26931505152, "net_debt_at_exit": -2.88898971648002,
            "exit_equity": 306.158304768, "proceeds": 214.3108133376,
            "gross_moic": 1.86357228989218, "multiple_on_rv": 1.62356676770909,
            "gross_irr": 0.093001837390861, "variance": 0.110813337600121,
        },
    },
    {
        "name": "Asset C", "entry_revenue": 83.8, "exit_year": 2026, "lp_cost": 62.0,
        "reported_value": 122.0, "hold_years": 4, "prior": 153.9,
        "expected": {
            "entry_ebitda": 20.95, "entry_ev": 230.45, "entry_net_debt": 62.85,
            "entry_equity": 167.6, "exit_ebitda": 22.626,
            "exit_ev": 271.512, "net_debt_at_exit": 51.537,
            "exit_equity": 219.975, "proceeds": 153.9825,
            "gross_moic": 2.48358870967742, "multiple_on_rv": 1.26215163934426,
            "gross_irr": 0.255364719657691, "variance": 0.0825000000000102,
        },
    },
    {
        "name": "Asset D", "entry_revenue": 102.4, "exit_year": 2029, "lp_cost": 84.9,
        "reported_value": 110.2, "hold_years": 5, "prior": 282.5,
        "expected": {
            "entry_ebitda": 25.6, "entry_ev": 281.6, "entry_net_debt": 76.8,
            "entry_equity": 204.8, "exit_ebitda": 34.828517376,
            "exit_ev": 417.942208512, "net_debt_at_exit": 14.507507712,
            "exit_equity": 403.4347008, "proceeds": 282.40429056,
            "gross_moic": 3.32631673215548, "multiple_on_rv": 2.56265236442831,
            "gross_irr": 0.271723568676384, "variance": -0.0957094399998937,
        },
    },
    {
        "name": "Asset E", "entry_revenue": 53.5, "exit_year": 2031, "lp_cost": 87.5,
        "reported_value": 105.1, "hold_years": 7, "prior": 187.4,
        "expected": {
            "entry_ebitda": 13.375, "entry_ev": 147.125, "entry_net_debt": 40.125,
            "entry_equity": 107.0, "exit_ebitda": 21.224444069376,
            "exit_ev": 254.693328832512, "net_debt_at_exit": -12.858747468288,
            "exit_equity": 267.5520763008, "proceeds": 187.28645341056,
            "gross_moic": 2.1404166104064, "multiple_on_rv": 1.78198338164187,
            "gross_irr": 0.114843855735655, "variance": -0.11354658943992,
        },
    },
]

for a in ASSETS:
    print(f"\n--- {a['name']} (exit {a['exit_year']}) vs. the workbook ---")
    b = asset_model_build(
        entry_revenue=a["entry_revenue"], entry_ebitda_margin=0.25, entry_ev_multiple=11.0,
        entry_net_debt_ebitda=3.0, fund_ownership_pct=0.7, years=YEARS,
        revenue_growth=GROWTH, ebitda_margin=MARGIN, fcf_conversion=CONVERSION,
        exit_year=a["exit_year"], exit_ev_multiple=12.0,
    )
    e = a["expected"]
    check("Entry EBITDA", b["entry_ebitda"], e["entry_ebitda"])
    check("Entry Enterprise Value", b["entry_enterprise_value"], e["entry_ev"])
    check("Entry Net Debt", b["entry_net_debt"], e["entry_net_debt"])
    check("Entry Equity Value", b["entry_equity_value"], e["entry_equity"])
    check("Exit EBITDA", b["exit_ebitda"], e["exit_ebitda"])
    check("Exit Enterprise Value", b["exit_enterprise_value"], e["exit_ev"])
    check("Less: Net Debt at Exit", b["net_debt_at_exit"], e["net_debt_at_exit"])
    check("Exit Equity Value", b["exit_equity_value"], e["exit_equity"])
    check("Gross Proceeds to Fund", b["gross_proceeds_to_fund"], e["proceeds"])

    r = asset_model_returns(b["gross_proceeds_to_fund"], a["lp_cost"], a["reported_value"],
                            a["hold_years"], a["prior"])
    check("Gross MOIC (vs Cost)", r["gross_moic_vs_cost"], e["gross_moic"])
    check("Multiple on RV", r["multiple_on_rv"], e["multiple_on_rv"])
    check("Gross IRR (annualised)", r["gross_irr_annualised"], e["gross_irr"])
    check("Variance vs Prior", r["variance_vs_prior"], e["variance"])

    # Cash Flow to Fund Model: proceeds land in the exit year and nowhere else.
    cf = {row["year"]: row["exit_proceeds_to_fund"] for row in b["cash_flow_to_fund"]}
    check("Exit proceeds land in the exit year", cf[a["exit_year"]], e["proceeds"])
    others = [v for y, v in cf.items() if y != a["exit_year"]]
    check("no proceeds in any other year", max(abs(v) for v in others), 0.0)

    if "revenue" in a:
        print("  (full schedule check)")
        for i, y in enumerate(YEARS):
            check(f"{y} Revenue", b["schedule"][i]["revenue"], a["revenue"][i])
            check(f"{y} EBITDA", b["schedule"][i]["ebitda"], a["ebitda"][i])
            check(f"{y} Free Cash Flow", b["schedule"][i]["fcf"], a["fcf"][i])
            check(f"{y} Net Debt - Beginning", b["schedule"][i]["net_debt_beginning"], a["nd_begin"][i])
            check(f"{y} Net Debt - Ending", b["schedule"][i]["net_debt_ending"], a["nd_end"][i])


print("\n--- Edge cases ---")
# Net debt is deliberately NOT floored at zero: two of the workbook's own assets end up
# in a net cash position, which lifts exit equity above enterprise value.
b_cash = asset_model_build(68.8, 0.25, 11.0, 3.0, 0.7, YEARS, GROWTH, MARGIN, CONVERSION, 2030, 12.0)
check("net cash position is preserved (not floored at 0)", b_cash["net_debt_at_exit"], -2.88898971648002)
print("  [INFO] exit equity exceeds enterprise value when net debt is negative, as it should:",
      b_cash["exit_equity_value"] > b_cash["exit_enterprise_value"])
if not b_cash["exit_equity_value"] > b_cash["exit_enterprise_value"]:
    FAILS.append("net cash lifts equity above EV")

# Exit year outside the projection columns -> flagged, not silently valued at zero.
b_out = asset_model_build(98.1, 0.25, 11.0, 3.0, 0.7, YEARS, GROWTH, MARGIN, CONVERSION, 2099, 12.0)
if b_out["exit_year_in_horizon"] is not False or b_out["gross_proceeds_to_fund"] != 0.0:
    FAILS.append("out-of-horizon exit year not flagged")
    print("  [FAIL] out-of-horizon exit year not flagged")
else:
    print("  [PASS] exit year outside the projection columns is flagged, proceeds 0")

# Mismatched vector lengths are rejected rather than silently mis-modelled.
try:
    asset_model_build(98.1, 0.25, 11.0, 3.0, 0.7, YEARS, GROWTH[:3], MARGIN, CONVERSION, 2028, 12.0)
    FAILS.append("mismatched vector lengths not rejected")
    print("  [FAIL] mismatched vector lengths not rejected")
except ValueError:
    print("  [PASS] mismatched per-year vector lengths raise ValueError")

# Zero cost / zero RV -> "NM" (None), not a ZeroDivisionError.
r_nm = asset_model_returns(100.0, 0.0, 0.0, 0, None)
if r_nm["gross_moic_vs_cost"] is None and r_nm["multiple_on_rv"] is None \
        and r_nm["gross_irr_annualised"] is None and r_nm["variance_vs_prior"] is None:
    print("  [PASS] zero cost / RV / hold -> None (the Excel's 'NM'), no crash")
else:
    FAILS.append("zero denominators not handled")
    print("  [FAIL] zero denominators not handled:", r_nm)


print("\n=== SUMMARY ===")
if FAILS:
    print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
    raise SystemExit(1)
print("ALL CHECKS PASSED -- the asset model reproduces the workbook exactly")

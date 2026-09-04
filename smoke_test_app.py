"""
Headless behaviour tests for app.py using Streamlit's AppTest.

Two kinds of check live here. The `run_and_check` ones drive a widget combination and
assert only that the script completes without an uncaught exception -- cheap insurance
that the UI wiring holds together. The `_check` ones assert on rendered values: that an
edit reaches the numbers it should, that the schedules tie to each other and to the
source workbook, and that the pricing panel reprices as expected.

Run: python3 smoke_test_app.py
"""
from streamlit.testing.v1 import AppTest

from voice_commands import describe, parse_command

FAILS = []


def _app(timeout=60):
    at = AppTest.from_file("app.py", default_timeout=timeout)
    at.run()
    return at


def run_and_check(label, mutate=None):
    at = _app(30)
    if mutate:
        mutate(at)
        at.run()
    if at.exception:
        for e in at.exception:
            print(f"  [FAIL] {label}: {e.value!r}")
            print("    ", e.stack_trace[-1] if e.stack_trace else "")
        FAILS.append(label)
    else:
        print(f"  [PASS] {label}")
    return at


def _check(label, cond, detail=""):
    if cond:
        print(f"  [PASS] {label}")
    else:
        print(f"  [FAIL] {label}  {detail}")
        FAILS.append(label)


def _metric(at, label):
    hits = [m.value for m in at.metric if m.label == label]
    assert hits, f"metric {label!r} not found"
    return hits[0]


def _num(at, label):
    hits = [n for n in at.number_input if n.label == label]
    assert hits, f"number_input {label!r} not found"
    return hits[0]


def _inv_tables(at):
    """The Investments tab's company tables, in render order: 0 current grid,
    1 current total, 2 current LP, 3 post-report grid, 4 post-report LP."""
    return [d.value for d in at.dataframe if "Company Name" in list(d.value.columns)]


def _line_tables(at):
    return [d.value for d in at.dataframe if "Line item" in list(d.value.columns)]


# ===========================================================================
print("--- The app runs across its widget combinations ---")
# ===========================================================================
run_and_check("default run")

run_and_check("fees and carry switched off",
              lambda at: [c for c in at.checkbox
                          if "Apply management fee" in c.label][0].set_value(False))

run_and_check("flat fee instead of two-tier",
              lambda at: [r for r in at.radio
                          if "Flat annual fee" in str(r.options)][0]
              .set_value("Flat annual fee (% of NAV)"))

run_and_check("declining-balance waterfall",
              lambda at: [r for r in at.radio
                          if "Declining hurdle balance" in str(r.options)][0]
              .set_value("Declining hurdle balance"))


def _catchup(at):
    [r for r in at.radio if "Declining hurdle balance" in str(r.options)][0].set_value(
        "Declining hurdle balance")
    at.run()
    [s for s in at.slider if "GP catch-up" in s.label][0].set_value(50.0)


run_and_check("declining-balance waterfall + 50% catch-up", _catchup)

run_and_check("unfunded return switched off",
              lambda at: [c for c in at.checkbox
                          if "generates its own return" in c.label][0].set_value(False))

run_and_check("leverage on",
              lambda at: [c for c in at.checkbox
                          if "finances part of the price" in c.label][0].set_value(True))

run_and_check("a different LP commitment", lambda at: _num(at, "LP").set_value(50.0))

run_and_check("one asset model switched off",
              lambda at: [c for c in at.checkbox
                          if "drive the fund forecast" in c.label][0].set_value(False))


def _everything(at):
    [r for r in at.radio if "Declining hurdle balance" in str(r.options)][0].set_value(
        "Declining hurdle balance")
    for c in at.checkbox:
        if "finances part of the price" in c.label:
            c.set_value(True)
    at.run()
    _num(at, "Premium / (Discount) to Reported Value (%)").set_value(-25.0)


run_and_check("everything together", _everything)


# ===========================================================================
print("\n--- Deal Terms drive the model ---")
# ===========================================================================
at = _app()
_check("the LP's share is derived from the two commitments",
       _metric(at, "LP %") == "3.30%", _metric(at, "LP %"))
_check("Funded is read off the cash-flow history at the LP's share (16.5)",
       _metric(at, "Funded") == "16.5", _metric(at, "Funded"))
_check("the blind pool is unfunded less the named follow-ons (374.8)",
       _metric(at, "Blind pool (fund $mm)") == "374.8", _metric(at, "Blind pool (fund $mm)"))

at = _app()
_num(at, "Unfunded").set_value(20.0)
at.run()
_check("raising unfunded raises the blind pool, follow-ons held back",
       float(_metric(at, "Blind pool (fund $mm)").replace(",", "")) > 374.8,
       _metric(at, "Blind pool (fund $mm)"))


# ===========================================================================
print("\n--- Asset models and the Investments grids ---")
# ===========================================================================
at = _app()
_check("one 'drives the forecast' toggle per company",
       len([c for c in at.checkbox if "drive the fund forecast" in c.label]) == 5,
       len([c for c in at.checkbox if "drive the fund forecast" in c.label]))
_check("five companies, at the deal's reported value",
       _metric(at, "Companies") == "5"
       and _metric(at, "Aggregate Reported Value") == "$657,300,000",
       (_metric(at, "Companies"), _metric(at, "Aggregate Reported Value")))
_check("the forecast horizon comes from the exit years (2026-2031)",
       _metric(at, "Forecast horizon").startswith("6 yrs"), _metric(at, "Forecast horizon"))

at = _app()
at.session_state["inv_current_editor"] = {
    "edited_rows": {0: {"RV": 250.0}}, "added_rows": [], "deleted_rows": []}
at.run()
_check("editing RV in the Investments grid moves fund NAV",
       _metric(at, "Aggregate Reported Value") == "$719,300,000",
       _metric(at, "Aggregate Reported Value"))
_check("that edit also reaches the Deal Snapshot widget (one shared store)",
       at.session_state["am_A_rv"] == 250.0, at.session_state["am_A_rv"])

at = _app()
_num(at, "Rows to add").set_value(3)
at.run()
bulk = [b for b in at.button if "Add 3 companies" in b.label]
_check("the Add button offers the chosen count", bool(bulk), [b.label for b in at.button][:6])
if bulk:
    bulk[0].click()
    at.run()
    counts = [len(t) for t in _inv_tables(at)]
    _check("adding 3 companies in one click reaches every view",
           _metric(at, "Companies") == "8" and counts[0] == 8 and counts[2] == 9,
           (_metric(at, "Companies"), counts))

at = _app()
at.session_state["inv_current_editor"] = {
    "edited_rows": {}, "added_rows": [], "deleted_rows": [1]}
at.run()
counts = [len(t) for t in _inv_tables(at)]
_check("deleting a row removes that company everywhere",
       _metric(at, "Companies") == "4" and counts[0] == 4 and counts[2] == 5,
       (_metric(at, "Companies"), counts))

at = _app()
moic_before = _metric(at, "Gross MOIC (vs Cost)")
[t for t in at.text_input if t.label == "Company"][0].set_value("Project Falcon")
at.run()
_check("renaming a company keeps its model intact (stable widget ids)",
       _metric(at, "Gross MOIC (vs Cost)") == moic_before,
       (moic_before, _metric(at, "Gross MOIC (vs Cost)")))


# ===========================================================================
print("\n--- Investment schedules tie to the source workbook ---")
# ===========================================================================
at = _app()
t = _inv_tables(at)
_check("all five Investments tables render", len(t) == 5, len(t))
if len(t) == 5:
    grid, total, lp, post_grid, post_lp = t
    head = [c for c in list(grid.columns) if c != "id"][:8]
    _check("the editable grid follows the workbook's column order",
           head == ["Company Name", "Inv. Date", "% of RV", "% of MV", "Cost", "RV",
                    "MV Adjustment (%)", "MV"], head)
    _check("inputs and computed columns live in one table",
           "Cost" in grid.columns and list(grid.columns)[-1] == "Proceeds")
    _check("Current Investments totals tie to the deal (426.4 cost / 657.3 RV)",
           abs(float(total.iloc[0]["Cost"]) - 426.4) < 0.05
           and abs(float(total.iloc[0]["RV"]) - 657.3) < 0.05,
           (total.iloc[0]["Cost"], total.iloc[0]["RV"]))
    a_row = grid[grid["Company Name"] == "Asset A"].iloc[0]
    _check("Asset A % of RV matches the workbook (28.6019%)",
           abs(float(a_row["% of RV"]) - 0.286018560778944) < 1e-9, a_row["% of RV"])
    _check("Asset A proceeds match its asset model (238.106358)",
           abs(float(a_row["Proceeds"]) - 238.106358) < 1e-5, a_row["Proceeds"])
    a_lp = lp[lp["Company Name"] == "Asset A"].iloc[0]
    _check("LP MOIC is scale-invariant (3.0923x)",
           abs(float(a_lp["LP MOIC"]) - 3.09229036363636) < 1e-6, a_lp["LP MOIC"])
    _check("fund and LP list the same companies", len(grid) == len(lp) - 1, (len(grid), len(lp)))
    _check("post-report draw is an outflow in its call year, returning at the assumed MOIC",
           float(post_grid.iloc[0]["2026"]) == -90.0
           and abs(float(post_grid.iloc[0]["Proceeds"]) - 90.0 * 1.15 ** 4) < 1e-6,
           (post_grid.iloc[0]["2026"], post_grid.iloc[0]["Proceeds"]))


# ===========================================================================
print("\n--- Funded / Unfunded Commitment blocks ---")
# ===========================================================================
lts = _line_tables(at)
funded = next((x for x in lts if "Proceeds after carried Interest" in list(x["Line item"])), None)
unfunded = next((x for x in lts if "Unfunded Commitment" in list(x["Line item"])), None)
_check("the Funded Commitment block renders", funded is not None)
_check("the Unfunded Commitment block renders", unfunded is not None)


def _row(t, name):
    return t[t["Line item"] == name].iloc[0]


if funded is not None:
    _check("Funded Commitment carries every workbook line, in order",
           list(funded["Line item"]) == [
               "Current Investments", "Net Cash", "Management fees",
               "Proceeds after management fees",
               "Future Carried Interest on Funded Commitment",
               "Accrued Carried Interest to Date", "Tax Blocker Leakage",
               "Proceeds after carried Interest"], list(funded["Line item"]))

    ycols = [c for c in funded.columns if c.isdigit()]
    cur, cash = _row(funded, "Current Investments"), _row(funded, "Net Cash")
    fees, after = _row(funded, "Management fees"), _row(funded, "Proceeds after management fees")
    _check("Net Cash is a balance-sheet line: value columns only",
           float(cash["RV"]) == 0.7 and all(float(cash[c]) == 0.0 for c in ycols))
    _check("Net Cash lifts reported value to the fund's 658.0",
           abs(float(after["RV"]) - 658.0) < 0.05, after["RV"])
    _check("Current Investments + Net Cash + fees == Proceeds after management fees",
           all(abs(float(cur[c]) + float(cash[c]) + float(fees[c]) - float(after[c])) < 1e-6
               for c in ycols))
    _check("management fees are a deduction", float(fees["Proceeds"]) < 0, fees["Proceeds"])
    final = _row(funded, "Proceeds after carried Interest")
    _check("after-fees less carry, accrued carry and blocker == final proceeds",
           all(abs(float(after[c])
                   + float(_row(funded, "Future Carried Interest on Funded Commitment")[c])
                   + float(_row(funded, "Accrued Carried Interest to Date")[c])
                   + float(_row(funded, "Tax Blocker Leakage")[c]) - float(final[c])) < 1e-6
               for c in ycols))

if unfunded is not None:
    _check("Unfunded Commitment carries every workbook line, in order",
           list(unfunded["Line item"]) == [
               "Post-Report Date Investments", "Carried Interest on Post-Report Investments",
               "Drawdown on remaining unfunded", "Return on Remaining Unfunded",
               "Unfunded Commitment"], list(unfunded["Line item"]))
    # MOIC is derived from the assumed IRR and hold (1.15^4), so the checks below assert
    # the relationships rather than hard-coding a multiple that would drift with them.
    _moic = 1.15 ** 4
    post_profit = float(_row(unfunded, "Post-Report Date Investments")["Proceeds"])
    _check("carry on post-report investments is 20% of their profit",
           abs(float(_row(unfunded, "Carried Interest on Post-Report Investments")["Proceeds"])
               + 0.2 * post_profit) < 1e-6,
           (_row(unfunded, "Carried Interest on Post-Report Investments")["Proceeds"], post_profit))
    drawn = float(_row(unfunded, "Drawdown on remaining unfunded")["Proceeds"])
    returned = float(_row(unfunded, "Return on Remaining Unfunded")["Proceeds"])
    _check("the blind pool is drawn in full (unfunded less the named follow-ons)",
           abs(drawn + 374.82) < 0.05, drawn)
    _check("and returns at the MOIC the assumed IRR and hold imply",
           abs(returned - abs(drawn) * _moic) < 0.05, (returned, abs(drawn) * _moic))


# ===========================================================================
print("\n--- The fees-and-carry explainer ---")
# ===========================================================================
roll = next((d.value for d in at.dataframe
             if "Hurdle balance, opening" in list(d.value.columns)), None)
_check("the year-by-year carry workings render", roll is not None)
if roll is not None:
    _check("no carry is taken while the hurdle is still open",
           all(r["Carry in year"] == 0 for _, r in roll.iterrows()
               if r["Hurdle balance, closing"] > 1e-6))

at2 = _app()
[r for r in at2.radio if "Declining hurdle balance" in str(r.options)][0].set_value(
    "Declining hurdle balance")
at2.run()
roll2 = next((d.value for d in at2.dataframe
              if "Hurdle balance, opening" in list(d.value.columns)), None)
_check("declining-balance workings accrue a preferred return",
       roll2 is not None and float(roll2["Preferred return accrued"].sum()) > 0)
_check("the explainer names the year carry starts, from the real numbers",
       any("On this forecast that happens in" in md.value and "20%" in md.value
           for md in at2.markdown))


# ===========================================================================
print("\n--- The pricing panel reprices live ---")
# ===========================================================================
at = _app()
panel = next((d.value for d in at.dataframe if "Effective Pricing" in list(d.value.columns)), None)
_check("the pricing panel renders", panel is not None)
if panel is not None:
    _check("it carries the workbook's pricing lines, in order",
           list(panel.iloc[:, 0]) == ["Net Effective Price", "Market Value", "Reported Value",
                                      "Unfunded", "Gross Exposure", "Gross Distributions",
                                      "Net Funded Exposure", "Net Proj. Proceeds"],
           list(panel.iloc[:, 0]))
    rv = panel[panel.iloc[:, 0] == "Reported Value"].iloc[0]
    unf = panel[panel.iloc[:, 0] == "Unfunded"].iloc[0]
    ge = panel[panel.iloc[:, 0] == "Gross Exposure"].iloc[0]
    _check("the quoted discount lands on Reported Value",
           abs(float(rv["Prem/(Disc)"]) + 0.10) < 1e-9, rv["Prem/(Disc)"])
    _check("effective Reported Value is the stated one less that discount",
           abs(float(rv["Effective Pricing"]) - float(rv["LP"]) * 0.9) < 1e-9,
           (rv["LP"], rv["Effective Pricing"]))
    _check("Gross Exposure is Reported Value plus Unfunded",
           abs(float(ge["LP"]) - (float(rv["LP"]) + float(unf["LP"]))) < 1e-9)

irr_at_10 = _metric(at, "Buyer IRR at this price")
at = _app()
_num(at, "Premium / (Discount) to Reported Value (%)").set_value(-30.0)
at.run()
irr_at_30 = _metric(at, "Buyer IRR at this price")
_check("a deeper discount raises the buyer's IRR",
       float(irr_at_30.rstrip("%")) > float(irr_at_10.rstrip("%")), (irr_at_10, irr_at_30))

ladder = next((d.value for d in at.dataframe
               if "Prem/(Disc)" in list(d.value.columns) and "IRR" in list(d.value.columns)), None)
_check("the price ladder renders", ladder is not None)
if ladder is not None:
    irrs = list(ladder["IRR"])
    _check("IRR rises as the price falls, across the whole ladder",
           all(a < b for a, b in zip(irrs, irrs[1:])), irrs)


# ===========================================================================
print("\n--- Voice control changes the model, visibly ---")
# ===========================================================================
# The mic itself can't be driven headlessly, so this exercises everything behind it:
# the same parse the app runs on a transcript, the same write into session state, and
# the effect that write has on the numbers.
at = _app()
assets = [{"aid": a, "name": at.session_state[f"am_{a}_name"]}
          for a in at.session_state["asset_ids"]]

irr_before = _metric(at, "Buyer IRR at this price")
c = parse_command("set the discount to twenty five percent", assets, at.session_state)
_check("a spoken discount is understood, and quoted as a negative premium",
       c is not None and c["key"] == "premium_discount_pct" and c["value"] == -25.0, c)
_check("the change is described with both the old and new value",
       describe(c) == "Premium / (Discount) to Reported Value: -10% → -25%", describe(c))
at.session_state[c["key"]] = c["value"]
at.run()
_check("applying it reprices the deal",
       _metric(at, "Buyer IRR at this price") != irr_before
       and float(_metric(at, "Buyer IRR at this price").rstrip("%")) > float(irr_before.rstrip("%")),
       (irr_before, _metric(at, "Buyer IRR at this price")))

at = _app()
c = parse_command("asset a exit year 2033", assets, at.session_state)
_check("a spoken exit year targets that asset", c is not None and c["key"] == "am_A_exit_cal", c)
at.session_state[c["key"]] = int(c["value"])
at.run()
_check("and moves the forecast horizon with it",
       _metric(at, "Forecast horizon").startswith("8 yrs"), _metric(at, "Forecast horizon"))

at = _app()
before = at.session_state["premium_discount_pct"]
_check("a sentence with no number changes nothing",
       parse_command("push the discount out a bit", assets, at.session_state) is None)
_check("an out-of-range value is refused rather than clamped",
       parse_command("discount 250 percent", assets, at.session_state) is None)
_check("state is untouched by a refused command",
       at.session_state["premium_discount_pct"] == before)

print("\n=== SUMMARY ===")
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")

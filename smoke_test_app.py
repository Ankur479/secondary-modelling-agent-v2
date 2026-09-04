"""
Headless smoke test of app.py using Streamlit's AppTest -- runs the whole script
with various widget states and checks for uncaught exceptions. Not a correctness
test (that's verify_v2_features.py / test_leverage_overlay.py) -- this just proves
the UI wiring doesn't crash.
Run: python3 smoke_test_app.py
"""
from streamlit.testing.v1 import AppTest

FAILS = []


def run_and_check(label, mutate=None):
    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()
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


print("--- Default run (Aggregate NAV mode, sample data) ---")
run_and_check("default aggregate mode")

print("\n--- Portfolio companies (detailed) mode, default expected-return ---")
run_and_check("portfolio mode", lambda at: at.sidebar.radio[0].set_value("Portfolio companies (detailed)"))

print("\n--- Portfolio mode: asset models drive the forecast by default ---")


def portfolio_asset_models(at):
    at.sidebar.radio[0].set_value("Portfolio companies (detailed)")
    at.run()
    # Every company should have a "drives the forecast" toggle rendered in the Asset Model tab.
    drives = [c for c in at.checkbox if "drive the fund forecast" in c.label]
    assert len(drives) >= 5, f"expected one drives-forecast toggle per company, found {len(drives)}"


run_and_check("portfolio + asset models on", portfolio_asset_models)

print("\n--- One asset model switched off (falls back to Expected Return %) ---")


def one_asset_model_off(at):
    at.sidebar.radio[0].set_value("Portfolio companies (detailed)")
    at.run()
    drives = [c for c in at.checkbox if "drive the fund forecast" in c.label]
    assert drives, "no drives-forecast toggles found"
    drives[0].set_value(False)


run_and_check("one asset model off", one_asset_model_off)

print("\n--- Unfunded commitment generates its own return ---")


def to_unfunded_return(at):
    cbs = [c for c in at.sidebar.checkbox]
    target = [c for c in cbs if "generates its own return" in c.label]
    assert target, f"could not find unfunded-return checkbox among {[c.label for c in cbs]}"
    target[0].set_value(True)


run_and_check("unfunded-return checkbox on", to_unfunded_return)

print("\n--- Leverage overlay + unfunded return together ---")


def to_leverage_and_unfunded(at):
    cbs = [c for c in at.sidebar.checkbox]
    for c in cbs:
        if "generates its own return" in c.label:
            c.set_value(True)
        if "finances part of the purchase" in c.label:
            c.set_value(True)


run_and_check("leverage + unfunded-return together", to_leverage_and_unfunded)

print("\n--- Fund/LP inputs at a different ratio ---")


def diff_lp_pct(at):
    ni = [n for n in at.sidebar.number_input]
    for n in ni:
        if n.label == "Selling LP's commitment ($)":
            n.set_value(5_000_000.0)


run_and_check("different LP commitment", diff_lp_pct)

print("\n--- Declining-balance waterfall (with GP catch-up) ---")


def to_declining_waterfall(at):
    radios = [r for r in at.sidebar.radio]
    wf = [r for r in radios if "hurdle balance" in str(r.options)]
    assert wf, f"could not find waterfall-style radio among {[r.options for r in radios]}"
    wf[0].set_value("Declining hurdle balance")


run_and_check("declining-balance waterfall", to_declining_waterfall)


def to_declining_waterfall_with_catchup(at):
    to_declining_waterfall(at)
    at.run()
    sliders = [s for s in at.sidebar.slider]
    catchup = [s for s in sliders if "GP catch-up" in s.label]
    assert catchup, f"could not find GP catch-up slider among {[s.label for s in sliders]}"
    catchup[0].set_value(50.0)


run_and_check("declining-balance waterfall + 50% catch-up", to_declining_waterfall_with_catchup)

print("\n--- Gross-reported performance inputs ---")


def with_gross_reported(at):
    ni = [n for n in at.sidebar.number_input]
    for n in ni:
        if n.label == "Gross MOIC (x)":
            n.set_value(2.1)
        if n.label == "Gross IRR (%)":
            n.set_value(28.0)


run_and_check("gross-reported MOIC/IRR shown in Analytics tab", with_gross_reported)

print("\n--- Portfolio mode + two-tier (step-down) management fee ---")


def to_two_tier_fee(at):
    at.sidebar.radio[0].set_value("Portfolio companies (detailed)")
    at.run()
    radios = [r for r in at.sidebar.radio]
    fee_radio = [r for r in radios if "step-down on remaining cost" in str(r.options)]
    assert fee_radio, f"could not find fee-basis radio among {[r.options for r in radios]}"
    fee_radio[0].set_value("Two-tier: flat on commitment, then step-down on remaining cost")


run_and_check("portfolio + two-tier management fee", to_two_tier_fee)

print("\n--- Everything together: Fund/LP + asset models + unfunded return + leverage + declining waterfall ---")


def everything(at):
    at.sidebar.radio[0].set_value("Portfolio companies (detailed)")
    at.run()
    radios = [r for r in at.sidebar.radio]
    wf = [r for r in radios if "hurdle balance" in str(r.options)][0]
    wf.set_value("Declining hurdle balance")
    fee_radio = [r for r in radios if "step-down on remaining cost" in str(r.options)][0]
    fee_radio.set_value("Two-tier: flat on commitment, then step-down on remaining cost")
    for c in at.sidebar.checkbox:
        if "generates its own return" in c.label:
            c.set_value(True)
        if "finances part of the purchase" in c.label:
            c.set_value(True)


run_and_check("everything combined", everything)

print("\n--- Deal Snapshot editing, add/remove, rename (behaviour, not just 'no crash') ---")


def _portfolio_app():
    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("Portfolio companies (detailed)")
    at.run()
    return at


def _metric(at, label):
    hits = [m.value for m in at.metric if m.label == label]
    assert hits, f"metric {label!r} not found"
    return hits[0]


def _check(label, cond, detail=""):
    if cond:
        print(f"  [PASS] {label}")
    else:
        print(f"  [FAIL] {label}  {detail}")
        FAILS.append(label)


at = _portfolio_app()
nav_before = _metric(at, "Aggregate Reported Value")
rv_input = [n for n in at.number_input if n.label == "Reported Value (RV, $mm)"][0]
rv_input.set_value(200.0)
at.run()
_check("editing a company's Reported Value moves fund NAV on the same rerun",
       _metric(at, "Aggregate Reported Value") == "$669,300,000",
       f"{nav_before} -> {_metric(at, 'Aggregate Reported Value')}")

at = _portfolio_app()
exit_input = [n for n in at.number_input if n.label == "Exit Year"][0]
exit_input.set_value(2035)
at.run()
_check("editing a company's Exit Year extends the forecast horizon",
       _metric(at, "Forecast horizon").startswith("10 yrs"), _metric(at, "Forecast horizon"))

at = _portfolio_app()
[b for b in at.button if "Add 1 company" in b.label][0].click()
at.run()
added = _metric(at, "Companies")
[b for b in at.button if b.label == "Remove"][0].click()
at.run()
_check("add / remove a company", added == "6" and _metric(at, "Companies") == "5",
       f"after add={added}, after remove={_metric(at, 'Companies')}")

# The point of the count box: N rows in one click, not N clicks.
at = _portfolio_app()
[n for n in at.number_input if n.label == "Rows to add"][0].set_value(3)
at.run()
bulk = [b for b in at.button if "Add 3 companies" in b.label]
_check("the Add button pluralises and offers the chosen count", bool(bulk),
       [b.label for b in at.button][:6])
if bulk:
    bulk[0].click()
    at.run()
    _check("adding 3 companies in one click", _metric(at, "Companies") == "8",
           _metric(at, "Companies"))

at = _portfolio_app()
ns = [n for n in at.number_input if n.label == "Rows to add"]
_check("both blocks have their own count box", len(ns) >= 2, len(ns))
if len(ns) >= 2:
    ns[1].set_value(2)
    at.run()
    bp = [b for b in at.button if "Add 2 investments" in b.label]
    if bp:
        bp[0].click()
        at.run()
    _check("adding 2 post-report investments in one click",
           len(at.session_state["followon_rows"]) == 3,
           len(at.session_state["followon_rows"]))

at = _portfolio_app()
moic_before = _metric(at, "Gross MOIC (vs Cost)")
[t for t in at.text_input if t.label == "Company"][0].set_value("Project Falcon")
at.run()
_check("renaming a company keeps its model intact (stable widget ids)",
       _metric(at, "Gross MOIC (vs Cost)") == moic_before,
       f"{moic_before} -> {_metric(at, 'Gross MOIC (vs Cost)')}")

print("\n--- Investments tab: column order and fund/LP tie-out ---")


def _inv_tables(at):
    """The Investments tab's five tables, in render order:
    0 current fund grid, 1 current total, 2 current LP, 3 post-report grid, 4 post-report LP."""
    return [d.value for d in at.dataframe if "Company Name" in list(d.value.columns)]


at = _portfolio_app()
t = _inv_tables(at)
_check("all five Investments tables render", len(t) == 5, len(t))

if len(t) == 5:
    grid, total, lp, post_grid, post_lp = t

    head = [c for c in list(grid.columns) if c != "id"][:8]
    _check("the editable grid follows the workbook's column order",
           head == ["Company Name", "Inv. Date", "% of RV", "% of MV", "Cost", "RV",
                    "MV Adjustment (%)", "MV"], head)
    _check("the grid's last column is Proceeds", list(grid.columns)[-1] == "Proceeds",
           list(grid.columns)[-1])
    _check("inputs and computed columns live in ONE table (no separate input grid)",
           "Cost" in grid.columns and "Proceeds" in grid.columns and len(t) == 5)
    _check("the grid carries a row id, so deleting a row can't reassign another model",
           "id" in grid.columns, list(grid.columns)[:2])

    _check("Current Investments total cost ties to the deal ($426.4mm)",
           abs(float(total.iloc[0]["Cost"]) - 426.4) < 0.05, total.iloc[0]["Cost"])
    _check("Current Investments total RV ties to fund NAV ($657.3mm)",
           abs(float(total.iloc[0]["RV"]) - 657.3) < 0.05, total.iloc[0]["RV"])

    a_row = grid[grid["Company Name"] == "Asset A"].iloc[0]
    _check("Asset A % of RV matches the workbook (28.6019%)",
           abs(float(a_row["% of RV"]) - 0.286018560778944) < 1e-9, a_row["% of RV"])
    _check("Asset A proceeds match its asset model (238.106358)",
           abs(float(a_row["Proceeds"]) - 238.106358) < 1e-5, a_row["Proceeds"])

    head_lp = list(lp.columns)[:5]
    _check("LP columns follow the workbook's order",
           head_lp == ["Company Name", "Inv. Date", "LP Cost", "LP RV", "LP MV"], head_lp)
    _check("last LP columns are Proceeds then MOIC",
           list(lp.columns)[-2:] == ["LP Proceeds", "LP MOIC"], list(lp.columns)[-2:])
    a_lp = lp[lp["Company Name"] == "Asset A"].iloc[0]
    _check("LP MOIC is scale-invariant (matches the fund's 3.0923x)",
           abs(float(a_lp["LP MOIC"]) - 3.09229036363636) < 1e-6, a_lp["LP MOIC"])
    _check("LP RV total is the LP's share of fund NAV",
           abs(float(lp[lp["Company Name"] == "Total"].iloc[0]["LP RV"]) - 21.700552) < 1e-3,
           lp[lp["Company Name"] == "Total"].iloc[0]["LP RV"])
    _check("fund and LP list the same companies",
           len(grid) == len(lp) - 1, f"grid={len(grid)} lp={len(lp)} (LP carries a Total row)")

    f_row = post_grid.iloc[0]
    _check("post-report investment shows its draw as an outflow in the call year",
           float(f_row["2026"]) == -90.0, f_row["2026"])
    _check("post-report proceeds land at the assumed MOIC (90 x 1.75)",
           abs(float(f_row["Proceeds"]) - 157.5) < 1e-6, f_row["Proceeds"])
    _check("post-report fund and LP list the same investments",
           len(post_grid) == len(post_lp) - 1,
           f"fund={len(post_grid)} lp={len(post_lp)} (LP carries a Total row)")


print("\n--- Investments grids: editable, dynamic rows, fund/LP row counts locked together ---")


def _counts(at):
    """Row counts of the five Investments tables, in render order."""
    return [len(t) for t in _inv_tables(at)]


# A data_editor's edits live in session_state as a delta dict, which is how a real
# user's keystroke reaches the script -- so driving it this way exercises the same path.
at = _portfolio_app()
at.session_state["inv_current_editor"] = {
    "edited_rows": {0: {"RV": 250.0}}, "added_rows": [], "deleted_rows": []}
at.run()
_check("editing RV in the Investments grid updates fund NAV",
       _metric(at, "Aggregate Reported Value") == "$719,300,000",
       _metric(at, "Aggregate Reported Value"))
_check("that edit also reaches the Deal Snapshot widget (one shared store)",
       at.session_state["am_A_rv"] == 250.0, at.session_state["am_A_rv"])

at = _portfolio_app()
at.session_state["inv_current_editor"] = {
    "edited_rows": {},
    "added_rows": [{"Company Name": "Asset G", "Cost": 50.0, "RV": 60.0,
                    "MV Adjustment (%)": 0.0, "Exit Year": 2030}],
    "deleted_rows": []}
at.run()
counts = _counts(at)
_check("adding a row in the grid adds a company everywhere",
       _metric(at, "Companies") == "6" and counts[0] == 6 and counts[2] == 7,
       f"companies={_metric(at, 'Companies')} table rows={counts}")
_check("the added company's RV lands in fund NAV",
       _metric(at, "Aggregate Reported Value") == "$717,300,000",
       _metric(at, "Aggregate Reported Value"))

at = _portfolio_app()
at.session_state["inv_current_editor"] = {
    "edited_rows": {}, "added_rows": [], "deleted_rows": [1]}
at.run()
counts = _counts(at)
_check("deleting a row removes that company everywhere",
       _metric(at, "Companies") == "4" and counts[0] == 4 and counts[2] == 5,
       f"companies={_metric(at, 'Companies')} table rows={counts}")

at = _portfolio_app()
at.session_state["followons_editor"] = {
    "edited_rows": {},
    "added_rows": [{"Company Name": "Asset H", "Cost": 40.0, "Year": 2}],
    "deleted_rows": []}
at.run()
counts = _counts(at)
_check("adding a post-report investment shows up in both its views",
       counts[3] == 2 and counts[4] == 3, f"post-report table rows={counts[3:]}")
_check("no exceptions across any of the grid edits", not at.exception, at.exception)

print("\n--- Funded / Unfunded Commitment blocks and the fees-and-carry explainer ---")


def _line_tables(at):
    return [d.value for d in at.dataframe if "Line item" in list(d.value.columns)]


at = _portfolio_app()
lts = _line_tables(at)
funded = next((t for t in lts if "Proceeds after carried Interest" in list(t["Line item"])), None)
unfunded = next((t for t in lts if "Unfunded Commitment" in list(t["Line item"])), None)
_check("the Funded Commitment block renders", funded is not None)
_check("the Unfunded Commitment block renders", unfunded is not None)

if funded is not None:
    items = list(funded["Line item"])
    _check("Funded Commitment carries every line of the source model, in its order",
           items == ["Current Investments", "Net Cash", "Management fees",
                     "Proceeds after management fees",
                     "Future Carried Interest on Funded Commitment",
                     "Accrued Carried Interest to Date", "Tax Blocker Leakage",
                     "Proceeds after carried Interest"], items)

    def _row(t, name):
        return t[t["Line item"] == name].iloc[0]

    cur = _row(funded, "Current Investments")
    _check("Current Investments cost/RV tie to the portfolio (426.4 / 657.3)",
           abs(float(cur["Cost"]) - 426.4) < 0.05 and abs(float(cur["RV"]) - 657.3) < 0.05,
           (cur["Cost"], cur["RV"]))

    cash = _row(funded, "Net Cash")
    year_cols = [c for c in funded.columns if c.isdigit()]
    _check("Net Cash is a balance-sheet line: value columns only, no cash flow",
           float(cash["RV"]) == 0.7 and all(float(cash[c]) == 0.0 for c in year_cols),
           (cash["RV"], [float(cash[c]) for c in year_cols]))
    _check("Net Cash lifts reported value to the fund's 658.0",
           abs(float(_row(funded, "Proceeds after management fees")["RV"]) - 658.0) < 0.05,
           _row(funded, "Proceeds after management fees")["RV"])

    # The block is a presentation of the app's own forecast, not a second calculation:
    # gross proceeds less the fee borne by them must land back on the distributions the
    # pricing tab uses.
    fees = _row(funded, "Management fees")
    after = _row(funded, "Proceeds after management fees")
    _check("Current Investments + Net Cash + fees == Proceeds after management fees",
           all(abs((float(cur[c]) + float(cash[c]) + float(fees[c])) - float(after[c])) < 1e-6
               for c in year_cols))
    _check("management fees are negative",
           float(fees["Proceeds"]) < 0, fees["Proceeds"])

    carry = _row(funded, "Future Carried Interest on Funded Commitment")
    final = _row(funded, "Proceeds after carried Interest")
    accrued = _row(funded, "Accrued Carried Interest to Date")
    blocker = _row(funded, "Tax Blocker Leakage")
    _check("after-fees less carry, accrued carry and blocker == final proceeds",
           all(abs((float(after[c]) + float(carry[c]) + float(accrued[c]) + float(blocker[c]))
                   - float(final[c])) < 1e-6 for c in year_cols))

if unfunded is not None:
    items = list(unfunded["Line item"])
    _check("Unfunded Commitment carries every line of the source model, in its order",
           items == ["Post-Report Date Investments",
                     "Carried Interest on Post-Report Investments",
                     "Drawdown on remaining unfunded", "Return on Remaining Unfunded",
                     "Unfunded Commitment"], items)
    post = unfunded[unfunded["Line item"] == "Post-Report Date Investments"].iloc[0]
    pcarry = unfunded[unfunded["Line item"] == "Carried Interest on Post-Report Investments"].iloc[0]
    ret = unfunded[unfunded["Line item"] == "Return on Remaining Unfunded"].iloc[0]
    draw = unfunded[unfunded["Line item"] == "Drawdown on remaining unfunded"].iloc[0]
    # 90 in, 157.5 back -> 67.5 profit -> 20% carry = 13.5
    _check("carry on post-report investments is 20% of their profit (13.5)",
           abs(float(pcarry["Proceeds"]) + 13.5) < 1e-6, pcarry["Proceeds"])
    _check("the blind pool is drawn in full (-374.8)",
           abs(float(draw["Proceeds"]) + 374.8) < 0.05, draw["Proceeds"])
    _check("the blind pool returns at the assumed 1.75x (655.9)",
           abs(float(ret["Proceeds"]) - 655.9) < 0.05, ret["Proceeds"])

# --- the explainer ---
roll = next((d.value for d in at.dataframe
             if "Hurdle balance, opening" in list(d.value.columns)), None)
_check("the year-by-year carry workings render", roll is not None)
if roll is not None and funded is not None:
    # The workings must agree with the block they explain: total carry in the roll-forward,
    # grossed to fund level, is the Future Carry line.
    lp_share = float(at.session_state["lp_commitment"]) / float(at.session_state["fund_commitment"]) \
        if "lp_commitment" in at.session_state else None
    total_carry_lp = float(roll["Carry in year"].sum())
    carry_line = abs(float(_row(funded, "Future Carried Interest on Funded Commitment")["Proceeds"]))
    if lp_share:
        _check("roll-forward carry equals the Funded Commitment carry line",
               abs(total_carry_lp / lp_share / 1e6 - carry_line) < 0.01,
               (total_carry_lp / lp_share / 1e6, carry_line))
    _check("no carry is taken while the hurdle is still open",
           all(r["Carry in year"] == 0 for _, r in roll.iterrows()
               if r["Hurdle balance, closing"] > 1e-6))

# Declining-balance style must produce a preferred-return column that actually accrues.
at2 = _portfolio_app()
[r for r in at2.sidebar.radio if "hurdle balance" in str(r.options)][0].set_value("Declining hurdle balance")
at2.run()
roll2 = next((d.value for d in at2.dataframe
              if "Hurdle balance, opening" in list(d.value.columns)), None)
_check("declining-balance workings accrue a preferred return",
       roll2 is not None and float(roll2["Preferred return accrued"].sum()) > 0,
       None if roll2 is None else roll2["Preferred return accrued"].sum())
_check("the explainer names the year carry starts and the rate, from the real numbers",
       any("On this forecast that happens in" in md.value and "20%" in md.value
           for md in at2.markdown),
       [md.value[:80] for md in at2.markdown if "forecast that happens" in md.value])

print("\n=== SUMMARY ===")
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    raise SystemExit(1)
print("ALL SMOKE TESTS PASSED")

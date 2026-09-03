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
[b for b in at.button if "Add a company" in b.label][0].click()
at.run()
added = _metric(at, "Companies")
[b for b in at.button if b.label == "Remove"][0].click()
at.run()
_check("add / remove a company", added == "6" and _metric(at, "Companies") == "5",
       f"after add={added}, after remove={_metric(at, 'Companies')}")

at = _portfolio_app()
moic_before = _metric(at, "Gross MOIC (vs Cost)")
[t for t in at.text_input if t.label == "Company"][0].set_value("Project Falcon")
at.run()
_check("renaming a company keeps its model intact (stable widget ids)",
       _metric(at, "Gross MOIC (vs Cost)") == moic_before,
       f"{moic_before} -> {_metric(at, 'Gross MOIC (vs Cost)')}")

print("\n=== SUMMARY ===")
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    raise SystemExit(1)
print("ALL SMOKE TESTS PASSED")

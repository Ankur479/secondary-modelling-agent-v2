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

print("\n--- Portfolio mode + bottom-up EV/EBITDA valuation ---")


def to_ebitda(at):
    at.sidebar.radio[0].set_value("Portfolio companies (detailed)")
    at.run()
    # radio[1] should now be the valuation-method radio
    radios = [r for r in at.sidebar.radio]
    valuation_radio = [r for r in radios if "EV/EBITDA" in str(r.options)]
    assert valuation_radio, f"could not find valuation-method radio among {[r.options for r in radios]}"
    valuation_radio[0].set_value("Bottom-up EV/EBITDA model (detailed)")


run_and_check("portfolio + EV/EBITDA mode", to_ebitda)

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

print("\n--- Everything together: Fund/LP + EV/EBITDA + unfunded return + leverage + declining waterfall ---")


def everything(at):
    at.sidebar.radio[0].set_value("Portfolio companies (detailed)")
    at.run()
    radios = [r for r in at.sidebar.radio]
    valuation_radio = [r for r in radios if "EV/EBITDA" in str(r.options)][0]
    valuation_radio.set_value("Bottom-up EV/EBITDA model (detailed)")
    wf = [r for r in radios if "hurdle balance" in str(r.options)][0]
    wf.set_value("Declining hurdle balance")
    for c in at.sidebar.checkbox:
        if "generates its own return" in c.label:
            c.set_value(True)
        if "finances part of the purchase" in c.label:
            c.set_value(True)


run_and_check("everything combined", everything)

print("\n=== SUMMARY ===")
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    raise SystemExit(1)
print("ALL SMOKE TESTS PASSED")

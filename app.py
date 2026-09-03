"""
Secondary Market Modelling Agent
--------------------------------
An AI-assisted Streamlit app for valuing LP stakes in the PE secondary market:
metrics to date (DPI/RVPI/TVPI/IRR), a forward cash-flow/NAV runoff forecast,
a secondary pricing sensitivity table, and an AI assistant that answers
questions grounded in the computed numbers.
"""
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ai_agent import ask_agent
from finance_engine import (
    apply_carry_waterfall,
    apply_carry_waterfall_declining_balance,
    build_unfunded_returns,
    build_unfunded_schedule,
    cash_flow_duration,
    ebitda_exit_value,
    forecast_cashflows,
    forecast_from_portfolio,
    fund_metrics_to_date,
    implied_annual_return,
    leverage_overlay,
    reported_vs_market_value,
    secondary_pricing,
)

st.set_page_config(page_title="Secondary Modelling Agent", layout="wide")

st.title("Secondary Market Modelling Agent")
st.caption("AI-assisted valuation and cash-flow forecasting for LP secondary transactions")

# --------------------------------------------------------------------------
# Sidebar inputs
# --------------------------------------------------------------------------
st.sidebar.header("1. Fund cash flow data")
use_sample = st.sidebar.checkbox("Use sample data", value=True)

uploaded = None
if not use_sample:
    uploaded = st.sidebar.file_uploader(
        "Upload CSV (columns: Date, Capital_Call, Distribution)", type=["csv"]
    )

if use_sample or uploaded is None:
    df = pd.read_csv("sample_fund_cashflows.csv", parse_dates=["Date"])
else:
    df = pd.read_csv(uploaded, parse_dates=["Date"])

cf_dates = df["Date"].dt.date.tolist()
calls = df["Capital_Call"].fillna(0).tolist()
dists = df["Distribution"].fillna(0).tolist()
original_paid_in = sum(calls)
original_distributions = sum(dists)

st.sidebar.header("1B. Fund vs. LP ownership")
st.sidebar.caption(
    "A secondary prices ONE LP's slice of the fund, not the whole fund. Everything you "
    "enter elsewhere in this app (the cash-flow CSV, NAV, accrued carry, unfunded amounts) "
    "is fund-level; the app scales it down to the selling LP's share automatically."
)
fund_commitment = st.sidebar.number_input(
    "Fund total commitment ($)", min_value=0.01, value=50_000_000.0, step=1_000_000.0, format="%.0f"
)
lp_commitment = st.sidebar.number_input(
    "Selling LP's commitment ($)", min_value=0.0, value=2_500_000.0, step=100_000.0, format="%.0f"
)
lp_pct = (lp_commitment / fund_commitment) if fund_commitment > 0 else 0.0
st.sidebar.caption(f"LP ownership = {lp_pct*100:.2f}% of the fund")

st.sidebar.header("2. Current position")
forecast_mode = st.sidebar.radio(
    "Forecast mode",
    ["Aggregate NAV (simple)", "Portfolio companies (detailed)"],
    index=0,
    help="Portfolio mode lets each holding have its own growth rate, valuation "
         "adjustment, and exit timing instead of one shared NAV on a single runoff curve.",
)
as_of = st.sidebar.date_input("As-of date", value=date.today())

if forecast_mode == "Aggregate NAV (simple)":
    nav_current = st.sidebar.number_input(
        "Current NAV / Reported Value ($)", min_value=0.0, value=62_000_000.0, step=1_000_000.0, format="%.0f"
    )
else:
    nav_current = None  # computed below from the portfolio companies table
    st.sidebar.caption("Current NAV is computed from the portfolio companies table in section 3.")

override_totals = st.sidebar.checkbox("Override paid-in capital / distributions to date", value=False)
if override_totals:
    paid_in_input = st.sidebar.number_input(
        "Paid-in capital ($)", min_value=0.0, value=float(original_paid_in), step=1_000_000.0, format="%.0f"
    )
    dist_input = st.sidebar.number_input(
        "Distributions to date ($)", min_value=0.0, value=float(original_distributions), step=1_000_000.0, format="%.0f"
    )
    if original_paid_in > 0:
        scale_c = paid_in_input / original_paid_in
        calls = [c * scale_c for c in calls]
    if original_distributions > 0:
        scale_d = dist_input / original_distributions
        dists = [d * scale_d for d in dists]
    st.sidebar.caption(
        "Historical cash flow timing (dates) is kept from the data; each amount is scaled "
        "proportionally so the totals match your override."
    )

st.sidebar.header("3. Forecast assumptions")
if forecast_mode == "Aggregate NAV (simple)":
    remaining_years = st.sidebar.slider("Remaining fund life (years)", 1, 10, 5)
    gross_return = st.sidebar.slider("Expected gross annual return on remaining NAV", 0.0, 0.30, 0.15, step=0.01)
    shape = st.sidebar.selectbox("Distribution runoff shape", ["back_ended", "even", "front_ended"], index=0)
    portfolio_df = None
else:
    gross_return = None
    shape = None
    st.sidebar.write(
        "Portfolio companies - Reported Value is the GP's official mark; MV Adjustment "
        "lets you apply your own diligence-based view on top of it."
    )
    default_portfolio = pd.DataFrame([
        {"Company": "Company A", "Reported Value ($M)": 15.0, "MV Adjustment (%)": 0.0,
         "Expected Return (%)": 25.0, "Exit Year 1": 3, "Exit % 1": 100.0, "Exit Year 2": 3, "Exit % 2": 0.0},
        {"Company": "Company B", "Reported Value ($M)": 12.0, "MV Adjustment (%)": -10.0,
         "Expected Return (%)": 18.0, "Exit Year 1": 3, "Exit % 1": 50.0, "Exit Year 2": 5, "Exit % 2": 100.0},
        {"Company": "Company C", "Reported Value ($M)": 10.0, "MV Adjustment (%)": 5.0,
         "Expected Return (%)": 10.0, "Exit Year 1": 2, "Exit % 1": 100.0, "Exit Year 2": 2, "Exit % 2": 0.0},
        {"Company": "Company D", "Reported Value ($M)": 15.0, "MV Adjustment (%)": 0.0,
         "Expected Return (%)": 20.0, "Exit Year 1": 4, "Exit % 1": 100.0, "Exit Year 2": 4, "Exit % 2": 0.0},
        {"Company": "Company E", "Reported Value ($M)": 10.0, "MV Adjustment (%)": -5.0,
         "Expected Return (%)": 12.0, "Exit Year 1": 5, "Exit % 1": 100.0, "Exit Year 2": 5, "Exit % 2": 0.0},
    ])
    portfolio_df = st.sidebar.data_editor(
        default_portfolio, num_rows="dynamic", width="stretch", key="portfolio_editor"
    )
    if len(portfolio_df) > 0:
        nav_current = float(portfolio_df["Reported Value ($M)"].sum()) * 1_000_000
        remaining_years = int(max(portfolio_df["Exit Year 1"].max(), portfolio_df["Exit Year 2"].max()))
    else:
        nav_current = 0.0
        remaining_years = 1
    st.sidebar.caption(f"Aggregate Reported NAV from portfolio: ${nav_current:,.0f}")

    valuation_method = st.sidebar.radio(
        "Company valuation method",
        ["Expected Return % (simple)", "Bottom-up EV/EBITDA model (detailed)"],
        index=0,
        help="Simple: use the flat 'Expected Return (%)' column above directly. Detailed: "
             "build each company's own Revenue -> EBITDA -> FCF -> Exit EV/EBITDA model below; "
             "the app backs out the equivalent annualized return and uses that instead (the "
             "'Expected Return (%)' column above is then ignored for companies with a matching "
             "row here). Exit timing (Exit Year 1) still comes from the table above.",
    )
    ebitda_df = None
    if valuation_method.startswith("Bottom-up"):
        st.sidebar.write("EV/EBITDA build per company (Company name must match the table above):")
        default_ebitda = pd.DataFrame([
            # Entry Revenue chosen so Entry Equity Value x Fund Ownership % lands close to
            # each company's default Reported Value above -- otherwise the implied return
            # comes out absurd purely from a scale mismatch between the two tables. Adjust
            # freely; the two tables just need to describe the same company consistently.
            {"Company": "Company A", "Entry Revenue ($M)": 10.7, "Entry EBITDA Margin (%)": 25.0,
             "Entry EV/EBITDA (x)": 11.0, "Entry Net Debt/EBITDA (x)": 3.0, "Revenue Growth (%)": 8.0,
             "FCF Conversion (%)": 50.0, "Exit EV/EBITDA (x)": 12.0, "Fund Ownership (%)": 70.0},
            {"Company": "Company B", "Entry Revenue ($M)": 7.7, "Entry EBITDA Margin (%)": 25.0,
             "Entry EV/EBITDA (x)": 11.0, "Entry Net Debt/EBITDA (x)": 3.0, "Revenue Growth (%)": 8.0,
             "FCF Conversion (%)": 50.0, "Exit EV/EBITDA (x)": 12.0, "Fund Ownership (%)": 70.0},
            {"Company": "Company C", "Entry Revenue ($M)": 7.5, "Entry EBITDA Margin (%)": 25.0,
             "Entry EV/EBITDA (x)": 11.0, "Entry Net Debt/EBITDA (x)": 3.0, "Revenue Growth (%)": 8.0,
             "FCF Conversion (%)": 50.0, "Exit EV/EBITDA (x)": 12.0, "Fund Ownership (%)": 70.0},
            {"Company": "Company D", "Entry Revenue ($M)": 10.7, "Entry EBITDA Margin (%)": 25.0,
             "Entry EV/EBITDA (x)": 11.0, "Entry Net Debt/EBITDA (x)": 3.0, "Revenue Growth (%)": 8.0,
             "FCF Conversion (%)": 50.0, "Exit EV/EBITDA (x)": 12.0, "Fund Ownership (%)": 70.0},
            {"Company": "Company E", "Entry Revenue ($M)": 6.8, "Entry EBITDA Margin (%)": 25.0,
             "Entry EV/EBITDA (x)": 11.0, "Entry Net Debt/EBITDA (x)": 3.0, "Revenue Growth (%)": 8.0,
             "FCF Conversion (%)": 50.0, "Exit EV/EBITDA (x)": 12.0, "Fund Ownership (%)": 70.0},
        ])
        ebitda_df = st.sidebar.data_editor(
            default_ebitda, num_rows="dynamic", width="stretch", key="ebitda_editor"
        )
        st.sidebar.caption(
            "Exit value = (Exit EBITDA x Exit EV/EBITDA multiple - Net Debt at exit) x Fund "
            "Ownership %. Net Debt pays down by that year's FCF each year (floored at zero). "
            "The app then solves for the flat annual rate that compounds the company's current "
            "Market Value (from the table above) to this exit value by Exit Year 1."
        )

st.sidebar.header("4. Unfunded commitment")
st.sidebar.write("Known follow-on investments (already identified):")
default_followons = pd.DataFrame([
    {"Name": "Follow-on A", "Amount ($M)": 2.0, "Year": 1},
])
known_followons_df = st.sidebar.data_editor(
    default_followons, num_rows="dynamic", width="stretch", key="followons_editor"
)
blind_pool_amount = st.sidebar.number_input(
    "Blind pool (unidentified future calls, $)", min_value=0.0, value=0.0, step=500_000.0, format="%.0f"
)
if blind_pool_amount > 0:
    blind_pool_years = st.sidebar.slider(
        "Blind pool call period (years)", 1, max(1, remaining_years), min(2, max(1, remaining_years))
    )
else:
    blind_pool_years = 0
st.sidebar.caption(
    "Known follow-ons are called in full in their specified year. The blind pool is spread "
    "evenly over its call period, front-loaded like a typical PE investment period."
)

unfunded_generates_return = st.sidebar.checkbox(
    "Unfunded commitment generates its own return", value=False,
    help="A capital call doesn't just sit as an outflow -- it funds a new investment that "
         "itself goes on to return money. Turn this on to project a return on every future "
         "call, a fixed hold period and MOIC after which it lands.",
)
if unfunded_generates_return:
    uc1, uc2 = st.sidebar.columns(2)
    unfunded_hold_years = uc1.number_input("Hold period (years)", min_value=1, value=3, step=1)
    unfunded_moic = uc2.number_input("Assumed MOIC (x)", min_value=0.1, value=1.5, step=0.1, format="%.1f")
    st.sidebar.caption(
        "Each call in year Y returns amount x MOIC in year Y + hold period. Calls that would "
        "mature beyond the forecast horizon are excluded (flagged below) rather than distorting "
        "the last forecast year."
    )
else:
    unfunded_hold_years = 0
    unfunded_moic = 0.0

st.sidebar.header("5. Fees & carried interest")
apply_fees = st.sidebar.checkbox("Apply management fee & carry", value=True)
mgmt_fee = st.sidebar.slider("Annual management fee (%)", 0.0, 5.0, 2.0, step=0.25) / 100
hurdle_rate = st.sidebar.slider("Preferred return / hurdle (%)", 0.0, 15.0, 8.0, step=0.5) / 100
carry_rate = st.sidebar.slider("Carried interest (%)", 0.0, 30.0, 20.0, step=1.0) / 100
accrued_carry = st.sidebar.number_input(
    "Accrued carried interest to date ($)", min_value=0.0, value=0.0, step=250_000.0, format="%.0f",
    help="Carry already crystallized/owed to the GP as of the as-of date - a liability that "
         "reduces the net value actually available to the LP, separate from carry the "
         "waterfall above computes on future distributions."
)
waterfall_style = st.sidebar.radio(
    "Waterfall style", ["Compounded threshold (default)", "Declining hurdle balance"], index=0,
    help="Both are legitimate European waterfalls, they just book the preferred return "
         "differently. Compounded threshold: distributions compare against a single "
         "target (all historical calls compounded to today at the hurdle rate) -- once "
         "cumulative LP distributions cross it, carry kicks in. Declining hurdle "
         "balance: a running balance (starting at unreturned paid-in capital) accrues "
         "the hurdle rate on itself and shrinks as distributions are applied to it; "
         "carry kicks in once that balance hits zero.",
)
gp_catchup_rate = 0.0
if waterfall_style == "Declining hurdle balance":
    gp_catchup_rate = st.sidebar.slider(
        "GP catch-up (%, 0 = none)", 0.0, 100.0, 0.0, step=5.0,
        help="Not the carry rate -- this only controls how fast the GP catches up to "
             "the full carry_rate split once the hurdle clears. 0% (default) means a "
             "straight carry_rate split from the first post-hurdle dollar.",
    ) / 100

st.sidebar.write("GP-reported Gross performance to date (optional -- shown next to the LP's actual Net figures):")
gpc1, gpc2 = st.sidebar.columns(2)
gp_reported_gross_moic = gpc1.number_input("Gross MOIC (x)", min_value=0.0, value=0.0, step=0.05, format="%.2f")
gp_reported_gross_irr = gpc2.number_input("Gross IRR (%)", min_value=0.0, value=0.0, step=0.5, format="%.1f") / 100
st.sidebar.caption(
    "Gross (before fees/carry) performance isn't derivable from the LP's own cash flows -- "
    "it's whatever the GP reports. Leave at 0 to hide the comparison."
)

st.sidebar.header("6. Leverage (optional)")
use_leverage = st.sidebar.checkbox("Buyer finances part of the purchase with a facility", value=False)
if use_leverage:
    leverage_pct = st.sidebar.slider("Leverage (% of purchase price)", 0.0, 90.0, 40.0, step=5.0) / 100
    leverage_rate = st.sidebar.slider("Facility interest rate (%)", 0.0, 15.0, 6.5, step=0.25) / 100
    st.sidebar.caption(
        "A subscription line / NAV facility funds this share of the purchase price at close. "
        "Available cash each year sweeps to interest then principal until the balance hits "
        "zero; the facility never funds unfunded capital calls, only the purchase itself."
    )
else:
    leverage_pct = 0.0
    leverage_rate = 0.0

st.sidebar.header("7. AI Agent")
api_key = st.sidebar.text_input("Anthropic API key (optional)", type="password")
st.sidebar.caption("No key? The assistant still answers using a rule-based summary of the numbers.")

# --------------------------------------------------------------------------
# Core calculations
# --------------------------------------------------------------------------
# Fund -> LP scaling happens once, right here, for every fund-level $ figure
# collected above. Everything downstream (to-date metrics, forecast, waterfall,
# unfunded schedule, pricing, leverage) then runs unchanged on these LP-level
# numbers -- ratios (DPI/RVPI/TVPI/IRR) are scale-invariant so they come out
# identical to the fund-level view; only $ amounts shrink to the LP's slice.
calls_lp = [c * lp_pct for c in calls]
dists_lp = [d * lp_pct for d in dists]
nav_current_lp = nav_current * lp_pct
accrued_carry_lp = accrued_carry * lp_pct

to_date = fund_metrics_to_date(cf_dates, calls_lp, dists_lp, nav_current_lp, as_of)

ebitda_details = []  # per-company EV/EBITDA build detail, for tab2 display
if forecast_mode == "Aggregate NAV (simple)":
    forecast_rows = forecast_cashflows(
        nav_current_lp, remaining_years, gross_return, shape, as_of,
        mgmt_fee_rate=mgmt_fee if apply_fees else 0.0,
    )
    portfolio_display = None
else:
    ebitda_lookup = {}
    if ebitda_df is not None and len(ebitda_df) > 0:
        ebitda_lookup = {r["Company"]: r for _, r in ebitda_df.iterrows()}

    companies = []
    portfolio_display_rows = []
    for _, row in portfolio_df.iterrows():
        rv = float(row["Reported Value ($M)"]) * 1_000_000
        adj = float(row["MV Adjustment (%)"]) / 100
        mv = reported_vs_market_value(rv, adj)
        mv_lp = mv * lp_pct
        exit_year_1 = int(row["Exit Year 1"])

        expected_return = float(row["Expected Return (%)"]) / 100
        used_ebitda_model = False
        e = ebitda_lookup.get(row["Company"])
        if e is not None:
            ev_result = ebitda_exit_value(
                entry_revenue=float(e["Entry Revenue ($M)"]) * 1_000_000,
                entry_ebitda_margin=float(e["Entry EBITDA Margin (%)"]) / 100,
                entry_ev_multiple=float(e["Entry EV/EBITDA (x)"]),
                entry_net_debt_ebitda=float(e["Entry Net Debt/EBITDA (x)"]),
                revenue_growth=float(e["Revenue Growth (%)"]) / 100,
                fcf_conversion=float(e["FCF Conversion (%)"]) / 100,
                exit_year=exit_year_1,
                exit_ev_multiple=float(e["Exit EV/EBITDA (x)"]),
                fund_ownership_pct=float(e["Fund Ownership (%)"]) / 100,
            )
            exit_proceeds_lp = ev_result["exit_proceeds_to_fund"] * lp_pct
            expected_return = implied_annual_return(mv_lp, exit_proceeds_lp, exit_year_1)
            used_ebitda_model = True
            ebitda_details.append({
                "Company": row["Company"], **ev_result, "implied_return": expected_return,
                "exit_proceeds_to_fund_lp": exit_proceeds_lp,
            })

        companies.append({
            "name": row["Company"],
            "current_value": mv_lp,
            "expected_return": expected_return,
            "exit_year_1": exit_year_1,
            "exit_pct_1": float(row["Exit % 1"]) / 100,
            "exit_year_2": int(row["Exit Year 2"]),
            "exit_pct_2": float(row["Exit % 2"]) / 100,
        })
        portfolio_display_rows.append({
            "Company": row["Company"], "Reported Value (Fund)": rv, "MV Adjustment": adj,
            "Market Value (Fund)": mv, "Market Value (LP)": mv_lp,
            "Valuation method": "EV/EBITDA model" if used_ebitda_model else "Expected Return %",
            "Return used": expected_return,
        })
    forecast_rows = forecast_from_portfolio(
        companies, mgmt_fee_rate=mgmt_fee if apply_fees else 0.0, as_of=as_of
    )
    portfolio_display = pd.DataFrame(portfolio_display_rows)

if apply_fees:
    if waterfall_style == "Declining hurdle balance":
        forecast_rows = apply_carry_waterfall_declining_balance(
            to_date.paid_in, to_date.distributions, forecast_rows, hurdle_rate, carry_rate, gp_catchup_rate
        )
    else:
        forecast_rows = apply_carry_waterfall(
            cf_dates, calls_lp, to_date.distributions, forecast_rows, hurdle_rate, carry_rate
        )
    distribution_key = "lp_distribution"
else:
    distribution_key = "gross_distribution"

known_followons = [
    {"name": r["Name"], "amount": float(r["Amount ($M)"]) * 1_000_000 * lp_pct, "year": int(r["Year"])}
    for _, r in known_followons_df.iterrows()
] if len(known_followons_df) > 0 else []
unfunded_calls = build_unfunded_schedule(known_followons, blind_pool_amount * lp_pct, blind_pool_years, forecast_rows)
total_unfunded = sum(unfunded_calls.values())

if unfunded_generates_return:
    unfunded_returns, unfunded_return_excluded = build_unfunded_returns(
        unfunded_calls, unfunded_hold_years, unfunded_moic, forecast_rows
    )
else:
    unfunded_returns, unfunded_return_excluded = {}, 0.0
total_unfunded_return = sum(unfunded_returns.values())

net_nav = max(0.0, nav_current_lp - accrued_carry_lp)

discount_levels = [-0.10, 0.0, 0.10, 0.20, 0.30, 0.40]
pricing = secondary_pricing(net_nav, forecast_rows, as_of, discount_levels, distribution_key,
                             unfunded_calls, unfunded_returns)

buyer_target_discount = 0.15  # used for the Buyer-vs-Seller headline comparison in tab 3
buyer_row = secondary_pricing(net_nav, forecast_rows, as_of, [buyer_target_discount], distribution_key,
                               unfunded_calls, unfunded_returns)[0]

leverage_result = None
if use_leverage and leverage_pct > 0:
    leverage_result = leverage_overlay(
        buyer_row, forecast_rows, as_of, distribution_key, unfunded_calls, leverage_pct, leverage_rate,
        unfunded_returns,
    )

# --------------------------------------------------------------------------
# Analytics: MV CAGR, cash-flow duration, RV/MV multiple, MOIC adjusted for CFs
# --------------------------------------------------------------------------
cf_duration_years = cash_flow_duration(forecast_rows, distribution_key, as_of)
terminal_value = sum(r[distribution_key] for r in forecast_rows) + (forecast_rows[-1]["ending_nav"] if forecast_rows else 0.0)
mv_cagr = implied_annual_return(net_nav, terminal_value, cf_duration_years) if cf_duration_years > 0 else 0.0

rv_mv_multiple = None
if portfolio_display is not None and len(portfolio_display) > 0:
    rv_total = portfolio_display["Reported Value (Fund)"].sum()
    mv_total = portfolio_display["Market Value (Fund)"].sum()
    rv_mv_multiple = rv_total / mv_total if mv_total else None

# "Unadjusted for CFs" = the raw hold-at-par TVPI already computed to date; "adjusted"
# = the seller (0%-discount) pricing scenario's MOIC, which nets the purchase price
# against actual projected LP distributions + unfunded returns -- i.e. time/cash-flow
# aware, vs. TVPI's simple point-in-time snapshot.
seller_row_for_moic = secondary_pricing(net_nav, forecast_rows, as_of, [0.0], distribution_key,
                                         unfunded_calls, unfunded_returns)[0]
moic_unadjusted = to_date.tvpi
moic_adjusted = seller_row_for_moic["moic"]

metrics_context = {
    "fund_lp": {
        "fund_commitment": fund_commitment,
        "lp_commitment": lp_commitment,
        "lp_pct": lp_pct,
        "fund_nav": nav_current,
        "lp_nav": nav_current_lp,
    },
    "to_date": {
        "paid_in": to_date.paid_in,
        "distributions": to_date.distributions,
        "nav_reported": to_date.nav,
        "accrued_carry_to_date": accrued_carry_lp,
        "net_nav_after_accrued_carry": net_nav,
        "dpi": to_date.dpi,
        "rvpi": to_date.rvpi,
        "tvpi": to_date.tvpi,
        "irr": to_date.irr,
    },
    "forecast": forecast_rows,
    "pricing_sensitivity": pricing,
    "best_pricing": pricing[2],  # 10% discount, used as a headline reference point
    "assumptions": {
        "forecast_mode": forecast_mode,
        "remaining_years": remaining_years,
        "gross_return": gross_return,
        "shape": shape,
        "fees_applied": apply_fees,
        "mgmt_fee": mgmt_fee,
        "hurdle_rate": hurdle_rate,
        "carry_rate": carry_rate,
        "accrued_carry_to_date": accrued_carry_lp,
        "known_followons": known_followons,
        "blind_pool_amount": blind_pool_amount * lp_pct,
        "blind_pool_years": blind_pool_years,
        "total_unfunded": total_unfunded,
        "unfunded_generates_return": unfunded_generates_return,
        "unfunded_hold_years": unfunded_hold_years,
        "unfunded_moic": unfunded_moic,
        "total_unfunded_return": total_unfunded_return,
        "unfunded_return_excluded_beyond_horizon": unfunded_return_excluded,
    },
    "leverage": {
        "used": use_leverage and leverage_pct > 0,
        "leverage_pct": leverage_pct,
        "interest_rate": leverage_rate,
        "buyer_discount_this_applies_to": buyer_target_discount,
        "unlevered_irr": buyer_row["irr"],
        "unlevered_moic": buyer_row["moic"],
        "levered_irr": leverage_result["levered_irr"] if leverage_result else None,
        "levered_moic": leverage_result["levered_moic"] if leverage_result else None,
        "initial_draw": leverage_result["initial_draw"] if leverage_result else None,
        "equity_invested": leverage_result["equity_invested"] if leverage_result else None,
    } if use_leverage else None,
    "analytics": {
        "waterfall_style": waterfall_style,
        "gp_catchup_rate": gp_catchup_rate,
        "gross_moic_reported": gp_reported_gross_moic or None,
        "gross_irr_reported": gp_reported_gross_irr or None,
        "net_moic_to_date": to_date.tvpi,
        "net_irr_to_date": to_date.irr,
        "mv_cagr": mv_cagr,
        "cash_flow_duration_years": cf_duration_years,
        "rv_mv_multiple": rv_mv_multiple,
        "moic_unadjusted_for_cfs": moic_unadjusted,
        "moic_adjusted_for_cfs": moic_adjusted,
    },
}

# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------
tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["Overview", "Cash Flow Forecast", "Secondary Pricing", "Analytics & Pricing Bridge", "AI Assistant"]
)

with tab1:
    st.caption(
        f"Fund commitment ${fund_commitment:,.0f} | Selling LP's commitment ${lp_commitment:,.0f} "
        f"({lp_pct*100:.2f}% of the fund) | Fund NAV ${nav_current:,.0f} -> LP NAV ${nav_current_lp:,.0f}. "
        f"Everything below is the LP's slice."
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Paid-in Capital (LP)", f"${to_date.paid_in:,.0f}")
    c2.metric("Distributions to date (LP)", f"${to_date.distributions:,.0f}")
    c3.metric("Reported NAV (LP)", f"${to_date.nav:,.0f}")
    c4.metric("TVPI", f"{to_date.tvpi:.2f}x")

    c5, c6, c7 = st.columns(3)
    irr_txt = f"{to_date.irr*100:.1f}%" if to_date.irr == to_date.irr else "n/a"
    c5.metric("IRR to date", irr_txt)
    c6.metric("DPI / RVPI", f"{to_date.dpi:.2f}x / {to_date.rvpi:.2f}x")
    c7.metric("Net NAV (LP, after accrued carry)", f"${net_nav:,.0f}")
    if accrued_carry_lp > 0:
        st.caption(
            f"${accrued_carry_lp:,.0f} of carry (LP share) has already crystallized/is owed to "
            f"the GP as of the as-of date, so only ${net_nav:,.0f} of the ${to_date.nav:,.0f} "
            f"reported NAV is economically available to the LP."
        )

    fig = go.Figure()
    fig.add_bar(x=[d.isoformat() for d in cf_dates], y=[-c for c in calls_lp], name="Capital Calls (LP)")
    fig.add_bar(x=[d.isoformat() for d in cf_dates], y=dists_lp, name="Distributions (LP)")
    fig.update_layout(title="Historical Cash Flows (LP share)", barmode="relative")
    st.plotly_chart(fig, width="stretch")

with tab2:
    if portfolio_display is not None:
        st.write("Reported Value vs. buyer-adjusted Market Value per company (Fund-level and the selling LP's share):")
        st.dataframe(
            portfolio_display.style.format({
                "Reported Value (Fund)": "${:,.0f}", "Market Value (Fund)": "${:,.0f}",
                "Market Value (LP)": "${:,.0f}", "MV Adjustment": "{:+.0%}", "Return used": "{:+.1%}",
            }),
            width="stretch",
        )
        if ebitda_details:
            with st.expander("Bottom-up EV/EBITDA build detail"):
                for d in ebitda_details:
                    st.markdown(f"**{d['Company']}**")
                    ec1, ec2, ec3, ec4 = st.columns(4)
                    ec1.metric("Entry Equity Value", f"${d['entry_equity_value']:,.0f}")
                    ec2.metric("Exit EBITDA", f"${d['exit_ebitda']:,.0f}")
                    ec3.metric("Exit Equity Value", f"${d['exit_equity_value']:,.0f}")
                    ec4.metric("Implied annual return", f"{d['implied_return']*100:.1f}%")
                    st.caption(
                        f"Exit proceeds to fund ${d['exit_proceeds_to_fund']:,.0f} at year "
                        f"{d['exit_year']} -> LP share ${d['exit_proceeds_to_fund_lp']:,.0f}. This "
                        f"implied return is what compounds the company's LP-level current Market "
                        f"Value to that LP-level exit proceeds by the exit year."
                    )
                    sched_df = pd.DataFrame(d["schedule"])
                    st.dataframe(
                        sched_df.style.format({
                            "revenue": "${:,.0f}", "ebitda": "${:,.0f}", "fcf": "${:,.0f}",
                            "beginning_net_debt": "${:,.0f}", "ending_net_debt": "${:,.0f}",
                        }),
                        width="stretch", hide_index=True,
                    )
                    st.divider()

    fdf = pd.DataFrame(forecast_rows)

    st.caption(
        "beginning_nav = last year's ending NAV. grown_nav = beginning_nav after this year's "
        "assumed gross return, before fees/distributions."
    )
    if forecast_mode != "Aggregate NAV (simple)":
        st.caption(
            "Portfolio mode: each company compounds at its own expected return off its Market "
            "Value, and pays out per its own exit schedule (which may be phased across two years) "
            "instead of following one shared runoff curve."
        )
        exit_rows = []
        for r in forecast_rows:
            for e in r.get("exits", []):
                exit_rows.append({"Year": r["year"], "Date": r["date"], "Company": e["name"], "Exit Value": e["value"]})
        if exit_rows:
            exit_df = pd.DataFrame(exit_rows)
            st.dataframe(exit_df.style.format({"Exit Value": "${:,.0f}"}), width="stretch")

    if total_unfunded > 0:
        st.caption(f"Buyer's unfunded call schedule (${total_unfunded:,.0f} total, LP share): {unfunded_calls}")
    if unfunded_generates_return and total_unfunded_return > 0:
        st.caption(
            f"Projected return on that unfunded commitment (${total_unfunded_return:,.0f} total, "
            f"LP share, at {unfunded_moic:.1f}x MOIC over a {unfunded_hold_years}-year hold): "
            f"{unfunded_returns}"
        )
        if unfunded_return_excluded > 0:
            st.caption(
                f"${unfunded_return_excluded:,.0f} of unfunded return falls beyond the forecast "
                f"horizon and is excluded rather than distorting the last year."
            )

    if apply_fees:
        st.caption(
            f"Net of a {mgmt_fee*100:.1f}% annual management fee and {carry_rate*100:.0f}% "
            f"carried interest above an {hurdle_rate*100:.1f}% preferred return."
        )
        fmt = {
            "beginning_nav": "${:,.0f}",
            "grown_nav": "${:,.0f}",
            "mgmt_fee": "${:,.0f}",
            "gross_distribution": "${:,.0f}",
            "lp_distribution": "${:,.0f}",
            "gp_carry": "${:,.0f}",
            "ending_nav": "${:,.0f}",
        }
        cols = ["year", "date", "beginning_nav", "grown_nav", "mgmt_fee", "gross_distribution", "gp_carry", "lp_distribution", "ending_nav"]
    else:
        fmt = {
            "beginning_nav": "${:,.0f}",
            "grown_nav": "${:,.0f}",
            "mgmt_fee": "${:,.0f}",
            "gross_distribution": "${:,.0f}",
            "ending_nav": "${:,.0f}",
        }
        cols = ["year", "date", "beginning_nav", "grown_nav", "mgmt_fee", "gross_distribution", "ending_nav"]

    st.dataframe(fdf[cols].style.format(fmt), width="stretch")

    fig2 = go.Figure()
    if apply_fees:
        fig2.add_bar(x=fdf["year"], y=fdf["lp_distribution"], name="LP Distribution (net)")
        fig2.add_bar(x=fdf["year"], y=fdf["gp_carry"], name="GP Carry")
        fig2.update_layout(barmode="stack")
    else:
        fig2.add_bar(x=fdf["year"], y=fdf["gross_distribution"], name="Projected Distribution")
    fig2.add_scatter(x=fdf["year"], y=fdf["ending_nav"], name="Ending NAV", yaxis="y2")
    if forecast_mode == "Aggregate NAV (simple)":
        chart_title = f"Projected Runoff ({shape.replace('_', ' ')}, {remaining_years}y, {gross_return*100:.0f}% gross return)"
    else:
        chart_title = f"Projected Runoff (portfolio companies, {remaining_years}y)"
    fig2.update_layout(
        title=chart_title,
        yaxis=dict(title="Distribution ($)"),
        yaxis2=dict(title="Ending NAV ($)", overlaying="y", side="right"),
    )
    st.plotly_chart(fig2, width="stretch")

with tab3:
    st.subheader("Buyer vs. Seller")
    seller_row = secondary_pricing(net_nav, forecast_rows, as_of, [0.0], distribution_key,
                                    unfunded_calls, unfunded_returns)[0]
    bc1, bc2 = st.columns(2)
    with bc1:
        st.markdown("**Seller (holds at par to net NAV, 0% discount)**")
        st.metric("Reference price", f"${seller_row['price']:,.0f}")
        st.metric("Implied IRR", f"{seller_row['irr']*100:.1f}%" if seller_row['irr'] == seller_row['irr'] else "n/a")
        st.metric("Implied MOIC", f"{seller_row['moic']:.2f}x")
    with bc2:
        st.markdown(f"**Buyer (at a {buyer_target_discount*100:.0f}% discount to net NAV)**")
        st.metric("Purchase price", f"${buyer_row['price']:,.0f}")
        st.metric("Projected IRR (unlevered)", f"{buyer_row['irr']*100:.1f}%" if buyer_row['irr'] == buyer_row['irr'] else "n/a")
        st.metric("Projected MOIC (unlevered)", f"{buyer_row['moic']:.2f}x")
        if leverage_result:
            lirr = leverage_result["levered_irr"]
            st.metric(
                f"Projected IRR (levered, {leverage_pct*100:.0f}% @ {leverage_rate*100:.1f}%)",
                f"{lirr*100:.1f}%" if lirr == lirr else "n/a",
                delta=f"{(lirr - buyer_row['irr'])*100:+.1f}pp vs. unlevered" if lirr == lirr else None,
            )
            st.metric(
                f"Projected MOIC (levered)",
                f"{leverage_result['levered_moic']:.2f}x",
                delta=f"{leverage_result['levered_moic'] - buyer_row['moic']:+.2f}x vs. unlevered",
            )
    st.caption(
        "Both sides project off the same underlying distributions; the Seller's figures are the "
        "'hold' case at the fund's net NAV, and the Buyer's figures show the extra return created "
        "purely by the negotiated discount. Levered figures (when a facility is enabled in the "
        "sidebar) are shown alongside the unlevered ones, never in place of them -- they show how "
        "much of the return is coming from financing rather than the underlying deal."
    )
    if leverage_result:
        with st.expander("Leverage facility schedule"):
            lev_df = pd.DataFrame(leverage_result["schedule"])
            st.dataframe(
                lev_df.style.format({
                    "beginning_balance": "${:,.0f}", "interest_accrued": "${:,.0f}",
                    "interest_paid": "${:,.0f}", "principal_repaid": "${:,.0f}", "ending_balance": "${:,.0f}",
                }),
                width="stretch",
            )
            st.caption(
                f"Initial draw ${leverage_result['initial_draw']:,.0f} at close; equity invested "
                f"${leverage_result['equity_invested']:,.0f} (buyer's share of the purchase price "
                f"plus all unfunded calls, which the facility does not finance)."
            )

    st.subheader("Full pricing sensitivity")
    if total_unfunded > 0:
        st.caption(
            f"Buyer also funds ${total_unfunded:,.0f} of unfunded commitment (known follow-ons + "
            f"blind pool, LP share) - included in 'Total invested' and netted against distributions below."
        )
    if unfunded_generates_return and total_unfunded_return > 0:
        st.caption(
            f"That unfunded commitment also returns ${total_unfunded_return:,.0f} (LP share) - "
            f"included in 'Total value' and 'moic', but NOT in 'Total invested' (the call amount "
            f"already covers that side)."
        )
    if accrued_carry_lp > 0:
        st.caption(f"Priced off net NAV of ${net_nav:,.0f} (reported NAV less ${accrued_carry_lp:,.0f} accrued carry, LP share).")
    pdf_ = pd.DataFrame(pricing)
    pdf_display = pdf_.copy()
    pdf_display["discount"] = pdf_display["discount"].map(lambda x: f"{x*100:.0f}%")
    pdf_display["price"] = pdf_display["price"].map(lambda x: f"${x:,.0f}")
    pdf_display["unfunded_calls"] = pdf_display["unfunded_calls"].map(lambda x: f"${x:,.0f}")
    pdf_display["unfunded_returns"] = pdf_display["unfunded_returns"].map(lambda x: f"${x:,.0f}")
    pdf_display["total_invested"] = pdf_display["total_invested"].map(lambda x: f"${x:,.0f}")
    pdf_display["irr"] = pdf_display["irr"].map(lambda x: f"{x*100:.1f}%" if x == x else "n/a")
    pdf_display["moic"] = pdf_display["moic"].map(lambda x: f"{x:.2f}x")
    display_cols = ["discount", "price", "unfunded_calls", "total_invested", "irr", "moic"]
    if unfunded_generates_return and total_unfunded_return > 0:
        display_cols.insert(3, "unfunded_returns")
    if not total_unfunded > 0:
        display_cols = [c for c in display_cols if c not in ("unfunded_calls", "total_invested")]
    st.dataframe(pdf_display[display_cols], width="stretch")

    fig3 = go.Figure()
    fig3.add_scatter(
        x=[p["discount"] * 100 for p in pricing],
        y=[p["irr"] * 100 for p in pricing],
        mode="lines+markers",
        name="Buyer IRR",
    )
    fig3.update_layout(
        title="Buyer IRR vs. Discount to Net NAV",
        xaxis_title="Discount to Net NAV (%)",
        yaxis_title="Projected IRR (%)",
    )
    st.plotly_chart(fig3, width="stretch")

with tab4:
    st.subheader("Historical performance: Gross vs. Net")
    if gp_reported_gross_moic or gp_reported_gross_irr:
        gpc1, gpc2 = st.columns(2)
        with gpc1:
            st.metric("Gross MOIC (GP-reported)", f"{gp_reported_gross_moic:.2f}x" if gp_reported_gross_moic else "n/a")
            st.metric("Net MOIC (LP actual, to date)", f"{to_date.tvpi:.2f}x")
        with gpc2:
            st.metric("Gross IRR (GP-reported)", f"{gp_reported_gross_irr*100:.1f}%" if gp_reported_gross_irr else "n/a")
            st.metric("Net IRR (LP actual, to date)", f"{to_date.irr*100:.1f}%" if to_date.irr == to_date.irr else "n/a")
        st.caption(
            "Gross figures are whatever the GP reports (entered in the sidebar) -- they aren't "
            "derivable from the LP's own cash flows alone, since historical fee/carry cash flows "
            "aren't tracked separately here. Net is the LP's own actual to-date performance, "
            "computed the same way as the Overview tab."
        )
    else:
        st.caption(
            "Enter the GP's reported Gross MOIC/IRR in the sidebar (section 5) to see it "
            "alongside the LP's actual Net figures here."
        )

    st.subheader("Return & timing analytics")
    ac1, ac2, ac3 = st.columns(3)
    ac1.metric("MV CAGR (projected growth)", f"{mv_cagr*100:.1f}%")
    ac2.metric("Cash-flow duration", f"{cf_duration_years:.2f} yrs")
    ac3.metric("RV / MV multiple", f"{rv_mv_multiple:.2f}x" if rv_mv_multiple else "n/a (aggregate mode)")
    st.caption(
        "MV CAGR: the flat annual rate that compounds the current net NAV to the projected "
        "total value (all future distributions + any residual NAV) over the cash-flow "
        "duration below -- a single-number growth-rate summary of the whole forecast. "
        "Cash-flow duration: the distribution-weighted average time until money comes back. "
        "RV/MV multiple: aggregate Reported Value over aggregate (diligence-adjusted) Market "
        "Value across portfolio companies -- only shown in Portfolio companies mode, since "
        "Aggregate NAV mode doesn't distinguish the two."
    )
    mc1, mc2 = st.columns(2)
    mc1.metric("MOIC, unadjusted for CFs", f"{moic_unadjusted:.2f}x")
    mc2.metric("MOIC, adjusted for CFs", f"{moic_adjusted:.2f}x")
    st.caption(
        "Unadjusted: TVPI, a point-in-time (distributions + NAV) / paid-in snapshot, blind to "
        "when cash actually arrives. Adjusted: the seller (0%-discount) pricing scenario's "
        "MOIC, which nets the net NAV purchase price against the actual projected LP "
        "distributions (+ unfunded returns, if enabled) -- timing-aware, and a closer match "
        "to what an XIRR-based buyer/seller conversation is really pricing."
    )

    st.subheader("Pricing bridge")
    bridge_scenario = st.selectbox("Scenario", ["At Par (0% discount)", f"Buyer ({buyer_target_discount*100:.0f}% discount)"])
    bridge_row = seller_row_for_moic if bridge_scenario.startswith("At Par") else buyer_row
    reported_value = net_nav
    market_value = net_nav  # net_nav is already the buyer's diligence-adjusted view where applicable (portfolio mode)
    gross_exposure = market_value + total_unfunded
    net_funded_exposure = market_value
    net_effective_price = bridge_row["price"]
    bridge_df = pd.DataFrame([
        {"Step": "Reported Value (net of accrued carry)", "$": reported_value},
        {"Step": "Market Value", "$": market_value},
        {"Step": "+ Unfunded Commitment -> Gross Exposure", "$": gross_exposure},
        {"Step": "Net Funded Exposure (Market Value, funded only)", "$": net_funded_exposure},
        {"Step": f"Net Effective Price ({bridge_scenario})", "$": net_effective_price},
    ])
    st.dataframe(bridge_df.style.format({"$": "${:,.0f}"}), width="stretch", hide_index=True)
    st.caption(
        "A step-by-step view of how the net NAV becomes a purchase price: the buyer's "
        "diligence-adjusted Market Value, grossed up for the unfunded commitment still to be "
        "called (Gross Exposure), separated back out to the funded-only exposure, and finally "
        "discounted (or not, for At Par) to the negotiated Net Effective Price."
    )

with tab5:
    st.write("Ask the AI agent about this fund's metrics, forecast, or pricing.")
    question = st.text_area("Question", placeholder="e.g. What discount to NAV is needed for a 20% IRR?")
    if st.button("Ask") and question:
        with st.spinner("Thinking..."):
            answer = ask_agent(question, metrics_context, api_key=api_key)
        st.markdown(answer)

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
    build_unfunded_schedule,
    forecast_cashflows,
    forecast_from_portfolio,
    fund_metrics_to_date,
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
to_date = fund_metrics_to_date(cf_dates, calls, dists, nav_current, as_of)

if forecast_mode == "Aggregate NAV (simple)":
    forecast_rows = forecast_cashflows(
        nav_current, remaining_years, gross_return, shape, as_of,
        mgmt_fee_rate=mgmt_fee if apply_fees else 0.0,
    )
    portfolio_display = None
else:
    companies = []
    portfolio_display_rows = []
    for _, row in portfolio_df.iterrows():
        rv = float(row["Reported Value ($M)"]) * 1_000_000
        adj = float(row["MV Adjustment (%)"]) / 100
        mv = reported_vs_market_value(rv, adj)
        companies.append({
            "name": row["Company"],
            "current_value": mv,
            "expected_return": float(row["Expected Return (%)"]) / 100,
            "exit_year_1": int(row["Exit Year 1"]),
            "exit_pct_1": float(row["Exit % 1"]) / 100,
            "exit_year_2": int(row["Exit Year 2"]),
            "exit_pct_2": float(row["Exit % 2"]) / 100,
        })
        portfolio_display_rows.append({
            "Company": row["Company"], "Reported Value": rv, "MV Adjustment": adj, "Market Value": mv,
        })
    forecast_rows = forecast_from_portfolio(
        companies, mgmt_fee_rate=mgmt_fee if apply_fees else 0.0, as_of=as_of
    )
    portfolio_display = pd.DataFrame(portfolio_display_rows)

if apply_fees:
    forecast_rows = apply_carry_waterfall(
        cf_dates, calls, to_date.distributions, forecast_rows, hurdle_rate, carry_rate
    )
    distribution_key = "lp_distribution"
else:
    distribution_key = "gross_distribution"

known_followons = [
    {"name": r["Name"], "amount": float(r["Amount ($M)"]) * 1_000_000, "year": int(r["Year"])}
    for _, r in known_followons_df.iterrows()
] if len(known_followons_df) > 0 else []
unfunded_calls = build_unfunded_schedule(known_followons, blind_pool_amount, blind_pool_years, forecast_rows)
total_unfunded = sum(unfunded_calls.values())

net_nav = max(0.0, nav_current - accrued_carry)

discount_levels = [-0.10, 0.0, 0.10, 0.20, 0.30, 0.40]
pricing = secondary_pricing(net_nav, forecast_rows, as_of, discount_levels, distribution_key, unfunded_calls)

buyer_target_discount = 0.15  # used for the Buyer-vs-Seller headline comparison in tab 3
buyer_row = secondary_pricing(net_nav, forecast_rows, as_of, [buyer_target_discount], distribution_key, unfunded_calls)[0]

leverage_result = None
if use_leverage and leverage_pct > 0:
    leverage_result = leverage_overlay(
        buyer_row, forecast_rows, as_of, distribution_key, unfunded_calls, leverage_pct, leverage_rate
    )

metrics_context = {
    "to_date": {
        "paid_in": to_date.paid_in,
        "distributions": to_date.distributions,
        "nav_reported": to_date.nav,
        "accrued_carry_to_date": accrued_carry,
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
        "accrued_carry_to_date": accrued_carry,
        "known_followons": known_followons,
        "blind_pool_amount": blind_pool_amount,
        "blind_pool_years": blind_pool_years,
        "total_unfunded": total_unfunded,
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
}

# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------
tab1, tab2, tab3, tab4 = st.tabs(
    ["Overview", "Cash Flow Forecast", "Secondary Pricing", "AI Assistant"]
)

with tab1:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Paid-in Capital", f"${to_date.paid_in:,.0f}")
    c2.metric("Distributions to date", f"${to_date.distributions:,.0f}")
    c3.metric("Reported NAV", f"${to_date.nav:,.0f}")
    c4.metric("TVPI", f"{to_date.tvpi:.2f}x")

    c5, c6, c7 = st.columns(3)
    irr_txt = f"{to_date.irr*100:.1f}%" if to_date.irr == to_date.irr else "n/a"
    c5.metric("IRR to date", irr_txt)
    c6.metric("DPI / RVPI", f"{to_date.dpi:.2f}x / {to_date.rvpi:.2f}x")
    c7.metric("Net NAV (after accrued carry)", f"${net_nav:,.0f}")
    if accrued_carry > 0:
        st.caption(
            f"${accrued_carry:,.0f} of carry has already crystallized/is owed to the GP as of "
            f"the as-of date, so only ${net_nav:,.0f} of the ${to_date.nav:,.0f} reported NAV "
            f"is economically available to the LP."
        )

    fig = go.Figure()
    fig.add_bar(x=[d.isoformat() for d in cf_dates], y=[-c for c in calls], name="Capital Calls")
    fig.add_bar(x=[d.isoformat() for d in cf_dates], y=dists, name="Distributions")
    fig.update_layout(title="Historical Cash Flows", barmode="relative")
    st.plotly_chart(fig, width="stretch")

with tab2:
    if portfolio_display is not None:
        st.write("Reported Value vs. buyer-adjusted Market Value per company:")
        st.dataframe(
            portfolio_display.style.format({
                "Reported Value": "${:,.0f}", "Market Value": "${:,.0f}", "MV Adjustment": "{:+.0%}",
            }),
            width="stretch",
        )

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
        st.caption(f"Buyer's unfunded call schedule (${total_unfunded:,.0f} total): {unfunded_calls}")

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
    seller_row = secondary_pricing(net_nav, forecast_rows, as_of, [0.0], distribution_key, unfunded_calls)[0]
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
            f"blind pool) - included in 'Total invested' and netted against distributions below."
        )
    if accrued_carry > 0:
        st.caption(f"Priced off net NAV of ${net_nav:,.0f} (reported NAV less ${accrued_carry:,.0f} accrued carry).")
    pdf_ = pd.DataFrame(pricing)
    pdf_display = pdf_.copy()
    pdf_display["discount"] = pdf_display["discount"].map(lambda x: f"{x*100:.0f}%")
    pdf_display["price"] = pdf_display["price"].map(lambda x: f"${x:,.0f}")
    pdf_display["unfunded_calls"] = pdf_display["unfunded_calls"].map(lambda x: f"${x:,.0f}")
    pdf_display["total_invested"] = pdf_display["total_invested"].map(lambda x: f"${x:,.0f}")
    pdf_display["irr"] = pdf_display["irr"].map(lambda x: f"{x*100:.1f}%" if x == x else "n/a")
    pdf_display["moic"] = pdf_display["moic"].map(lambda x: f"{x:.2f}x")
    display_cols = ["discount", "price", "unfunded_calls", "total_invested", "irr", "moic"] if total_unfunded > 0 else ["discount", "price", "irr", "moic"]
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
    st.write("Ask the AI agent about this fund's metrics, forecast, or pricing.")
    question = st.text_area("Question", placeholder="e.g. What discount to NAV is needed for a 20% IRR?")
    if st.button("Ask") and question:
        with st.spinner("Thinking..."):
            answer = ask_agent(question, metrics_context, api_key=api_key)
        st.markdown(answer)

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
    forecast_cashflows,
    forecast_from_portfolio,
    fund_metrics_to_date,
    secondary_pricing,
    unfunded_commitment_calls,
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
    help="Portfolio mode lets each holding have its own growth rate and exit year instead of "
         "one shared NAV growing on a single runoff curve.",
)
as_of = st.sidebar.date_input("As-of date", value=date.today())

if forecast_mode == "Aggregate NAV (simple)":
    nav_current = st.sidebar.number_input(
        "Current NAV ($)", min_value=0.0, value=62_000_000.0, step=1_000_000.0, format="%.0f"
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
    st.sidebar.write("Portfolio companies (value, expected annual return, exit year):")
    default_portfolio = pd.DataFrame([
        {"Company": "Company A", "Value ($M)": 15.0, "Return (%)": 25.0, "Exit Year": 3},
        {"Company": "Company B", "Value ($M)": 12.0, "Return (%)": 18.0, "Exit Year": 5},
        {"Company": "Company C", "Value ($M)": 10.0, "Return (%)": 10.0, "Exit Year": 2},
        {"Company": "Company D", "Value ($M)": 15.0, "Return (%)": 20.0, "Exit Year": 4},
        {"Company": "Company E", "Value ($M)": 10.0, "Return (%)": 12.0, "Exit Year": 5},
    ])
    portfolio_df = st.sidebar.data_editor(
        default_portfolio, num_rows="dynamic", width="stretch", key="portfolio_editor"
    )
    if len(portfolio_df) > 0:
        nav_current = float(portfolio_df["Value ($M)"].sum()) * 1_000_000
        remaining_years = int(portfolio_df["Exit Year"].max())
    else:
        nav_current = 0.0
        remaining_years = 1
    st.sidebar.caption(f"Aggregate current NAV from portfolio: ${nav_current:,.0f}")

st.sidebar.header("4. Unfunded commitment")
unfunded_commitment = st.sidebar.number_input(
    "Unfunded commitment the buyer assumes ($)", min_value=0.0, value=0.0, step=500_000.0, format="%.0f"
)
if unfunded_commitment > 0:
    call_years = st.sidebar.slider(
        "Capital call period (years)", 1, remaining_years, min(2, remaining_years)
    )
    st.sidebar.caption(
        "Spread evenly over this many years starting immediately - capital calls in PE funds "
        "typically taper off well before the fund's final years."
    )
else:
    call_years = 0

st.sidebar.header("5. Fees & carried interest")
apply_fees = st.sidebar.checkbox("Apply management fee & carry", value=True)
mgmt_fee = st.sidebar.slider("Annual management fee (%)", 0.0, 5.0, 2.0, step=0.25) / 100
hurdle_rate = st.sidebar.slider("Preferred return / hurdle (%)", 0.0, 15.0, 8.0, step=0.5) / 100
carry_rate = st.sidebar.slider("Carried interest (%)", 0.0, 30.0, 20.0, step=1.0) / 100

st.sidebar.header("6. AI Agent")
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
else:
    companies = [
        {
            "name": row["Company"],
            "current_value": float(row["Value ($M)"]) * 1_000_000,
            "expected_return": float(row["Return (%)"]) / 100,
            "exit_year": int(row["Exit Year"]),
        }
        for _, row in portfolio_df.iterrows()
    ]
    forecast_rows = forecast_from_portfolio(
        companies, mgmt_fee_rate=mgmt_fee if apply_fees else 0.0, as_of=as_of
    )

if apply_fees:
    forecast_rows = apply_carry_waterfall(
        cf_dates, calls, to_date.distributions, forecast_rows, hurdle_rate, carry_rate
    )
    distribution_key = "lp_distribution"
else:
    distribution_key = "gross_distribution"

unfunded_calls = unfunded_commitment_calls(
    unfunded_commitment, forecast_rows, call_years if unfunded_commitment > 0 else None
)

discount_levels = [-0.10, 0.0, 0.10, 0.20, 0.30, 0.40]
pricing = secondary_pricing(nav_current, forecast_rows, as_of, discount_levels, distribution_key, unfunded_calls)

metrics_context = {
    "to_date": {
        "paid_in": to_date.paid_in,
        "distributions": to_date.distributions,
        "nav": to_date.nav,
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
        "unfunded_commitment": unfunded_commitment,
        "call_years": call_years,
    },
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
    c3.metric("Current NAV", f"${to_date.nav:,.0f}")
    c4.metric("TVPI", f"{to_date.tvpi:.2f}x")

    c5, c6 = st.columns(2)
    irr_txt = f"{to_date.irr*100:.1f}%" if to_date.irr == to_date.irr else "n/a"
    c5.metric("IRR to date", irr_txt)
    c6.metric("DPI / RVPI", f"{to_date.dpi:.2f}x / {to_date.rvpi:.2f}x")

    fig = go.Figure()
    fig.add_bar(x=[d.isoformat() for d in cf_dates], y=[-c for c in calls], name="Capital Calls")
    fig.add_bar(x=[d.isoformat() for d in cf_dates], y=dists, name="Distributions")
    fig.update_layout(title="Historical Cash Flows", barmode="relative")
    st.plotly_chart(fig, width="stretch")

with tab2:
    fdf = pd.DataFrame(forecast_rows)

    st.caption(
        "beginning_nav = last year's ending NAV. grown_nav = beginning_nav after this year's "
        "assumed gross return, before fees/distributions."
    )
    if forecast_mode != "Aggregate NAV (simple)":
        st.caption(
            "Portfolio mode: each company compounds at its own expected return and pays out in full "
            "in its own exit year, instead of following one shared runoff curve."
        )
        exit_rows = []
        for r in forecast_rows:
            for e in r.get("exits", []):
                exit_rows.append({"Year": r["year"], "Date": r["date"], "Company": e["name"], "Exit Value": e["value"]})
        if exit_rows:
            exit_df = pd.DataFrame(exit_rows)
            st.dataframe(
                exit_df.style.format({"Exit Value": "${:,.0f}"}), width="stretch"
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
    if unfunded_commitment > 0:
        st.caption(
            f"Buyer also funds ${unfunded_commitment:,.0f} of unfunded commitment over "
            f"{call_years} year(s) - included in 'Total invested' and netted against distributions "
            f"in the IRR/MOIC below."
        )
    pdf_ = pd.DataFrame(pricing)
    pdf_display = pdf_.copy()
    pdf_display["discount"] = pdf_display["discount"].map(lambda x: f"{x*100:.0f}%")
    pdf_display["price"] = pdf_display["price"].map(lambda x: f"${x:,.0f}")
    pdf_display["unfunded_calls"] = pdf_display["unfunded_calls"].map(lambda x: f"${x:,.0f}")
    pdf_display["total_invested"] = pdf_display["total_invested"].map(lambda x: f"${x:,.0f}")
    pdf_display["irr"] = pdf_display["irr"].map(lambda x: f"{x*100:.1f}%" if x == x else "n/a")
    pdf_display["moic"] = pdf_display["moic"].map(lambda x: f"{x:.2f}x")
    display_cols = ["discount", "price", "unfunded_calls", "total_invested", "irr", "moic"] if unfunded_commitment > 0 else ["discount", "price", "irr", "moic"]
    st.dataframe(pdf_display[display_cols], width="stretch")

    fig3 = go.Figure()
    fig3.add_scatter(
        x=[p["discount"] * 100 for p in pricing],
        y=[p["irr"] * 100 for p in pricing],
        mode="lines+markers",
        name="Buyer IRR",
    )
    fig3.update_layout(
        title="Buyer IRR vs. Discount to NAV",
        xaxis_title="Discount to NAV (%)",
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

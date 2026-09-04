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
    asset_model_build,
    asset_model_returns,
    build_unfunded_returns,
    build_unfunded_schedule,
    carry_rollforward,
    cash_flow_duration,
    crossover_fee_schedule,
    forecast_cashflows,
    forecast_from_portfolio,
    fund_metrics_to_date,
    implied_annual_return,
    leverage_overlay,
    remaining_cost_basis_by_year,
    reported_vs_market_value,
    secondary_pricing,
)

st.set_page_config(page_title="Secondary Modelling Agent", layout="wide")

st.title("Secondary Market Modelling Agent")
st.caption("AI-assisted valuation and cash-flow forecasting for LP secondary transactions")

# --------------------------------------------------------------------------
# Widget defaults
# --------------------------------------------------------------------------
# Seeded here, before the widgets are created, so each one opens on its intended
# value rather than falling back to its minimum.
st.session_state.setdefault("carry_pct", 20.0)
st.session_state.setdefault("hurdle_pct", 8.0)
st.session_state.setdefault("premium_discount_pct", -10.0)

# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------
# Created first because every tab writes into these containers out of order: Deal
# Terms holds the inputs and so must render before anything reads them; the
# Investments grids must render before the Asset Model widgets they share state
# with; and the schedules that depend on the fund forecast are appended last.
tab_terms, tab_inv, tab_asset, tab_ai = st.tabs(
    ["Deal Terms", "Investments", "Asset Model (Bottom-up)", "AI Assistant"]
)

# --------------------------------------------------------------------------
# Deal Terms -- the term sheet, laid out as the source workbook's two blocks
# --------------------------------------------------------------------------
with tab_terms:
    st.subheader("Deal Terms")
    dt1, dt2, dt3, dt4 = st.columns(4)
    currency = dt1.selectbox("Currency", ["USD", "EUR", "GBP", "CHF", "SEK", "JPY"], index=0)
    as_of = dt2.date_input(
        "Report Date", value=date(2025, 12, 31),
        help="The date the fund's reported position is struck. The model prices as of this "
             "date, and forecast year 1 is the calendar year after it -- so with the default "
             "the projection columns run 2026 onward, the same grid the asset models use.",
    )
    mv_date = dt3.date_input(
        "Market Value Date", value=date(2026, 4, 30),
        help="The date the buyer's own mark is struck. Recorded on the term sheet; the model "
             "still prices off the Report Date position.",
    )
    closing_date = dt4.date_input(
        "Closing Date", value=date(2026, 6, 30),
        help="When the transfer completes and the price is paid. Recorded here; say the word "
             "and I'll make the pricing IRR run off this date instead of the Report Date.",
    )

    st.divider()
    st.subheader("Fund Terms")

    ft1, ft2, ft3 = st.columns(3)
    vintage_year = int(ft1.number_input("Vintage Year", min_value=1980, max_value=2100,
                                        value=2022, step=1))
    carry_rate = ft2.number_input("Carried Interest (%)", min_value=0.0, max_value=50.0,
                                  step=1.0, format="%.1f", key="carry_pct") / 100
    hurdle_rate = ft3.number_input("Preferred Return (%)", min_value=0.0, max_value=25.0,
                                   step=0.5, format="%.1f", key="hurdle_pct") / 100

    st.markdown("**Commitments** ($mm, and the LP's share of the fund)")
    c1, c2, c3 = st.columns(3)
    fund_commitment_m = c1.number_input("Fund", min_value=0.01, value=1014.7, step=10.0,
                                        format="%.1f")
    lp_commitment_m = c2.number_input("LP", min_value=0.0, value=33.5, step=0.5, format="%.1f")
    fund_commitment = fund_commitment_m * 1_000_000
    lp_commitment = lp_commitment_m * 1_000_000
    lp_pct = (lp_commitment / fund_commitment) if fund_commitment > 0 else 0.0
    c3.metric("LP %", f"{lp_pct*100:.2f}%")
    st.caption(
        "A secondary prices ONE LP's slice, not the whole fund. Everything entered elsewhere "
        "is fund-level; the app scales it to this share before anything downstream runs."
    )

    st.markdown("**Fund cash flow history**")
    h1, h2 = st.columns([1, 2])
    use_sample = h1.checkbox("Use sample data", value=True)
    uploaded = None
    if not use_sample:
        uploaded = h2.file_uploader("Upload CSV (Date, Capital_Call, Distribution)", type=["csv"])
    if use_sample or uploaded is None:
        df = pd.read_csv("sample_fund_cashflows.csv", parse_dates=["Date"])
    else:
        df = pd.read_csv(uploaded, parse_dates=["Date"])
    cf_dates = df["Date"].dt.date.tolist()
    calls = df["Capital_Call"].fillna(0).tolist()
    dists = df["Distribution"].fillna(0).tolist()
    original_paid_in = sum(calls)
    original_distributions = sum(dists)

    override_totals = st.checkbox("Override paid-in capital / distributions to date", value=False)
    if override_totals:
        o1, o2 = st.columns(2)
        paid_in_input = o1.number_input("Paid-in capital, fund ($mm)", min_value=0.0,
                                        value=float(original_paid_in) / 1_000_000, step=1.0,
                                        format="%.1f") * 1_000_000
        dist_input = o2.number_input("Distributions to date, fund ($mm)", min_value=0.0,
                                     value=float(original_distributions) / 1_000_000, step=1.0,
                                     format="%.1f") * 1_000_000
        if original_paid_in > 0:
            calls = [c * (paid_in_input / original_paid_in) for c in calls]
        if original_distributions > 0:
            dists = [d * (dist_input / original_distributions) for d in dists]
        st.caption(
            "Cash flow dates are kept from the data; each amount is scaled proportionally so "
            "the totals match the override."
        )

    st.markdown("**Position** ($mm, at the LP's share unless noted)")
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Funded", f"{sum(calls) * lp_pct / 1_000_000:,.1f}")
    expired_m = p2.number_input(
        "Expired", min_value=0.0, value=1.7, step=0.1, format="%.1f",
        help="Commitment released without ever being called -- it can no longer be drawn, so "
             "it is neither funded nor available unfunded.",
    )
    lp_unfunded_m = p3.number_input(
        "Unfunded", min_value=0.0, value=15.346, step=0.1, format="%.3f",
        help="Commitment still callable. Known follow-ons (Investments tab) are drawn from "
             "this; whatever is left is the blind pool.",
    )
    p4.metric("Net Distribution to date", f"{sum(dists) * lp_pct / 1_000_000:,.1f}")

    q1, q2, q3, q4 = st.columns(4)
    net_cash_m = q1.number_input(
        "Net Cash", min_value=-1000.0, value=0.7, step=0.1, format="%.1f",
        help="Fund-level $mm. Surplus cash and other assets outside the portfolio marks -- the "
             "gap between the fund's reported value and the sum of its company marks.",
    )
    accrued_carry = q2.number_input(
        "Accrued Carried Interest", min_value=0.0, value=0.0, step=0.1, format="%.1f",
        help="Fund-level $mm. Carry already owed to the GP as of the Report Date -- a liability "
             "against the value available to the LP.",
    ) * 1_000_000
    gp_commitment_m = q3.number_input("GP Commitment", min_value=0.0, value=47.0, step=1.0,
                                      format="%.1f", help="Fund-level $mm.")
    gp_reported_m = q4.number_input("GP Reported Value", min_value=0.0, value=60.8, step=1.0,
                                    format="%.1f", help="Fund-level $mm.")

    st.markdown("**Management Fees**")
    fee_structure = st.radio(
        "Basis",
        ["Two-tier: commitment, then remaining cost", "Flat annual fee (% of NAV)"],
        index=0, horizontal=True,
        help="Two-tier mirrors a common real schedule: a flat rate on the fund's TOTAL "
             "COMMITMENT during the investment period, then from the crossover year a "
             "(usually lower) rate on each company's remaining invested COST, which shrinks "
             "as companies exit. Flat charges one rate on the year's NAV throughout.",
    )
    use_two_tier_fee = fee_structure.startswith("Two-tier")
    apply_fees = st.checkbox("Apply management fee & carried interest", value=True)
    if use_two_tier_fee:
        m1, m2 = st.columns(2)
        fee_rate_initial = m1.number_input(
            "Initial — on Commitments (%)", min_value=0.0, max_value=5.0, value=1.9, step=0.1,
            format="%.1f") / 100
        fee_rate_post = m2.number_input(
            "Post-crossover — on Remaining Cost (%)", min_value=0.0, max_value=5.0, value=1.9,
            step=0.1, format="%.1f") / 100
        mgmt_fee = 0.0
    else:
        fee_rate_initial = fee_rate_post = 0.0
        mgmt_fee = st.number_input("Annual management fee (% of NAV)", min_value=0.0,
                                   max_value=5.0, value=2.0, step=0.25, format="%.2f") / 100

    st.markdown("**Waterfall**")
    waterfall_style = st.radio(
        "Style", ["Compounded threshold (default)", "Declining hurdle balance"],
        index=0, horizontal=True,
        help="Both are legitimate European waterfalls; they book the preferred return "
             "differently. Compounded threshold: every call compounded forward to one target, "
             "crossed by cumulative LP distributions. Declining hurdle balance: a running "
             "balance that accrues the preferred return and shrinks as distributions are "
             "applied. The Investments tab shows the workings either way.",
    )
    gp_catchup_rate = 0.0
    if waterfall_style == "Declining hurdle balance":
        gp_catchup_rate = st.slider(
            "GP catch-up (%, 0 = none)", 0.0, 100.0, 0.0, step=5.0,
            help="Not the carry rate -- only how fast the GP catches up to the full split once "
                 "the hurdle clears.",
        ) / 100

    st.markdown("**Historical Fund Performance** (as reported by the GP)")
    hp1, hp2, hp3 = st.columns(3)
    hist_as_of = hp1.date_input("As of", value=date(2026, 3, 31))
    gp_reported_gross_moic = hp2.number_input("MOIC — Gross (x)", min_value=0.0, value=1.53,
                                              step=0.01, format="%.2f")
    gp_reported_gross_irr = hp3.number_input("IRR — Gross (%)", min_value=0.0, value=18.0,
                                             step=0.5, format="%.1f") / 100
    st.caption(
        "Gross performance is whatever the GP reports -- it isn't derivable from the LP's own "
        "cash flows, since historical fee and carry flows aren't tracked separately. The Net "
        "figures beside them below are the LP's actual to-date performance, computed here."
    )

# --------------------------------------------------------------------------
# Asset Model (Bottom-up) -- one full company model per portfolio holding
# --------------------------------------------------------------------------
# Per-asset defaults: the five current investments of the source deal, at fund level.
# Keyed by a STABLE id, not by name -- the name is itself an editable field, and keying
# widgets off a value the user can change would reset that company's whole model the
# moment it's renamed.
ASSET_DEFAULTS = {
    "A": {"name": "Asset A", "inv_date": date(2022, 3, 1), "cost": 77.0, "rv": 188.0,
          "mv_adj": 0.0, "exit_cal": 2028, "entry_revenue": 98.1, "prior_proceeds": 238.1,
          "expected_return": 25.0},
    "B": {"name": "Asset B", "inv_date": date(2023, 1, 1), "cost": 115.0, "rv": 132.0,
          "mv_adj": 0.0, "exit_cal": 2030, "entry_revenue": 68.8, "prior_proceeds": 214.2,
          "expected_return": 12.0},
    "C": {"name": "Asset C", "inv_date": date(2022, 10, 1), "cost": 62.0, "rv": 122.0,
          "mv_adj": 6.5574, "exit_cal": 2026, "entry_revenue": 83.8, "prior_proceeds": 153.9,
          "expected_return": 20.0},
    "D": {"name": "Asset D", "inv_date": date(2024, 2, 1), "cost": 84.9, "rv": 110.2,
          "mv_adj": 0.0, "exit_cal": 2029, "entry_revenue": 102.4, "prior_proceeds": 282.5,
          "expected_return": 25.0},
    "E": {"name": "Asset E", "inv_date": date(2024, 6, 1), "cost": 87.5, "rv": 105.1,
          "mv_adj": 0.0, "exit_cal": 2031, "entry_revenue": 53.5, "prior_proceeds": 187.4,
          "expected_return": 12.0},
}
COMMON_DEFAULTS = {
    "entry_ebitda_margin": 25.0, "entry_ev_multiple": 11.0, "entry_net_debt_ebitda": 3.0,
    "fund_ownership": 70.0, "exit_ev_multiple": 12.0, "revenue_growth": 8.0,
    "ebitda_margin": 25.0, "fcf_conversion": 50.0,
}

asset_builds = {}      # company name -> {"build": ..., "returns": ..., "drives_forecast": bool}
portfolio_rows = []    # the portfolio table, now assembled from each company's Deal Snapshot

if True:
    st.session_state.setdefault("asset_ids", list(ASSET_DEFAULTS.keys()))
    st.session_state.setdefault("asset_seq", 0)


def _asset_default(aid, field):
    """Default for one field of one asset: the deal's own value if it's one of the five
    originals, otherwise a generic starting point for a company the user just added."""
    # Assumptions that start the same for every company, whether it came with the deal
    # or was added just now.
    shared = {
        "entry_margin": COMMON_DEFAULTS["entry_ebitda_margin"],
        "entry_ev": COMMON_DEFAULTS["entry_ev_multiple"],
        "entry_nd": COMMON_DEFAULTS["entry_net_debt_ebitda"],
        "ownership": COMMON_DEFAULTS["fund_ownership"],
        "exit_ev": COMMON_DEFAULTS["exit_ev_multiple"],
        "exit_pct_1": 100.0, "exit_pct_2": 0.0, "drives": True,
    }
    if field in shared:
        return shared[field]
    if field == "exit_cal_2":
        # The optional second exit defaults to the first, i.e. no phasing.
        return int(_asset_state(aid, "exit_cal"))
    if aid in ASSET_DEFAULTS and field in ASSET_DEFAULTS[aid]:
        return ASSET_DEFAULTS[aid][field]
    generic = {
        "name": f"New Asset {aid}", "inv_date": as_of, "cost": 0.0, "rv": 10.0,
        "mv_adj": 0.0, "exit_cal": as_of.year + 3, "prior_proceeds": 0.0,
        "expected_return": 15.0,
    }
    if field == "entry_revenue":
        # Size entry revenue so entry equity x ownership lands near this company's
        # Reported Value -- otherwise a new company opens on an absurd implied return.
        rv = st.session_state.get(f"am_{aid}_rv", generic["rv"])
        return round(float(rv) / 1.4, 1) if rv else 10.0
    return generic[field]


def _asset_state(aid, field):
    """Current value of a Deal Snapshot field, read before the widgets are drawn.
    Streamlit populates session_state ahead of the script run, so this is the live
    value -- which lets fund-level totals (NAV, horizon) be computed up front."""
    return st.session_state.get(f"am_{aid}_{field}", _asset_default(aid, field))


# Every per-asset field lives in session_state under "am_<id>_<field>". Seeding them all
# up front means the widgets further down can be keyed-only (no value= argument), the
# editable grids can write to them, and -- crucially -- the asset models can be BUILT
# before any widget renders, which is what lets one grid show inputs and computed columns
# side by side.
PER_ASSET_FIELDS = ("name", "inv_date", "cost", "rv", "mv_adj", "exit_cal", "expected_return",
                    "prior_proceeds", "entry_revenue", "entry_margin", "entry_ev", "entry_nd",
                    "ownership", "exit_ev", "exit_pct_1", "exit_cal_2", "exit_pct_2", "drives")

if True:
    for _aid in st.session_state["asset_ids"]:
        for _f in PER_ASSET_FIELDS:
            st.session_state.setdefault(f"am_{_aid}_{_f}", _asset_default(_aid, _f))


# Unfunded assumptions are read up here (to price post-report investments) but their widgets
# live in the sidebar further down, so they are seeded the same way. These are seeded in
# BOTH forecast modes -- a keyed checkbox that first renders unseeded would latch on to
# False and stay there when the mode is switched.
st.session_state.setdefault("unfunded_generates_return", True)
st.session_state.setdefault("unfunded_hold_years", 4)
st.session_state.setdefault("unfunded_irr", 15.0)
# MOIC is derived from the assumed IRR and hold, the way the source model derives it --
# and derived HERE, before anything reads it, so the post-report investments priced in
# the Investments grids and the blind pool priced later cannot use different multiples.
UNFUNDED_HOLD = int(st.session_state["unfunded_hold_years"])
UNFUNDED_IRR = float(st.session_state["unfunded_irr"]) / 100
UNFUNDED_MOIC = (1 + UNFUNDED_IRR) ** UNFUNDED_HOLD
st.session_state.setdefault("followon_rows", [
    # The source model's post-report-date investment: a new company committed after the
    # reporting date, funded in the first forecast year.
    {"Name": "Asset F (post-report)", "Cost": 90.0, "Year": 1},
])

def _ops_vectors(aid, years):
    """Per-year revenue growth / EBITDA margin / FCF conversion for one asset, as fractions.

    Held in a plain (non-widget) session_state store so the model can be built before the
    per-year grid renders; that grid writes back into this store when edited. The store is
    stretched or trimmed whenever the forecast horizon changes, carrying the last year's
    assumption forward rather than snapping back to the default.
    """
    key = f"am_{aid}_ops_store"
    store = st.session_state.get(key)
    if not isinstance(store, dict):
        store = {}
    out = {}
    for field, default in (("growth", COMMON_DEFAULTS["revenue_growth"]),
                           ("margin", COMMON_DEFAULTS["ebitda_margin"]),
                           ("conv", COMMON_DEFAULTS["fcf_conversion"])):
        vals = list(store.get(field, []))
        while len(vals) < len(years):
            vals.append(vals[-1] if vals else default)
        out[field] = vals[:len(years)]
    st.session_state[key] = out
    return out


asset_builds = {}      # company name -> {"build", "returns", "drives_forecast", ...}
portfolio_rows = []    # the portfolio table the fund-level forecast consumes
precomputed = {}       # asset id -> everything the Asset Model tab needs to render
inv_expanders = {}

if True:
    asset_ids = st.session_state["asset_ids"]
    # Fund-level totals have to be known before anything renders: % of Fund RV needs the
    # total, and the projection grid needs the longest hold.
    total_rv = sum(float(_asset_state(aid, "rv")) for aid in asset_ids) if asset_ids else 0.0
    nav_current = total_rv * 1_000_000
    total_mv = sum(float(_asset_state(aid, "rv")) * (1 + float(_asset_state(aid, "mv_adj")) / 100)
                   for aid in asset_ids) if asset_ids else 0.0
    exit_rels = [max(1, int(_asset_state(aid, "exit_cal")) - as_of.year) for aid in asset_ids]
    remaining_years = max(exit_rels) if exit_rels else 1
    proj_years = [as_of.year + t for t in range(1, max(1, remaining_years) + 1)]
    year_cols = [str(y) for y in proj_years]

    for aid in asset_ids:
        ops = _ops_vectors(aid, proj_years)
        exit_cal = max(as_of.year + 1, int(_asset_state(aid, "exit_cal")))
        rv_m = float(_asset_state(aid, "rv"))
        cost_m = float(_asset_state(aid, "cost"))
        adj_pct = float(_asset_state(aid, "mv_adj"))
        mv_m = rv_m * (1 + adj_pct / 100)
        inv_date = _asset_state(aid, "inv_date")
        inv_year = inv_date.year if isinstance(inv_date, date) else as_of.year
        hold_years = max(0, exit_cal - inv_year)
        name = str(_asset_state(aid, "name"))

        build = asset_model_build(
            entry_revenue=float(_asset_state(aid, "entry_revenue")),
            entry_ebitda_margin=float(_asset_state(aid, "entry_margin")) / 100,
            entry_ev_multiple=float(_asset_state(aid, "entry_ev")),
            entry_net_debt_ebitda=float(_asset_state(aid, "entry_nd")),
            fund_ownership_pct=float(_asset_state(aid, "ownership")) / 100,
            years=proj_years,
            revenue_growth=[v / 100 for v in ops["growth"]],
            ebitda_margin=[v / 100 for v in ops["margin"]],
            fcf_conversion=[v / 100 for v in ops["conv"]],
            exit_year=exit_cal,
            exit_ev_multiple=float(_asset_state(aid, "exit_ev")),
        )
        prior = float(_asset_state(aid, "prior_proceeds"))
        rets = asset_model_returns(build["gross_proceeds_to_fund"], cost_m, rv_m, hold_years,
                                   prior if prior != 0 else None)

        precomputed[aid] = {
            "name": name, "build": build, "returns": rets, "ops": ops,
            "cost_m": cost_m, "rv_m": rv_m, "adj_pct": adj_pct, "mv_m": mv_m,
            "inv_date": inv_date, "exit_cal": exit_cal, "hold_years": hold_years,
            "exit_rel": max(1, exit_cal - as_of.year),
        }
        asset_builds[name] = {
            "build": build, "returns": rets,
            "drives_forecast": bool(_asset_state(aid, "drives")),
            "exit_cal_year": exit_cal, "hold_years": hold_years, "cost_m": cost_m, "rv_m": rv_m,
        }
        portfolio_rows.append({
            "Company": name,
            "Investment Date": inv_date,
            "Cost Basis ($M)": cost_m,
            "Reported Value ($M)": rv_m,
            "MV Adjustment (%)": adj_pct,
            "Expected Return (%)": float(_asset_state(aid, "expected_return")),
            "Exit Year 1": max(1, exit_cal - as_of.year),
            "Exit % 1": float(_asset_state(aid, "exit_pct_1")),
            "Exit Year 2": max(1, int(_asset_state(aid, "exit_cal_2")) - as_of.year),
            "Exit % 2": float(_asset_state(aid, "exit_pct_2")),
        })


# --------------------------------------------------------------------------
# Investments tab -- one editable schedule per block, fund and LP views
# --------------------------------------------------------------------------
# This renders BEFORE the per-asset widgets below, and that ordering is load-bearing: an
# edit here is written straight into the same session_state keys those widgets use, and
# Streamlit only allows writing a widget's key before that widget is instantiated on this
# run. So the two views can never drift. Because the models were built above, the same
# grid can carry editable inputs and computed columns -- there is no separate input table.

def _rows_to_add(singular, plural, key):
    """A count box next to an Add button, so N rows arrive in one click instead of N clicks."""
    c1, c2 = st.columns([1, 3])
    n = int(c1.number_input("Rows to add", min_value=1, max_value=50, value=1, step=1,
                            key=f"{key}_n", label_visibility="collapsed"))
    return n, c2.button(f"➕ Add {n} {singular if n == 1 else plural}", key=f"{key}_btn")


with tab_inv:
    st.subheader("Investments")
    st.caption(
        f"All figures in $mm. Fund level is the whole fund; LP level is the selling LP's "
        f"{lp_pct*100:.2f}% share of the same schedule -- both always list the same "
        "companies. White columns are editable; the shaded ones are computed."
    )

    # ---------------- Current Investments ----------------
    st.markdown("### Current Investments")
    inv_expanders["cur_fund"] = st.expander("Fund level", expanded=True)
    inv_expanders["cur_lp"] = st.expander("LP level", expanded=False)

    with inv_expanders["cur_fund"]:
        n_add, add_clicked = _rows_to_add("company", "companies", "cur_add")
        if add_clicked:
            for _ in range(n_add):
                st.session_state["asset_seq"] += 1
                st.session_state["asset_ids"].append(f"N{st.session_state['asset_seq']}")
            st.rerun()

        cur_grid = pd.DataFrame([
            {
                "id": aid,
                "Company Name": precomputed[aid]["name"],
                "Inv. Date": pd.to_datetime(precomputed[aid]["inv_date"]),
                "% of RV": (precomputed[aid]["rv_m"] / total_rv) if total_rv else 0.0,
                "% of MV": (precomputed[aid]["mv_m"] / total_mv) if total_mv else 0.0,
                "Cost": precomputed[aid]["cost_m"],
                "RV": precomputed[aid]["rv_m"],
                "MV Adjustment (%)": precomputed[aid]["adj_pct"],
                "MV": precomputed[aid]["mv_m"],
                "Exit Year": precomputed[aid]["exit_cal"],
                **{str(r["year"]): r["exit_proceeds_to_fund"]
                   for r in precomputed[aid]["build"]["cash_flow_to_fund"]},
                "Proceeds": precomputed[aid]["build"]["gross_proceeds_to_fund"],
            }
            for aid in asset_ids
        ], columns=(["id", "Company Name", "Inv. Date", "% of RV", "% of MV", "Cost", "RV",
                     "MV Adjustment (%)", "MV", "Exit Year"] + year_cols + ["Proceeds"]))

        computed_cols = ["% of RV", "% of MV", "MV", "Proceeds"] + year_cols
        edited_cur = st.data_editor(
            cur_grid, num_rows="dynamic", width="stretch", hide_index=True,
            key="inv_current_editor",
            disabled=computed_cols,
            column_order=[c for c in cur_grid.columns if c != "id"],
            column_config={
                "Inv. Date": st.column_config.DateColumn("Inv. Date", format="YYYY-MM-DD"),
                "% of RV": st.column_config.NumberColumn("% of RV", format="%.2f%%"),
                "% of MV": st.column_config.NumberColumn("% of MV", format="%.2f%%"),
                "Cost": st.column_config.NumberColumn("Cost", format="%.1f"),
                "RV": st.column_config.NumberColumn("RV", format="%.1f"),
                "MV Adjustment (%)": st.column_config.NumberColumn("MV Adjustment (%)", format="%.4f"),
                "MV": st.column_config.NumberColumn("MV", format="%.1f"),
                "Exit Year": st.column_config.NumberColumn("Exit Year", format="%d", step=1),
                "Proceeds": st.column_config.NumberColumn("Proceeds", format="%.1f"),
                **{c: st.column_config.NumberColumn(c, format="%.1f") for c in year_cols},
            },
        )

        # Push edits back into the shared store. Each row carries the id it came from, so
        # deleting a row in the middle can't silently reassign another company's model.
        _dirty = False
        _seen = []
        for _, erow in edited_cur.iterrows():
            _aid = erow.get("id")
            if _aid is None or (isinstance(_aid, float) and pd.isna(_aid)):
                st.session_state["asset_seq"] += 1
                _aid = f"N{st.session_state['asset_seq']}"
                st.session_state["asset_ids"].append(_aid)
                for _f in PER_ASSET_FIELDS:
                    st.session_state.setdefault(f"am_{_aid}_{_f}", _asset_default(_aid, _f))
                _dirty = True
            _seen.append(_aid)
            _fields = {
                "name": (str(erow["Company Name"]) if pd.notna(erow["Company Name"])
                         else _asset_default(_aid, "name")),
                "inv_date": (pd.to_datetime(erow["Inv. Date"]).date() if pd.notna(erow["Inv. Date"])
                             else _asset_default(_aid, "inv_date")),
                "cost": float(erow["Cost"]) if pd.notna(erow["Cost"]) else 0.0,
                "rv": float(erow["RV"]) if pd.notna(erow["RV"]) else 0.0,
                "mv_adj": float(erow["MV Adjustment (%)"]) if pd.notna(erow["MV Adjustment (%)"]) else 0.0,
                "exit_cal": (int(erow["Exit Year"]) if pd.notna(erow["Exit Year"])
                             else _asset_default(_aid, "exit_cal")),
            }
            for _f, _v in _fields.items():
                _key = f"am_{_aid}_{_f}"
                if st.session_state.get(_key) != _v:
                    st.session_state[_key] = _v
                    _dirty = True

        for _aid in list(st.session_state["asset_ids"]):
            if _aid not in _seen:
                st.session_state["asset_ids"].remove(_aid)
                _dirty = True
        if _dirty:
            st.rerun()

        if asset_ids:
            cur_total = pd.DataFrame([{
                "Company Name": "Total - Current Investments",
                "% of RV": 1.0, "% of MV": 1.0,
                "Cost": sum(precomputed[a]["cost_m"] for a in asset_ids),
                "RV": sum(precomputed[a]["rv_m"] for a in asset_ids),
                "MV": sum(precomputed[a]["mv_m"] for a in asset_ids),
                **{str(y): sum(precomputed[a]["build"]["cash_flow_to_fund"][i]["exit_proceeds_to_fund"]
                               for a in asset_ids)
                   for i, y in enumerate(proj_years)},
                "Proceeds": sum(precomputed[a]["build"]["gross_proceeds_to_fund"] for a in asset_ids),
            }])
            st.dataframe(
                cur_total.style.format({
                    "% of RV": "{:.2%}", "% of MV": "{:.2%}", "Cost": "{:,.1f}",
                    "RV": "{:,.1f}", "MV": "{:,.1f}", "Proceeds": "{:,.1f}",
                    **{c: "{:,.1f}" for c in year_cols}}, na_rep=""),
                width="stretch", hide_index=True,
            )

    with inv_expanders["cur_lp"]:
        if not asset_ids:
            st.caption("Nothing in this block yet.")
        else:
            st.caption(
                f"The same {len(asset_ids)} companies as the Fund level view, at the selling "
                f"LP's {lp_pct*100:.2f}% share. MOIC is unaffected by that scaling, which is "
                "why the fund and LP multiples agree."
            )
            lp_rows = []
            for aid in asset_ids:
                p = precomputed[aid]
                lp_rows.append({
                    "Company Name": p["name"],
                    "Inv. Date": p["inv_date"].isoformat() if isinstance(p["inv_date"], date) else "",
                    "LP Cost": p["cost_m"] * lp_pct, "LP RV": p["rv_m"] * lp_pct,
                    "LP MV": p["mv_m"] * lp_pct,
                    **{str(r["year"]): r["exit_proceeds_to_fund"] * lp_pct
                       for r in p["build"]["cash_flow_to_fund"]},
                    "LP Proceeds": p["build"]["gross_proceeds_to_fund"] * lp_pct,
                    "LP MOIC": (p["build"]["gross_proceeds_to_fund"] / p["cost_m"]) if p["cost_m"] > 0 else None,
                })
            _t = {"Company Name": "Total", "Inv. Date": "",
                  "LP Cost": sum(r["LP Cost"] for r in lp_rows),
                  "LP RV": sum(r["LP RV"] for r in lp_rows),
                  "LP MV": sum(r["LP MV"] for r in lp_rows),
                  **{c: sum(r[c] for r in lp_rows) for c in year_cols},
                  "LP Proceeds": sum(r["LP Proceeds"] for r in lp_rows)}
            _t["LP MOIC"] = (_t["LP Proceeds"] / _t["LP Cost"]) if _t["LP Cost"] else None
            lp_rows.append(_t)
            st.dataframe(
                pd.DataFrame(lp_rows).style.format({
                    "LP Cost": "{:,.2f}", "LP RV": "{:,.2f}", "LP MV": "{:,.2f}",
                    "LP Proceeds": "{:,.2f}", "LP MOIC": "{:.2f}x",
                    **{c: "{:,.2f}" for c in year_cols}}, na_rep=""),
                width="stretch", hide_index=True,
            )

    # ---------------- Post-Report Investments ----------------
    st.markdown("### Post-Report Investments")
    inv_expanders["post_fund"] = st.expander("Fund level", expanded=True)
    inv_expanders["post_lp"] = st.expander("LP level", expanded=False)

    _gen = bool(st.session_state["unfunded_generates_return"])
    _hold, _moic = UNFUNDED_HOLD, UNFUNDED_MOIC

    def _post_flows(cost, call_rel):
        """Cost drawn in the call year, the return landing after the assumed hold."""
        call_year = as_of.year + int(call_rel)
        flows = {call_year: -float(cost)}
        proceeds = 0.0
        if _gen and _hold > 0:
            ret_year = call_year + _hold
            if ret_year in proj_years:
                proceeds = float(cost) * _moic
                flows[ret_year] = flows.get(ret_year, 0.0) + proceeds
        return flows, proceeds, call_year

    with inv_expanders["post_fund"]:
        n_add_p, add_p = _rows_to_add("investment", "investments", "post_add")
        if add_p:
            for _ in range(n_add_p):
                st.session_state["followon_rows"].append(
                    {"Name": "New investment", "Cost": 0.0, "Year": 1})
            st.rerun()

        post_grid_rows = []
        for fr in st.session_state["followon_rows"]:
            flows, proceeds, call_year = _post_flows(fr.get("Cost", 0.0), fr.get("Year", 1))
            post_grid_rows.append({
                "Company Name": fr.get("Name", ""),
                "Year": int(fr.get("Year", 1)),
                "Inv. Date": str(call_year),
                "Cost": float(fr.get("Cost", 0.0)),
                "RV": float(fr.get("Cost", 0.0)),
                "MV": float(fr.get("Cost", 0.0)),
                **{str(y): flows.get(y, 0.0) for y in proj_years},
                "Proceeds": proceeds,
            })
        post_grid = pd.DataFrame(post_grid_rows, columns=(
            ["Company Name", "Year", "Inv. Date", "Cost", "RV", "MV"] + year_cols + ["Proceeds"]))

        edited_post = st.data_editor(
            post_grid, num_rows="dynamic", width="stretch", hide_index=True,
            key="followons_editor",
            disabled=["Inv. Date", "RV", "MV", "Proceeds"] + year_cols,
            column_config={
                "Year": st.column_config.NumberColumn(
                    "Year", format="%d", step=1,
                    help="Forecast year the capital is called, counted off the as-of date."),
                "Cost": st.column_config.NumberColumn("Cost", format="%.1f"),
                "RV": st.column_config.NumberColumn("RV", format="%.1f"),
                "MV": st.column_config.NumberColumn("MV", format="%.1f"),
                "Proceeds": st.column_config.NumberColumn("Proceeds", format="%.1f"),
                **{c: st.column_config.NumberColumn(c, format="%.1f") for c in year_cols},
            },
        )

        new_followons = [
            {"Name": str(r["Company Name"]) if pd.notna(r["Company Name"]) else "New investment",
             "Cost": float(r["Cost"]) if pd.notna(r["Cost"]) else 0.0,
             "Year": int(r["Year"]) if pd.notna(r["Year"]) else 1}
            for _, r in edited_post.iterrows()
        ]
        if new_followons != st.session_state["followon_rows"]:
            st.session_state["followon_rows"] = new_followons
            st.rerun()

        st.caption(
            "Cost is drawn in the call year (shown negative), and the return lands after the "
            "assumed hold period at the assumed MOIC (sidebar section 4). Reported and Market "
            "Value are held at cost, since a brand new position has no independent mark yet."
        )

    with inv_expanders["post_lp"]:
        if not st.session_state["followon_rows"]:
            st.caption("Nothing in this block yet.")
        else:
            st.caption(
                f"The same {len(st.session_state['followon_rows'])} investment(s) as the Fund "
                f"level view, at the selling LP's {lp_pct*100:.2f}% share."
            )
            plp = []
            for fr in st.session_state["followon_rows"]:
                flows, proceeds, call_year = _post_flows(fr.get("Cost", 0.0), fr.get("Year", 1))
                cost = float(fr.get("Cost", 0.0))
                plp.append({
                    "Company Name": fr.get("Name", ""), "Inv. Date": str(call_year),
                    "LP Cost": cost * lp_pct, "LP RV": cost * lp_pct, "LP MV": cost * lp_pct,
                    **{str(y): flows.get(y, 0.0) * lp_pct for y in proj_years},
                    "LP Proceeds": proceeds * lp_pct,
                    "LP MOIC": (proceeds / cost) if cost > 0 else None,
                })
            _t = {"Company Name": "Total", "Inv. Date": "",
                  "LP Cost": sum(r["LP Cost"] for r in plp),
                  "LP RV": sum(r["LP RV"] for r in plp),
                  "LP MV": sum(r["LP MV"] for r in plp),
                  **{c: sum(r[c] for r in plp) for c in year_cols},
                  "LP Proceeds": sum(r["LP Proceeds"] for r in plp)}
            _t["LP MOIC"] = (_t["LP Proceeds"] / _t["LP Cost"]) if _t["LP Cost"] else None
            plp.append(_t)
            st.dataframe(
                pd.DataFrame(plp).style.format({
                    "LP Cost": "{:,.2f}", "LP RV": "{:,.2f}", "LP MV": "{:,.2f}",
                    "LP Proceeds": "{:,.2f}", "LP MOIC": "{:.2f}x",
                    **{c: "{:,.2f}" for c in year_cols}}, na_rep=""),
                width="stretch", hide_index=True,
            )

# The fund forecast consumes the follow-ons in the same shape it always has.
known_followons_df = pd.DataFrame(
    [{"Name": r["Name"], "Amount ($M)": r["Cost"], "Year": r["Year"]}
     for r in st.session_state.get("followon_rows", [])],
    columns=["Name", "Amount ($M)", "Year"],
)


with tab_asset:
    st.subheader("Asset Model (Bottom-up)")
    st.caption(
        "One model per holding, in $mm at fund level, laid out like the source workbook's "
        "per-asset tabs. Everything is editable here -- Deal Snapshot included -- and this "
        "is the only place these numbers are entered, so the fund-level NAV, forecast "
        "horizon and pricing all follow from what you set below."
    )

    # Totals and every asset's model were computed above, before any widget rendered --
    # that is what lets the Investments grid show inputs and results in one table.
    hc1, hc2, hc3 = st.columns(3)
    hc1.metric("Companies", f"{len(asset_ids)}")
    hc2.metric("Aggregate Reported Value", f"${nav_current:,.0f}")
    hc3.metric("Forecast horizon", f"{remaining_years} yrs ({proj_years[0]}-{proj_years[-1]})")

    n_add_am, add_am = _rows_to_add("company", "companies", "am_add")
    if add_am:
        for _ in range(n_add_am):
            st.session_state["asset_seq"] += 1
            st.session_state["asset_ids"].append(f"N{st.session_state['asset_seq']}")
        st.rerun()

    if not asset_ids:
        st.info("No companies yet -- add one above, or in the Investments tab.")

    for aid in list(asset_ids):
        pre = precomputed[aid]
        name = pre["name"]

        with st.expander(f"{name}", expanded=False):
            sc1, sc2 = st.columns([4, 1])
            with sc2:
                if st.button("Remove", key=f"am_{aid}_remove"):
                    st.session_state["asset_ids"].remove(aid)
                    st.rerun()
            drives = st.checkbox(
                "Use this asset model to drive the fund forecast",
                key=f"am_{aid}_drives",
                help="On: this company's expected return in the forecast is whatever this "
                     "model implies (its exit proceeds compounded back from today's Market "
                     "Value). Off: the fallback 'Expected Return (%)' below is used instead, "
                     "and this model is display-only.",
            )

            st.markdown("**Deal Snapshot**")
            d1, d2, d3 = st.columns(3)
            name = d1.text_input("Company", key=f"am_{aid}_name")
            inv_date = d2.date_input(
                "Investment Date", min_value=date(1990, 1, 1), max_value=date(2100, 12, 31),
                key=f"am_{aid}_inv_date",
            )
            cost_m = d3.number_input(
                "LP Cost ($mm, gross)", min_value=0.0, step=0.1, format="%.1f",
                key=f"am_{aid}_cost",
            )
            d4, d5, d6 = st.columns(3)
            rv_m = d4.number_input(
                "Reported Value (RV, $mm)", min_value=0.0, step=0.1, format="%.1f",
                key=f"am_{aid}_rv",
                help="The GP's official mark for this company, at fund level.",
            )
            adj_pct = d5.number_input(
                "MV Adjustment (%)", step=0.5, format="%.4f", key=f"am_{aid}_mv_adj",
                help="Your own diligence view on top of the GP's mark. Market Value = RV x "
                     "(1 + this). Leave at 0 to take the mark at face value.",
            )
            exit_cal = int(d6.number_input(
                "Exit Year", min_value=as_of.year + 1, max_value=as_of.year + 30, step=1,
                key=f"am_{aid}_exit_cal",
                help="Calendar year this position is exited. It sets the forecast horizon "
                     "and the projection columns below.",
            ))

            mv_m = rv_m * (1 + adj_pct / 100)
            exit_rel = max(1, exit_cal - as_of.year)
            inv_year = inv_date.year if isinstance(inv_date, date) else as_of.year
            hold_years = max(0, exit_cal - inv_year)

            derived = pd.DataFrame([
                {"Line item": "Market Value (MV)", "Value": f"{mv_m:,.1f}"},
                {"Line item": "% of Fund RV", "Value": f"{(rv_m / total_rv * 100) if total_rv else 0:,.2f}%"},
                {"Line item": "Hold Period (yrs, from inv. date)", "Value": f"{hold_years}"},
                {"Line item": "Exit in forecast year", "Value": f"{exit_rel}"},
            ])
            st.dataframe(derived, width="stretch", hide_index=True)

            with st.popover("Phased exit & fallback return"):
                st.caption(
                    "By default the whole position is realised in the exit year above. Use "
                    "these to split the exit across two years, or to set the flat return "
                    "used when this asset model is switched off."
                )
                exit_pct_1 = st.number_input(
                    "Exit % in the exit year", min_value=0.0, max_value=100.0, step=5.0,
                    format="%.1f", key=f"am_{aid}_exit_pct_1",
                )
                exit_cal_2 = int(st.number_input(
                    "Second exit year", min_value=as_of.year + 1, max_value=as_of.year + 30,
                    step=1, key=f"am_{aid}_exit_cal_2",
                ))
                exit_pct_2 = st.number_input(
                    "Exit % of what's left, in that year", min_value=0.0, max_value=100.0,
                    step=5.0, format="%.1f", key=f"am_{aid}_exit_pct_2",
                )
                expected_return_fallback = st.number_input(
                    "Fallback Expected Return (%)", step=1.0, format="%.1f",
                    key=f"am_{aid}_expected_return",
                )

            st.markdown("**Entry Assumptions**")
            e1, e2, e3 = st.columns(3)
            entry_revenue = e1.number_input(
                "Entry Revenue ($mm)", min_value=0.0, step=0.1, format="%.1f",
                key=f"am_{aid}_entry_revenue",
            )
            entry_margin = e2.number_input(
                "Entry EBITDA Margin (%)", min_value=0.0, max_value=100.0, step=0.5,
                format="%.1f", key=f"am_{aid}_entry_margin",
            )
            entry_ev_mult = e3.number_input(
                "Entry EV/EBITDA (x)", min_value=0.0, step=0.5, format="%.1f",
                key=f"am_{aid}_entry_ev",
            )
            e4, e5 = st.columns(2)
            entry_nd_mult = e4.number_input(
                "Entry Net Debt / EBITDA (x)", min_value=0.0, step=0.25, format="%.2f",
                key=f"am_{aid}_entry_nd",
            )
            fund_ownership = e5.number_input(
                "Fund Ownership (%)", min_value=0.0, max_value=100.0, step=1.0, format="%.1f",
                key=f"am_{aid}_ownership",
            )

            st.markdown("**Operating Projections ($mm)**")
            _ops = pre["ops"]
            ops_grid = pd.DataFrame([
                {"Line item": "Revenue Growth (%)",
                 **{str(y): _ops["growth"][i] for i, y in enumerate(proj_years)}},
                {"Line item": "EBITDA Margin (%)",
                 **{str(y): _ops["margin"][i] for i, y in enumerate(proj_years)}},
                {"Line item": "FCF Conversion (% of EBITDA)",
                 **{str(y): _ops["conv"][i] for i, y in enumerate(proj_years)}},
            ])
            ops_df = st.data_editor(
                ops_grid, width="stretch", hide_index=True, key=f"am_{aid}_ops",
                disabled=["Line item"],
            )

            def _row_vals(frame, label, fallback):
                match = frame[frame["Line item"] == label]
                if len(match) == 0:
                    return [fallback] * len(proj_years)
                r = match.iloc[0]
                out = []
                for y in proj_years:
                    try:
                        out.append(float(r[str(y)]))
                    except (KeyError, TypeError, ValueError):
                        out.append(fallback)
                return out

            _edited_ops = {
                "growth": _row_vals(ops_df, "Revenue Growth (%)", COMMON_DEFAULTS["revenue_growth"]),
                "margin": _row_vals(ops_df, "EBITDA Margin (%)", COMMON_DEFAULTS["ebitda_margin"]),
                "conv": _row_vals(ops_df, "FCF Conversion (% of EBITDA)", COMMON_DEFAULTS["fcf_conversion"]),
            }
            if _edited_ops != _ops:
                # The build above was made from the stored vectors; a change here means the
                # store is stale, so save it and rerun so every view reflects the new curve.
                st.session_state[f"am_{aid}_ops_store"] = _edited_ops
                st.rerun()

            st.markdown("**Exit Valuation**")
            exit_ev_mult = st.number_input(
                "Exit EV/EBITDA (x)", min_value=0.0, step=0.5, format="%.1f",
                key=f"am_{aid}_exit_ev",
            )

            build = pre["build"]

            # Entry block, computed -- shown under its inputs so the build reads top to bottom.
            entry_calc = pd.DataFrame([
                {"Line item": "Entry EBITDA", "Value": build["entry_ebitda"]},
                {"Line item": "Entry Enterprise Value", "Value": build["entry_enterprise_value"]},
                {"Line item": "Entry Net Debt", "Value": build["entry_net_debt"]},
                {"Line item": "Entry Equity Value", "Value": build["entry_equity_value"]},
            ])

            sched = pd.DataFrame(build["schedule"])
            ops_out = pd.DataFrame(
                [
                    {"Line item": "Revenue", **{str(r["year"]): r["revenue"] for r in build["schedule"]}},
                    {"Line item": "EBITDA", **{str(r["year"]): r["ebitda"] for r in build["schedule"]}},
                    {"Line item": "Free Cash Flow", **{str(r["year"]): r["fcf"] for r in build["schedule"]}},
                    {"Line item": "Net Debt - Beginning",
                     **{str(r["year"]): r["net_debt_beginning"] for r in build["schedule"]}},
                    {"Line item": "Less: Debt Paydown (FCF)",
                     **{str(r["year"]): r["debt_paydown"] for r in build["schedule"]}},
                    {"Line item": "Net Debt - Ending",
                     **{str(r["year"]): r["net_debt_ending"] for r in build["schedule"]}},
                ]
            )

            if not build["exit_year_in_horizon"]:
                st.warning(
                    f"Exit year {exit_cal} falls outside the projection columns "
                    f"({proj_years[0]}-{proj_years[-1]}). Extend the forecast horizon by "
                    "moving this company's Exit Year 1 inside it."
                )
            exit_calc = pd.DataFrame([
                {"Line item": "Exit EBITDA", "Value": build["exit_ebitda"]},
                {"Line item": "Exit EV/EBITDA (x)", "Value": exit_ev_mult},
                {"Line item": "Exit Enterprise Value", "Value": build["exit_enterprise_value"]},
                {"Line item": "Less: Net Debt at Exit", "Value": build["net_debt_at_exit"]},
                {"Line item": "Exit Equity Value", "Value": build["exit_equity_value"]},
                {"Line item": "Fund Ownership (%)", "Value": fund_ownership},
                {"Line item": "Gross Proceeds to Fund ($mm)", "Value": build["gross_proceeds_to_fund"]},
            ])

            st.markdown("**Returns & Tie-Out**")
            prior_proceeds = st.number_input(
                "Prior Fund Model Proceeds (hardcode)", step=0.1, format="%.1f",
                key=f"am_{aid}_prior_proceeds",
                help="What this asset's proceeds were in the last version of the fund model "
                     "circulated. Kept as a hardcode so a re-run can be diffed against what "
                     "the client last saw. Set to 0 to ignore.",
            )
            rets = pre["returns"]

            # --- render the computed blocks, in the source model's order ---
            st.markdown("_Entry Assumptions (computed)_")
            st.dataframe(entry_calc.style.format({"Value": "{:,.3f}"}), width="stretch", hide_index=True)
            st.markdown("_Operating Projections (computed)_")
            st.dataframe(
                ops_out.style.format({str(y): "{:,.2f}" for y in proj_years}),
                width="stretch", hide_index=True,
            )
            st.markdown("_Exit Valuation (computed)_")
            st.dataframe(exit_calc.style.format({"Value": "{:,.3f}"}), width="stretch", hide_index=True)

            r1, r2, r3, r4 = st.columns(4)
            r1.metric("Gross MOIC (vs Cost)",
                      f"{rets['gross_moic_vs_cost']:.2f}x" if rets["gross_moic_vs_cost"] is not None else "NM")
            r2.metric("Multiple on RV",
                      f"{rets['multiple_on_rv']:.2f}x" if rets["multiple_on_rv"] is not None else "NM")
            r3.metric("Gross IRR (annualised)",
                      f"{rets['gross_irr_annualised']*100:.1f}%" if rets["gross_irr_annualised"] is not None else "NM")
            r4.metric("Variance vs Prior",
                      f"{rets['variance_vs_prior']:+,.2f}" if rets["variance_vs_prior"] is not None else "--")

            st.markdown("**Cash Flow to Fund Model**")
            cf_out = pd.DataFrame([
                {"Line item": "Exit Proceeds to Fund",
                 **{str(r["year"]): r["exit_proceeds_to_fund"] for r in build["cash_flow_to_fund"]}},
            ])
            st.dataframe(
                cf_out.style.format({str(y): "{:,.2f}" for y in proj_years}),
                width="stretch", hide_index=True,
            )
            st.caption(
                "Fund-level $mm. The selling LP's share of this is what reaches the Cash Flow "
                f"Forecast tab: {lp_pct*100:.2f}% = "
                f"${build['gross_proceeds_to_fund'] * lp_pct:,.2f}mm in {exit_cal}."
            )


# --------------------------------------------------------------------------
# Deal Terms, part 2: the assumptions that need the forecast horizon
# --------------------------------------------------------------------------
# These append below the term sheet above. They sit here rather than up there
# because their controls are bounded by the forecast horizon, which is only known
# once every company's exit year has been read.
with tab_terms:
    st.markdown("**Unfunded Assumptions**")
    ua1, ua2, ua3 = st.columns(3)
    unfunded_generates_return = ua1.checkbox(
        "Unfunded generates its own return", key="unfunded_generates_return",
        help="A capital call isn't just an outflow -- it funds a new investment that goes on "
             "to return money. On: every future call is projected forward at the return below.",
    )
    if unfunded_generates_return:
        unfunded_hold_years = int(ua2.number_input("Hold Years", min_value=1, step=1,
                                                   key="unfunded_hold_years"))
        unfunded_irr = ua3.number_input(
            "IRR (%)", min_value=0.0, max_value=100.0, step=0.5, format="%.1f",
            key="unfunded_irr",
            help="The return assumed on capital called from here. The MOIC below follows from "
                 "it and the hold period, the way the source model derives it.",
        ) / 100
        unfunded_moic = (1 + unfunded_irr) ** unfunded_hold_years
        st.caption(
            f"MOIC = (1 + {unfunded_irr*100:.1f}%)^{unfunded_hold_years} = "
            f"**{unfunded_moic:.3f}x**. Each call in year Y returns that multiple in year "
            f"Y + {unfunded_hold_years}; calls maturing beyond the horizon are excluded rather "
            "than distorting the last forecast year."
        )
    else:
        unfunded_hold_years = 0
        unfunded_moic = 0.0
        unfunded_irr = 0.0

    # The blind pool is what's left of the LP's unfunded commitment once the named
    # follow-ons are taken out of it -- exactly the source model's drawdown line, so
    # there is no separate blind-pool amount to keep in step.
    known_followon_total_fund = sum(float(r.get("Cost", 0.0))
                                    for r in st.session_state.get("followon_rows", []))
    lp_unfunded_fund_m = (lp_unfunded_m / lp_pct) if lp_pct else 0.0
    blind_pool_amount = max(0.0, (lp_unfunded_fund_m - known_followon_total_fund) * 1_000_000)
    bp1, bp2 = st.columns(2)
    bp1.metric("Blind pool (fund $mm)", f"{blind_pool_amount/1_000_000:,.1f}")
    if blind_pool_amount > 0:
        blind_pool_years = int(bp2.number_input(
            "Blind pool call period (years)", min_value=1, max_value=max(1, remaining_years),
            value=min(2, max(1, remaining_years)), step=1,
        ))
    else:
        blind_pool_years = 0
    st.caption(
        f"Unfunded {lp_unfunded_m:,.3f}mm at the LP's share = {lp_unfunded_fund_m:,.1f}mm at "
        f"fund level, less {known_followon_total_fund:,.1f}mm of named follow-ons entered in "
        "the Investments tab. Named follow-ons are called in full in their year; the blind "
        "pool is spread evenly over its call period."
    )

    if use_two_tier_fee:
        st.markdown("**Management fee crossover**")
        crossover_year = st.slider(
            "Crossover year — the fee basis switches here", 1, max(1, remaining_years),
            min(2, max(1, remaining_years)),
            help="Forecast year in which the fee stops being charged on committed capital and "
                 "starts being charged on remaining invested cost. Typically the end of the "
                 "fund's investment period.",
        )
    else:
        crossover_year = 1

    st.markdown("**Credit Facility Assumptions**")
    cf1, cf2, cf3 = st.columns(3)
    use_leverage = cf1.checkbox("Buyer finances part of the price", value=False)
    if use_leverage:
        leverage_pct = cf2.number_input("Leverage (% of price)", min_value=0.0, max_value=90.0,
                                        value=40.0, step=5.0, format="%.0f") / 100
        leverage_rate = cf3.number_input("Interest rate (%)", min_value=0.0, max_value=15.0,
                                         value=6.5, step=0.25, format="%.2f") / 100
        st.caption(
            "A subscription line / NAV facility funds this share of the purchase price at "
            "close. Available cash each year sweeps to interest then principal until the "
            "balance clears; the facility never funds capital calls, only the purchase."
        )
    else:
        leverage_pct = 0.0
        leverage_rate = 0.0


# --------------------------------------------------------------------------
# Pricing -- the sidebar, so the price is on screen while anything is changed
# --------------------------------------------------------------------------
# The input half renders here, before the pricing engine runs; the panel itself is
# written into the sidebar further down, once the forecast exists. Streamlit appends
# to the sidebar in call order, so the two halves read as one block.
st.sidebar.header("Pricing")
premium_discount = st.sidebar.number_input(
    "Premium / (Discount) to Reported Value (%)", min_value=-90.0, max_value=50.0,
    step=0.5, format="%.1f", key="premium_discount_pct",
    help="Negative is a discount, positive a premium -- the sign convention the term sheet "
         "uses. Everything below reprices the moment this changes.",
) / 100
gross_dists_to_closing_m = st.sidebar.number_input(
    "Gross Distributions (LP $mm)", value=-2.0, step=0.5, format="%.1f",
    help="Distributions received between the Report Date and closing. They belong to the "
         "buyer's side of the ledger, so they reduce the effective price.",
)
# secondary_pricing works in discounts (positive = cheaper than NAV); the term sheet
# quotes the same number as a signed premium.
buyer_target_discount = -premium_discount

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

asset_model_details = []  # per-company bottom-up build detail, for display
fee_schedule_display = None  # two-tier management fee schedule, for the fee explainer
companies = []
portfolio_display_rows = []
for row in portfolio_rows:
    rv = float(row["Reported Value ($M)"]) * 1_000_000
    adj = float(row["MV Adjustment (%)"]) / 100
    mv = reported_vs_market_value(rv, adj)
    mv_lp = mv * lp_pct
    exit_year_1 = int(row["Exit Year 1"])

    # Each company's return comes from its bottom-up asset model (built in the
    # Asset Model tab above) unless that model is switched off, in which case the
    # flat Expected Return (%) from this table is the fallback.
    expected_return = float(row["Expected Return (%)"]) / 100
    used_asset_model = False
    am = asset_builds.get(str(row["Company"]))
    if am is not None and am["drives_forecast"] and am["build"]["exit_year_in_horizon"]:
        exit_proceeds_lp = am["build"]["gross_proceeds_to_fund"] * 1_000_000 * lp_pct
        expected_return = implied_annual_return(mv_lp, exit_proceeds_lp, exit_year_1)
        used_asset_model = True
        asset_model_details.append({
            "Company": row["Company"],
            "exit_year": am["exit_cal_year"],
            "gross_proceeds_to_fund": am["build"]["gross_proceeds_to_fund"] * 1_000_000,
            "exit_proceeds_to_fund_lp": exit_proceeds_lp,
            "implied_return": expected_return,
            "gross_moic_vs_cost": am["returns"]["gross_moic_vs_cost"],
            "gross_irr_annualised": am["returns"]["gross_irr_annualised"],
        })

    cost = float(row.get("Cost Basis ($M)", 0.0)) * 1_000_000
    cost_lp = cost * lp_pct

    companies.append({
        "name": row["Company"],
        "current_value": mv_lp,
        "expected_return": expected_return,
        "exit_year_1": exit_year_1,
        "exit_pct_1": float(row["Exit % 1"]) / 100,
        "exit_year_2": int(row["Exit Year 2"]),
        "exit_pct_2": float(row["Exit % 2"]) / 100,
        "cost": cost_lp,
    })
    portfolio_display_rows.append({
        "Company": row["Company"], "Cost Basis (Fund)": cost, "Reported Value (Fund)": rv,
        "MV Adjustment": adj, "Market Value (Fund)": mv, "Market Value (LP)": mv_lp,
        "Unrealized MOIC (RV/Cost)": (rv / cost) if cost > 0 else None,
        "Valuation method": "Asset model (bottom-up)" if used_asset_model else "Expected Return %",
        "Return used": expected_return,
    })

fee_override_by_year = None
fee_schedule_display = None
if apply_fees and use_two_tier_fee and companies:
    max_year = max(max(int(c["exit_year_1"]), int(c["exit_year_2"])) for c in companies)
    max_year = max(1, max_year)
    remaining_cost = remaining_cost_basis_by_year(companies, max_year)
    fee_override_by_year = crossover_fee_schedule(
        total_commitment=lp_commitment, remaining_cost_by_year=remaining_cost,
        crossover_year=crossover_year, fee_rate_initial=fee_rate_initial,
        fee_rate_post=fee_rate_post,
    )
    fee_schedule_display = pd.DataFrame([
        {
            "Year": t,
            "Basis": "Commitment (investment period)" if t < crossover_year else "Remaining cost basis",
            "Fee rate": fee_rate_initial if t < crossover_year else fee_rate_post,
            "Fee basis ($)": lp_commitment if t < crossover_year else remaining_cost[t],
            "Fee ($)": fee_override_by_year[t],
        }
        for t in sorted(fee_override_by_year)
    ])

forecast_rows = forecast_from_portfolio(
    companies, mgmt_fee_rate=mgmt_fee if apply_fees else 0.0, as_of=as_of,
    fee_override_by_year=fee_override_by_year,
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

# --------------------------------------------------------------------------
# Pricing panel -- the source model's pricing block, live in the sidebar
# --------------------------------------------------------------------------
# Three columns, as the workbook has them: what the LP holds, what the buyer
# effectively pays for it, and the premium or discount that implies on each line.
_lp_rv_m = (total_rv + net_cash_m) * lp_pct
_lp_mv_m = (total_mv + net_cash_m) * lp_pct
_eff_rv_m = _lp_rv_m * (1 + premium_discount)
_eff_mv_m = _eff_rv_m                      # the mark the buyer is actually paying for
_gross_exposure_m = _lp_rv_m + lp_unfunded_m
_eff_gross_exposure_m = _eff_rv_m + lp_unfunded_m
_net_funded_m = _gross_exposure_m + gross_dists_to_closing_m
_eff_net_funded_m = _eff_gross_exposure_m + gross_dists_to_closing_m
_net_price_m = _eff_mv_m + gross_dists_to_closing_m
_par_price_m = _lp_mv_m + gross_dists_to_closing_m
_net_proj_proceeds_m = (sum(r[distribution_key] for r in forecast_rows)
                        + total_unfunded_return - total_unfunded) / 1_000_000


def _pd(effective, stated):
    return (effective / stated - 1) if stated else None


_pricing_rows = [
    ("Net Effective Price", _par_price_m, _net_price_m, _pd(_net_price_m, _par_price_m)),
    ("Market Value", _lp_mv_m, _eff_mv_m, _pd(_eff_mv_m, _lp_mv_m)),
    ("Reported Value", _lp_rv_m, _eff_rv_m, premium_discount),
    ("Unfunded", lp_unfunded_m, lp_unfunded_m, 0.0),
    ("Gross Exposure", _gross_exposure_m, _eff_gross_exposure_m,
     _pd(_eff_gross_exposure_m, _gross_exposure_m)),
    ("Gross Distributions", gross_dists_to_closing_m, gross_dists_to_closing_m, 0.0),
    ("Net Funded Exposure", _net_funded_m, _eff_net_funded_m, _pd(_eff_net_funded_m, _net_funded_m)),
    ("Net Proj. Proceeds", _net_proj_proceeds_m, _net_proj_proceeds_m, None),
]
_pricing_df = pd.DataFrame(_pricing_rows, columns=["", "LP", "Effective Pricing", "Prem/(Disc)"])

with st.sidebar:
    st.dataframe(
        _pricing_df.style.format({"LP": "{:,.2f}", "Effective Pricing": "{:,.2f}",
                                  "Prem/(Disc)": "{:+.1%}"}, na_rep=""),
        width="stretch", hide_index=True,
    )
    st.caption("LP's share, $mm.")

    st.metric("Buyer IRR at this price",
              f"{buyer_row['irr']*100:.1f}%" if buyer_row["irr"] == buyer_row["irr"] else "n/a")
    b1, b2 = st.columns(2)
    b1.metric("MOIC", f"{buyer_row['moic']:.2f}x")
    b2.metric("At par", f"{seller_row_for_moic['irr']*100:.1f}%"
              if seller_row_for_moic["irr"] == seller_row_for_moic["irr"] else "n/a")
    if leverage_result:
        st.metric("Levered IRR", f"{leverage_result['levered_irr']*100:.1f}%"
                  if leverage_result["levered_irr"] == leverage_result["levered_irr"] else "n/a",
                  delta=f"{(leverage_result['levered_irr'] - buyer_row['irr'])*100:+.1f} pts"
                  if leverage_result["levered_irr"] == leverage_result["levered_irr"] else None)

    with st.expander("Across a range of prices"):
        _ladder = pd.DataFrame([
            {"Prem/(Disc)": -d, "Price": r["price"] / 1_000_000, "IRR": r["irr"], "MOIC": r["moic"]}
            for d, r in zip(discount_levels, pricing)
        ])
        st.dataframe(
            _ladder.style.format({"Prem/(Disc)": "{:+.0%}", "Price": "{:,.2f}",
                                  "IRR": "{:.1%}", "MOIC": "{:.2f}x"}),
            width="stretch", hide_index=True,
        )
        st.caption(
            "The same buyer cash flows at other prices. A deeper discount buys the identical "
            "projected proceeds for less, so IRR and MOIC both rise -- the forecast is not "
            "re-cut for each row."
        )


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
        "remaining_years": remaining_years,
        "fees_applied": apply_fees,
        "mgmt_fee": mgmt_fee,
        "use_two_tier_fee": use_two_tier_fee,
        "two_tier_fee_rate_initial": fee_rate_initial if use_two_tier_fee else None,
        "two_tier_fee_rate_post_crossover": fee_rate_post if use_two_tier_fee else None,
        "two_tier_fee_crossover_year": crossover_year if use_two_tier_fee else None,
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
# Investments tab: the blind-pool footnote
# --------------------------------------------------------------------------
# The schedules themselves were rendered above, before the per-asset widgets. Only
# this note has to wait, because the blind pool is a sidebar input that renders later.
if blind_pool_amount > 0:
    with inv_expanders["post_fund"]:
        st.caption(
            f"Separately, ${blind_pool_amount:,.0f} of blind-pool commitment (fund level) is "
            f"drawn over {blind_pool_years} year(s). It isn't a line here because it isn't a "
            "named investment yet -- it flows through the Cash Flow Forecast."
        )

# --------------------------------------------------------------------------
# Funded & Unfunded Commitment -- the source model's two summary blocks
# --------------------------------------------------------------------------
# These take the two investment blocks above and walk them down to net proceeds:
# fees, carry and cash on the funded side; the post-report investments, their carry
# and the remaining drawdown on the unfunded side. Every line is the app's own
# forecast presented in the workbook's row order, not a second calculation -- the
# "Proceeds after management fees" line IS the distribution the pricing tab uses.
if forecast_rows:

    def _fund(x):
        """LP-level engine output -> fund level, in $mm."""
        return (x / lp_pct / 1_000_000) if lp_pct else 0.0

    def _lp_mm(x):
        """LP-level engine output -> $mm, still at the LP's share."""
        return x / 1_000_000

    # The engine charges the management fee inside the NAV roll-forward, so a year's
    # distribution is already net of it. To show fees on their own line the way the
    # workbook does, split that year's distribution back into its pre-fee amount and
    # the fee borne by it: fee_ratio is exactly what the engine applied.
    fee_lines = []
    for r in forecast_rows:
        grown = r.get("grown_nav", 0.0)
        fee = r.get("mgmt_fee", 0.0)
        ratio = ((grown - fee) / grown) if grown > 0 else 1.0
        dist = r.get("gross_distribution", 0.0)
        pre_fee = (dist / ratio) if ratio > 0 else dist
        fee_lines.append({
            "year": r["year"],
            "pre_fee_distribution": pre_fee,
            "fee_on_distribution": pre_fee - dist,
            "fee_total": fee,
            "distribution": dist,
            "gp_carry": r.get("gp_carry", 0.0),
            "lp_distribution": r.get("lp_distribution", dist),
        })
    total_dist = sum(f["distribution"] for f in fee_lines) or 1.0

    with tab_inv:
        st.markdown("### Funded Commitment")
        fc_fund_exp = st.expander("Fund level", expanded=True)
        fc_lp_exp = st.expander("LP level", expanded=False)

        with fc_fund_exp:
            nc1, nc2 = st.columns(2)
            net_cash_m = nc1.number_input(
                "Net Cash ($mm)", value=0.7, step=0.1, format="%.1f", key="funded_net_cash",
                help="Surplus cash and other fund assets sitting outside the portfolio marks "
                     "-- the gap between the fund's reported value and the sum of its company "
                     "marks. Shown as a balance-sheet line, as in the source model.",
            )
            tax_blocker_m = nc2.number_input(
                "Tax Blocker Leakage ($mm)", value=0.0, step=0.1, format="%.1f",
                key="funded_tax_blocker",
                help="Value lost through a blocker structure, spread across the exit years in "
                     "proportion to each year's proceeds. Negative to the LP, so enter it as a "
                     "positive number here and it is deducted.",
            )
            st.caption(
                "These two lines are presentational: they appear in this block's arithmetic "
                "but do not move the Secondary Pricing tab, which prices off NAV and the "
                "forecast. Say the word and I'll wire them into NAV too."
            )

        def _funded_rows(scale):
            """scale: _fund for the whole fund, _lp_mm for the selling LP's share."""
            cost = sum(precomputed[a]["cost_m"] for a in asset_ids)
            rv = sum(precomputed[a]["rv_m"] for a in asset_ids)
            mv = sum(precomputed[a]["mv_m"] for a in asset_ids)
            if scale is _lp_mm:
                cost, rv, mv = cost * lp_pct, rv * lp_pct, mv * lp_pct
                cash = net_cash_m * lp_pct
                blocker = tax_blocker_m * lp_pct
                accrued = accrued_carry_lp / 1_000_000
            else:
                cash = net_cash_m
                blocker = tax_blocker_m
                accrued = accrued_carry / 1_000_000

            by_year = {f["year"]: f for f in fee_lines}

            def row(label, per_year, c=0.0, r=0.0, m=0.0):
                d = {"Line item": label, "Cost": c, "RV": r, "MV": m}
                for i, y in enumerate(proj_years, start=1):
                    d[str(y)] = per_year(by_year[i]) if i in by_year else 0.0
                d["Proceeds"] = sum(d[str(y)] for y in proj_years)
                return d

            current = row("Current Investments", lambda f: scale(f["pre_fee_distribution"]),
                          cost, rv, mv)
            # Net cash is a balance-sheet item in the source model: it shows in the value
            # columns, not as a cash flow in any year.
            netcash = {"Line item": "Net Cash", "Cost": 0.0, "RV": cash, "MV": cash,
                       **{str(y): 0.0 for y in proj_years}, "Proceeds": 0.0}
            fees = row("Management fees", lambda f: -scale(f["fee_on_distribution"]))
            after_fees = {"Line item": "Proceeds after management fees"}
            for k in ["Cost", "RV", "MV"] + [str(y) for y in proj_years] + ["Proceeds"]:
                after_fees[k] = current[k] + netcash[k] + fees[k]

            future_carry = row("Future Carried Interest on Funded Commitment",
                               lambda f: -scale(f["gp_carry"]))
            accrued_row = row("Accrued Carried Interest to Date",
                              lambda f: -accrued * (f["distribution"] / total_dist))
            blocker_row = row("Tax Blocker Leakage",
                              lambda f: -blocker * (f["distribution"] / total_dist))
            after_carry = {"Line item": "Proceeds after carried Interest"}
            for k in ["Cost", "RV", "MV"] + [str(y) for y in proj_years] + ["Proceeds"]:
                after_carry[k] = (after_fees[k] + future_carry[k] + accrued_row[k] + blocker_row[k])

            return [current, netcash, fees, after_fees, future_carry, accrued_row,
                    blocker_row, after_carry]

        def _show_block(rows, container, lp_view):
            cols = (["Line item", "Cost", "RV", "MV"] + [str(y) for y in proj_years] + ["Proceeds"])
            df = pd.DataFrame(rows, columns=cols)
            if lp_view:
                df = df.rename(columns={"Cost": "LP Cost", "RV": "LP RV", "MV": "LP MV",
                                        "Proceeds": "LP Proceeds"})
                # MOIC only where it means something: proceeds against the cost that earned them.
                df["LP MOIC"] = [
                    (r["LP Proceeds"] / r["LP Cost"]) if r["LP Cost"] else None
                    for _, r in df.iterrows()
                ]
                money = ["LP Cost", "LP RV", "LP MV", "LP Proceeds"]
            else:
                money = ["Cost", "RV", "MV", "Proceeds"]
            fmt = {c: "{:,.2f}" for c in money}
            fmt.update({str(y): "{:,.2f}" for y in proj_years})
            if lp_view:
                fmt["LP MOIC"] = "{:.2f}x"
            with container:
                st.dataframe(df.style.format(fmt, na_rep=""), width="stretch", hide_index=True)

        _show_block(_funded_rows(_fund), fc_fund_exp, lp_view=False)
        _show_block(_funded_rows(_lp_mm), fc_lp_exp, lp_view=True)

        with fc_fund_exp:
            borne_by_nav = sum(f["fee_total"] - f["fee_on_distribution"] for f in fee_lines)
            st.caption(
                f"Memo: a further ${_fund(borne_by_nav):,.2f}mm of management fee is borne by "
                "NAV still held rather than by a distribution, so it shows up as smaller "
                "proceeds in later years rather than as a line here."
            )

        # ---------------- Unfunded Commitment ----------------
        st.markdown("### Unfunded Commitment")
        uc_fund_exp = st.expander("Fund level", expanded=True)
        uc_lp_exp = st.expander("LP level", expanded=False)

        # The blind pool on its own: the same schedule builder, with no follow-ons in it.
        blind_calls = build_unfunded_schedule([], blind_pool_amount * lp_pct, blind_pool_years,
                                              forecast_rows)
        # build_unfunded_returns also reports what falls beyond the horizon; only the
        # schedule itself is needed here.
        blind_returns = (build_unfunded_returns(blind_calls, unfunded_hold_years, unfunded_moic,
                                                forecast_rows)[0]
                         if unfunded_generates_return else {})

        post_cost = sum(float(r.get("Cost", 0.0)) for r in st.session_state["followon_rows"])
        post_flows, post_proceeds = {}, 0.0
        for fr in st.session_state["followon_rows"]:
            f, p, _ = _post_flows(fr.get("Cost", 0.0), fr.get("Year", 1))
            for y, v in f.items():
                post_flows[y] = post_flows.get(y, 0.0) + v
            post_proceeds += p

        def _unfunded_rows(lp_view):
            s = lp_pct if lp_view else 1.0

            def row(label, per_year, c=0.0, r=0.0, m=0.0):
                d = {"Line item": label, "Cost": c, "RV": r, "MV": m}
                for y in proj_years:
                    d[str(y)] = per_year(y)
                d["Proceeds"] = sum(d[str(y)] for y in proj_years)
                return d

            post = row("Post-Report Date Investments",
                       lambda y: post_flows.get(y, 0.0) * s,
                       post_cost * s, post_cost * s, post_cost * s)
            # Carry on the post-report investments' own profit, spread across the years the
            # money actually comes back in.
            profit = max(0.0, post_proceeds - post_cost)
            carry_amt = profit * carry_rate if apply_fees else 0.0
            post_carry = row(
                "Carried Interest on Post-Report Investments",
                lambda y: -carry_amt * s * ((max(0.0, post_flows.get(y, 0.0)) / post_proceeds)
                                            if post_proceeds else 0.0),
            )
            # blind_calls / blind_returns are LP-level dollars keyed by forecast year number.
            gross_up = 1.0 if lp_view else ((1 / lp_pct) if lp_pct else 0.0)
            drawdown = row("Drawdown on remaining unfunded",
                           lambda y: -blind_calls.get(y - as_of.year, 0.0) / 1_000_000 * gross_up)
            ret = row("Return on Remaining Unfunded",
                      lambda y: blind_returns.get(y - as_of.year, 0.0) / 1_000_000 * gross_up)
            total = {"Line item": "Unfunded Commitment"}
            for k in ["Cost", "RV", "MV"] + [str(y) for y in proj_years] + ["Proceeds"]:
                total[k] = post[k] + post_carry[k] + drawdown[k] + ret[k]
            return [post, post_carry, drawdown, ret, total]

        _show_block(_unfunded_rows(False), uc_fund_exp, lp_view=False)
        _show_block(_unfunded_rows(True), uc_lp_exp, lp_view=True)
        with uc_fund_exp:
            st.caption(
                "Drawdown is the blind-pool commitment still to be called (the named "
                "follow-ons are the row above it); its return lands after the assumed hold at "
                "the assumed MOIC. Both are shown as they hit the fund, negative when called."
            )

        # ---------------- How fees and carry are calculated ----------------
        st.markdown("### How fees and carried interest are calculated")
        if not apply_fees:
            st.info("Fees and carry are switched off in the sidebar, so there is nothing to show here.")
        else:
            SERIES_1 = "#2a78d6"   # blue   -- validated categorical slot 1
            SERIES_2 = "#eb6834"   # orange -- validated categorical slot 2
            GRID = "rgba(0,0,0,0.08)"

            # ---- Management fee ----
            st.markdown("**Management fee**")
            if use_two_tier_fee:
                st.write(
                    f"Two rates on two different bases. Through forecast year "
                    f"{crossover_year - 1} the fee is **{fee_rate_initial*100:.1f}% of committed "
                    f"capital** — it does not matter how much is actually invested. From year "
                    f"{crossover_year} the basis switches to **{fee_rate_post*100:.1f}% of the "
                    f"cost still in the ground**, which shrinks as companies exit. That is why "
                    f"the bars below step down rather than staying flat."
                )
                fee_basis_rows = [
                    {"Year": as_of.year + int(r["Year"]),
                     "Basis": ("Committed capital" if int(r["Year"]) < crossover_year
                               else "Remaining cost"),
                     "Fee basis": r["Fee basis ($)"] / lp_pct / 1_000_000 if lp_pct else 0.0,
                     "Fee": r["Fee ($)"] / lp_pct / 1_000_000 if lp_pct else 0.0}
                    for _, r in fee_schedule_display.iterrows()
                ] if fee_schedule_display is not None else []
            else:
                st.write(
                    f"A flat **{mgmt_fee*100:.1f}% a year on the portfolio's value**, charged "
                    "before anything is distributed. The basis is the NAV itself, so the fee "
                    "falls away naturally as the portfolio is realised — the bars below are "
                    "that year's value being charged on."
                )
                fee_basis_rows = [
                    {"Year": as_of.year + r["year"], "Basis": "Portfolio value (NAV)",
                     "Fee basis": _fund(r.get("grown_nav", 0.0)),
                     "Fee": _fund(r.get("mgmt_fee", 0.0))}
                    for r in forecast_rows
                ]

            if fee_basis_rows:
                fb = pd.DataFrame(fee_basis_rows)
                fig_fee = go.Figure()
                # One bar trace per basis, so the change of basis is a change of colour and
                # carries a legend entry -- identity is never colour alone.
                for i, basis in enumerate(fb["Basis"].unique()):
                    part = fb[fb["Basis"] == basis]
                    fig_fee.add_bar(
                        x=part["Year"], y=part["Fee basis"], name=basis,
                        marker_color=[SERIES_1, SERIES_2][i % 2],
                        text=[f"fee {v:,.1f}" for v in part["Fee"]], textposition="outside",
                        hovertemplate="%{x}<br>Basis %{y:,.1f}mm<br>%{text}mm<extra></extra>",
                    )
                fig_fee.update_layout(
                    title="What the fee is charged on, and the fee it produces ($mm, fund level)",
                    yaxis=dict(title="Fee basis ($mm)", gridcolor=GRID, zerolinecolor=GRID),
                    xaxis=dict(title="", gridcolor=GRID),
                    plot_bgcolor="rgba(0,0,0,0)", bargap=0.35, height=340,
                    legend=dict(orientation="h", y=-0.18),
                )
                st.plotly_chart(fig_fee, width="stretch")
                st.caption(
                    "The bar is the basis; the number above it is the fee that basis produces. "
                    "They are shown on one scale rather than two axes -- a fee is a thin slice "
                    "of its basis, and plotting both on their own axes would make them look "
                    "comparable when they are not."
                )

            # ---- Carried interest ----
            st.markdown("**Carried interest**")
            rf_style = "declining" if waterfall_style == "Declining hurdle balance" else "compounded"
            rollf = carry_rollforward(forecast_rows, to_date.paid_in, to_date.distributions,
                                       hurdle_rate, style=rf_style)
            cleared = next((r for r in rollf if r["hurdle_cleared"]), None)
            total_carry = sum(r["carry_in_year"] for r in rollf)
            capital_base = max(0.0, to_date.paid_in - to_date.distributions)

            if rf_style == "declining":
                mechanic = (
                    f"A running balance starts at the **${capital_base/1_000_000:,.2f}mm of the "
                    f"LP's capital not yet returned**, grows by the {hurdle_rate*100:.1f}% "
                    "preferred return each year, and shrinks as distributions are applied to it. "
                    "The GP earns nothing while that balance is open."
                )
            else:
                mechanic = (
                    f"Every historical capital call is compounded forward at "
                    f"{hurdle_rate*100:.1f}% to give a single target. The GP earns nothing until "
                    "cumulative LP distributions cross it."
                )
            if cleared is not None:
                when = (f"On this forecast that happens in **{as_of.year + cleared['year']}**, "
                        f"after which the GP takes {carry_rate*100:.0f}% of everything above the "
                        f"line — **${total_carry/1_000_000:,.2f}mm** in total (LP's share of the "
                        "fund, since carry is charged on this LP's own cash flows).")
            else:
                when = ("On this forecast that never happens inside the horizon, so **no carry "
                        "is taken at all** — every dollar projected goes to the LP.")
            st.write(mechanic + " " + when)

            rf_df = pd.DataFrame(rollf)
            rf_df["Year"] = [as_of.year + r["year"] for r in rollf]
            fig_c = go.Figure()
            fig_c.add_scatter(
                x=rf_df["Year"], y=rf_df["cumulative_distributions"] / 1_000_000,
                name="Cumulative distributions", mode="lines+markers",
                line=dict(color=SERIES_1, width=2), marker=dict(size=8),
                hovertemplate="%{x}<br>Cumulative distributions %{y:,.2f}mm<extra></extra>",
            )
            hurdle_line = ((rf_df["cumulative_distributions"] + rf_df["hurdle_balance_closing"])
                           / 1_000_000)
            fig_c.add_scatter(
                x=rf_df["Year"], y=hurdle_line,
                name="Capital + preferred still to be covered", mode="lines+markers",
                line=dict(color=SERIES_2, width=2, dash="dash"), marker=dict(size=8),
                hovertemplate="%{x}<br>Hurdle line %{y:,.2f}mm<extra></extra>",
            )
            if cleared is not None:
                cy = as_of.year + cleared["year"]
                fig_c.add_vline(x=cy, line_width=1, line_dash="dot", line_color=SERIES_2)
                fig_c.add_annotation(x=cy, yref="paper", y=1.0, text="hurdle cleared -> carry starts",
                                      showarrow=False, yshift=8, font=dict(size=11))
            fig_c.update_layout(
                title="When the GP starts earning carry ($mm, LP's share)",
                yaxis=dict(title="$mm", gridcolor=GRID, zerolinecolor=GRID),
                xaxis=dict(title="", gridcolor=GRID),
                plot_bgcolor="rgba(0,0,0,0)", height=340,
                legend=dict(orientation="h", y=-0.18),
            )
            st.plotly_chart(fig_c, width="stretch")
            st.caption(
                "The two lines meet when the LP has been made whole on capital and preferred "
                "return. Carry is zero to the left of that point no matter how large the "
                "distributions are — that is what makes it a European, whole-of-fund waterfall."
            )

            with st.expander("Year-by-year workings"):
                disp = pd.DataFrame([{
                    "Year": as_of.year + r["year"],
                    "Distribution": r["distribution"],
                    "Hurdle balance, opening": r["hurdle_balance_opening"],
                    "Preferred return accrued": r["preferred_accrued"],
                    "Applied to capital + pref": r["applied_to_capital_and_pref"],
                    "Hurdle balance, closing": r["hurdle_balance_closing"],
                    "Cumulative distributions": r["cumulative_distributions"],
                    "Cumulative preferred": r["cumulative_preferred"],
                    "Carry entitlement, cumulative": r["carry_entitlement_cumulative"],
                    "Carry in year": r["carry_in_year"],
                } for r in rollf])
                st.dataframe(
                    disp.style.format({c: "${:,.0f}" for c in disp.columns if c != "Year"}),
                    width="stretch", hide_index=True,
                )
                st.caption(
                    "LP-level dollars, the basis the waterfall actually runs on. 'Carry in year' "
                    "is read straight off the waterfall that produced the forecast, so this "
                    "table can never disagree with the numbers above it."
                )


with tab_ai:
    st.write("Ask the AI agent about this fund's metrics, forecast, or pricing.")
    question = st.text_area("Question", placeholder="e.g. What discount to NAV is needed for a 20% IRR?")
    if st.button("Ask") and question:
        with st.spinner("Thinking..."):
            answer = ask_agent(question, metrics_context, api_key=api_key)
        st.markdown(answer)

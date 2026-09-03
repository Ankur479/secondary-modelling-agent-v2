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
    "Fund total commitment ($)", min_value=0.01, value=1_014_700_000.0, step=1_000_000.0, format="%.0f"
)
lp_commitment = st.sidebar.number_input(
    "Selling LP's commitment ($)", min_value=0.0, value=33_500_000.0, step=100_000.0, format="%.0f"
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
as_of = st.sidebar.date_input("As-of date", value=date(2025, 12, 31))
st.sidebar.caption(
    "Forecast year 1 is the calendar year after this date, so with the default as-of date "
    "the projection columns run 2026 onward -- the same grid the asset models use."
)

if forecast_mode == "Aggregate NAV (simple)":
    nav_current = st.sidebar.number_input(
        "Current NAV / Reported Value ($)", min_value=0.0, value=657_300_000.0, step=1_000_000.0, format="%.0f"
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
else:
    gross_return = None
    shape = None
    st.sidebar.caption(
        "Every per-company input -- cost, marks, exit timing and the full operating build -- "
        "lives in the **Asset Model (Bottom-up)** tab, one section per company. Add or remove "
        "companies there too."
    )


# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------
# Created up front because the Asset Model tab is rendered FIRST, before the core
# calculations below: each asset's bottom-up build is what sets that company's
# expected return, so its inputs have to be read before the fund-level forecast
# runs. Streamlit lets a tab be written into at any point in the script, so the
# remaining tabs are filled in further down, after the numbers exist.
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
    ["Overview", "Cash Flow Forecast", "Investments", "Secondary Pricing",
     "Asset Model (Bottom-up)", "AI Assistant"]
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

if forecast_mode == "Portfolio companies (detailed)":
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

if forecast_mode == "Portfolio companies (detailed)":
    for _aid in st.session_state["asset_ids"]:
        for _f in PER_ASSET_FIELDS:
            st.session_state.setdefault(f"am_{_aid}_{_f}", _asset_default(_aid, _f))


# Unfunded assumptions are read up here (to price post-report investments) but their widgets
# live in the sidebar further down, so they are seeded the same way. These are seeded in
# BOTH forecast modes -- a keyed checkbox that first renders unseeded would latch on to
# False and stay there when the mode is switched.
st.session_state.setdefault("unfunded_generates_return", True)
st.session_state.setdefault("unfunded_hold_years", 4)
st.session_state.setdefault("unfunded_moic", 1.75)
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

if forecast_mode == "Portfolio companies (detailed)":
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


with tab3:
    st.subheader("Investments")
    if forecast_mode != "Portfolio companies (detailed)":
        st.info(
            "Switch **Forecast mode** to *Portfolio companies (detailed)* in the sidebar to see "
            "the investment schedule. Aggregate NAV mode has no per-company detail to show."
        )
    else:
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

        _gen = bool(st.session_state.get("unfunded_generates_return", True))
        _hold = int(st.session_state.get("unfunded_hold_years", 4))
        _moic = float(st.session_state.get("unfunded_moic", 1.75))

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


with tab5:
    st.subheader("Asset Model (Bottom-up)")
    if forecast_mode != "Portfolio companies (detailed)":
        st.info(
            "Switch **Forecast mode** to *Portfolio companies (detailed)* in the sidebar to "
            "model each holding bottom-up. In Aggregate NAV mode the fund is projected off a "
            "single NAV and runoff curve instead, so there are no individual assets to build."
        )
    else:
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
# Remaining sidebar inputs
# --------------------------------------------------------------------------
# These sit here, after the Asset Model tab, because their controls depend on the
# forecast horizon (remaining_years), which in portfolio mode is only known once
# every company's exit year has been read above. st.sidebar.* writes to the sidebar
# from anywhere in the script, so they still render in order under section 3.
st.sidebar.header("4. Unfunded commitment")
st.sidebar.caption(
    "Known follow-on investments are entered in the **Investments** tab, under Post-Report "
    "Investments. What's left below applies to them and to the blind pool."
)
blind_pool_amount = st.sidebar.number_input(
    "Blind pool (unidentified future calls, $)", min_value=0.0, value=374_800_000.0, step=500_000.0, format="%.0f"
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
    "Unfunded commitment generates its own return", key="unfunded_generates_return",
    help="A capital call doesn't just sit as an outflow -- it funds a new investment that "
         "itself goes on to return money. Turn this on to project a return on every future "
         "call, a fixed hold period and MOIC after which it lands.",
)
if unfunded_generates_return:
    uc1, uc2 = st.sidebar.columns(2)
    unfunded_hold_years = uc1.number_input("Hold period (years)", min_value=1, step=1,
                                           key="unfunded_hold_years")
    unfunded_moic = uc2.number_input("Assumed MOIC (x)", min_value=0.1, step=0.05, format="%.2f",
                                     key="unfunded_moic")
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

use_two_tier_fee = False
crossover_year = 1
fee_rate_initial = 0.0
fee_rate_post = 0.0
if apply_fees and forecast_mode == "Portfolio companies (detailed)":
    fee_structure = st.sidebar.radio(
        "Management fee basis",
        ["Flat annual fee (% of NAV)", "Two-tier: flat on commitment, then step-down on remaining cost"],
        index=0,
        help="Flat: the classic simple case, a single rate charged on the year's grown NAV "
             "(what the 'Annual management fee' slider below controls). Two-tier: mirrors a "
             "common real fund schedule -- a flat rate on the FUND'S TOTAL COMMITMENT during "
             "the investment period, then from a chosen crossover year onward, a (usually "
             "lower) rate on each company's remaining invested COST basis, which shrinks as "
             "companies exit. Requires a Cost Basis ($M) per company in the table above.",
    )
    use_two_tier_fee = fee_structure.startswith("Two-tier")
    if use_two_tier_fee:
        fc1, fc2 = st.sidebar.columns(2)
        fee_rate_initial = fc1.number_input(
            "Fee rate, investment period (% of commitment)", min_value=0.0, max_value=5.0,
            value=1.9, step=0.1, format="%.1f",
        ) / 100
        fee_rate_post = fc2.number_input(
            "Fee rate, post-crossover (% of remaining cost)", min_value=0.0, max_value=5.0,
            value=1.9, step=0.1, format="%.1f",
        ) / 100
        crossover_year = st.sidebar.slider(
            "Crossover year (step-down starts here)", 1, max(1, remaining_years),
            min(2, max(1, remaining_years)),
            help="Forecast year in which the fee basis switches from total commitment to "
                 "remaining cost basis. Typically the end of the fund's investment period.",
        )
        st.sidebar.caption(
            "Before the crossover year: fee = rate x fund's total commitment (LP share). "
            "From the crossover year on: fee = rate x that year's remaining cost basis "
            "(sum of Cost Basis for companies not yet exited -- a company still counts in "
            "full during its own exit year, then drops out the year after)."
        )
    mgmt_fee = st.sidebar.slider(
        "Annual management fee (%)", 0.0, 5.0, 2.0, step=0.25,
        help="Used when 'Flat annual fee' is selected above.",
    ) / 100
else:
    mgmt_fee = st.sidebar.slider("Annual management fee (%)", 0.0, 5.0, 2.0, step=0.25) / 100
    if apply_fees:
        st.sidebar.caption(
            "Two-tier (step-down on remaining cost) fee basis is available in Portfolio "
            "companies (detailed) mode, since it needs each company's cost basis."
        )
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

asset_model_details = []  # per-company bottom-up build detail, for display
fee_schedule_display = None  # two-tier management fee schedule, for tab2 display
if forecast_mode == "Aggregate NAV (simple)":
    forecast_rows = forecast_cashflows(
        nav_current_lp, remaining_years, gross_return, shape, as_of,
        mgmt_fee_rate=mgmt_fee if apply_fees else 0.0,
    )
    portfolio_display = None
else:
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
# Tab content (the tabs themselves were created above the Asset Model section)
# --------------------------------------------------------------------------
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
                "Cost Basis (Fund)": "${:,.0f}", "Reported Value (Fund)": "${:,.0f}",
                "Market Value (Fund)": "${:,.0f}", "Market Value (LP)": "${:,.0f}",
                "Unrealized MOIC (RV/Cost)": "{:.2f}x", "MV Adjustment": "{:+.0%}", "Return used": "{:+.1%}",
            }),
            width="stretch",
        )
        if fee_schedule_display is not None:
            with st.expander("Two-tier management fee schedule"):
                st.caption(
                    f"Investment period (years 1-{crossover_year - 1}): {fee_rate_initial*100:.1f}% of "
                    f"the LP's committed capital (${lp_commitment:,.0f}). From year {crossover_year} on: "
                    f"{fee_rate_post*100:.1f}% of that year's remaining cost basis -- shrinking as "
                    "companies exit."
                )
                st.dataframe(
                    fee_schedule_display.style.format({"Fee rate": "{:.2%}", "Fee basis ($)": "${:,.0f}", "Fee ($)": "${:,.0f}"}),
                    width="stretch", hide_index=True,
                )
        if asset_model_details:
            with st.expander("What each asset model is feeding into this forecast"):
                st.caption(
                    "Summary only -- the full build for each company (Deal Snapshot through "
                    "Cash Flow to Fund Model) lives in the **Asset Model (Bottom-up)** tab."
                )
                am_df = pd.DataFrame(asset_model_details)
                st.dataframe(
                    am_df.style.format({
                        "gross_proceeds_to_fund": "${:,.0f}",
                        "exit_proceeds_to_fund_lp": "${:,.0f}",
                        "implied_return": "{:+.1%}",
                        "gross_moic_vs_cost": "{:.2f}x",
                        "gross_irr_annualised": "{:.1%}",
                    }),
                    width="stretch", hide_index=True,
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
        if use_two_tier_fee:
            fee_caption = (
                f"Net of a two-tier management fee ({fee_rate_initial*100:.1f}% of commitment "
                f"through year {crossover_year - 1}, then {fee_rate_post*100:.1f}% of remaining "
                f"cost basis from year {crossover_year} on) and {carry_rate*100:.0f}% carried "
                f"interest above an {hurdle_rate*100:.1f}% preferred return."
            )
        else:
            fee_caption = (
                f"Net of a {mgmt_fee*100:.1f}% annual management fee and {carry_rate*100:.0f}% "
                f"carried interest above an {hurdle_rate*100:.1f}% preferred return."
            )
        st.caption(fee_caption)
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

# --------------------------------------------------------------------------
# Investments tab: the blind-pool footnote
# --------------------------------------------------------------------------
# The schedules themselves were rendered above, before the per-asset widgets. Only
# this note has to wait, because the blind pool is a sidebar input that renders later.
if forecast_mode == "Portfolio companies (detailed)" and blind_pool_amount > 0:
    with inv_expanders["post_fund"]:
        st.caption(
            f"Separately, ${blind_pool_amount:,.0f} of blind-pool commitment (fund level) is "
            f"drawn over {blind_pool_years} year(s). It isn't a line here because it isn't a "
            "named investment yet -- it flows through the Cash Flow Forecast."
        )

with tab4:
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

# --------------------------------------------------------------------------
# Analytics & Pricing Bridge -- appended to the Overview tab (Streamlit lets a tab
# container be written into more than once, so this lands under the metrics and
# cash-flow chart already rendered in tab1 above). It sits here in the script
# rather than up there because it needs the pricing scenarios computed in between.
# --------------------------------------------------------------------------
with tab1:
    st.divider()
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

with tab6:
    st.write("Ask the AI agent about this fund's metrics, forecast, or pricing.")
    question = st.text_area("Question", placeholder="e.g. What discount to NAV is needed for a 20% IRR?")
    if st.button("Ask") and question:
        with st.spinner("Thinking..."):
            answer = ask_agent(question, metrics_context, api_key=api_key)
        st.markdown(answer)

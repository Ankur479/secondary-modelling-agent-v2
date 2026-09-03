# Secondary Market Modelling Agent

An AI agent for **PE secondaries (LP stake) modelling**, packaged as a Streamlit app.

## What it does

1. **Metrics to date** — from historical capital calls/distributions + current NAV, computes
   paid-in capital, DPI, RVPI, TVPI, and IRR to date (XIRR on actual dates).
2. **Forward runoff forecast** — projects the fund's remaining NAV and yearly distributions
   over its remaining life, using a configurable runoff shape (back-ended / even / front-ended)
   and an assumed gross return on the unrealized book.
3. **Secondary pricing** — for a range of discounts/premiums to NAV, computes the buyer's
   implied IRR and MOIC, so you can see what price maps to what return.
4. **AI Assistant** — a chat tab where you can ask natural-language questions ("what discount
   is needed for a 20% IRR?", "is this a good deal?"). It answers grounded in the exact numbers
   the engine computed. Works with an Anthropic API key (full LLM reasoning) or without one
   (deterministic rule-based summary), so the demo never breaks.
5. **Fund vs. LP ownership** — a secondary prices ONE LP's slice of the fund, not the whole
   fund. Enter the fund's total commitment and the selling LP's commitment; every fund-level $
   figure you enter elsewhere (cash flows, NAV, accrued carry, unfunded amounts) is scaled down
   to the LP's share automatically before anything downstream runs.
6. **Unfunded commitment generates its own return** (optional) — a future capital call isn't
   just an outflow; it funds a new investment that itself returns money. Turn this on to
   project a return (hold period + MOIC) on every future call.
7. **Asset Model (Bottom-up)** — its own tab, and the only place per-company inputs live: one
   full model per holding, laid out like a real per-asset tab — Deal Snapshot (all editable:
   name, investment date, cost, reported value, MV adjustment, exit year), Entry Assumptions,
   Operating Projections ($mm, with per-year revenue growth / EBITDA margin / FCF conversion),
   Exit Valuation, Returns & Tie-Out, and Cash Flow to Fund Model. Add or remove companies with
   one click. Each model backs out the annualized return that drives that company in the fund
   forecast, and can be switched off per company to fall back to a flat Expected Return %.
   Fund NAV and the forecast horizon are derived from these snapshots, so there is no second
   table to keep in sync.
8. **Leverage overlay** (optional) — model the buyer financing part of the purchase price with
   a subscription-line/NAV facility, shown alongside (never in place of) the unlevered return.
9. **Declining-balance waterfall + Analytics & Pricing Bridge** (optional) — an alternate,
   equally valid European waterfall mechanic (a running hurdle balance that accrues and shrinks,
   vs. the default compounded-threshold test), plus an analytics block (in the Overview tab)
   covering Gross-vs-Net performance, MV CAGR, cash-flow duration, RV/MV multiple, and MOIC
   adjusted vs. unadjusted for interim cash flows.
10. **Investments tab** — the fund model's two investment blocks, in its own column order:
    Current Investments (Company Name, Inv. Date, % of RV, % of MV, Cost, RV, MV Adjustment, MV,
    a column per forecast year, Proceeds) and Post-Report Investments below it, each with a
    Fund-level and an LP-level dropdown. The LP view carries LP Cost / LP RV / LP MV / LP
    Proceeds / LP MOIC — the same schedule at the selling LP's ownership. Each Fund-level
    view is **one editable schedule**, not an input table plus an output table: white columns
    take input, shaded ones are computed from it. Set a count and press Add to get that many
    rows in a single click, or delete rows in place. Those edits are the same store the Asset
    Model tab's Deal Snapshot writes to, so editing in either place updates both, and the Fund
    and LP views are two renderings of one list — their company counts can never diverge.
11. **Two-tier ("step-down") management fee** (optional, Portfolio companies mode) — mirrors a
    common real fund schedule: a flat rate on the fund's total commitment during the investment
    period, then from a chosen crossover year onward, a (usually lower) rate on each company's
    remaining invested cost basis, which shrinks as companies exit. The default flat fee-on-NAV
    stays available and is still the default.

## Files

- `app.py` — Streamlit UI (6 tabs: Overview *(incl. Analytics & Pricing Bridge)*, Cash Flow
  Forecast, Investments, Secondary Pricing, Asset Model (Bottom-up), AI Assistant)
- `finance_engine.py` — core calculations (XIRR, runoff model, pricing sensitivity, waterfalls,
  bottom-up asset model, two-tier management fee)
- `ai_agent.py` — AI agent wrapper (Claude + fallback)
- `sample_fund_cashflows.csv` — sample fund history so the app runs out of the box

The app's defaults are a real secondary: a 2022-vintage $1,014.7mm fund, a $33.5mm selling LP
(3.30%), five current investments at $426.4mm cost / $657.3mm reported value, and the asset
models reproduce that deal's per-company builds line for line (`test_v5_asset_model.py` pins
them to the source workbook's own computed values).

## Run it

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then open the local URL Streamlit prints. Toggle "Use sample data" off in the sidebar to upload
your own fund's cash flow CSV (columns: `Date, Capital_Call, Distribution`).

To enable the full AI assistant, paste an Anthropic API key into the sidebar field (or set the
`ANTHROPIC_API_KEY` environment variable before launching).

## Presenting this to investors

Suggested flow for a demo:

1. **Overview tab** — show the fund's track record (TVPI, IRR to date) as the starting point.
2. **Cash Flow Forecast tab** — show how the agent projects the remaining runoff, and flex the
   assumptions sliders live to show sensitivity.
3. **Secondary Pricing tab** — the core pitch: show the IRR/MOIC a buyer gets at different
   discounts to NAV, so a price can be defended with numbers, not gut feel.
4. **Asset Model tab** — drill into any single company: its operating build, exit valuation, and
   the tie-out back to what the fund model previously showed. This is where a sceptical buyer's
   "where does that number come from?" gets answered.
5. **AI Assistant tab** — ask it a live question in front of the room to show it reasons over
   the actual model output rather than giving generic answers.

The Overview tab also carries the Gross-vs-Net bridge and timing analytics, so the headline
story and the "why" sit on one screen.

Key assumptions to be upfront about: the gross return and runoff shape are analyst inputs, not
predictions — frame the tool as a transparent, adjustable pricing calculator plus an AI layer
that explains the output, not a black-box forecaster.

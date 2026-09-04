# Secondary Market Modelling Agent

A deal-modelling tool for **PE secondaries (LP stake) transactions**, built as a Streamlit app.

It follows the order a secondaries analyst actually works in: set the terms, build each asset
from the bottom up, read the investment schedule that falls out of it, and watch the price move
while you do — the pricing panel is pinned to the sidebar, so it is never more than a glance away.

## The four tabs

**Deal Terms** — the term sheet, in the two blocks a fund model uses. *Deal Terms*: currency,
report date, market value date, closing date. *Fund Terms*: vintage, carried interest, preferred
return, commitments (fund and the selling LP's, with the resulting ownership %), the cash-flow
history, the position (funded, expired, unfunded, net cash, accrued carry, GP commitment),
management fee basis, waterfall style, the GP's reported gross performance, unfunded
assumptions, and the credit facility. Everything the model runs on is entered here.

**Investments** — four blocks, each with a Fund-level and an LP-level view.
*Current Investments* and *Post-Report Investments* are editable schedules: white columns take
input, shaded ones are computed, and a count box adds as many rows as you ask for in one click.
*Funded Commitment* and *Unfunded Commitment* then walk those down to net proceeds, line for
line — Current Investments, Net Cash, Management fees, Proceeds after management fees, Future
Carried Interest, Accrued Carried Interest, Tax Blocker Leakage, Proceeds after carry; then
Post-Report Date Investments, their carry, Drawdown on remaining unfunded, Return on Remaining
Unfunded. Below them, fees and carry are explained three ways: a sentence built from the live
numbers, a chart of what the fee is charged on and when the hurdle clears, and the year-by-year
workings for anyone checking it line by line.

**Asset Model (Bottom-up)** — one full model per holding, laid out like a per-asset tab: Deal
Snapshot, Entry Assumptions, Operating Projections ($mm, with per-year revenue growth, EBITDA
margin and FCF conversion), Exit Valuation, Returns & Tie-Out, Cash Flow to Fund Model. Each
model backs out the annualised return that drives its company in the fund forecast, and can be
switched off per company to fall back to a flat expected return.

**Voice control (sidebar)** — speak a change instead of typing it: *"discount fifteen
percent"*, *"carry twenty"*, *"asset D exit year 2031"*, *"asset A cost ninety"*. Useful in
the moment it was built for — flexing assumptions on a call or in front of a client without
looking away from the screen. Three things make it safe to use on a live model: it is scoped
to the handful of knobs that actually get flexed, so it can't wander into the wrong field; it
refuses anything it isn't sure of rather than guessing (no number, unknown field, out-of-range
value, a year not spoken in full); and it never changes anything silently — what it heard and
what it changed are shown with an Undo. Assets answer to their letter as well as their name,
so a deal can be flexed out loud without saying the company or the fund. Audio is sent away
for transcription, so treat it like a browser search box; if the package isn't installed the
feature simply hides and everything else works.

**AI Assistant** — ask a question in plain language; it answers from the exact numbers the
engine computed. Works with an Anthropic API key, or without one via a deterministic summary.

## The pricing panel (sidebar)

Always visible: Net Effective Price, Market Value, Reported Value, Unfunded, Gross Exposure,
Gross Distributions, Net Funded Exposure and Net Proj. Proceeds, in three columns — what the LP
holds, what the buyer effectively pays, and the premium or discount that implies. Quote a
premium or discount and everything reprices at once, with the buyer's IRR and MOIC beside it and
a ladder of alternative prices underneath.

## How the pieces stay honest

- **One store, two editors.** A company's cost, marks and exit year can be edited in the
  Investments grid or in its Deal Snapshot; both write to the same state, so they cannot drift.
- **Compute, then render.** Every asset model is built before any widget draws, which is what
  lets one grid show inputs and results together and keeps the two tabs from computing different
  answers from the same inputs.
- **The blocks present the forecast, they don't recompute it.** "Proceeds after management fees"
  *is* the distribution the pricing panel uses — asserted by test.
- **The explanation reads carry off the waterfall** that produced the forecast, so the workings
  can never contradict the numbers they explain.

## Files

- `app.py` — the Streamlit UI
- `finance_engine.py` — XIRR, the runoff and portfolio forecasts, both waterfall mechanics and
  their roll-forward, the bottom-up asset model, the two-tier management fee, unfunded schedules,
  leverage overlay and secondary pricing
- `ai_agent.py` — AI agent wrapper (Claude + deterministic fallback)
- `sample_fund_cashflows.csv` — sample fund history so the app runs out of the box
- `voice_commands.py` — the spoken-command grammar (pure Python, no Streamlit)
- `smoke_test_app.py` — 65 behaviour checks driven through the real widgets
- `test_*.py` — engine tests, including `test_v5_asset_model.py`, which pins every asset to the
  source workbook's own recalculated values, and `test_v6_rollforward.py` for the carry workings

The defaults are a real secondary: a 2022-vintage $1,014.7mm fund, a $33.5mm selling LP (3.30%),
five current investments at $426.4mm cost and $657.3mm reported value.

## Run it

```bash
pip install -r requirements.txt
streamlit run app.py
```

Toggle "Use sample data" off in Deal Terms to upload your own cash-flow CSV
(`Date, Capital_Call, Distribution`). Paste an Anthropic API key in the AI Assistant tab to
enable full LLM reasoning.

Key assumptions to be upfront about with a client: the operating assumptions, exit timing and
unfunded return are analyst inputs, not predictions. The tool is a transparent, adjustable
pricing model that shows its workings — not a black-box forecaster.

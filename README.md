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
7. **Bottom-up EV/EBITDA company model** (optional, Portfolio companies mode) — instead of a
   flat "Expected Return %" per company, build each company's own Revenue -> EBITDA -> FCF ->
   Exit EV/EBITDA model; the app backs out the equivalent annualized return from it.
8. **Leverage overlay** (optional) — model the buyer financing part of the purchase price with
   a subscription-line/NAV facility, shown alongside (never in place of) the unlevered return.

## Files

- `app.py` — Streamlit UI (4 tabs: Overview, Cash Flow Forecast, Secondary Pricing, AI Assistant)
- `finance_engine.py` — core calculations (XIRR, runoff model, pricing sensitivity)
- `ai_agent.py` — AI agent wrapper (Claude + fallback)
- `sample_fund_cashflows.csv` — sample fund history so the app runs out of the box

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
4. **AI Assistant tab** — ask it a live question in front of the room to show it reasons over
   the actual model output rather than giving generic answers.

Key assumptions to be upfront about: the gross return and runoff shape are analyst inputs, not
predictions — frame the tool as a transparent, adjustable pricing calculator plus an AI layer
that explains the output, not a black-box forecaster.

"""
Core financial engine for PE secondary (LP stake) modelling.

Covers three things an LP secondaries desk actually needs:
1. Metrics to date (DPI / RVPI / TVPI / IRR) from historical cash flows + current NAV.
2. A forward runoff forecast of the fund's remaining NAV and distributions.
3. Secondary pricing: buyer IRR / MOIC at different discounts (or premiums) to NAV.
"""
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# IRR (XIRR) on irregularly dated cash flows
# ---------------------------------------------------------------------------

def xnpv(rate: float, cashflows: List[Tuple[date, float]]) -> float:
    """Net present value of dated cash flows at a given annual rate."""
    if rate <= -1.0:
        rate = -0.999999
    t0 = cashflows[0][0]
    return sum(cf / (1 + rate) ** ((d - t0).days / 365.0) for d, cf in cashflows)


def xirr(cashflows: List[Tuple[date, float]]) -> float:
    """Annualized IRR of dated cash flows, solved by bisection (robust, no external deps)."""
    cashflows = sorted(cashflows, key=lambda x: x[0])
    lo, hi = -0.9999, 10.0
    f_lo, f_hi = xnpv(lo, cashflows), xnpv(hi, cashflows)
    if f_lo * f_hi > 0:
        return float("nan")  # no sign change in range -> can't bracket a root
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = xnpv(mid, cashflows)
        if abs(f_mid) < 1e-6:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


# ---------------------------------------------------------------------------
# Metrics to date
# ---------------------------------------------------------------------------

@dataclass
class FundToDate:
    paid_in: float
    distributions: float
    nav: float
    dpi: float
    rvpi: float
    tvpi: float
    irr: float


def fund_metrics_to_date(cf_dates: List[date], calls: List[float], dists: List[float],
                          nav_current: float, as_of: date) -> FundToDate:
    paid_in = sum(calls)
    distributions = sum(dists)
    dpi = distributions / paid_in if paid_in else 0.0
    rvpi = nav_current / paid_in if paid_in else 0.0
    tvpi = dpi + rvpi

    cashflows = [(d, -c) for d, c in zip(cf_dates, calls) if c] + \
                [(d, dist) for d, dist in zip(cf_dates, dists) if dist] + \
                [(as_of, nav_current)]
    irr = xirr(cashflows)

    return FundToDate(paid_in, distributions, nav_current, dpi, rvpi, tvpi, irr)


# ---------------------------------------------------------------------------
# Forward runoff forecast
# ---------------------------------------------------------------------------

def build_runoff_curve(n_years: int, shape: str = "back_ended") -> List[float]:
    """
    Yearly distribution rate applied to the (grown) declining NAV balance.
    Guaranteed to fully liquidate the position by the final year.
    `shape` controls whether distributions are weighted toward the early
    years (front_ended), spread evenly (even), or weighted toward the later
    years (back_ended) -- typical of a PE harvest period.
    """
    def cum_frac(t: int) -> float:
        x = t / n_years
        if shape == "back_ended":
            return x ** 2
        elif shape == "front_ended":
            return 1 - (1 - x) ** 2
        return x  # even

    rates = []
    f_prev = 0.0
    for t in range(1, n_years + 1):
        f_t = cum_frac(t)
        remaining = 1 - f_prev
        rate = (f_t - f_prev) / remaining if remaining > 1e-9 else 1.0
        rates.append(min(rate, 1.0))
        f_prev = f_t
    rates[-1] = 1.0  # force full liquidation in the final year
    return rates


def forecast_cashflows(nav_current: float, remaining_years: int, gross_return: float,
                        shape: str, as_of: date, mgmt_fee_rate: float = 0.0) -> List[Dict]:
    """
    mgmt_fee_rate: annual management fee, charged on the grown NAV each year before
    distributions are carved out (a simplification of NAV-basis fee schedules common
    in later-stage/secondary funds).
    """
    rates = build_runoff_curve(remaining_years, shape)
    rows = []
    balance = nav_current
    for i, rate in enumerate(rates, start=1):
        beginning_nav = balance  # true start-of-year balance (= prior year's ending_nav)
        grown = beginning_nav * (1 + gross_return)
        fee = grown * mgmt_fee_rate
        grown_after_fee = grown - fee
        dist = grown_after_fee * rate
        balance = grown_after_fee - dist
        try:
            fdate = as_of.replace(year=as_of.year + i)
        except ValueError:
            fdate = as_of.replace(year=as_of.year + i, day=28)
        rows.append({
            "year": i,
            "date": fdate,
            "beginning_nav": beginning_nav,
            "grown_nav": grown,
            "mgmt_fee": fee,
            "gross_distribution": dist,
            "ending_nav": balance,
        })
    return rows


# ---------------------------------------------------------------------------
# Bottom-up (portfolio company level) forecast - alternative to the aggregate
# NAV + runoff-curve approach above, for when individual holdings have
# meaningfully different growth rates and exit timing.
# ---------------------------------------------------------------------------

def forecast_from_portfolio(companies: List[Dict], mgmt_fee_rate: float, as_of: date) -> List[Dict]:
    """
    companies: list of dicts with keys:
      name, current_value (the value the forecast compounds from - typically a buyer's
      diligence-adjusted Market Value, see reported_vs_market_value below),
      expected_return, exit_year_1, exit_pct_1, exit_year_2, exit_pct_2.

    Each company compounds at its own expected_return every year it's still held.
    exit_pct_1 is the fraction of that year's (post-fee) value distributed in
    exit_year_1; exit_pct_2 is the fraction of what's left distributed in
    exit_year_2. Setting exit_year_1 == exit_year_2 with exit_pct_1=0, exit_pct_2=1.0
    reproduces a single lump-sum exit; setting them apart with both percentages > 0
    models a phased/partial realization (e.g. a partial secondary sale of the
    position followed by a later full exit), which is common in real deals where a
    company's proceeds arrive across more than one year.

    Management fee is charged fund-level each year on the aggregate value of
    companies not yet fully exited, deducted proportionally from each held
    company's value.

    Returns rows in the same shape as forecast_cashflows (plus an 'exits' list per
    row for transparency), so apply_carry_waterfall/secondary_pricing work unchanged.
    """
    if not companies:
        return []
    max_year = max(max(int(c.get("exit_year_1", 1)), int(c.get("exit_year_2", 1))) for c in companies)
    max_year = max(1, max_year)
    values = {i: float(c["current_value"]) for i, c in enumerate(companies)}
    fully_exited = set()
    rows = []
    for t in range(1, max_year + 1):
        beginning_nav = sum(v for i, v in values.items() if i not in fully_exited)
        grown_vals = {}
        grown_total = 0.0
        for i, c in enumerate(companies):
            if i in fully_exited:
                continue
            gv = values[i] * (1 + float(c["expected_return"]))
            grown_vals[i] = gv
            grown_total += gv
        fee = grown_total * mgmt_fee_rate
        fee_ratio = ((grown_total - fee) / grown_total) if grown_total > 0 else 0.0
        distribution = 0.0
        exits_this_year = []
        for i, c in enumerate(companies):
            if i in fully_exited:
                continue
            net_val = grown_vals[i] * fee_ratio
            this_company_dist = 0.0
            if int(c.get("exit_year_1", 0)) == t and float(c.get("exit_pct_1", 0)) > 0:
                amt = net_val * float(c["exit_pct_1"])
                this_company_dist += amt
                net_val -= amt
            if int(c.get("exit_year_2", 0)) == t and float(c.get("exit_pct_2", 0)) > 0:
                amt = net_val * float(c["exit_pct_2"])
                this_company_dist += amt
                net_val -= amt
            distribution += this_company_dist
            if this_company_dist > 0:
                exits_this_year.append({"name": c.get("name", f"Company {i + 1}"), "value": this_company_dist})
            if net_val <= 1e-6:
                fully_exited.add(i)
                values[i] = 0.0
            else:
                values[i] = net_val
        ending_nav = sum(v for i, v in values.items() if i not in fully_exited)
        try:
            fdate = as_of.replace(year=as_of.year + t)
        except ValueError:
            fdate = as_of.replace(year=as_of.year + t, day=28)
        rows.append({
            "year": t,
            "date": fdate,
            "beginning_nav": beginning_nav,
            "grown_nav": grown_total,
            "mgmt_fee": fee,
            "gross_distribution": distribution,
            "ending_nav": ending_nav,
            "exits": exits_this_year,
        })
    return rows


def ebitda_exit_value(entry_revenue: float, entry_ebitda_margin: float, entry_ev_multiple: float,
                       entry_net_debt_ebitda: float, revenue_growth: float, fcf_conversion: float,
                       exit_year: int, exit_ev_multiple: float, fund_ownership_pct: float) -> Dict:
    """
    Bottom-up operating build for one portfolio company (mirrors a standalone
    EV/EBITDA asset model): Entry Revenue/EBITDA margin/EV multiple/Net Debt
    give the entry capital structure; the company then grows Revenue at a flat
    annual rate, holding its EBITDA margin flat, generating free cash flow
    (fcf_conversion x EBITDA) that pays down Net Debt dollar-for-dollar each
    year (floored at zero -- debt doesn't go negative into a net cash position
    here for simplicity). At exit_year, the year's EBITDA is re-rated at
    exit_ev_multiple to get Exit Enterprise Value, less that year's remaining
    Net Debt, to get Exit Equity Value; fund_ownership_pct converts that into
    the fund's Gross Proceeds.

    Returns a dict with the full year-by-year schedule plus the exit summary,
    so a caller can both show the build and use exit_proceeds_to_fund
    downstream (e.g. to back out an implied annualized return for
    forecast_from_portfolio -- see app.py).
    """
    entry_ebitda = entry_revenue * entry_ebitda_margin
    entry_ev = entry_ebitda * entry_ev_multiple
    entry_net_debt = entry_ebitda * entry_net_debt_ebitda
    entry_equity = entry_ev - entry_net_debt

    exit_year = max(1, int(exit_year))
    schedule = []
    revenue = entry_revenue
    net_debt = entry_net_debt
    exit_ebitda = entry_ebitda
    exit_net_debt = entry_net_debt
    for t in range(1, exit_year + 1):
        revenue = revenue * (1 + revenue_growth)
        ebitda = revenue * entry_ebitda_margin
        fcf = ebitda * fcf_conversion
        beginning_net_debt = net_debt
        net_debt = max(0.0, net_debt - fcf)
        schedule.append({
            "year": t, "revenue": revenue, "ebitda": ebitda, "fcf": fcf,
            "beginning_net_debt": beginning_net_debt, "ending_net_debt": net_debt,
        })
        if t == exit_year:
            exit_ebitda = ebitda
            exit_net_debt = net_debt

    exit_enterprise_value = exit_ebitda * exit_ev_multiple
    exit_equity_value = exit_enterprise_value - exit_net_debt
    exit_proceeds_to_fund = exit_equity_value * fund_ownership_pct

    return {
        "entry_ebitda": entry_ebitda,
        "entry_ev": entry_ev,
        "entry_net_debt": entry_net_debt,
        "entry_equity_value": entry_equity,
        "schedule": schedule,
        "exit_year": exit_year,
        "exit_ebitda": exit_ebitda,
        "exit_enterprise_value": exit_enterprise_value,
        "exit_net_debt": exit_net_debt,
        "exit_equity_value": exit_equity_value,
        "exit_proceeds_to_fund": exit_proceeds_to_fund,
    }


def implied_annual_return(current_value: float, exit_value: float, years: int) -> float:
    """
    Backs out the flat annualized growth rate that would take `current_value`
    to `exit_value` over `years` -- lets an EV/EBITDA-derived exit_proceeds_to_fund
    (see ebitda_exit_value above) plug straight into forecast_from_portfolio's
    'expected_return' compounding without changing that function at all: after
    `years` of compounding at this rate, current_value lands exactly on
    exit_value (before management fees, which forecast_from_portfolio still
    applies on top, same as the simple Expected-Return% mode).
    """
    if current_value <= 0 or years <= 0:
        return 0.0
    if exit_value <= 0:
        return -1.0  # total loss, compounds to zero
    return (exit_value / current_value) ** (1.0 / years) - 1.0


def cash_flow_duration(forecast_rows: List[Dict], distribution_key: str, as_of: date) -> float:
    """
    Weighted-average time (in years) until the projected distributions arrive --
    each year's distribution weighted by its distance from as_of, divided by total
    distributions. A standard duration measure; a low number means cash comes back
    fast, a high one means it's back-loaded. Returns 0.0 if there's nothing to weight
    (no positive distributions in the forecast).
    """
    total = 0.0
    weighted = 0.0
    for row in forecast_rows:
        amt = row.get(distribution_key, 0.0)
        if amt <= 0:
            continue
        years = (row["date"] - as_of).days / 365.0
        weighted += amt * years
        total += amt
    return weighted / total if total > 0 else 0.0


def reported_vs_market_value(reported_value: float, mv_adjustment: float) -> float:
    """
    Market Value = a buyer's own diligence-adjusted view of a holding's value,
    expressed as a +/- adjustment to the GP's officially Reported Value.
    E.g. mv_adjustment = -0.10 means the buyer marks the holding down 10% versus
    what the GP reports (common when a buyer has more current information on a
    pending sale process, for example).
    """
    return reported_value * (1 + mv_adjustment)


# ---------------------------------------------------------------------------
# Carried interest waterfall (European-style: return of capital -> preferred
# return / hurdle -> GP catch-up skipped for simplicity -> 80/20 carry split)
# ---------------------------------------------------------------------------

def hurdle_threshold(cf_dates: List[date], calls: List[float], hurdle_rate: float,
                      as_of: date) -> float:
    """
    Total amount an LP must have received by `as_of` to have earned exactly the
    hurdle rate on every capital call (each call compounds from its own call date).
    Below this cumulative total, LP gets 100% of distributions; above it, the GP
    starts participating via carried interest.
    """
    return sum(
        c * (1 + hurdle_rate) ** ((as_of - d).days / 365.0)
        for d, c in zip(cf_dates, calls) if c
    )


def apply_carry_waterfall(cf_dates: List[date], calls: List[float], distributions_to_date: float,
                           forecast_rows: List[Dict], hurdle_rate: float, carry_rate: float) -> List[Dict]:
    """
    Splits each forecast year's gross_distribution into an LP share and a GP
    carry share. Historical distributions to date are treated as already
    received by the LP (a simplifying assumption: no carry has crystallized
    yet as of the as-of date -- reasonable while the fund is still below its
    hurdle, which is the common case for a mid-life fund).
    """
    cum_lp = distributions_to_date
    out = []
    for row in forecast_rows:
        g = row["gross_distribution"]
        threshold = hurdle_threshold(cf_dates, calls, hurdle_rate, row["date"])
        if cum_lp >= threshold:
            lp_amt = g * (1 - carry_rate)
            gp_amt = g * carry_rate
        else:
            room = threshold - cum_lp
            lp_from_pref = min(g, room)
            remainder = g - lp_from_pref
            lp_amt = lp_from_pref + remainder * (1 - carry_rate)
            gp_amt = remainder * carry_rate
        cum_lp += lp_amt
        out.append({
            **row,
            "hurdle_threshold": threshold,
            "lp_distribution": lp_amt,
            "gp_carry": gp_amt,
        })
    return out


def apply_carry_waterfall_declining_balance(paid_in_to_date: float, distributions_to_date: float,
                                             forecast_rows: List[Dict], hurdle_rate: float,
                                             carry_rate: float, gp_catchup_rate: float = 0.0) -> List[Dict]:
    """
    An alternative to apply_carry_waterfall() above, matching a declining-balance
    hurdle mechanic some LPA waterfalls document instead of a single compounded
    point-in-time threshold: both are legitimate European-waterfall formulations,
    they just book the preferred return differently.

    - Capital Base = paid-in capital still owed back to the LP as of the as-of date
      (paid_in_to_date minus distributions_to_date, floored at 0). Distributions
      already received pre-as-of-date are assumed to have already cleared their own
      hurdle -- the same simplifying assumption apply_carry_waterfall makes.
    - A Hurdle Balance rolls forward year by year: it GROWS each year by the
      preferred return rate applied to itself (the balance still owed, like an
      amortizing loan accruing interest), and SHRINKS as that year's distribution is
      applied to it (capital, then preferred, in that priority) -- rather than being
      compared against a single static compounded target the way apply_carry_waterfall
      does.
    - Once the Hurdle Balance hits zero (capital + all preferred paid back), carry
      kicks in on distributions above the cumulative capital-plus-preferred line, at
      carry_rate. gp_catchup_rate (0.0-1.0, default 0%) lets the GP catch up faster
      once the hurdle clears, by reducing how much of the cumulative preferred return
      is credited back to the LP side of the entitlement calc -- 0% (this
      implementation's default, matching a template that had it turned off) means no
      catch-up, a straight carry_rate split above the hurdle from the first dollar.

    Returns rows shaped identically to apply_carry_waterfall's output (same keys,
    'hurdle_threshold' repurposed to mean the remaining Hurdle Balance each year) so
    it's a drop-in alternative wherever apply_carry_waterfall is used.
    """
    capital_base = max(0.0, paid_in_to_date - distributions_to_date)
    hurdle_balance = capital_base
    cum_dist = 0.0
    cum_pref = 0.0
    cum_carry_entitlement = 0.0
    out = []
    for row in forecast_rows:
        g = row["gross_distribution"]
        pref_accrued = hurdle_balance * hurdle_rate
        applied = min(hurdle_balance + pref_accrued, g)
        hurdle_balance = hurdle_balance + pref_accrued - applied
        cum_dist += g
        cum_pref += pref_accrued
        if hurdle_balance <= 1e-6:
            carry_cum_target = carry_rate * max(0.0, cum_dist - capital_base - (1 - gp_catchup_rate) * cum_pref)
        else:
            carry_cum_target = cum_carry_entitlement  # hurdle not cleared yet, no carry accrues
        gp_amt = carry_cum_target - cum_carry_entitlement
        cum_carry_entitlement = carry_cum_target
        lp_amt = g - gp_amt
        out.append({
            **row,
            "hurdle_threshold": hurdle_balance,
            "lp_distribution": lp_amt,
            "gp_carry": gp_amt,
        })
    return out


# ---------------------------------------------------------------------------
# Unfunded commitment (future capital calls the secondary buyer must fund)
# ---------------------------------------------------------------------------

def unfunded_commitment_calls(unfunded_amount: float, forecast_rows: List[Dict],
                               call_years: int = None) -> Dict[int, float]:
    """
    Spreads an unfunded commitment evenly across the first `call_years` of the
    forecast (capital calls in PE funds typically taper off well before the
    fund's final years, so we front-load them rather than spreading across
    the whole remaining life). Returns {year: call_amount}.
    """
    n = len(forecast_rows)
    if unfunded_amount <= 0 or n == 0:
        return {r["year"]: 0.0 for r in forecast_rows}
    if call_years is None:
        call_years = max(1, n // 2)
    call_years = max(1, min(call_years, n))
    per_year = unfunded_amount / call_years
    return {r["year"]: (per_year if r["year"] <= call_years else 0.0) for r in forecast_rows}


def build_unfunded_schedule(known_followons: List[Dict], blind_pool_amount: float,
                             blind_pool_call_years: int, forecast_rows: List[Dict]) -> Dict[int, float]:
    """
    Splits a buyer's future funding obligation into two pieces, mirroring how real
    secondary deals separate what's already known from what isn't:
      - known_followons: specific, already-identified future investments, each
        {"name": str, "amount": float, "year": int} - called in full in that year.
      - blind_pool_amount: remaining unfunded commitment with no identified specific
        use yet, spread evenly over blind_pool_call_years (front-loaded, like
        unfunded_commitment_calls above).
    Returns a combined {year: total_call_amount}.
    """
    n = len(forecast_rows)
    schedule = {r["year"]: 0.0 for r in forecast_rows}
    if n == 0:
        return schedule
    for f in known_followons or []:
        y = max(1, min(int(f["year"]), n))
        schedule[y] = schedule.get(y, 0.0) + float(f["amount"])
    if blind_pool_amount and blind_pool_amount > 0:
        by = blind_pool_call_years if blind_pool_call_years else max(1, n // 2)
        by = max(1, min(int(by), n))
        per_year = blind_pool_amount / by
        for y in range(1, by + 1):
            schedule[y] = schedule.get(y, 0.0) + per_year
    return schedule


def build_unfunded_returns(unfunded_calls: Dict[int, float], hold_years: int, moic: float,
                            forecast_rows: List[Dict]) -> Tuple[Dict[int, float], float]:
    """
    A buyer's future capital-call obligation isn't purely an outflow -- the capital
    called funds a new investment that itself goes on to return money. Each call in
    year Y is assumed to return `moic` times its amount, `hold_years` after it was
    called (a simplifying single-hold-period assumption, since we don't have a real
    forecast for money not yet even called).

    Returns (returns_by_year, excluded_amount): `returns_by_year` is a {year:
    return_amount} dict shaped like unfunded_calls, only for calls whose maturity
    (year + hold_years) still falls within the forecast horizon; `excluded_amount`
    is the total return from calls that mature beyond the horizon (dropped rather
    than distorting the forecast's last year), so callers can flag it in the UI.
    """
    n = len(forecast_rows)
    returns_by_year = {r["year"]: 0.0 for r in forecast_rows}
    excluded_amount = 0.0
    if n == 0 or hold_years <= 0 or moic <= 0:
        return returns_by_year, excluded_amount
    for call_year, amount in (unfunded_calls or {}).items():
        if not amount:
            continue
        maturity_year = int(call_year) + int(hold_years)
        return_amount = float(amount) * float(moic)
        if maturity_year <= n:
            returns_by_year[maturity_year] = returns_by_year.get(maturity_year, 0.0) + return_amount
        else:
            excluded_amount += return_amount
    return returns_by_year, excluded_amount


# ---------------------------------------------------------------------------
# Secondary pricing
# ---------------------------------------------------------------------------

def leverage_overlay(scenario: Dict, forecast_rows: List[Dict], as_of: date,
                      distribution_key: str, unfunded_calls: Dict[int, float],
                      leverage_pct: float, interest_rate: float,
                      unfunded_returns: Dict[int, float] = None) -> Dict:
    """
    Overlays debt financing on top of one secondary_pricing() scenario (e.g. the
    buyer's row at a given discount): the buyer draws `leverage_pct` of the
    purchase price at t=0 from a credit facility (a subscription line / NAV
    facility) instead of funding the whole price with equity.

    Repayment logic (documented, overridable assumption -- desks vary on how
    aggressively they sweep): each period, whatever cash the fund actually
    distributes (plus any unfunded-commitment return maturing that period) net
    of that period's unfunded call (if positive) is swept to pay accrued
    interest first, then principal, until the balance hits zero. If a period's
    net cash flow is zero or negative (e.g. a call exceeds that period's
    distribution), no debt service happens that period and the accrued
    interest capitalizes into the balance rather than erroring. The facility
    only finances the purchase price -- unfunded capital calls are always
    funded directly by the equity holder, never by this facility.

    unfunded_returns: optional {year: return_amount} from build_unfunded_returns()
    -- cash the unfunded commitment itself returns once it matures. Treated the
    same as a distribution (available for debt service, then paid to equity),
    since by the time it lands it's just cash in the buyer's hands.

    Returns levered IRR/MOIC for the buyer's equity cash flows only, meant to
    be shown ALONGSIDE (never instead of) the unlevered scenario['irr'] /
    scenario['moic'] this was built from -- leverage changes the return
    profile, not the underlying deal.

    At leverage_pct = 0.0 this reproduces scenario['irr'] and scenario['moic']
    exactly (same cash flows, same formulas) -- see test_leverage_overlay.py.
    """
    unfunded_returns = unfunded_returns or {}
    price = scenario["price"]
    total_calls = scenario["unfunded_calls"]
    initial_draw = price * leverage_pct
    equity_at_t0 = price - initial_draw
    equity_invested_total = equity_at_t0 + total_calls

    balance = initial_draw
    prev_date = as_of
    schedule = []
    levered_flows = [(as_of, -equity_at_t0)]
    equity_returned_total = 0.0

    for r in forecast_rows:
        gross_dist = r[distribution_key] + unfunded_returns.get(r["year"], 0.0)
        call = unfunded_calls.get(r["year"], 0.0)
        net_cf = gross_dist - call
        period_years = max((r["date"] - prev_date).days, 0) / 365.0
        beginning_balance = balance
        interest_due = beginning_balance * interest_rate * period_years
        cash_for_debt_service = max(net_cf, 0.0)

        if cash_for_debt_service > 0:
            interest_paid = min(cash_for_debt_service, interest_due)
            principal_paid = min(cash_for_debt_service - interest_paid, balance)
            unpaid_interest = interest_due - interest_paid
        else:
            interest_paid = 0.0
            principal_paid = 0.0
            unpaid_interest = interest_due
        balance = balance - principal_paid + unpaid_interest

        equity_received = gross_dist - interest_paid - principal_paid
        equity_returned_total += equity_received
        equity_cf_this_period = equity_received - call
        levered_flows.append((r["date"], equity_cf_this_period))

        schedule.append({
            "year": r["year"],
            "date": r["date"],
            "beginning_balance": beginning_balance,
            "interest_accrued": interest_due,
            "interest_paid": interest_paid,
            "principal_repaid": principal_paid,
            "ending_balance": balance,
        })
        prev_date = r["date"]

    levered_irr = xirr(levered_flows)
    levered_moic = equity_returned_total / equity_invested_total if equity_invested_total else float("nan")

    return {
        "leverage_pct": leverage_pct,
        "interest_rate": interest_rate,
        "initial_draw": initial_draw,
        "equity_invested": equity_invested_total,
        "levered_irr": levered_irr,
        "levered_moic": levered_moic,
        "ending_balance": balance,
        "schedule": schedule,
    }


def secondary_pricing(nav_current: float, forecast_rows: List[Dict], as_of: date,
                       discount_levels: List[float], distribution_key: str = "gross_distribution",
                       unfunded_calls: Dict[int, float] = None,
                       unfunded_returns: Dict[int, float] = None) -> List[Dict]:
    """discount_levels: positive = discount to NAV, negative = premium.
    distribution_key: which forecast field the buyer actually receives
    ('gross_distribution' if ignoring fees/carry, 'lp_distribution' if applying them).
    unfunded_calls: optional {year: call_amount} the buyer must additionally fund;
    these are netted against distributions and added to the buyer's invested capital.
    unfunded_returns: optional {year: return_amount} from build_unfunded_returns() --
    cash the unfunded commitment itself returns once it matures; added to distributions
    and to total value received (but NOT to invested capital -- the call amount already
    covers that side)."""
    unfunded_calls = unfunded_calls or {}
    unfunded_returns = unfunded_returns or {}
    total_calls = sum(unfunded_calls.values())
    total_returns = sum(unfunded_returns.values())
    results = []
    for disc in discount_levels:
        price = nav_current * (1 - disc)
        cashflows = [(as_of, -price)] + [
            (r["date"], r[distribution_key] - unfunded_calls.get(r["year"], 0.0)
             + unfunded_returns.get(r["year"], 0.0))
            for r in forecast_rows
        ]
        irr = xirr(cashflows)
        total_dist = sum(r[distribution_key] for r in forecast_rows) + total_returns
        total_invested = price + total_calls
        moic = total_dist / total_invested if total_invested else float("nan")
        results.append({
            "discount": disc,
            "price": price,
            "unfunded_calls": total_calls,
            "unfunded_returns": total_returns,
            "total_invested": total_invested,
            "irr": irr,
            "moic": moic,
        })
    return results

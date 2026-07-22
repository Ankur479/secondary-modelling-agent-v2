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


# ---------------------------------------------------------------------------
# Secondary pricing
# ---------------------------------------------------------------------------

def secondary_pricing(nav_current: float, forecast_rows: List[Dict], as_of: date,
                       discount_levels: List[float], distribution_key: str = "gross_distribution",
                       unfunded_calls: Dict[int, float] = None) -> List[Dict]:
    """discount_levels: positive = discount to NAV, negative = premium.
    distribution_key: which forecast field the buyer actually receives
    ('gross_distribution' if ignoring fees/carry, 'lp_distribution' if applying them).
    unfunded_calls: optional {year: call_amount} the buyer must additionally fund;
    these are netted against distributions and added to the buyer's invested capital."""
    unfunded_calls = unfunded_calls or {}
    total_calls = sum(unfunded_calls.values())
    results = []
    for disc in discount_levels:
        price = nav_current * (1 - disc)
        cashflows = [(as_of, -price)] + [
            (r["date"], r[distribution_key] - unfunded_calls.get(r["year"], 0.0)) for r in forecast_rows
        ]
        irr = xirr(cashflows)
        total_dist = sum(r[distribution_key] for r in forecast_rows)
        total_invested = price + total_calls
        moic = total_dist / total_invested if total_invested else float("nan")
        results.append({
            "discount": disc,
            "price": price,
            "unfunded_calls": total_calls,
            "total_invested": total_invested,
            "irr": irr,
            "moic": moic,
        })
    return results

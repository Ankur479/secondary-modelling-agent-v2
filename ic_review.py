"""
The two things an investment committee asks that a single forecast can't answer:
what happens if this goes badly, and what should worry us.

Kept separate from finance_engine because this is judgement tooling rather than
mechanics -- and pure Python, so both halves can be tested directly.
"""
from datetime import date
from typing import Dict, List, Optional

from finance_engine import implied_annual_return

# The two levers an IC actually argues about in a secondary. Everything else -- fees,
# hurdle, the price itself -- is held constant across cases on purpose: you are buying
# at one price, and the question is what that price returns in different worlds.
SCENARIOS = [
    {"name": "Downside", "exit_slip_years": 2, "proceeds_haircut": 0.25,
     "note": "Exits slip two years and come in a quarter light -- roughly a turn off "
             "the exit multiple plus a delayed process."},
    {"name": "Base", "exit_slip_years": 0, "proceeds_haircut": 0.0,
     "note": "The model as built."},
    {"name": "Upside", "exit_slip_years": 0, "proceeds_haircut": -0.15,
     "note": "Exits land on time and 15% ahead of plan."},
]


def scenario_companies(companies: List[Dict], exit_slip_years: int = 0,
                        proceeds_haircut: float = 0.0) -> List[Dict]:
    """Re-cut a portfolio for one scenario.

    Both levers act on the exit, not on today's mark, because that is where the
    uncertainty in a secondary actually sits -- the mark is a fact you inherit, the
    exit is the bet.

    - exit_slip_years pushes every exit out, and the same proceeds arriving later is a
      lower annualised return, which is exactly the damage a delayed process does.
    - proceeds_haircut scales what the exit pays (negative for an upside case).

    Returns a new list; the input is not modified.
    """
    out = []
    for c in companies:
        value = float(c.get("current_value", 0.0))
        r = float(c.get("expected_return", 0.0))
        y1 = int(c.get("exit_year_1", 1))
        y2 = int(c.get("exit_year_2", y1))

        new_y1 = max(1, y1 + int(exit_slip_years))
        new_y2 = max(new_y1, y2 + int(exit_slip_years))

        # What the exit pays in the base case, then the scenario's version of it.
        base_proceeds = value * ((1 + r) ** y1)
        scen_proceeds = base_proceeds * (1 - proceeds_haircut)
        new_r = implied_annual_return(value, scen_proceeds, new_y1)

        out.append({**c, "expected_return": new_r,
                    "exit_year_1": new_y1, "exit_year_2": new_y2})
    return out


def risk_flags(*, as_of: date, today: Optional[date] = None,
               company_values: Optional[Dict[str, float]] = None,
               proceeds_by_year: Optional[Dict[int, float]] = None,
               purchase_price: float = 0.0, unfunded: float = 0.0,
               blind_pool_proceeds: float = 0.0, total_proceeds: float = 0.0,
               carry_taken: float = 0.0) -> List[Dict]:
    """What a careful reviewer would circle in the margin, computed from the model.

    Every flag is derived, not asserted: each carries the number that triggered it so
    the reader can disagree with the threshold rather than with the tool. Returns a
    list of {level, title, detail}, worst first; level is "serious", "watch" or "info".
    """
    today = today or date.today()
    company_values = company_values or {}
    proceeds_by_year = proceeds_by_year or {}
    flags = []

    # --- how old is the mark you are buying against ---
    months_stale = (today.year - as_of.year) * 12 + (today.month - as_of.month)
    if months_stale >= 12:
        flags.append({"level": "serious", "title": "The mark is over a year old",
                      "detail": f"Reported as at {as_of:%b %Y}, roughly {months_stale} months "
                                "ago. A year of unreported movement sits between that NAV and "
                                "what is actually being bought."})
    elif months_stale >= 6:
        flags.append({"level": "watch", "title": "The mark is more than six months old",
                      "detail": f"Reported as at {as_of:%b %Y}, roughly {months_stale} months "
                                "ago. Worth asking the GP for a roll-forward before pricing "
                                "off it."})

    # --- concentration ---
    total_value = sum(company_values.values())
    if total_value > 0 and company_values:
        top_name, top_value = max(company_values.items(), key=lambda kv: kv[1])
        top_share = top_value / total_value
        ranked = sorted(company_values.values(), reverse=True)
        top3_share = sum(ranked[:3]) / total_value
        if top_share >= 0.40:
            flags.append({"level": "serious", "title": f"{top_name} is {top_share:.0%} of the portfolio",
                          "detail": "At this weight the deal is largely a single-asset "
                                    "underwrite. The diligence effort should reflect that."})
        elif top_share >= 0.30:
            flags.append({"level": "watch", "title": f"{top_name} is {top_share:.0%} of the portfolio",
                          "detail": "One name drives the outcome; a miss there is not offset "
                                    "by the rest of the book."})
        # Only meaningful once there are enough names for the statement to say
        # something: with four equal holdings the top three are 75% by arithmetic, not
        # by concentration. At five or more, 75% is genuine skew.
        if len(company_values) >= 5 and top3_share >= 0.75:
            flags.append({"level": "watch", "title": f"Top three names are {top3_share:.0%} of value",
                          "detail": "The tail contributes little. Price the top three properly "
                                    "and the rest is rounding."})

    # --- unfunded exposure relative to what is being paid ---
    if purchase_price > 0 and unfunded > 0:
        ratio = unfunded / purchase_price
        if ratio >= 1.0:
            flags.append({"level": "serious",
                          "title": f"Unfunded is {ratio:.1f}x the purchase price",
                          "detail": "Most of the capital at risk has not been called yet, so "
                                    "the return depends more on the GP's future decisions than "
                                    "on the assets being bought today."})
        elif ratio >= 0.5:
            flags.append({"level": "watch",
                          "title": f"Unfunded is {ratio:.0%} of the purchase price",
                          "detail": "A substantial share of the exposure is still to be called. "
                                    "Check the LPA's default provisions and the buyer's own "
                                    "funding plan."})

    # --- timing concentration ---
    positive = {y: v for y, v in proceeds_by_year.items() if v > 0}
    total_pos = sum(positive.values())
    if total_pos > 0:
        peak_year, peak = max(positive.items(), key=lambda kv: kv[1])
        share = peak / total_pos
        if share >= 0.60:
            flags.append({"level": "watch",
                          "title": f"{share:.0%} of projected proceeds land in {peak_year}",
                          "detail": "The return is a bet on one exit window. If that window "
                                    "shuts, the IRR moves a long way -- see the Downside case."})

    # --- how much of the answer is an assumption rather than an asset ---
    if total_proceeds > 0 and blind_pool_proceeds > 0:
        share = blind_pool_proceeds / total_proceeds
        if share >= 0.25:
            flags.append({"level": "watch",
                          "title": f"{share:.0%} of proceeds come from the blind pool",
                          "detail": "That much of the return rests on an assumed multiple for "
                                    "investments that have not been made yet, in companies "
                                    "nobody has seen."})

    if carry_taken <= 0:
        flags.append({"level": "info", "title": "No carry is projected",
                      "detail": "On this forecast the fund never clears its hurdle, so the GP "
                                "earns nothing. Worth sanity-checking the exit assumptions -- "
                                "and note the GP has every incentive to disagree."})

    order = {"serious": 0, "watch": 1, "info": 2}
    return sorted(flags, key=lambda f: order.get(f["level"], 3))

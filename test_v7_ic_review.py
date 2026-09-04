"""
Verification of the IC review tooling: scenario re-cuts and the risk flags.

The scenario checks are arithmetic -- a slipped exit and a haircut have exact,
hand-computable effects on the implied return, and the direction of travel (downside
below base below upside) must always hold. The flag checks are mostly about restraint:
a flag that fires when it shouldn't trains the reader to ignore all of them.

Run: python3 test_v7_ic_review.py
"""
from datetime import date

from ic_review import SCENARIOS, risk_flags, scenario_companies

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


BASE = [
    {"name": "A", "current_value": 100.0, "expected_return": 0.20, "exit_year_1": 3,
     "exit_pct_1": 1.0, "exit_year_2": 3, "exit_pct_2": 0.0, "cost": 60.0},
    {"name": "B", "current_value": 50.0, "expected_return": 0.10, "exit_year_1": 5,
     "exit_pct_1": 1.0, "exit_year_2": 5, "exit_pct_2": 0.0, "cost": 40.0},
]


# ===========================================================================
print("\n--- Scenario re-cuts ---")
# ===========================================================================
same = scenario_companies(BASE)
check("no levers pulled -> the portfolio is unchanged",
      all(abs(a["expected_return"] - b["expected_return"]) < 1e-12
          and a["exit_year_1"] == b["exit_year_1"] for a, b in zip(same, BASE)))
check("the input list is never mutated", BASE[0]["expected_return"] == 0.20)

# A two-year slip: the same exit proceeds arriving in year 5 instead of year 3.
slipped = scenario_companies(BASE, exit_slip_years=2)
expected_proceeds = 100.0 * 1.20 ** 3
expected_r = (expected_proceeds / 100.0) ** (1 / 5) - 1
check("a slipped exit moves the year", slipped[0]["exit_year_1"] == 5)
check("and lowers the annualised return by exactly the extra time",
      abs(slipped[0]["expected_return"] - expected_r) < 1e-12,
      (slipped[0]["expected_return"], expected_r))
check("the exit proceeds themselves are unchanged by a slip",
      abs(100.0 * (1 + slipped[0]["expected_return"]) ** 5 - expected_proceeds) < 1e-9)

# A 25% haircut with no slip.
cut = scenario_companies(BASE, proceeds_haircut=0.25)
expected_r_cut = (expected_proceeds * 0.75 / 100.0) ** (1 / 3) - 1
check("a haircut lowers the return", abs(cut[0]["expected_return"] - expected_r_cut) < 1e-12,
      (cut[0]["expected_return"], expected_r_cut))
check("a haircut takes exactly that share off the exit",
      abs(100.0 * (1 + cut[0]["expected_return"]) ** 3 - expected_proceeds * 0.75) < 1e-9)

up = scenario_companies(BASE, proceeds_haircut=-0.15)
check("a negative haircut is an upside case",
      up[0]["expected_return"] > BASE[0]["expected_return"])

# The ordering that makes the table worth showing at all.
down = scenario_companies(BASE, exit_slip_years=2, proceeds_haircut=0.25)
check("downside < base < upside, for every company",
      all(d["expected_return"] < b["expected_return"] < u["expected_return"]
          for d, b, u in zip(down, BASE, up)))

check("a slip can never pull an exit before year 1",
      scenario_companies(BASE, exit_slip_years=-99)[0]["exit_year_1"] == 1)
check("the second exit never lands before the first",
      all(c["exit_year_2"] >= c["exit_year_1"]
          for c in scenario_companies(BASE, exit_slip_years=1)))
check("the shipped scenario set is downside / base / upside",
      [s["name"] for s in SCENARIOS] == ["Downside", "Base", "Upside"])


# ===========================================================================
print("\n--- Risk flags: what they catch ---")
# ===========================================================================
def titles(flags):
    return " | ".join(f["title"] for f in flags)


f = risk_flags(as_of=date(2024, 1, 31), today=date(2026, 1, 31),
               company_values={"A": 10.0}, purchase_price=100.0)
check("a two-year-old mark is serious",
      any(x["level"] == "serious" and "over a year old" in x["title"] for x in f), titles(f))

f = risk_flags(as_of=date(2025, 6, 30), today=date(2026, 1, 31),
               company_values={"A": 10.0, "B": 10.0, "C": 10.0, "D": 10.0},
               purchase_price=100.0)
check("a seven-month-old mark is a watch, not a crisis",
      any(x["level"] == "watch" and "six months" in x["title"] for x in f), titles(f))

f = risk_flags(as_of=date(2026, 1, 1), today=date(2026, 2, 1),
               company_values={"A": 10.0, "B": 10.0, "C": 10.0, "D": 10.0},
               purchase_price=100.0, carry_taken=5.0)
check("a fresh, spread, sensibly funded deal raises nothing but info",
      all(x["level"] == "info" for x in f), titles(f))

f = risk_flags(as_of=date(2026, 1, 1), today=date(2026, 2, 1),
               company_values={"Big": 45.0, "B": 20.0, "C": 20.0, "D": 15.0},
               purchase_price=100.0, carry_taken=5.0)
check("a 45% position is serious and names the company",
      any(x["level"] == "serious" and "Big" in x["title"] for x in f), titles(f))

# With three names the top three are 100% of value by definition, and with four they
# are 75% -- neither says anything about concentration, so neither should be flagged.
# What IS worth saying about a three-name book is that each name is a third of it.
f = risk_flags(as_of=date(2026, 1, 1), today=date(2026, 2, 1),
               company_values={"Big": 33.0, "B": 33.0, "C": 34.0},
               purchase_price=100.0, carry_taken=5.0)
check("a three-name book is not flagged for the tautology that its top three are all of it",
      not any("Top three" in x["title"] for x in f), titles(f))
check("but each name being a third of the book is flagged",
      any("of the portfolio" in x["title"] for x in f), titles(f))

f = risk_flags(as_of=date(2026, 1, 1), today=date(2026, 2, 1),
               company_values={"Big": 50.0, "B": 20.0, "C": 15.0, "D": 8.0, "E": 7.0},
               purchase_price=100.0, carry_taken=5.0)
check("a genuinely top-heavy five-name book does flag top-three concentration",
      any("Top three" in x["title"] for x in f), titles(f))

f = risk_flags(as_of=date(2026, 1, 1), today=date(2026, 2, 1),
               company_values={"A": 10.0, "B": 10.0, "C": 10.0, "D": 10.0},
               purchase_price=50.0, unfunded=60.0, carry_taken=5.0)
check("unfunded larger than the price is serious",
      any(x["level"] == "serious" and "purchase price" in x["title"] for x in f), titles(f))

f = risk_flags(as_of=date(2026, 1, 1), today=date(2026, 2, 1),
               company_values={"A": 10.0, "B": 10.0, "C": 10.0, "D": 10.0},
               purchase_price=100.0, unfunded=60.0, carry_taken=5.0)
check("unfunded at 60% of the price is a watch",
      any(x["level"] == "watch" and "60%" in x["title"] for x in f), titles(f))

f = risk_flags(as_of=date(2026, 1, 1), today=date(2026, 2, 1),
               company_values={"A": 10.0, "B": 10.0, "C": 10.0, "D": 10.0},
               proceeds_by_year={2028: 80.0, 2029: 20.0}, purchase_price=100.0, carry_taken=5.0)
check("proceeds bunched into one year are flagged, with the year named",
      any("2028" in x["title"] for x in f), titles(f))

f = risk_flags(as_of=date(2026, 1, 1), today=date(2026, 2, 1),
               company_values={"A": 10.0, "B": 10.0, "C": 10.0, "D": 10.0},
               purchase_price=100.0, blind_pool_proceeds=40.0, total_proceeds=100.0,
               carry_taken=5.0)
check("heavy reliance on the blind pool is flagged",
      any("blind pool" in x["title"] for x in f), titles(f))

f = risk_flags(as_of=date(2026, 1, 1), today=date(2026, 2, 1),
               company_values={"A": 10.0, "B": 10.0, "C": 10.0, "D": 10.0},
               purchase_price=100.0, carry_taken=0.0)
check("a forecast where the GP never earns carry is worth noting",
      any("No carry" in x["title"] for x in f), titles(f))


# ===========================================================================
print("\n--- Risk flags: restraint ---")
# ===========================================================================
clean = dict(as_of=date(2026, 1, 1), today=date(2026, 2, 1),
             company_values={"A": 10.0, "B": 10.0, "C": 10.0, "D": 10.0},
             proceeds_by_year={2028: 30.0, 2029: 35.0, 2030: 35.0},
             purchase_price=100.0, unfunded=10.0, blind_pool_proceeds=5.0,
             total_proceeds=100.0, carry_taken=5.0)
f = risk_flags(**clean)
check("a genuinely clean deal raises no flags at all", f == [], titles(f))

f = risk_flags(as_of=date(2026, 1, 1), today=date(2026, 2, 1))
check("no portfolio data -> no invented flags",
      all(x["level"] == "info" for x in f), titles(f))

f = risk_flags(as_of=date(2024, 1, 1), today=date(2026, 2, 1),
               company_values={"Big": 60.0, "B": 40.0}, purchase_price=10.0, unfunded=50.0,
               proceeds_by_year={2028: 100.0}, blind_pool_proceeds=60.0,
               total_proceeds=100.0, carry_taken=0.0)
check("the worst are listed first", f[0]["level"] == "serious", [x["level"] for x in f])
check("everything wrong at once is caught, not just the first thing", len(f) >= 5, titles(f))


print("\n=== SUMMARY ===")
if FAILS:
    print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")

"""
Standalone tests for leverage_overlay(), using synthetic dummy data only.
Run: python3 test_leverage_overlay.py
"""
from datetime import date

from finance_engine import secondary_pricing, leverage_overlay, xirr


def test_zero_leverage_matches_unlevered_exactly():
    print("\n--- TEST 1: 0% leverage reproduces the unlevered scenario exactly ---")
    as_of = date(2026, 1, 1)
    forecast_rows = [
        {"year": 1, "date": date(2027, 1, 1), "gross_distribution": 3_000_000.0},
        {"year": 2, "date": date(2028, 1, 1), "gross_distribution": 5_000_000.0},
        {"year": 3, "date": date(2029, 1, 1), "gross_distribution": 8_000_000.0},
    ]
    unfunded_calls = {1: 500_000.0, 2: 0.0, 3: 0.0}
    nav = 10_000_000.0
    scenario = secondary_pricing(nav, forecast_rows, as_of, [0.15], "gross_distribution", unfunded_calls)[0]

    result = leverage_overlay(scenario, forecast_rows, as_of, "gross_distribution", unfunded_calls,
                               leverage_pct=0.0, interest_rate=0.065)

    print(f"  unlevered irr={scenario['irr']*100:.6f}%   levered(0%) irr={result['levered_irr']*100:.6f}%")
    print(f"  unlevered moic={scenario['moic']:.6f}x  levered(0%) moic={result['levered_moic']:.6f}x")
    print(f"  ending_balance={result['ending_balance']}")

    assert abs(result["levered_irr"] - scenario["irr"]) < 1e-9, "0% leverage IRR must match unlevered exactly"
    assert abs(result["levered_moic"] - scenario["moic"]) < 1e-9, "0% leverage MOIC must match unlevered exactly"
    assert result["ending_balance"] == 0.0
    print("  PASS")


def test_facility_fully_repaid_before_horizon_ends():
    print("\n--- TEST 2: facility draws, gets fully repaid, stays at zero ---")
    as_of = date(2026, 1, 1)
    # Big early distributions so the facility pays off well before year 4-5.
    forecast_rows = [
        {"year": 1, "date": date(2027, 1, 1), "gross_distribution": 15_000_000.0},
        {"year": 2, "date": date(2028, 1, 1), "gross_distribution": 1_000_000.0},
        {"year": 3, "date": date(2029, 1, 1), "gross_distribution": 1_000_000.0},
        {"year": 4, "date": date(2030, 1, 1), "gross_distribution": 1_000_000.0},
    ]
    unfunded_calls = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
    nav = 20_000_000.0
    # 30% discount so price (14M) is comfortably below total distributions (18M) --
    # a genuinely profitable deal, so leverage has real cheap-debt-vs-project-return
    # arbitrage to demonstrate (0% discount would make price ~= total distributions,
    # i.e. a roughly break-even deal, which isn't a fair test of whether leverage helps).
    scenario = secondary_pricing(nav, forecast_rows, as_of, [0.30], "gross_distribution", unfunded_calls)[0]
    # price = 14M, 40% leverage -> initial_draw = 5.6M, easily covered by year-1's 15M distribution.

    result = leverage_overlay(scenario, forecast_rows, as_of, "gross_distribution", unfunded_calls,
                               leverage_pct=0.40, interest_rate=0.065)

    for row in result["schedule"]:
        print(f"  year {row['year']}: beg={row['beginning_balance']:,.0f} "
              f"interest_due={row['interest_accrued']:,.0f} paid={row['interest_paid']:,.0f} "
              f"principal={row['principal_repaid']:,.0f} end={row['ending_balance']:,.0f}")

    print(f"  unlevered irr={scenario['irr']*100:.2f}%  unlevered moic={scenario['moic']:.3f}x  price={scenario['price']:,.0f}")
    assert result["initial_draw"] == scenario["price"] * 0.40
    assert result["schedule"][0]["ending_balance"] == 0.0, "should fully repay in year 1 given the large distribution"
    assert all(row["ending_balance"] == 0.0 for row in result["schedule"][1:]), "balance must stay at zero once repaid"
    assert result["levered_irr"] > scenario["irr"], "leverage should boost IRR when the facility is cheap and repaid quickly"
    print(f"  unlevered irr={scenario['irr']*100:.2f}%   levered irr={result['levered_irr']*100:.2f}%")
    print("  PASS")


def test_shortfall_period_capitalizes_interest_not_silently_ignored():
    print("\n--- TEST 3: a period where the call exceeds the distribution capitalizes interest ---")
    as_of = date(2026, 1, 1)
    forecast_rows = [
        {"year": 1, "date": date(2027, 1, 1), "gross_distribution": 500_000.0},   # call > distribution this year
        {"year": 2, "date": date(2028, 1, 1), "gross_distribution": 12_000_000.0},
    ]
    unfunded_calls = {1: 2_000_000.0, 2: 0.0}  # year 1 net cash flow is NEGATIVE
    nav = 10_000_000.0
    scenario = secondary_pricing(nav, forecast_rows, as_of, [0.0], "gross_distribution", unfunded_calls)[0]

    result = leverage_overlay(scenario, forecast_rows, as_of, "gross_distribution", unfunded_calls,
                               leverage_pct=0.50, interest_rate=0.065)

    y1 = result["schedule"][0]
    print(f"  year 1: beginning_balance={y1['beginning_balance']:,.0f} interest_accrued={y1['interest_accrued']:,.0f} "
          f"interest_paid={y1['interest_paid']:,.0f} principal_repaid={y1['principal_repaid']:,.0f} "
          f"ending_balance={y1['ending_balance']:,.0f}")

    assert y1["interest_paid"] == 0.0, "no cash available for debt service in a shortfall period"
    assert y1["principal_repaid"] == 0.0
    expected_capitalized = y1["beginning_balance"] * 0.065 * ((date(2027, 1, 1) - as_of).days / 365.0)
    assert abs(y1["ending_balance"] - (y1["beginning_balance"] + expected_capitalized)) < 1e-6, (
        "unpaid interest must capitalize into the balance, not vanish"
    )
    print("  PASS -- shortfall handled explicitly (capitalized), not silently ignored")


if __name__ == "__main__":
    test_zero_leverage_matches_unlevered_exactly()
    test_facility_fully_repaid_before_horizon_ends()
    test_shortfall_period_capitalizes_interest_not_silently_ignored()
    print("\nALL LEVERAGE OVERLAY TESTS PASSED")

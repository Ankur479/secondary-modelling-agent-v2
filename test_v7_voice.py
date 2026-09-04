"""
Verification of the voice command grammar.

The risk with voice control of a pricing model is not that it fails to understand -- a
shrug is harmless -- but that it understands WRONGLY and changes a number nobody meant.
So most of these checks are about refusing: no number, no known field, an out-of-range
value, a year that wasn't spoken in full. Each expectation is written out by hand.

Run: python3 test_v7_voice.py
"""
from voice_commands import describe, extract_number, parse_command, resolve_asset

FAILS = []
ASSETS = [{"aid": "A", "name": "Asset A"}, {"aid": "B", "name": "Asset B"},
          {"aid": "C", "name": "Project Falcon"}]
STATE = {"premium_discount_pct": -10.0, "carry_pct": 20.0, "hurdle_pct": 8.0,
         "am_A_exit_cal": 2028, "am_A_cost": 77.0, "am_A_rv": 188.0,
         "am_C_exit_cal": 2026}


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def p(text):
    return parse_command(text, ASSETS, STATE)


# ===========================================================================
print("\n--- Numbers, spoken however they come out ---")
# ===========================================================================
check("digits", extract_number("set discount to 15 percent") == 15.0)
check("decimals", extract_number("cost 84.9") == 84.9)
check("a year", extract_number("exit 2031") == 2031.0)
check("a word", extract_number("carry twenty percent") == 20.0)
check("two words", extract_number("carry twenty five percent") == 25.0)
check("hyphenated words", extract_number("growth twenty-five percent") == 25.0)
check("teens", extract_number("discount fifteen") == 15.0)
check("digits win over a word that might be a mis-hear",
      extract_number("for 12 percent") == 12.0, extract_number("for 12 percent"))
check("no number at all -> None", extract_number("raise the discount a bit") is None)


# ===========================================================================
print("\n--- Which asset is being talked about ---")
# ===========================================================================
check("by letter", (resolve_asset("asset b exit 2030", ASSETS) or {}).get("aid") == "B")
check("by name", (resolve_asset("project falcon cost 60", ASSETS) or {}).get("aid") == "C")
check("'company D' phrasing", (resolve_asset("company a cost 80", ASSETS) or {}).get("aid") == "A")
check("no asset mentioned -> None", resolve_asset("discount fifteen", ASSETS) is None)


# ===========================================================================
print("\n--- Fund-level knobs ---")
# ===========================================================================
c = p("set the discount to 15 percent")
check("discount is understood", c is not None and c["key"] == "premium_discount_pct", c)
check("a spoken discount becomes a negative premium, as a term sheet quotes it",
      c and c["value"] == -15.0, c and c["value"])
check("the old value is reported so the change can be shown",
      c and c["old_value"] == -10.0, c and c["old_value"])
check("describe() reads as a sentence an analyst can check",
      describe(c) == "Premium / (Discount) to Reported Value: -10% → -15%", describe(c))

c = p("premium of 5 percent")
check("a premium stays positive", c and c["value"] == 5.0, c and c["value"])

c = p("carried interest twenty percent")
check("carried interest", c and c["key"] == "carry_pct" and c["value"] == 20.0, c)
c = p("carry 25")
check("the short form 'carry'", c and c["key"] == "carry_pct" and c["value"] == 25.0, c)

c = p("preferred return 10 percent")
check("preferred return", c and c["key"] == "hurdle_pct" and c["value"] == 10.0, c)
c = p("hurdle 9")
check("the short form 'hurdle'", c and c["key"] == "hurdle_pct" and c["value"] == 9.0, c)


# ===========================================================================
print("\n--- Per-asset knobs ---")
# ===========================================================================
c = p("asset a exit year 2031")
check("an asset's exit year", c and c["key"] == "am_A_exit_cal" and c["value"] == 2031.0, c)
check("the change is labelled with the company, not a raw key",
      c and c["label"] == "Asset A — Exit Year", c and c["label"])

c = p("asset a cost 90")
check("an asset's cost", c and c["key"] == "am_A_cost" and c["value"] == 90.0, c)
c = p("asset a reported value 200")
check("an asset's reported value", c and c["key"] == "am_A_rv" and c["value"] == 200.0, c)
check("'reported value' beats the shorter 'value' alias it contains",
      c and c["key"] == "am_A_rv", c and c["key"])
c = p("project falcon exit 2029")
check("an asset addressed by name", c and c["key"] == "am_C_exit_cal" and c["value"] == 2029.0, c)
c = p("asset b growth 12 percent")
check("an asset's revenue growth", c and c["key"] == "am_B_growth_pct" and c["value"] == 12.0, c)


# ===========================================================================
print("\n--- What it refuses to do (the part that matters) ---")
# ===========================================================================
check("silence", p("") is None)
check("no number -> no change", p("push the discount out a bit") is None)
check("a number but no field -> no change", p("fifteen") is None)
check("an unknown field -> no change", p("set the tax rate to 21 percent") is None)
check("a discount beyond the allowed range is refused, not clamped",
      p("discount 250 percent") is None)
check("a carry above 50% is refused", p("carry 80 percent") is None)
check("a two-digit year is refused rather than guessed",
      p("asset a exit 31") is None, p("asset a exit 31"))
check("a year outside any plausible fund life is refused",
      p("asset a exit year 1850") is None)
check("describe(None) says so plainly",
      describe(None) == "Didn't catch a change in that.")

# An asset field spoken without naming an asset must not silently hit a fund-level knob.
c = p("cost 90")
check("an asset field with no asset named -> no change", c is None, c)

# The whole point: a parse never mutates anything by itself.
before = dict(STATE)
p("discount 30 percent")
check("parsing never mutates the caller's state", STATE == before)


print("\n=== SUMMARY ===")
if FAILS:
    print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")

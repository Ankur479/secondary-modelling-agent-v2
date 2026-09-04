"""
Turning a spoken sentence into one specific change to one specific model input.

Deliberately narrow. This only recognises the handful of knobs an analyst actually
flexes in front of someone -- the price, the fund's economics, and a single asset's
cost, mark, exit year or growth -- because a voice command that might hit any of sixty
inputs is a voice command you cannot trust. Everything here is pure Python with no
Streamlit import, so the grammar can be tested directly.

Two rules shape the design:

1. **Nothing is applied silently.** parse_command returns a description of the change
   -- field, old value, new value -- and the caller is expected to show it and offer an
   undo. A misheard digit in a pricing model that changes a number without saying so is
   worse than no voice control at all.
2. **You never have to say anything confidential.** Assets are addressed by their letter
   ("asset D") as well as by name, so a deal can be flexed out loud without the company
   or the fund ever being spoken.
"""
from typing import Dict, List, Optional

# Spoken number words. Speech recognisers usually return digits, but not always, and
# "twenty" for a carry rate is a natural thing to say.
_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}

# Fund-level knobs. `key` is the session_state key the app's widget is bound to.
FUND_FIELDS = [
    {"key": "premium_discount_pct", "label": "Premium / (Discount) to Reported Value",
     "unit": "%", "aliases": ["discount", "premium", "price"], "min": -90.0, "max": 50.0},
    {"key": "carry_pct", "label": "Carried Interest", "unit": "%",
     "aliases": ["carried interest", "carry"], "min": 0.0, "max": 50.0},
    {"key": "hurdle_pct", "label": "Preferred Return", "unit": "%",
     "aliases": ["preferred return", "preferred", "hurdle", "pref"], "min": 0.0, "max": 25.0},
]

# Per-asset knobs. `key_template` is filled with the asset's id.
ASSET_FIELDS = [
    {"key_template": "am_{aid}_exit_cal", "label": "Exit Year", "unit": "year",
     "aliases": ["exit year", "exit"], "min": 1900, "max": 2200},
    {"key_template": "am_{aid}_cost", "label": "LP Cost", "unit": "$mm",
     "aliases": ["cost basis", "cost"], "min": 0.0, "max": 1e6},
    {"key_template": "am_{aid}_rv", "label": "Reported Value", "unit": "$mm",
     "aliases": ["reported value", "mark", "rv"], "min": 0.0, "max": 1e6},
    {"key_template": "am_{aid}_growth_pct", "label": "Revenue Growth (all years)",
     "unit": "%", "aliases": ["revenue growth", "growth"], "min": -50.0, "max": 100.0},
]


def _lookup(store, key):
    """Read one key from a mapping-like store without assuming it has .get --
    Streamlit's session state proxy behaves differently in the app and in its test
    harness, and this is only ever used to report a value, never to change one."""
    try:
        return store[key]
    except Exception:
        return None


def _normalise(text: str) -> str:
    return " ".join(str(text or "").lower().replace("%", " percent ").split())


def extract_number(text: str) -> Optional[float]:
    """The first number in the sentence, whether spoken as digits or as words.

    Digits win when both appear, because a recogniser that produced digits was more
    confident about them than a word-match on something like "for" / "four".
    """
    words = _normalise(text).replace(",", "").split()
    for w in words:
        cleaned = w.rstrip(".")
        try:
            return float(cleaned)
        except ValueError:
            pass
    # Word forms: "twenty", "twenty five", "twenty-five".
    flat = " ".join(words).replace("-", " ").split()
    for i, w in enumerate(flat):
        if w in _TENS:
            nxt = flat[i + 1] if i + 1 < len(flat) else ""
            return float(_TENS[w] + (_UNITS[nxt] if nxt in _UNITS and _UNITS[nxt] < 10 else 0))
        if w in _UNITS:
            return float(_UNITS[w])
    return None


def resolve_asset(text: str, assets: List[Dict]) -> Optional[Dict]:
    """Which asset the sentence is about.

    assets: [{"aid": "A", "name": "Asset A"}, ...]. Matched on the spoken name first
    (most specific), then on "asset <letter>", so neither has to be memorised.
    """
    t = _normalise(text)
    by_name = sorted(assets, key=lambda a: -len(str(a.get("name", ""))))
    for a in by_name:
        name = _normalise(a.get("name", ""))
        if name and name in t:
            return a
    for a in assets:
        aid = str(a.get("aid", "")).lower()
        if aid and (f"asset {aid}" in t or f"company {aid}" in t):
            return a
    return None


def _match_field(text: str, fields: List[Dict]) -> Optional[Dict]:
    """Longest alias wins, so "reported value" isn't swallowed by "value"."""
    t = _normalise(text)
    best, best_len = None, 0
    for f in fields:
        for alias in f["aliases"]:
            if alias in t and len(alias) > best_len:
                best, best_len = f, len(alias)
    return best


def parse_command(text: str, assets: Optional[List[Dict]] = None,
                  current: Optional[Dict] = None) -> Optional[Dict]:
    """Parse one spoken sentence into a single proposed change.

    Returns None when nothing was understood -- which the caller should surface as
    "didn't catch that" rather than guessing. On success:

        {"key", "label", "value", "old_value", "unit", "asset", "transcript"}

    `current` is the caller's session state, used only to report the old value so the
    change can be shown before it is applied.
    """
    if not text or not str(text).strip():
        return None
    current = current or {}
    assets = assets or []
    t = _normalise(text)

    number = extract_number(t)
    if number is None:
        return None

    asset = resolve_asset(t, assets)
    field = _match_field(t, ASSET_FIELDS) if asset else None
    if asset and field:
        key = field["key_template"].format(aid=asset["aid"])
        label = f"{asset.get('name', asset['aid'])} — {field['label']}"
    else:
        field = _match_field(t, FUND_FIELDS)
        if field is None:
            return None
        key, label = field["key"], field["label"]
        asset = None

    value = number
    # "discount" is quoted as a negative premium, the way a term sheet writes it, so a
    # spoken "discount fifteen" has to become -15 even though 15 was said.
    if field.get("key") == "premium_discount_pct":
        said_discount = "discount" in t
        said_premium = "premium" in t
        if said_discount and value > 0:
            value = -value
        elif said_premium and value < 0:
            value = abs(value)

    lo, hi = field.get("min"), field.get("max")
    if lo is not None and value < lo:
        return None
    if hi is not None and value > hi:
        return None
    # A year must be spoken in full; "exit 31" is too easy to mishear as a quantity.
    if field.get("unit") == "year" and not (1900 <= value <= 2200):
        return None

    return {
        "key": key,
        "label": label,
        "value": value,
        "old_value": _lookup(current, key),
        "unit": field.get("unit", ""),
        "asset": asset["aid"] if asset else None,
        "transcript": str(text).strip(),
    }


def describe(change: Dict) -> str:
    """One line an analyst can check at a glance before trusting the number."""
    if not change:
        return "Didn't catch a change in that."
    unit = change.get("unit", "")
    def fmt(v):
        if v is None:
            return "—"
        if unit == "year":
            return f"{int(v)}"
        if unit == "%":
            return f"{v:g}%"
        return f"{v:g}"
    return f"{change['label']}: {fmt(change.get('old_value'))} → {fmt(change['value'])}"

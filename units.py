"""Quantities and units. Pure functions, no database.

Everything is stored in canonical units: grams (g), millilitres (ml), or a counted unit
(piece, clove, can, bunch, head, handful). The user's unit system (metric or US) only
changes how amounts are DISPLAYED and how they are typed; stored numbers never change."""
import re

COUNT_UNITS = ("piece", "clove", "can", "bunch", "head", "handful")
UNIT_SYSTEMS = ("metric", "us")
US_REGIONS = {"US", "LR", "MM"}  # countries that use US customary units in everyday cooking


def units_for_region(region: str | None) -> str:
    return "us" if (region or "").strip().upper() in US_REGIONS else "metric"


# ----------------------------------------------------------- text normalisation
def _norm_word(w: str) -> str:
    if w.endswith(("us", "is")):
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("oes") and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def norm(text: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", str(text).lower().replace("-", " "))
    return " ".join(_norm_word(w) for w in text.split())


def tokens_match(a: str, b: str) -> bool:
    ta, tb = set(norm(a).split()), set(norm(b).split())
    return bool(ta and tb) and (ta <= tb or tb <= ta)


def pluralize(label: str, amount: float) -> str:
    """'egg' -> 'eggs' when amount != 1. Works on the last word of a label."""
    words = label.split()
    if not words:
        return label
    last = _norm_word(words[-1])
    if amount != 1:
        if last.endswith("y") and len(last) > 2 and last[-2] not in "aeiou":
            last = last[:-1] + "ies"
        elif last.endswith(("s", "x", "z", "ch", "sh")) or last in ("tomato", "potato"):
            last += "es"
        else:
            last += "s"
    words[-1] = last
    return " ".join(words)


# ----------------------------------------------------------------------- tables
_UNITS: dict[str, tuple[str, float]] = {}


def _add(canon: str, factor: float, *words: str) -> None:
    for w in words:
        _UNITS[w] = (canon, factor)


_add("g", 1, "g", "gram", "grams", "gr")
_add("g", 1000, "kg", "kilo", "kilos", "kilogram", "kilograms")
_add("g", 28.3495, "oz", "ounce", "ounces")
_add("g", 453.592, "lb", "lbs", "pound", "pounds")
_add("ml", 1, "ml", "milliliter", "milliliters", "millilitre", "millilitres")
_add("ml", 1000, "l", "liter", "liters", "litre", "litres")
_add("ml", 4.92892, "tsp", "teaspoon", "teaspoons")
_add("ml", 14.7868, "tbsp", "tablespoon", "tablespoons")
_add("ml", 236.588, "cup", "cups")
_add("ml", 29.5735, "floz")
_add("piece", 1, "piece", "pieces", "pc", "pcs", "whole")
_add("piece", 12, "dozen")
_add("clove", 1, "clove", "cloves")
_add("can", 1, "can", "cans", "tin", "tins")
_add("bunch", 1, "bunch", "bunches")
_add("head", 1, "head", "heads")
_add("handful", 1, "handful", "handfuls")

# Grams per millilitre, for converting between weight and volume of the same ingredient.
_DENSITY = {"rice": 0.78, "cooked rice": 0.67, "flour": 0.507, "sugar": 0.845, "brown sugar": 0.90, "butter": 0.96,
            "cheese": 0.48, "red lentil": 0.80, "lentil": 0.80, "frozen pea": 0.63, "oat": 0.38, "cocoa": 0.36,
            "ginger": 0.40, "honey": 1.4, "milk": 1.03, "water": 1.0, "oil": 0.92, "soy sauce": 1.15, "broth": 1.0}

# The unit each known ingredient is normally tracked in (most specific name wins).
_DEFAULT_UNIT = {
    **{n: "g" for n in ("rice", "cooked rice", "flour", "sugar", "brown sugar", "butter", "cheese", "red lentil", "lentil",
                        "frozen pea", "spaghetti", "pasta", "ground beef", "ginger", "oat")},
    **{n: "ml" for n in ("soy sauce", "milk", "vegetable broth", "broth", "cumin", "paprika", "curry powder", "chili flake",
                         "thyme", "parsley", "baking powder", "oil", "olive oil", "water", "honey")},
    "garlic": "clove", "canned tomato": "can", "black bean": "can", "chickpea": "can", "coconut milk": "can",
    "broccoli": "head", "basil": "handful",
}


def _best(table: dict, name: str):
    best, size = None, 0
    for key, value in table.items():
        n = len(key.split())
        if set(norm(key).split()) <= set(norm(name).split()) and n > size:
            best, size = value, n
    return best


def default_unit(name: str) -> str:
    return _best(_DEFAULT_UNIT, name) or "piece"


def density(name: str) -> float | None:
    return _best(_DENSITY, name)


def to_unit(amount: float, from_unit: str, to_unit_: str, name: str = "") -> float | None:
    """Converts between canonical units. Weight<->volume needs a known density; counts must match exactly."""
    if from_unit == to_unit_:
        return amount
    if {from_unit, to_unit_} == {"g", "ml"}:
        d = density(name)
        if not d:
            return None
        return amount / d if from_unit == "g" else amount * d
    return None


# --------------------------------------------------------------------- parsing
_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_WORD_NUM = {w: float(i) for i, w in enumerate(_ONES)}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
_NUM = (r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)(?![\d/])|(?:(?:" + "|".join(_TENS) + r")(?:[\s-](?:one|two|three|four|five|six|seven|eight|nine))?|"
        + "|".join(sorted(_ONES, key=len, reverse=True)) + r"|an?|half)(?![a-z])")
_LEAD = re.compile(rf"^\s*(?P<num>{_NUM})\s*(?P<unit>[a-z]+)?\s*(?P<rest>.*)$", re.I)


def parse_number(token: str) -> float | None:
    t = token.lower().replace("-", " ").strip()
    if t in ("a", "an"):
        return 1.0
    if t == "half":
        return 0.5
    if t in _WORD_NUM:
        return _WORD_NUM[t]
    if (m := re.fullmatch(r"(\d+)\s+(\d+)/(\d+)", t)):
        return int(m[1]) + int(m[2]) / int(m[3]) if int(m[3]) else None
    if (m := re.fullmatch(r"(\d+)/(\d+)", t)):
        return int(m[1]) / int(m[2]) if int(m[2]) else None
    parts = t.split()
    if parts and parts[0] in _TENS:
        return float(_TENS[parts[0]] + (_WORD_NUM.get(parts[1], 0) if len(parts) > 1 else 0))
    try:
        return float(t)
    except ValueError:
        return None


def _split(text: str):
    """-> (number, (canonical_unit, factor) | None, rest). Unknown unit words stay in `rest`."""
    t = re.sub(r"\bfl\.? ?oz\b", "floz", text.lower())
    m = _LEAD.match(t)
    if not m:
        return None, None, t
    num = parse_number(m["num"])
    unit_word, rest = m["unit"], m["rest"]
    unit = _UNITS.get(unit_word) if unit_word else None
    if unit_word and unit is None:
        rest = f"{unit_word} {rest}".strip()
    return num, unit, rest


def _canonical(num: float, unit, name: str) -> tuple[float, str]:
    if unit is None:
        return num, "piece"
    canon, factor = unit
    amount = num * factor
    if canon in ("g", "ml"):
        target = default_unit(name) if name else canon
        if target in ("g", "ml") and target != canon:
            converted = to_unit(amount, canon, target, name)
            if converted is not None:
                return converted, target
    return amount, canon


def parse_quantity(text: str, name: str = "") -> tuple[float | None, str | None]:
    """'2 cups' -> (370.0, 'g') for rice; '1 can (400 g)' -> (1.0, 'can'); 'to taste' -> (None, None)."""
    num, unit, _rest = _split(text)
    if num is None or num <= 0:
        return None, None
    amount, canon = _canonical(num, unit, name)
    return round(amount, 2), canon


def parse_item(text: str) -> tuple[str, float | None, str | None]:
    """'6 eggs' -> ('eggs', 6.0, 'piece'); '500 g of rice' -> ('rice', 500.0, 'g'); 'lemons' -> ('lemons', None, None)."""
    t = re.sub(r"\s+", " ", text.strip().lower()).strip(" .!,")
    t = re.sub(r"^(?:some|more|the|my)\s+", "", t)
    num, unit, rest = _split(t)
    if num is None:
        return re.sub(r"^(?:an?)\s+", "", t), None, None
    name = re.sub(r"^(?:of\s+)?(?:(?:an?|the)\s+)?", "", rest).strip()
    if not name:
        return "", None, None
    if num <= 0:
        return name, 0.0, "piece"
    amount, canon = _canonical(num, unit, name)
    return name, round(amount, 2), canon


# ------------------------------------------------------------------ formatting
def fmt_num(n: float) -> str:
    return str(int(round(n))) if abs(n - round(n)) < 0.05 else f"{n:.1f}".rstrip("0").rstrip(".")


def _fmt_metric(n: float) -> str:
    """Whole numbers from 10 up ('185 g'), one decimal below ('5.9 g'): easy to read and to say aloud."""
    return str(int(round(n))) if n >= 10 else fmt_num(n)


def _fmt_fraction(n: float, step: float) -> str:
    n = round(n / step) * step
    whole, frac = int(n), n - int(n)
    names = {0.25: "1/4", 0.5: "1/2", 0.75: "3/4", 0.125: "1/8", 0.375: "3/8", 0.625: "5/8", 0.875: "7/8"}
    f = names.get(round(frac, 3))
    if f is None:
        return str(whole) if not frac else fmt_num(n)
    return f"{whole} {f}" if whole else f


def format_amount(amount: float, unit: str, system: str = "metric") -> str:
    if unit == "g":
        if system == "us":
            oz = amount / 28.3495
            return f"{fmt_num(oz / 16)} lb" if oz >= 16 else f"{fmt_num(oz)} oz"
        return f"{fmt_num(amount / 1000)} kg" if amount >= 1000 else f"{_fmt_metric(amount)} g"
    if unit == "ml":
        if system == "us":
            if amount < 14:
                return f"{_fmt_fraction(amount / 4.92892, 0.25)} tsp"
            if amount < 59:
                return f"{_fmt_fraction(amount / 14.7868, 0.5)} tbsp"
            cups = round(amount / 236.588 / 0.25) * 0.25
            return f"{_fmt_fraction(cups, 0.25)} {'cup' if cups <= 1 else 'cups'}"
        return f"{fmt_num(amount / 1000)} l" if amount >= 1000 else f"{_fmt_metric(amount)} ml"
    return f"{fmt_num(amount)} {unit}{'s' if amount != 1 else ''}"


def format_item(label: str, amount: float | None, unit: str | None, system: str = "metric") -> str:
    """Speakable text for a pantry or recipe line, in the user's unit system."""
    if amount is None or unit is None:
        return label
    n = fmt_num(amount)
    if unit == "piece":
        return f"{n} {pluralize(label, amount)}"
    if unit in COUNT_UNITS:
        base = re.sub(r"^canned\s+", "", label)
        return f"{n} {unit}{'s' if amount != 1 else ''} of {base}"
    return f"{format_amount(amount, unit, system)} {label}"


# ---------------------------------------------------- "switch to US units" phrases
_UNITS_VERB = re.compile(r"\b(switch|change|set|use|show|convert|go|make|put)\b", re.I)
_UNITS_CONTEXT = re.compile(r"\b(units?|measurements?|measuring|metric|imperial|customary|system)\b", re.I)
_UNITS_QUESTION = re.compile(r"\b(what|which|how|why|when|where)\b", re.I)
_METRIC_WORD = r"metric|grams?|millilit\w+|kilograms?"
_US_WORD = (r"u\.?s\.?(?=\s*(?:units?|measurements?|customary|system|standard)\b|\s*[.!]?$)|american|imperial|customary"
            r"|cups?|ounces?|pounds?|fahrenheit")


def parse_units_command(text: str) -> str | None:
    """'switch to US units', 'change the measurements from metric to US', 'use metric' -> 'us' | 'metric'.
    The last system mentioned wins. Questions, and anything with numbers ('use 2 cups of rice'), are not commands."""
    t = text.strip()
    if (not _UNITS_VERB.search(t) or not _UNITS_CONTEXT.search(t) or _UNITS_QUESTION.search(t) or re.search(r"\d", t)):
        return None
    last = None
    for m in re.finditer(rf"(?P<metric>\b(?:{_METRIC_WORD})\b)|(?P<us>\b(?:{_US_WORD})(?!\w))", t, re.I):
        last = "metric" if m.group("metric") else "us"
    return last
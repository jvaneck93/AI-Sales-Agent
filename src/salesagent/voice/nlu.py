"""Rule-based understanding of what a prospect says (Dutch).

Used by the agent simulator, the eval harness, post-call classification
without a structured result, and feature extraction from recordings. It is
deliberately simple and transparent; an LLM classifier can replace it later
behind the same functions.
"""

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from salesagent.voice.library import OPT_OUT

TZ = ZoneInfo("Europe/Amsterdam")
WEEKDAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag"]
MONTHS = [
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
]
YES = re.compile(
    r"\b(ja|jazeker|klopt|zeker|dat ben ik|spreekt u mee|prima|goed|is goed|oké|oke|"
    r"akkoord|graag|dat kan|doe maar|interessant|mag|prima)\b"
)
NO = re.compile(r"\b(nee|neen|niet|geen|liever niet)\b")
HANG_UP = "[ophangen]"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def detect_objection(text: str, library: list[dict]) -> str | None:
    """The first matching objection category. Opt-out always wins."""
    t = normalize(text)
    ordered = sorted(library, key=lambda e: e["category"] != OPT_OUT)
    for entry in ordered:
        if any(trigger in t for trigger in entry["triggers"]):
            return entry["category"]
    return None


def is_yes(text: str) -> bool:
    t = normalize(text)
    return bool(YES.search(t)) and not re.match(r"^(nee|neen)\b", t)


def is_no(text: str) -> bool:
    t = normalize(text)
    return bool(re.match(r"^(nee|neen)\b", t)) or (bool(NO.search(t)) and not is_yes(t))


ALL_SCOPE = re.compile(r"\b(helemaal|niemand|alles|allemaal|nergens|geen enkel|alle)\b")


def opt_out_scope(text: str, brand_name: str) -> str:
    """'merk' only when the person clearly limits it to this brand. Else 'alles'.

    Decision: when in doubt, block everything.
    """
    t = normalize(text)
    if ALL_SCOPE.search(t):
        return "alles"
    brand = brand_name.lower()
    only = "alleen" in t or "enkel" in t or "specifiek" in t or "voor dit merk" in t
    if (only and (brand in t or "merk" in t or "dit" in t)) or t in (brand, f"alleen {brand}"):
        return "merk"
    return "alles"


def _at_ten(day) -> datetime:
    return datetime.combine(day, time(10, 0), TZ)


def parse_callback(text: str, now: datetime) -> datetime | None:
    """A callback moment from Dutch text, or None when no moment is named."""
    t = normalize(text)
    today = now.astimezone(TZ).date()
    if "morgen" in t and "overmorgen" not in t:
        return _at_ten(today + timedelta(days=1))
    if "overmorgen" in t:
        return _at_ten(today + timedelta(days=2))
    if "volgende week" in t or "week later" in t:
        return _at_ten(today + timedelta(days=7 - today.weekday()))
    match = re.search(r"over (\w+) (week|weken|maand|maanden)", t)
    if match:
        words = {"een": 1, "één": 1, "twee": 2, "drie": 3, "vier": 4, "zes": 6, "paar": 2}
        n = int(match.group(1)) if match.group(1).isdigit() else words.get(match.group(1), 1)
        days = n * (7 if match.group(2).startswith("we") else 30)
        return _at_ten(today + timedelta(days=days))
    if "na de zomer" in t:
        year = today.year + (1 if today.month >= 9 else 0)
        return _at_ten(today.replace(year=year, month=9, day=1))
    if "volgend jaar" in t:
        return _at_ten(today.replace(year=today.year + 1, month=1, day=15))
    for index, name in enumerate(WEEKDAYS):
        if name in t:
            ahead = (index - today.weekday()) % 7 or 7
            return _at_ten(today + timedelta(days=ahead))
    for index, name in enumerate(MONTHS, start=1):
        if re.search(rf"\b{name}\b", t):
            year = today.year + (1 if index <= today.month else 0)
            return _at_ten(today.replace(year=year, month=index, day=1))
    return None


def pick_slot(text: str, options: list[str]) -> int | None:
    """Which of the spoken slot options the prospect chose, by weekday or order."""
    t = normalize(text)
    for index, option in enumerate(options):
        if option.split()[0] in t:
            return index
    if re.search(r"\b(eerste|die eerste)\b", t):
        return 0
    if re.search(r"\b(tweede|laatste)\b", t):
        return min(1, len(options) - 1) if "tweede" in t else len(options) - 1
    if is_yes(t) and len(options) == 1:
        return 0
    return None

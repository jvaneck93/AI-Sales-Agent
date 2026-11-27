"""Remove personal data from transcripts before analysis (and before any LLM).

Replaced by tokens: e-mail, phone numbers, Dutch licence plates, postcodes,
KvK numbers, IBAN, and names from a known list (contacts in our database and
agent names from the CATI export) plus names introduced as "met <Naam>".
"""

import re

PATTERNS = [
    ("[EMAIL]", re.compile(r"\b[\w.+-]+@[\w-]+(\.[\w-]+)+\b")),
    ("[IBAN]", re.compile(r"\bNL\d{2}\s?[A-Z]{4}(\s?\d){10}\b", re.I)),
    ("[TELEFOON]", re.compile(r"(\+31|0031|\b0)[\s-]?\(?\d{1,3}\)?([\s-]?\d){6,8}\b")),
    ("[KVK]", re.compile(r"\b\d{8}\b")),
    ("[POSTCODE]", re.compile(r"\b\d{4}\s?[A-Z]{2}\b")),
    (
        "[KENTEKEN]",
        re.compile(
            r"\b([A-Z]{2}-?\d{2}-?[A-Z]{2}|\d{2}-?[A-Z]{2}-?\d{2}|[A-Z]{2}-?[A-Z]{2}-?\d{2}|"
            r"\d{2}-?[A-Z]{3}-?\d|[A-Z]-?\d{3}-?[A-Z]{2}|[A-Z]{2}-?\d{3}-?[A-Z]|"
            r"[A-Z]{3}-?\d{2}-?[A-Z]|\d-?[A-Z]{3}-?\d{2})\b"
        ),
    ),
]
INTRO = re.compile(
    r"\b((?i:met|ben|is|heet|naam is|spreekt u met|spreek je met))\s+"
    r"((?:[A-Z][a-zà-ÿ]+)(?:\s+(?:van|de|der|den|ter|ten|van der|van den))?(?:\s+[A-Z][a-zà-ÿ]+)?)"
)
TITLE = re.compile(
    r"\b(collega|meneer|mevrouw|heer|dhr\.?|mw\.?|mevr\.?)\s+"
    r"((?:van |de |van der |van den )?[A-Z][a-zà-ÿ]+)"
)
NOT_NAMES = {
    "Goedemorgen",
    "Goedemiddag",
    "Dag",
    "Ja",
    "Nee",
    "Prima",
    "Oké",
    "De",
    "Het",
    "Een",
    "Wij",
    "We",
    "Ik",
    "U",
}


def pseudonymize(text: str, known_names: list[str] = ()) -> str:
    for token, pattern in PATTERNS:
        text = pattern.sub(token, text)
    for name in sorted({n for n in known_names if n and len(n) > 2}, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(name)}\b", "[NAAM]", text, flags=re.I)
        for part in name.split():
            if len(part) > 3 and part[0].isupper():
                text = re.sub(rf"\b{re.escape(part)}\b", "[NAAM]", text)

    def intro(match: re.Match) -> str:
        name = match.group(2)
        if name.split()[0] in NOT_NAMES:
            return match.group(0)
        return f"{match.group(1)} [NAAM]"

    text = INTRO.sub(intro, text)
    return TITLE.sub(lambda m: f"{m.group(1)} [NAAM]", text)


def pseudonymize_turns(turns: list[dict], known_names: list[str] = ()) -> list[dict]:
    return [dict(t, message=pseudonymize(t["message"], known_names)) for t in turns]

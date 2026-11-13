"""Normalisation of raw input values (phone, KvK number, legal form, ...)."""

import re
import unicodedata

from salesagent.models import LegalForm

_KVK_RE = re.compile(r"\d{8}")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_WEBSITE_RE = re.compile(r"https?://[^\s/]+\.[^\s/]+.*")


def normalize_kvk(raw: str) -> str | None:
    """Return the 8-digit KvK number, or None when it is not exactly 8 digits."""
    value = raw.strip().replace(" ", "")
    return value if _KVK_RE.fullmatch(value) else None


def normalize_phone(raw: str) -> str | None:
    """Return a phone number in E.164 form (+31...), or None when invalid."""
    value = raw.strip().replace("(0)", "")
    value = re.sub(r"[\s\-().]", "", value)
    if value.startswith("00"):
        value = "+" + value[2:]
    elif value.startswith("0") and len(value) == 10:
        value = "+31" + value[1:]
    if re.fullmatch(r"\+\d{9,15}", value):
        return value
    return None


def normalize_email(raw: str) -> str | None:
    value = raw.strip().lower()
    return value if _EMAIL_RE.fullmatch(value) else None


def normalize_website(raw: str) -> str | None:
    value = raw.strip()
    if not re.match(r"https?://", value, re.IGNORECASE):
        value = "https://" + value
    return value if _WEBSITE_RE.fullmatch(value) else None


_LEGAL_FORM_ALIASES: dict[str, LegalForm] = {
    "bv": LegalForm.BV,
    "beslotenvennootschap": LegalForm.BV,
    "beslotenvennootschapmetgewonestructuur": LegalForm.BV,
    "beslotenvennootschapmetbeperkteaansprakelijkheid": LegalForm.BV,
    "nv": LegalForm.NV,
    "naamlozevennootschap": LegalForm.NV,
    "stichting": LegalForm.STICHTING,
    "vereniging": LegalForm.VERENIGING,
    "verenigingmetvolledigerechtsbevoegdheid": LegalForm.VERENIGING,
    "verenigingvaneigenaars": LegalForm.VERENIGING,
    "cooperatie": LegalForm.COOPERATIE,
    "cooperatieua": LegalForm.COOPERATIE,
    "cooperatieba": LegalForm.COOPERATIE,
    "cooperatiewa": LegalForm.COOPERATIE,
    "eenmanszaak": LegalForm.EENMANSZAAK,
    "zzp": LegalForm.EENMANSZAAK,
    "vof": LegalForm.VOF,
    "vennootschaponderfirma": LegalForm.VOF,
    "cv": LegalForm.CV,
    "commanditairevennootschap": LegalForm.CV,
    "maatschap": LegalForm.MAATSCHAP,
}


def normalize_legal_form(raw: str | None) -> LegalForm:
    """Map a raw legal form to a known one.

    Only exact, known spellings are accepted. Anything else is UNKNOWN, which
    means "do not call" (hard rule 1). This includes an association with
    limited legal capacity ("beperkte rechtsbevoegdheid").
    """
    if not raw:
        return LegalForm.UNKNOWN
    decomposed = unicodedata.normalize("NFKD", raw.lower())
    key = "".join(ch for ch in decomposed if ch.isalnum() and not unicodedata.combining(ch))
    return _LEGAL_FORM_ALIASES.get(key, LegalForm.UNKNOWN)

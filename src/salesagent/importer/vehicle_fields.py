"""Validation of vehicle values, shared by the lead import and the vehicle import."""

import re
from datetime import date, datetime

VEHICLE_OPTIONAL = (
    "make",
    "model",
    "build_year",
    "ownership",
    "lease_company",
    "contract_end_date",
)
_OWNERSHIP = {
    "koop": "koop",
    "gekocht": "koop",
    "eigendom": "koop",
    "lease": "lease",
    "operational lease": "lease",
    "financial lease": "lease",
    "financiele lease": "lease",
    "private lease": "lease",
}
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y%m%d")


def normalize_license_plate(raw: str) -> str | None:
    """Dutch plates have 6 letters/digits; stored in upper case without dashes."""
    value = re.sub(r"[\s\-]", "", raw).upper()
    return value if re.fullmatch(r"[A-Z0-9]{6}", value) else None


def parse_date(raw: str) -> date | None:
    value = raw.strip()
    if "T" in value:
        value = value.split("T", 1)[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_build_year(raw: str) -> int | None:
    """A year, or the year of a date such as 'datum eerste toelating'."""
    value = raw.strip()
    if value.isdigit() and len(value) == 4:
        year = int(value)
    else:
        parsed = parse_date(value)
        if parsed is None:
            return None
        year = parsed.year
    return year if 1950 <= year <= date.today().year + 1 else None


def vehicle_fields(values: dict[str, str], warn) -> dict[str, object]:
    """Validated vehicle fields. ``warn(reason)`` is called for each skipped value."""
    data: dict[str, object] = {}
    for fld in ("make", "model", "lease_company"):
        if values.get(fld):
            data[fld] = values[fld][:64] if fld != "lease_company" else values[fld]
    if values.get("build_year"):
        year = parse_build_year(values["build_year"])
        if year is None:
            warn("Bouwjaar is ongeldig en overgeslagen")
        else:
            data["build_year"] = year
    if values.get("ownership"):
        ownership = _OWNERSHIP.get(values["ownership"].strip().lower())
        if ownership is None:
            warn("Koop/lease niet herkend en overgeslagen")
        else:
            data["ownership"] = ownership
    if values.get("contract_end_date"):
        end_date = parse_date(values["contract_end_date"])
        if end_date is None:
            warn("Einddatum contract is ongeldig en overgeslagen")
        else:
            data["contract_end_date"] = end_date
    return data

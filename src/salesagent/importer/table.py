"""Read CSV and Excel files as rows of text, and match their columns to our fields.

Column names in the source file are not fixed. A column is matched on the name in
the mapping file first, then on a list of common Dutch and English names. Matching
ignores case, spaces and punctuation.
"""

import csv
import io
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
CSV_SUFFIXES = {".csv", ".txt", ".tsv"}
HEADER_SEARCH_ROWS = 20

# Field -> common column names. One name belongs to one field only.
ALIASES: dict[str, tuple[str, ...]] = {
    "name": (
        "bedrijfsnaam",
        "naam",
        "handelsnaam",
        "statutaire naam",
        "naam onderneming",
        "onderneming",
        "organisatie",
        "organisatienaam",
        "bedrijf",
        "company",
        "company name",
    ),
    "kvk_number": (
        "kvk-nummer",
        "kvk",
        "kvk nr",
        "kvknr",
        "kvk-nr",
        "kvk nummer",
        "kamer van koophandel",
        "kvk-nummer (8 cijfers)",
        "coc",
        "coc number",
        "chamber of commerce number",
    ),
    "legal_form": ("rechtsvorm", "rechtsvorm omschrijving", "juridische vorm", "legal form"),
    "address": ("adres", "vestigingsadres", "bezoekadres", "straat en huisnummer", "address"),
    "street": ("straat", "straatnaam", "street"),
    "house_number": ("huisnummer", "huisnr", "nummer", "huisnummer toevoeging", "house number"),
    "postal_code": ("postcode", "postal code", "zip"),
    "city": ("plaats", "vestigingsplaats", "woonplaats", "stad", "plaatsnaam", "city"),
    "sector": (
        "sector",
        "branche",
        "sbi omschrijving",
        "sbi-omschrijving",
        "activiteit",
        "hoofdactiviteit",
        "industry",
    ),
    "website": ("website", "url", "internetadres", "www", "web"),
    "phone": (
        "telefoon",
        "telefoonnummer",
        "tel",
        "tel.",
        "telefoon algemeen",
        "algemeen nummer",
        "phone",
    ),
    "email": ("e-mail", "email", "e-mailadres", "emailadres", "mail", "algemeen e-mailadres"),
    "contact_name": ("contactpersoon", "naam contactpersoon", "contact"),
    "contact_role": ("functie", "functie contactpersoon", "rol"),
    "contact_phone": (
        "telefoon contactpersoon",
        "mobiel",
        "mobiel nummer",
        "direct nummer",
        "telefoon contact",
    ),
    "contact_email": ("e-mail contactpersoon", "email contactpersoon", "e-mail contact"),
    "license_plate": ("kenteken", "kentekennummer", "license plate", "registration"),
    "make": ("merk", "automerk", "voertuigmerk", "make"),
    "model": ("model", "handelsbenaming", "type voertuig"),
    "build_year": ("bouwjaar", "datum eerste toelating", "eerste toelating"),
    "ownership": ("koop of lease", "koop/lease", "lease/koop", "eigendom", "financiering"),
    "lease_company": ("leasemaatschappij", "lessor", "leasebedrijf", "leasemij"),
    "contract_end_date": (
        "einddatum contract",
        "einddatum lease",
        "einddatum leasecontract",
        "contract einde",
        "einde contract",
        "einddatum",
    ),
}

FIELD_LABELS = {
    "name": "Bedrijfsnaam",
    "kvk_number": "KvK-nummer",
    "legal_form": "Rechtsvorm",
    "address": "Adres",
    "street": "Straat",
    "house_number": "Huisnummer",
    "postal_code": "Postcode",
    "city": "Plaats",
    "sector": "Sector",
    "website": "Website",
    "phone": "Telefoon",
    "email": "E-mail",
    "contact_name": "Contactpersoon",
    "contact_role": "Functie",
    "contact_phone": "Telefoon contactpersoon",
    "contact_email": "E-mail contactpersoon",
    "license_plate": "Kenteken",
    "make": "Merk",
    "model": "Model",
    "build_year": "Bouwjaar",
    "ownership": "Koop of lease",
    "lease_company": "Leasemaatschappij",
    "contract_end_date": "Einddatum contract",
}


class TableError(Exception):
    """The file cannot be read. Message is Dutch."""


def header_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(ch for ch in decomposed if ch.isalnum() and not unicodedata.combining(ch))


_ALIAS_INDEX = {header_key(a): fld for fld, names in ALIASES.items() for a in names}


@dataclass
class Table:
    header: list[str]
    header_line: int
    rows: Iterator[tuple[int, list[str]]]
    detail: str


@dataclass
class ColumnMatch:
    indexes: dict[str, int] = field(default_factory=dict)
    used: dict[str, str] = field(default_factory=dict)  # field -> column header
    ignored: list[str] = field(default_factory=list)


def match_columns(header: list[str], configured: dict[str, str], fields: tuple[str, ...]):
    """Match header columns to fields: configured name first, then common names."""
    keys = [header_key(h) for h in header]
    result = ColumnMatch()
    taken: set[int] = set()
    for fld in fields:
        wanted = configured.get(fld)
        if wanted and header_key(wanted) in keys:
            index = keys.index(header_key(wanted))
            if index not in taken:
                result.indexes[fld] = index
                taken.add(index)
    for index, key in enumerate(keys):
        fld = _ALIAS_INDEX.get(key)
        if index in taken or fld is None or fld not in fields or fld in result.indexes:
            continue
        result.indexes[fld] = index
        taken.add(index)
    result.used = {fld: header[i] for fld, i in sorted(result.indexes.items(), key=lambda x: x[1])}
    result.ignored = [h for i, h in enumerate(header) if i not in taken and h.strip()]
    return result


def _looks_like_header(cells: list[str], configured: dict[str, str]) -> bool:
    keys = {header_key(c) for c in cells if c}
    wanted = {header_key(configured[f]) for f in ("name", "kvk_number") if configured.get(f)}
    wanted |= {k for k, f in _ALIAS_INDEX.items() if f in ("name", "kvk_number")}
    return bool(keys & wanted)


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "ja" if value else "nee"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _decode(raw: bytes, encoding: str) -> tuple[str, str]:
    if encoding and encoding != "auto":
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError as exc:
            raise TableError(f"Het bestand is niet te lezen als {encoding}.") from exc
    try:
        return raw.decode("utf-8-sig"), "UTF-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "Windows-1252"


def _read_csv(path: Path, configured: dict[str, str], delimiter: str, encoding: str) -> Table:
    text, used_encoding = _decode(path.read_bytes(), encoding)
    first = text.lstrip().split("\n", 1)[0]
    if not delimiter or delimiter == "auto":
        delimiter = max((";", ",", "\t", "|"), key=first.count)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    header, header_line, buffered = None, 0, []
    for cells in reader:
        buffered.append((reader.line_num, cells))
        if _looks_like_header(cells, configured):
            header, header_line = cells, reader.line_num
            break
        if len(buffered) >= HEADER_SEARCH_ROWS:
            break
    if header is None:
        # No known column found: fall back to the first non-empty row for the error message.
        header_line, header = next(((n, c) for n, c in buffered if any(c)), (1, []))
        rows: Iterator = iter(())
    else:
        rows = ((reader.line_num, cells) for cells in reader)
    name = {";": "puntkomma", ",": "komma", "\t": "tab", "|": "streep"}.get(delimiter, delimiter)
    return Table(header, header_line, rows, f"CSV ({used_encoding}, gescheiden door {name})")


def _excel_rows(workbook, iterator, start: int):
    try:
        for number, values in enumerate(iterator, start=start):
            yield number, [_cell_text(v) for v in values]
    finally:
        workbook.close()  # releases the file, so a temporary upload can be deleted


def _read_excel(path: Path, configured: dict[str, str], sheet: str | None) -> Table:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency is installed
        raise TableError("Excel-ondersteuning ontbreekt (openpyxl).") from exc
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise TableError("Het Excel-bestand kan niet worden geopend.") from exc
    if sheet:
        if sheet not in workbook.sheetnames:
            raise TableError(f"Werkblad '{sheet}' bestaat niet in het Excel-bestand.")
        candidates = [workbook[sheet]]
    else:
        candidates = list(workbook.worksheets)
    for worksheet in candidates:
        iterator = worksheet.iter_rows(values_only=True)
        for number, values in enumerate(iterator, start=1):
            cells = [_cell_text(v) for v in values]
            if _looks_like_header(cells, configured):
                rows = _excel_rows(workbook, iterator, number + 1)
                return Table(cells, number, rows, f"Excel (werkblad '{worksheet.title}')")
            if number >= HEADER_SEARCH_ROWS:
                break
    first = candidates[0]
    header = next(
        ([_cell_text(v) for v in vals] for vals in first.iter_rows(values_only=True) if any(vals)),
        [],
    )
    workbook.close()
    return Table(header, 1, iter(()), f"Excel (werkblad '{first.title}')")


def read_table(
    path: Path,
    configured: dict[str, str],
    *,
    delimiter: str = "auto",
    encoding: str = "auto",
    sheet: str | None = None,
) -> Table:
    """Open a CSV or Excel file and find its header row."""
    suffix = path.suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        return _read_excel(path, configured, sheet)
    if suffix == ".xls":
        raise TableError("Oud Excel-formaat (.xls). Sla het bestand op als .xlsx of als CSV.")
    if suffix in CSV_SUFFIXES or not suffix:
        return _read_csv(path, configured, delimiter, encoding)
    raise TableError(f"Bestandstype {suffix} wordt niet ondersteund. Gebruik .xlsx of .csv.")


def clean_kvk(raw: str) -> tuple[str | None, bool]:
    """KvK number as 8 digits, and whether a leading zero was restored.

    Excel drops the leading zero of numbers such as 01234567. A KvK number always
    has 8 digits, so a 7-digit number gets its zero back (reported as a warning).
    """
    value = re.sub(r"[\s.\-]", "", raw)
    if re.fullmatch(r"\d{8}", value):
        return value, False
    if re.fullmatch(r"\d{7}", value):
        return "0" + value, True
    return None, False

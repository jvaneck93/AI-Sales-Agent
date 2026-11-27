"""Lead import from CSV or Excel: column matching, validation and deduplication.

A file has one row per company, or one row per vehicle with the company columns
repeated (as in a fleet export). Rows with the same KvK number become one company.
Column names come from config/import_mapping.yaml, with common names as fallback.
"""

import csv
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from salesagent.importer.table import (
    FIELD_LABELS,
    TableError,
    clean_kvk,
    match_columns,
    read_table,
)
from salesagent.importer.vehicle_fields import (
    VEHICLE_OPTIONAL,
    normalize_license_plate,
    vehicle_fields,
)
from salesagent.models import Company, Contact, LegalForm, Vehicle, utcnow
from salesagent.normalize import (
    normalize_email,
    normalize_legal_form,
    normalize_phone,
    normalize_website,
)

REQUIRED_FIELDS = ("name", "kvk_number")
CONTACT_FIELDS = ("contact_name", "contact_role", "contact_phone", "contact_email")
ADDRESS_PARTS = ("street", "house_number", "postal_code")
VEHICLE_FIELDS = ("license_plate", *VEHICLE_OPTIONAL)
OPTIONAL_FIELDS = (
    "legal_form",
    "address",
    "city",
    "sector",
    "website",
    "phone",
    "email",
    *CONTACT_FIELDS,
    *ADDRESS_PARTS,
    *VEHICLE_FIELDS,
)
FLUSH_EVERY = 5000
LIST_ALL_BELOW = 40  # list every issue when there are fewer than this


class ImportConfigError(Exception):
    """The mapping file or the file's columns are not usable. Message is in Dutch."""


@dataclass(frozen=True)
class ImportMapping:
    columns: dict[str, str]
    delimiter: str = "auto"
    encoding: str = "auto"
    sheet: str | None = None


@dataclass(frozen=True)
class RowIssue:
    """A problem with one line. Holds no names, only line, KvK number and reason."""

    line: int
    kvk_number: str | None
    reason: str


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _group_key(reason: str) -> str:
    return re.sub(r"\s*\(eerder op regel \d+\)", "", reason)


@dataclass
class ImportReport:
    total: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    rejected: list[RowIssue] = field(default_factory=list)
    warnings: list[RowIssue] = field(default_factory=list)
    source: str = ""
    columns_used: dict[str, str] = field(default_factory=dict)
    columns_missing: list[str] = field(default_factory=list)
    columns_ignored: list[str] = field(default_factory=list)
    legal_forms: Counter = field(default_factory=Counter)
    callable_forms: tuple[str, ...] = ()
    vehicle_mode: bool = False
    vehicles_created: int = 0
    vehicles_updated: int = 0
    seconds: float = 0.0
    report_file: Path | None = None

    @property
    def callable_count(self) -> int:
        return sum(self.legal_forms[f] for f in self.callable_forms)

    def render(self) -> str:
        lines = ["Importrapport"]
        if self.source:
            lines.append(f"  Bestand:          {self.source}")
        lines += [
            f"  Regels gelezen:   {_fmt(self.total)}",
            f"  Nieuw:            {_fmt(self.created)}",
            f"  Bijgewerkt:       {_fmt(self.updated)}",
            f"  Ongewijzigd:      {_fmt(self.unchanged)}",
        ]
        if self.vehicle_mode:
            lines.append(
                f"  Voertuigen:       {_fmt(self.vehicles_created)} nieuw, "
                f"{_fmt(self.vehicles_updated)} bijgewerkt"
            )
        lines += [
            f"  Afgekeurd:        {_fmt(len(self.rejected))}",
            f"  Waarschuwingen:   {_fmt(len(self.warnings))}",
        ]
        if self.seconds:
            lines.append(f"  Duur:             {self.seconds:.1f} s".replace(".", ","))
        if self.legal_forms:
            forms = " · ".join(f"{form} {_fmt(n)}" for form, n in self.legal_forms.most_common())
            lines += ["", "Rechtsvorm (bedrijven in dit bestand):", f"  {forms}"]
            if self.callable_forms:
                lines.append(
                    f"  Koud belbaar op rechtsvorm ({', '.join(self.callable_forms)}): "
                    f"{_fmt(self.callable_count)}. De rechtsvorm wordt per belronde "
                    "opnieuw gecontroleerd."
                )
        if self.columns_used:
            lines += ["", "Kolommen:"]
            lines += [
                f"  {FIELD_LABELS.get(f, f)} ← '{col}'" for f, col in self.columns_used.items()
            ]
            if self.columns_missing:
                lines.append("  Niet gevonden (optioneel): " + ", ".join(self.columns_missing))
            if self.columns_ignored:
                lines.append(
                    "  Niet gebruikt: " + ", ".join(f"'{c}'" for c in self.columns_ignored)
                )
        for title, issues in (("Afgekeurd", self.rejected), ("Waarschuwingen", self.warnings)):
            if not issues:
                continue
            lines.append(f"\n{title}:")
            if len(issues) < LIST_ALL_BELOW:
                lines.extend(
                    f"  regel {i.line} (KvK {i.kvk_number or 'onbekend'}): {i.reason}"
                    for i in issues
                )
                continue
            groups: dict[str, list[RowIssue]] = {}
            for issue in issues:
                groups.setdefault(_group_key(issue.reason), []).append(issue)
            for reason, group in sorted(groups.items(), key=lambda g: -len(g[1])):
                examples = ", ".join(str(i.line) for i in group[:5])
                lines.append(f"  {_fmt(len(group))}x {reason} (bv. regel {examples})")
        if self.report_file:
            lines.append(f"\nVolledige lijst per regel: {self.report_file}")
        return "\n".join(lines)

    def write_issues(self, directory: Path) -> None:
        """Write all issues to a CSV for Excel (line, KvK number, kind, reason)."""
        if not (self.rejected or self.warnings):
            return
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"import-{datetime.now():%Y%m%d-%H%M%S}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(["Regel", "KvK-nummer", "Soort", "Reden"])
            for kind, issues in (("afgekeurd", self.rejected), ("waarschuwing", self.warnings)):
                for i in issues:
                    writer.writerow([i.line, i.kvk_number or "", kind, i.reason])
        self.report_file = path


def load_mapping(
    path: Path,
    *,
    section: str = "columns",
    required: tuple[str, ...] = REQUIRED_FIELDS,
    optional: tuple[str, ...] = OPTIONAL_FIELDS,
) -> ImportMapping:
    """Read one column mapping (``section``) from the YAML mapping file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ImportConfigError(f"Kan kolomtoewijzing niet lezen: {path}") from exc
    columns = raw.get(section) or {}
    unknown = set(columns) - set(required) - set(optional)
    if unknown:
        raise ImportConfigError(
            f"Onbekende velden in kolomtoewijzing ({section}): {', '.join(sorted(unknown))}"
        )
    missing = [f for f in required if not columns.get(f)]
    if missing:
        raise ImportConfigError(
            f"Verplichte velden ontbreken in kolomtoewijzing ({section}): {', '.join(missing)}"
        )
    return ImportMapping(
        columns={k: str(v) for k, v in columns.items() if v},
        delimiter=str(raw.get("delimiter") or "auto"),
        encoding=str(raw.get("encoding") or "auto"),
        sheet=raw.get("sheet") or None,
    )


def open_table(
    path: Path,
    mapping: ImportMapping,
    *,
    required: tuple[str, ...] = REQUIRED_FIELDS,
    optional: tuple[str, ...] = OPTIONAL_FIELDS,
    report: ImportReport | None = None,
):
    """Yield (line number, values per field) for each non-empty row."""
    try:
        table = read_table(
            path,
            mapping.columns,
            delimiter=mapping.delimiter,
            encoding=mapping.encoding,
            sheet=mapping.sheet,
        )
    except TableError as exc:
        raise ImportConfigError(str(exc)) from exc
    match = match_columns(table.header, mapping.columns, required + optional)
    missing = [f for f in required if f not in match.indexes]
    if missing:
        wanted = ", ".join(
            f"{FIELD_LABELS[f]} (kolom '{mapping.columns.get(f, FIELD_LABELS[f])}')"
            for f in missing
        )
        found = ", ".join(f"'{h}'" for h in table.header if h) or "geen"
        raise ImportConfigError(
            f"Verplichte kolommen niet gevonden in het bestand: {wanted}. "
            f"Gevonden kolommen: {found}. Hernoem de kolom in het bestand of pas "
            "config/import_mapping.yaml aan."
        )
    if report is not None:
        report.source = f"{table.detail}, kopregel op regel {table.header_line}"
        report.columns_used = match.used
        report.columns_missing = [
            FIELD_LABELS[f]
            for f in optional
            if f not in match.indexes
            and f not in ADDRESS_PARTS
            and (f not in VEHICLE_OPTIONAL or "license_plate" in match.indexes)
            and not (f == "address" and "street" in match.indexes)
            and f != "license_plate"
        ]
        report.columns_ignored = match.ignored
    indexes = match.indexes

    def rows():
        for line, cells in table.rows:
            values = {f: (cells[i].strip() if i < len(cells) else "") for f, i in indexes.items()}
            if any(values.values()):
                yield line, values

    return rows(), set(indexes)


@dataclass
class _Context:
    session: Session
    report: ImportReport
    companies: dict[str, Company]
    plates: dict[str, Vehicle]
    vehicle_mode: bool
    seen: dict[str, int] = field(default_factory=dict)
    plate_lines: dict[str, int] = field(default_factory=dict)


def import_file(
    session: Session,
    path: Path,
    mapping: ImportMapping,
    *,
    report_dir: Path | None = None,
) -> ImportReport:
    """Import companies, contact persons and (optionally) vehicles. Commits once at the end."""
    from salesagent.compliance.gate import callable_legal_forms

    started = time.perf_counter()
    report = ImportReport()
    rows, present = open_table(path, mapping, report=report)
    vehicle_mode = "license_plate" in present
    report.vehicle_mode = vehicle_mode

    query = select(Company)
    if present & set(CONTACT_FIELDS):
        query = query.options(selectinload(Company.contacts))
    companies = {c.kvk_number: c for c in session.scalars(query)}
    plates = (
        {v.license_plate: v for v in session.scalars(select(Vehicle)) if v.license_plate}
        if vehicle_mode
        else {}
    )
    ctx = _Context(session, report, companies, plates, vehicle_mode)
    for line, values in rows:
        report.total += 1
        _process_row(ctx, line, values)
        if report.total % FLUSH_EVERY == 0:
            session.flush()
    session.commit()

    order = [f.value for f in callable_legal_forms()]
    report.callable_forms = tuple(sorted(order))
    report.legal_forms = Counter(ctx.companies[k].legal_form.value for k in ctx.seen)
    report.seconds = time.perf_counter() - started
    if report_dir is not None:
        report.write_issues(report_dir)
    return report


# Kept for callers that still use the old name.
import_csv = import_file


def _process_row(ctx: _Context, line: int, values: dict[str, str]) -> None:
    report = ctx.report
    kvk, padded = clean_kvk(values["kvk_number"])
    if kvk is None:
        reason = (
            "KvK-nummer ontbreekt"
            if not values["kvk_number"]
            else "KvK-nummer is ongeldig (moet uit precies 8 cijfers bestaan)"
        )
        report.rejected.append(RowIssue(line, None, reason))
        return
    if padded and kvk not in ctx.seen:
        report.warnings.append(
            RowIssue(line, kvk, "KvK-nummer had 7 cijfers: voorloopnul aangevuld")
        )

    if kvk in ctx.seen:
        company = ctx.companies[kvk]
        if ctx.vehicle_mode:
            # Fleet export: one row per vehicle, company columns repeated.
            _add_vehicle(ctx, company, values, line, kvk)
            _upsert_contact(company, values, line, report)
            return
        first = f"eerder op regel {ctx.seen[kvk]}"
        if _upsert_contact(company, values, line, report):
            report.warnings.append(
                RowIssue(
                    line,
                    kvk,
                    f"Dubbel KvK-nummer ({first}): bedrijfsgegevens genegeerd, "
                    "contactpersoon toegevoegd",
                )
            )
        else:
            report.rejected.append(RowIssue(line, kvk, f"Dubbel KvK-nummer ({first})"))
        return
    if not values["name"]:
        report.rejected.append(RowIssue(line, kvk, "Bedrijfsnaam ontbreekt"))
        return
    ctx.seen[kvk] = line

    data: dict[str, object] = {"name": values["name"][:255]}
    address = values.get("address") or _compose_address(values)
    if address:
        data["address"] = address[:255]
    for fld in ("city", "sector"):
        if values.get(fld):
            data[fld] = values[fld][:255]
    for fld, normalizer, label in (
        ("phone", normalize_phone, "Telefoonnummer"),
        ("email", normalize_email, "E-mailadres"),
        ("website", normalize_website, "Website"),
    ):
        if values.get(fld):
            normalized = normalizer(values[fld])
            if normalized is None:
                report.warnings.append(RowIssue(line, kvk, f"{label} is ongeldig en overgeslagen"))
            else:
                data[fld] = normalized

    raw_form = values.get("legal_form", "")
    legal_form = normalize_legal_form(raw_form)
    if legal_form is LegalForm.UNKNOWN:
        reason = (
            "Rechtsvorm niet herkend" if raw_form else "Rechtsvorm ontbreekt"
        ) + ": wordt niet gebeld tot de rechtsvorm bekend is"
        report.warnings.append(RowIssue(line, kvk, reason))

    company = ctx.companies.get(kvk)
    if company is None:
        company = Company(kvk_number=kvk, legal_form=LegalForm.UNKNOWN, **data)
        _set_legal_form(company, legal_form, raw_form)
        ctx.session.add(company)
        ctx.companies[kvk] = company
        _upsert_contact(company, values, line, report)
        if ctx.vehicle_mode:
            _add_vehicle(ctx, company, values, line, kvk)
        report.created += 1
        return

    changed = _upsert_contact(company, values, line, report)
    for fld, value in data.items():
        if getattr(company, fld) != value:
            setattr(company, fld, value)
            changed = True
    # An unknown legal form in the file never overwrites a known one.
    if legal_form is not LegalForm.UNKNOWN and company.legal_form != legal_form:
        _set_legal_form(company, legal_form, raw_form)
        changed = True
    if ctx.vehicle_mode:
        _add_vehicle(ctx, company, values, line, kvk)
    if changed:
        report.updated += 1
    else:
        report.unchanged += 1


def _compose_address(values: dict[str, str]) -> str:
    street = " ".join(v for v in (values.get("street"), values.get("house_number")) if v)
    postal = values.get("postal_code", "")
    return ", ".join(v for v in (street, postal) if v)


def _add_vehicle(ctx: _Context, company: Company, values, line: int, kvk: str) -> None:
    report = ctx.report
    raw = values.get("license_plate", "")
    if not raw:
        if any(values.get(f) for f in VEHICLE_OPTIONAL):
            report.warnings.append(RowIssue(line, kvk, "Kenteken ontbreekt: voertuig overgeslagen"))
        return
    plate = normalize_license_plate(raw)
    if plate is None:
        report.warnings.append(RowIssue(line, kvk, "Kenteken is ongeldig: voertuig overgeslagen"))
        return
    if plate in ctx.plate_lines:
        report.warnings.append(
            RowIssue(
                line,
                kvk,
                f"Dubbel kenteken (eerder op regel {ctx.plate_lines[plate]}): overgeslagen",
            )
        )
        return
    ctx.plate_lines[plate] = line
    data = vehicle_fields(values, lambda r: report.warnings.append(RowIssue(line, kvk, r)))
    vehicle = ctx.plates.get(plate)
    if vehicle is None:
        vehicle = Vehicle(license_plate=plate, company=company, **data)
        ctx.session.add(vehicle)
        ctx.plates[plate] = vehicle
        report.vehicles_created += 1
        return
    changed = vehicle.company is not company
    if changed:
        vehicle.company = company
    for fld, value in data.items():
        if getattr(vehicle, fld) != value:
            setattr(vehicle, fld, value)
            changed = True
    if changed:
        report.vehicles_updated += 1


def _set_legal_form(company: Company, legal_form: LegalForm, raw: str) -> None:
    company.legal_form = legal_form
    company.legal_form_raw = raw[:255] if raw else None
    if legal_form is not LegalForm.UNKNOWN:
        company.legal_form_source = "csv"
        company.legal_form_checked_at = utcnow()


def _upsert_contact(
    company: Company, values: dict[str, str], line: int, report: ImportReport
) -> bool:
    """Add or update the contact person on this row. Returns True when something changed."""
    name = values.get("contact_name", "")
    role = values.get("contact_role", "")
    if not name and not role:
        return False

    data: dict[str, str] = {}
    for fld, normalizer, label in (
        ("contact_phone", normalize_phone, "Telefoonnummer contactpersoon"),
        ("contact_email", normalize_email, "E-mailadres contactpersoon"),
    ):
        if values.get(fld):
            normalized = normalizer(values[fld])
            if normalized is None:
                report.warnings.append(
                    RowIssue(line, company.kvk_number, f"{label} is ongeldig en overgeslagen")
                )
            else:
                data[fld.removeprefix("contact_")] = normalized
    if name:
        data["name"] = name[:255]
    if role:
        data["role"] = role[:255]

    def same(contact: Contact) -> bool:
        if name:
            return (contact.name or "").casefold() == name.casefold()
        return not contact.name and (contact.role or "").casefold() == role.casefold()

    existing = next((c for c in company.contacts if same(c)), None)
    if existing is None:
        company.contacts.append(Contact(**data))
        return True
    changed = False
    for fld, value in data.items():
        if getattr(existing, fld) != value:
            setattr(existing, fld, value)
            changed = True
    return changed

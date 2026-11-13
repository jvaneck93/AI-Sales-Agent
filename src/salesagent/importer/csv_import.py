"""CSV import with configurable column mapping, validation and deduplication."""

import csv
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from salesagent.models import Company, LegalForm, utcnow
from salesagent.normalize import (
    normalize_email,
    normalize_kvk,
    normalize_legal_form,
    normalize_phone,
    normalize_website,
)

REQUIRED_FIELDS = ("name", "kvk_number")
OPTIONAL_FIELDS = ("legal_form", "address", "city", "sector", "website", "phone", "email")


class ImportConfigError(Exception):
    """The mapping file or the CSV header is not usable. Message is in Dutch."""


@dataclass(frozen=True)
class ImportMapping:
    columns: dict[str, str]
    delimiter: str = ";"
    encoding: str = "utf-8-sig"


@dataclass(frozen=True)
class RowIssue:
    """A problem with one CSV line. Holds no names, only line, KvK number and reason."""

    line: int
    kvk_number: str | None
    reason: str


@dataclass
class ImportReport:
    total: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    rejected: list[RowIssue] = field(default_factory=list)
    warnings: list[RowIssue] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "Importrapport",
            f"  Regels gelezen:   {self.total}",
            f"  Nieuw:            {self.created}",
            f"  Bijgewerkt:       {self.updated}",
            f"  Ongewijzigd:      {self.unchanged}",
            f"  Afgekeurd:        {len(self.rejected)}",
            f"  Waarschuwingen:   {len(self.warnings)}",
        ]
        for title, issues in (("Afgekeurd", self.rejected), ("Waarschuwingen", self.warnings)):
            if issues:
                lines.append(f"\n{title}:")
                lines.extend(
                    f"  regel {i.line} (KvK {i.kvk_number or 'onbekend'}): {i.reason}"
                    for i in issues
                )
        return "\n".join(lines)


def load_mapping(path: Path) -> ImportMapping:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ImportConfigError(f"Kan kolomtoewijzing niet lezen: {path}") from exc
    columns = raw.get("columns") or {}
    unknown = set(columns) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
    if unknown:
        raise ImportConfigError(
            f"Onbekende velden in kolomtoewijzing: {', '.join(sorted(unknown))}"
        )
    missing = [f for f in REQUIRED_FIELDS if not columns.get(f)]
    if missing:
        raise ImportConfigError(
            f"Verplichte velden ontbreken in kolomtoewijzing: {', '.join(missing)}"
        )
    return ImportMapping(
        columns={k: v for k, v in columns.items() if v},
        delimiter=raw.get("delimiter", ";"),
        encoding=raw.get("encoding", "utf-8-sig"),
    )


def import_csv(session: Session, csv_path: Path, mapping: ImportMapping) -> ImportReport:
    """Import companies from a CSV file. Commits once at the end."""
    report = ImportReport()
    seen_kvk: dict[str, int] = {}

    with csv_path.open(encoding=mapping.encoding, newline="") as handle:
        reader = csv.DictReader(handle, delimiter=mapping.delimiter)
        header = reader.fieldnames or []
        missing = [col for col in mapping.columns.values() if col not in header]
        if missing:
            raise ImportConfigError(
                "Kolommen uit de kolomtoewijzing ontbreken in de CSV: " + ", ".join(missing)
            )

        for row in reader:
            line = reader.line_num
            report.total += 1
            values = {fld: (row.get(col) or "").strip() for fld, col in mapping.columns.items()}
            _process_row(session, values, line, seen_kvk, report)

    session.commit()
    return report


def _process_row(
    session: Session,
    values: dict[str, str],
    line: int,
    seen_kvk: dict[str, int],
    report: ImportReport,
) -> None:
    kvk = normalize_kvk(values["kvk_number"])
    if kvk is None:
        reason = (
            "KvK-nummer ontbreekt"
            if not values["kvk_number"]
            else "KvK-nummer is ongeldig (moet uit precies 8 cijfers bestaan; "
            "let op weggevallen voorloopnullen)"
        )
        report.rejected.append(RowIssue(line, None, reason))
        return
    if not values["name"]:
        report.rejected.append(RowIssue(line, kvk, "Bedrijfsnaam ontbreekt"))
        return
    if kvk in seen_kvk:
        report.rejected.append(
            RowIssue(line, kvk, f"Dubbel KvK-nummer (eerder op regel {seen_kvk[kvk]})")
        )
        return
    seen_kvk[kvk] = line

    data: dict[str, object] = {"name": values["name"]}
    for fld in ("address", "city", "sector"):
        if values.get(fld):
            data[fld] = values[fld]

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

    company = session.scalar(select(Company).where(Company.kvk_number == kvk))
    if company is None:
        company = Company(kvk_number=kvk, legal_form=LegalForm.UNKNOWN, **data)
        _set_legal_form(company, legal_form, raw_form)
        session.add(company)
        report.created += 1
        return

    changed = False
    for fld, value in data.items():
        if getattr(company, fld) != value:
            setattr(company, fld, value)
            changed = True
    # An unknown legal form in the CSV never overwrites a known one.
    if legal_form is not LegalForm.UNKNOWN and company.legal_form != legal_form:
        _set_legal_form(company, legal_form, raw_form)
        changed = True
    if changed:
        report.updated += 1
    else:
        report.unchanged += 1


def _set_legal_form(company: Company, legal_form: LegalForm, raw: str) -> None:
    company.legal_form = legal_form
    company.legal_form_raw = raw or None
    if legal_form is not LegalForm.UNKNOWN:
        company.legal_form_source = "csv"
        company.legal_form_checked_at = utcnow()

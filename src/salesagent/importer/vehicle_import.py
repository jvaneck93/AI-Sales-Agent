"""Vehicle import: a separate CSV with one vehicle per row, linked by KvK number."""

import re
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from salesagent.importer.csv_import import (
    ImportMapping,
    ImportReport,
    RowIssue,
    load_mapping,
    open_mapped_csv,
)
from salesagent.models import Company, Vehicle
from salesagent.normalize import normalize_kvk

VEHICLE_REQUIRED = ("kvk_number", "license_plate")
VEHICLE_OPTIONAL = (
    "make",
    "model",
    "build_year",
    "ownership",
    "lease_company",
    "contract_end_date",
)
_OWNERSHIP = {"koop": "koop", "gekocht": "koop", "eigendom": "koop", "lease": "lease"}
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y")


def load_vehicle_mapping(path: Path) -> ImportMapping:
    return load_mapping(
        path, section="vehicle_columns", required=VEHICLE_REQUIRED, optional=VEHICLE_OPTIONAL
    )


def normalize_license_plate(raw: str) -> str | None:
    """Dutch plates have 6 letters/digits; stored in upper case without dashes."""
    value = re.sub(r"[\s\-]", "", raw).upper()
    return value if re.fullmatch(r"[A-Z0-9]{6}", value) else None


def parse_date(raw: str) -> date | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def import_vehicles(session: Session, csv_path: Path, mapping: ImportMapping) -> ImportReport:
    """Import vehicles. Deduplicates on license plate. Commits once at the end."""
    report = ImportReport()
    seen: dict[str, int] = {}

    for line, values in open_mapped_csv(csv_path, mapping):
        report.total += 1
        kvk = normalize_kvk(values["kvk_number"])
        if kvk is None:
            report.rejected.append(RowIssue(line, None, "KvK-nummer ontbreekt of is ongeldig"))
            continue
        plate = normalize_license_plate(values["license_plate"])
        if plate is None:
            report.rejected.append(RowIssue(line, kvk, "Kenteken ontbreekt of is ongeldig"))
            continue
        if plate in seen:
            report.rejected.append(
                RowIssue(line, kvk, f"Dubbel kenteken (eerder op regel {seen[plate]})")
            )
            continue
        company = session.scalar(select(Company).where(Company.kvk_number == kvk))
        if company is None:
            report.rejected.append(
                RowIssue(line, kvk, "Geen bedrijf met dit KvK-nummer: importeer eerst het bedrijf")
            )
            continue
        seen[plate] = line

        data: dict[str, object] = {"company_id": company.id}
        for fld in ("make", "model", "lease_company"):
            if values.get(fld):
                data[fld] = values[fld]
        if values.get("build_year"):
            year = values["build_year"]
            if year.isdigit() and 1950 <= int(year) <= date.today().year + 1:
                data["build_year"] = int(year)
            else:
                report.warnings.append(RowIssue(line, kvk, "Bouwjaar is ongeldig en overgeslagen"))
        if values.get("ownership"):
            ownership = _OWNERSHIP.get(values["ownership"].lower())
            if ownership is None:
                report.warnings.append(
                    RowIssue(line, kvk, "Koop/lease niet herkend en overgeslagen")
                )
            else:
                data["ownership"] = ownership
        if values.get("contract_end_date"):
            end_date = parse_date(values["contract_end_date"])
            if end_date is None:
                report.warnings.append(
                    RowIssue(line, kvk, "Einddatum contract is ongeldig en overgeslagen")
                )
            else:
                data["contract_end_date"] = end_date

        vehicle = session.scalar(select(Vehicle).where(Vehicle.license_plate == plate))
        if vehicle is None:
            session.add(Vehicle(license_plate=plate, **data))
            report.created += 1
            continue
        changed = False
        for fld, value in data.items():
            if getattr(vehicle, fld) != value:
                setattr(vehicle, fld, value)
                changed = True
        if changed:
            report.updated += 1
        else:
            report.unchanged += 1

    session.commit()
    return report

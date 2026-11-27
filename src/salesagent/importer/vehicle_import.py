"""Vehicle import: a separate file (CSV or Excel) with one vehicle per row, linked by KvK."""

import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from salesagent.importer.csv_import import (
    ImportMapping,
    ImportReport,
    RowIssue,
    load_mapping,
    open_table,
)
from salesagent.importer.table import clean_kvk
from salesagent.importer.vehicle_fields import (
    VEHICLE_OPTIONAL,
    normalize_license_plate,
    parse_date,
    vehicle_fields,
)
from salesagent.models import Company, Vehicle

VEHICLE_REQUIRED = ("kvk_number", "license_plate")
__all__ = [
    "VEHICLE_OPTIONAL",
    "VEHICLE_REQUIRED",
    "import_vehicles",
    "load_vehicle_mapping",
    "normalize_license_plate",
    "parse_date",
]


def load_vehicle_mapping(path: Path) -> ImportMapping:
    return load_mapping(
        path, section="vehicle_columns", required=VEHICLE_REQUIRED, optional=VEHICLE_OPTIONAL
    )


def import_vehicles(
    session: Session, path: Path, mapping: ImportMapping, *, report_dir: Path | None = None
) -> ImportReport:
    """Import vehicles. Deduplicates on license plate. Commits once at the end."""
    started = time.perf_counter()
    report = ImportReport(vehicle_mode=True)
    rows, _ = open_table(
        path, mapping, required=VEHICLE_REQUIRED, optional=VEHICLE_OPTIONAL, report=report
    )
    companies = {c.kvk_number: c for c in session.scalars(select(Company))}
    plates = {v.license_plate: v for v in session.scalars(select(Vehicle)) if v.license_plate}
    seen: dict[str, int] = {}

    for line, values in rows:
        report.total += 1
        kvk, _ = clean_kvk(values["kvk_number"])
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
        company = companies.get(kvk)
        if company is None:
            report.rejected.append(
                RowIssue(line, kvk, "Geen bedrijf met dit KvK-nummer: importeer eerst het bedrijf")
            )
            continue
        seen[plate] = line
        data = vehicle_fields(
            values, lambda r, ln=line, k=kvk: report.warnings.append(RowIssue(ln, k, r))
        )
        vehicle = plates.get(plate)
        if vehicle is None:
            vehicle = Vehicle(license_plate=plate, company=company, **data)
            session.add(vehicle)
            plates[plate] = vehicle
            report.created += 1
            continue
        changed = vehicle.company is not company
        if changed:
            vehicle.company = company
        for fld, value in data.items():
            if getattr(vehicle, fld) != value:
                setattr(vehicle, fld, value)
                changed = True
        if changed:
            report.updated += 1
        else:
            report.unchanged += 1

    session.commit()
    report.seconds = time.perf_counter() - started
    if report_dir is not None:
        report.write_issues(report_dir)
    return report

"""Web page to try the import and the compliance gate in a browser.

All form actions are relative, so the page also works behind a proxy prefix.
"""

import tempfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from salesagent.api.common import get_session, render
from salesagent.api.dashboard import router as dashboard_router
from salesagent.api.voice_api import router as voice_router
from salesagent.compliance.gate import callable_legal_forms, may_be_called
from salesagent.compliance.register import ConsentError, register_objection, register_opt_in
from salesagent.config import get_settings
from salesagent.db import upgrade_database
from salesagent.importer.csv_import import ImportConfigError, import_file, load_mapping
from salesagent.importer.vehicle_import import import_vehicles, load_vehicle_mapping
from salesagent.models import Company, Consent, ConsentType, Contact, LegalForm, Vehicle
from salesagent.normalize import normalize_kvk
from salesagent.privacy import delete_company, purge_expired

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MAPPING_PATH = PROJECT_ROOT / "config" / "import_mapping.yaml"
SAMPLE_COMPANIES = PROJECT_ROOT / "data" / "voorbeeld_bedrijven.csv"
SAMPLE_VEHICLES = PROJECT_ROOT / "data" / "voorbeeld_voertuigen.csv"
# Put large lead files here and import them from the Data page (no upload needed).
IMPORT_DIR = PROJECT_ROOT / "data" / "import"
REPORT_DIR = PROJECT_ROOT / "data" / "importrapporten"
IMPORT_SUFFIXES = {".csv", ".txt", ".xlsx", ".xlsm"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    upgrade_database()
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="AI-belbureau", lifespan=lifespan)
app.include_router(dashboard_router)
app.include_router(voice_router)


PAGE_ROWS = 100


def _page(
    request: Request,
    session: Session,
    *,
    message: str = "",
    error: str = "",
    report: str = "",
    q: str = "",
) -> HTMLResponse:
    query = select(Company).order_by(Company.kvk_number)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            or_(Company.name.ilike(like), Company.kvk_number.like(like), Company.city.ilike(like))
        )
    companies = session.scalars(query.limit(PAGE_ROWS)).all()
    rows = []
    for company in companies:
        end_dates = [v.contract_end_date for v in company.vehicles if v.contract_end_date]
        rows.append(
            {
                "company": company,
                "decision": may_be_called(company, session),
                "next_end_date": min(end_dates) if end_dates else None,
            }
        )
    objections = session.scalars(
        select(Consent)
        .where(Consent.type == ConsentType.OBJECTION)
        .order_by(Consent.id.desc())
        .limit(50)
    ).all()
    settings = get_settings()
    forms = dict(
        session.execute(select(Company.legal_form, func.count()).group_by(Company.legal_form)).all()
    )
    callable_forms = callable_legal_forms()
    total = sum(forms.values())
    context = {
        "rows": rows,
        "q": q,
        "page_rows": PAGE_ROWS,
        "objections": objections,
        "objection_count": session.scalar(
            select(func.count()).select_from(Consent).where(Consent.type == ConsentType.OBJECTION)
        ),
        "message": message,
        "error": error,
        "report": report,
        "today": date.today().isoformat(),
        "dry_run": settings.dry_run,
        "retention_transcript_days": settings.retention_transcript_days,
        "retention_contact_days": settings.retention_contact_days,
        "import_files": _import_files(),
        "import_dir": IMPORT_DIR,
        "legal_forms": sorted(((f.value, n) for f, n in forms.items()), key=lambda item: -item[1]),
        "stats": {
            "companies": total,
            "callable_form": sum(n for f, n in forms.items() if f in callable_forms),
            "unknown_form": forms.get(LegalForm.UNKNOWN, 0),
            "contacts": session.scalar(select(func.count()).select_from(Contact)),
            "vehicles": session.scalar(select(func.count()).select_from(Vehicle)),
        },
    }
    return render(request, "index.html", nav="data", **context)


def _import_files() -> list[dict]:
    if not IMPORT_DIR.is_dir():
        return []
    files = [
        p
        for p in IMPORT_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in IMPORT_SUFFIXES and not p.name.startswith("~$")
    ]
    return [
        {"name": p.name, "kb": max(1, p.stat().st_size // 1024)}
        for p in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
    ]


def _find_company(session: Session, kvk: str) -> Company | None:
    normalized = normalize_kvk(kvk)
    if normalized is None:
        return None
    return session.scalar(select(Company).where(Company.kvk_number == normalized))


async def _save_upload(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in IMPORT_SUFFIXES:
        suffix = ".csv"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        while chunk := await upload.read(1024 * 1024):
            handle.write(chunk)
        return Path(handle.name)


def _run_import(request: Request, session: Session, path: Path, *, vehicles: bool):
    try:
        if vehicles:
            report = import_vehicles(
                session, path, load_vehicle_mapping(MAPPING_PATH), report_dir=REPORT_DIR
            )
        else:
            report = import_file(session, path, load_mapping(MAPPING_PATH), report_dir=REPORT_DIR)
    except ImportConfigError as exc:
        session.rollback()
        return _page(request, session, error=str(exc))
    what = "Voertuigen" if vehicles else "Leads"
    return _page(request, session, message=f"{what} geïmporteerd.", report=report.render())


@app.get("/data", response_class=HTMLResponse)
def index(request: Request, q: str = "", session: Session = Depends(get_session)):
    return _page(request, session, q=q)


@app.post("/import", response_class=HTMLResponse)
async def import_companies(
    request: Request, bestand: UploadFile, session: Session = Depends(get_session)
):
    path = await _save_upload(bestand)
    try:
        return _run_import(request, session, path, vehicles=False)
    finally:
        path.unlink(missing_ok=True)


@app.post("/import-voertuigen", response_class=HTMLResponse)
async def import_vehicle_file(
    request: Request, bestand: UploadFile, session: Session = Depends(get_session)
):
    path = await _save_upload(bestand)
    try:
        return _run_import(request, session, path, vehicles=True)
    finally:
        path.unlink(missing_ok=True)


@app.post("/import-map", response_class=HTMLResponse)
def import_from_folder(
    request: Request,
    bestand: str = Form(...),
    soort: str = Form("leads"),
    session: Session = Depends(get_session),
):
    """Import a file the user placed in data/import (no upload needed for large files)."""
    names = {f["name"] for f in _import_files()}
    if bestand not in names:
        return _page(request, session, error="Bestand niet gevonden in de importmap.")
    return _run_import(request, session, IMPORT_DIR / bestand, vehicles=soort == "voertuigen")


@app.get("/import/sjabloon.xlsx")
def import_template():
    from salesagent.importer.template import build_template

    return Response(
        build_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="sjabloon-leads.xlsx"'},
    )


@app.post("/voorbeelddata", response_class=HTMLResponse)
def load_sample_data(request: Request, session: Session = Depends(get_session)):
    companies = import_file(session, SAMPLE_COMPANIES, load_mapping(MAPPING_PATH))
    vehicles = import_vehicles(session, SAMPLE_VEHICLES, load_vehicle_mapping(MAPPING_PATH))
    report = f"BEDRIJVEN\n{companies.render()}\n\nVOERTUIGEN\n{vehicles.render()}"
    return _page(request, session, message="Voorbeelddata geladen.", report=report)


@app.post("/optin", response_class=HTMLResponse)
def opt_in(
    request: Request,
    kvk: str = Form(...),
    bron: str = Form(""),
    datum: str = Form(""),
    bewijs: str = Form(""),
    session: Session = Depends(get_session),
):
    company = _find_company(session, kvk)
    if company is None:
        return _page(request, session, error=f"Geen bedrijf gevonden met KvK-nummer {kvk}.")
    try:
        consent_date = date.fromisoformat(datum)
    except ValueError:
        return _page(request, session, error="Vul een geldige datum in.")
    try:
        register_opt_in(session, company, source=bron, consent_date=consent_date, evidence=bewijs)
    except ConsentError as exc:
        session.rollback()
        return _page(request, session, error=str(exc))
    session.commit()
    return _page(request, session, message=f"Toestemming vastgelegd voor KvK {kvk}.")


@app.post("/verzet", response_class=HTMLResponse)
def objection(
    request: Request,
    kvk: str = Form(""),
    telefoon: str = Form(""),
    bron: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        register_objection(session, source=bron, kvk_number=kvk or None, phone=telefoon or None)
    except ConsentError as exc:
        session.rollback()
        return _page(request, session, error=str(exc))
    session.commit()
    return _page(
        request, session, message="Verzet geregistreerd. Dit blokkeert direct en blijvend."
    )


@app.post("/verwijder", response_class=HTMLResponse)
def remove_company(request: Request, kvk: str = Form(...), session: Session = Depends(get_session)):
    company = _find_company(session, kvk)
    if company is None:
        return _page(request, session, error=f"Geen bedrijf gevonden met KvK-nummer {kvk}.")
    delete_company(session, company)
    session.commit()
    return _page(
        request,
        session,
        message=f"KvK {kvk} is verwijderd met alle gekoppelde gegevens. "
        "Een eventueel verzet blijft geregistreerd.",
    )


@app.post("/opschonen", response_class=HTMLResponse)
def purge(request: Request, session: Session = Depends(get_session)):
    settings = get_settings()
    result = purge_expired(
        session,
        transcript_days=settings.retention_transcript_days,
        contact_days=settings.retention_contact_days,
    )
    session.commit()
    return _page(request, session, message="Bewaartermijnen toegepast.", report=result.render())

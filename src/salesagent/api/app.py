"""Web page to try the import and the compliance gate in a browser.

All form actions are relative, so the page also works behind a proxy prefix.
"""

import tempfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from salesagent.compliance.gate import may_be_called
from salesagent.compliance.register import ConsentError, register_objection, register_opt_in
from salesagent.config import get_settings
from salesagent.db import make_session_factory, upgrade_database
from salesagent.importer.csv_import import ImportConfigError, import_csv, load_mapping
from salesagent.importer.vehicle_import import import_vehicles, load_vehicle_mapping
from salesagent.models import Company, Consent, ConsentType
from salesagent.normalize import normalize_kvk
from salesagent.privacy import delete_company, purge_expired

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MAPPING_PATH = PROJECT_ROOT / "config" / "import_mapping.yaml"
SAMPLE_COMPANIES = PROJECT_ROOT / "data" / "voorbeeld_bedrijven.csv"
SAMPLE_VEHICLES = PROJECT_ROOT / "data" / "voorbeeld_voertuigen.csv"

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_session_factory = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    upgrade_database()
    yield


app = FastAPI(title="Sales AI Agent", lifespan=lifespan)


def get_session():
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory()
    with _session_factory() as session:
        yield session


def _page(
    request: Request,
    session: Session,
    *,
    message: str = "",
    error: str = "",
    report: str = "",
) -> HTMLResponse:
    companies = session.scalars(select(Company).order_by(Company.kvk_number)).all()
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
        select(Consent).where(Consent.type == ConsentType.OBJECTION).order_by(Consent.id)
    ).all()
    settings = get_settings()
    context = {
        "rows": rows,
        "objections": objections,
        "message": message,
        "error": error,
        "report": report,
        "today": date.today().isoformat(),
        "dry_run": settings.dry_run,
        "retention_transcript_days": settings.retention_transcript_days,
        "retention_contact_days": settings.retention_contact_days,
        "stats": {
            "companies": len(rows),
            "allowed": sum(1 for r in rows if r["decision"].allowed),
            "denied": sum(1 for r in rows if not r["decision"].allowed),
            "contacts": sum(len(r["company"].contacts) for r in rows),
            "vehicles": sum(len(r["company"].vehicles) for r in rows),
        },
    }
    return templates.TemplateResponse(request, "index.html", context)


def _find_company(session: Session, kvk: str) -> Company | None:
    normalized = normalize_kvk(kvk)
    if normalized is None:
        return None
    return session.scalar(select(Company).where(Company.kvk_number == normalized))


async def _save_upload(upload: UploadFile) -> Path:
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as handle:
        handle.write(await upload.read())
        return Path(handle.name)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)):
    return _page(request, session)


@app.post("/import", response_class=HTMLResponse)
async def import_companies(
    request: Request, bestand: UploadFile, session: Session = Depends(get_session)
):
    path = await _save_upload(bestand)
    try:
        report = import_csv(session, path, load_mapping(MAPPING_PATH))
    except ImportConfigError as exc:
        return _page(request, session, error=str(exc))
    except UnicodeDecodeError:
        return _page(request, session, error="Het bestand is geen leesbare UTF-8 CSV.")
    finally:
        path.unlink(missing_ok=True)
    return _page(request, session, message="Bedrijven geïmporteerd.", report=report.render())


@app.post("/import-voertuigen", response_class=HTMLResponse)
async def import_vehicle_file(
    request: Request, bestand: UploadFile, session: Session = Depends(get_session)
):
    path = await _save_upload(bestand)
    try:
        report = import_vehicles(session, path, load_vehicle_mapping(MAPPING_PATH))
    except ImportConfigError as exc:
        return _page(request, session, error=str(exc))
    except UnicodeDecodeError:
        return _page(request, session, error="Het bestand is geen leesbare UTF-8 CSV.")
    finally:
        path.unlink(missing_ok=True)
    return _page(request, session, message="Voertuigen geïmporteerd.", report=report.render())


@app.post("/voorbeelddata", response_class=HTMLResponse)
def load_sample_data(request: Request, session: Session = Depends(get_session)):
    companies = import_csv(session, SAMPLE_COMPANIES, load_mapping(MAPPING_PATH))
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

"""Contacts, vehicles, KvK API client, deletion, retention and the web page."""

from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select

from salesagent.compliance.gate import may_be_called, start_call_attempt
from salesagent.compliance.kvk import KvkApiClient, KvkError, resolve_legal_form
from salesagent.compliance.register import register_objection, register_opt_in
from salesagent.importer.csv_import import import_csv, load_mapping
from salesagent.importer.vehicle_import import (
    import_vehicles,
    load_vehicle_mapping,
    normalize_license_plate,
    parse_date,
)
from salesagent.models import (
    CallAttempt,
    Company,
    CompanyStatus,
    Consent,
    Contact,
    LegalForm,
    Vehicle,
    utcnow,
)
from salesagent.privacy import delete_company, delete_contact, purge_expired
from tests.conftest import PROJECT_ROOT

MAPPING = PROJECT_ROOT / "config" / "import_mapping.yaml"
COMPANIES = PROJECT_ROOT / "data" / "voorbeeld_bedrijven.csv"
VEHICLES = PROJECT_ROOT / "data" / "voorbeeld_voertuigen.csv"


def _company(session, kvk):
    return session.scalar(select(Company).where(Company.kvk_number == kvk))


# --- Contacts -----------------------------------------------------------------------


def test_contacts_are_imported_including_second_contact_on_duplicate_row(session):
    report = import_csv(session, COMPANIES, load_mapping(MAPPING))

    first = _company(session, "10000001")
    assert {c.name for c in first.contacts} == {"Testpersoon Een", "Testpersoon Drie"}
    assert first.address == "Industrieweg 1"  # duplicate row did not wipe company data
    one = next(c for c in first.contacts if c.name == "Testpersoon Een")
    assert (one.role, one.phone) == ("Wagenparkbeheerder", "+31381234568")
    role_only = _company(session, "10000006").contacts[0]
    assert (role_only.name, role_only.role) == (None, "Inkoper")
    assert any(i.line == 15 and "contactpersoon toegevoegd" in i.reason for i in report.warnings)


def test_reimport_does_not_duplicate_contacts(session):
    mapping = load_mapping(MAPPING)
    import_csv(session, COMPANIES, mapping)
    import_csv(session, COMPANIES, mapping)
    assert len(session.scalars(select(Contact)).all()) == 4


# --- Vehicles -----------------------------------------------------------------------


def test_vehicle_import_report(session):
    import_csv(session, COMPANIES, load_mapping(MAPPING))
    report = import_vehicles(session, VEHICLES, load_vehicle_mapping(MAPPING))

    assert report.total == 8
    assert report.created == 5
    rejected = {i.line: i.reason for i in report.rejected}
    assert set(rejected) == {6, 7, 8}
    assert "Dubbel kenteken" in rejected[6]
    assert "Geen bedrijf" in rejected[7]
    assert "Kenteken ontbreekt" in rejected[8]
    assert sum(1 for i in report.warnings if i.line == 9) == 3

    vehicles = {v.license_plate: v for v in _company(session, "10000001").vehicles}
    assert set(vehicles) == {"V001AA", "V002AA", "XX01YY"}
    assert vehicles["V002AA"].contract_end_date == date(2026, 12, 31)
    assert vehicles["V001AA"].ownership == "lease"
    assert vehicles["XX01YY"].ownership == "koop"


def test_vehicle_reimport_is_idempotent(session):
    import_csv(session, COMPANIES, load_mapping(MAPPING))
    mapping = load_vehicle_mapping(MAPPING)
    import_vehicles(session, VEHICLES, mapping)
    second = import_vehicles(session, VEHICLES, mapping)
    assert (second.created, second.updated, second.unchanged) == (0, 0, 5)
    assert len(session.scalars(select(Vehicle)).all()) == 5


@pytest.mark.parametrize(
    ("raw", "expected"), [("v-001-aa", "V001AA"), ("XX 01 YY", "XX01YY"), ("ABC", None), ("", None)]
)
def test_normalize_license_plate(raw, expected):
    assert normalize_license_plate(raw) == expected


def test_parse_date():
    assert parse_date("2027-03-31") == date(2027, 3, 31)
    assert parse_date("31-12-2026") == date(2026, 12, 31)
    assert parse_date("31/12/2026") == date(2026, 12, 31)
    assert parse_date("geen datum") is None


# --- KvK API client -----------------------------------------------------------------


def _kvk_client(handler) -> KvkApiClient:
    return KvkApiClient(
        "test-key",
        "https://kvk.invalid/api/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_kvk_api_client_reads_legal_form(make_company):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["apikey"] = request.headers["apikey"]
        return httpx.Response(
            200, json={"_embedded": {"eigenaar": {"rechtsvorm": "BeslotenVennootschap"}}}
        )

    company = make_company(LegalForm.UNKNOWN)
    assert resolve_legal_form(company, _kvk_client(handler)) is LegalForm.BV
    assert seen["url"].endswith(f"/basisprofielen/{company.kvk_number}")
    assert seen["apikey"] == "test-key"
    assert may_be_called(company).allowed


@pytest.mark.parametrize(
    "payload",
    [
        {"_embedded": {"eigenaar": {"rechtsvorm": "Iets Onbekends"}}},
        {"_embedded": {}},
        {},
    ],
)
def test_kvk_api_client_unrecognised_answer_stays_unknown(make_company, payload):
    company = make_company(LegalForm.UNKNOWN)
    client = _kvk_client(lambda request: httpx.Response(200, json=payload))
    assert resolve_legal_form(company, client) is LegalForm.UNKNOWN
    assert not may_be_called(company).allowed


def test_kvk_api_client_not_found_and_errors(make_company):
    company = make_company(LegalForm.UNKNOWN)
    assert _kvk_client(lambda r: httpx.Response(404)).get_legal_form("12345678") is None
    with pytest.raises(KvkError):
        _kvk_client(lambda r: httpx.Response(500)).get_legal_form("12345678")

    def boom(request):
        raise httpx.ConnectError("down")

    with pytest.raises(KvkError):
        resolve_legal_form(company, _kvk_client(boom))
    assert company.legal_form is LegalForm.UNKNOWN


# --- Deletion and retention (hard rule 7) -------------------------------------------


def test_delete_company_removes_everything_except_objection(session, make_company):
    company = make_company(LegalForm.VOF)
    kvk = company.kvk_number
    company.contacts.append(Contact(name="Testpersoon", role="Eigenaar"))
    company.vehicles.append(Vehicle(license_plate="T001ST"))
    register_opt_in(
        session, company, source="formulier", consent_date=date.today(), evidence="nr 1"
    )
    start_call_attempt(session, company)
    register_objection(session, company=company, source="telefoongesprek")

    delete_company(session, company)
    session.expire_all()

    assert _company(session, kvk) is None
    assert session.scalars(select(Contact)).all() == []
    assert session.scalars(select(Vehicle)).all() == []
    assert session.scalars(select(CallAttempt)).all() == []
    remaining = session.scalars(select(Consent)).all()
    assert [c.type.value for c in remaining] == ["verzet"]
    assert remaining[0].kvk_number == kvk
    assert remaining[0].company_id is None


def test_delete_contact(session, make_company):
    company = make_company()
    company.contacts.append(Contact(name="Testpersoon"))
    session.flush()
    delete_contact(session, company.contacts[0])
    session.expire_all()
    assert session.scalars(select(Contact)).all() == []
    assert session.get(Company, company.id) is not None


def test_purge_clears_old_transcripts_but_keeps_evidence(session, make_company):
    company = make_company()
    now = utcnow()
    old = start_call_attempt(session, company, now=now - timedelta(days=100))
    old.summary, old.transcript_ref, old.outcome = "verslag", "ref-1", "geen interesse"
    recent = start_call_attempt(session, company, now=now - timedelta(days=10))
    recent.summary = "recent verslag"

    result = purge_expired(session, transcript_days=90, contact_days=365, now=now)

    assert result.transcripts_cleared == 1
    assert (old.summary, old.transcript_ref) == (None, None)
    assert old.outcome == "geen interesse"
    assert old.basis_detail
    assert recent.summary == "recent verslag"


def test_purge_deletes_contacts_of_closed_companies_only(session, make_company):
    now = utcnow()
    closed = make_company(status=CompanyStatus.NO_INTEREST)
    active = make_company(status=CompanyStatus.CALLABLE)
    for company in (closed, active):
        company.contacts.append(Contact(name="Testpersoon"))
    session.flush()

    assert (
        purge_expired(session, transcript_days=90, contact_days=365, now=now).contacts_deleted == 0
    )
    later = now + timedelta(days=400)
    result = purge_expired(session, transcript_days=90, contact_days=365, now=later)

    session.expire_all()
    assert result.contacts_deleted == 1
    assert closed.contacts == []
    assert len(active.contacts) == 1


# --- Web page -----------------------------------------------------------------------


@pytest.fixture
def web(tmp_path):
    """Test client plus a session on the same file database.

    A file database is used because the endpoints run in worker threads.
    """
    from fastapi.testclient import TestClient

    from salesagent.api.app import app, get_session
    from salesagent.db import Base, make_engine, make_session_factory

    engine = make_engine(f"sqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    def override():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    with factory() as session:
        # No "with TestClient": the lifespan (migrations on the real database) is not run.
        yield TestClient(app), session
    app.dependency_overrides.clear()
    engine.dispose()


def _allowed(session, kvk: str) -> bool:
    session.expire_all()
    return may_be_called(_company(session, kvk), session).allowed


def test_web_flow(web):
    client, session = web
    assert "Nog geen bedrijven" in client.get("/").text

    page = client.post("/voorbeelddata").text
    assert "Voorbeelddata geladen" in page
    assert "Dubbel KvK-nummer" in page
    assert "Demo Bouwgroep N.V." in page

    with COMPANIES.open("rb") as handle:
        page = client.post("/import", files={"bestand": ("b.csv", handle, "text/csv")}).text
    assert "Ongewijzigd:      10" in page

    page = client.post(
        "/optin",
        data={"kvk": "10000004", "bron": "formulier", "datum": "2026-01-05", "bewijs": "nr 9"},
    ).text
    assert "Toestemming vastgelegd" in page
    assert _allowed(session, "10000004")

    page = client.post("/optin", data={"kvk": "10000005", "datum": "2026-01-05"}).text
    assert "vereist een bron" in page
    assert not _allowed(session, "10000005")

    client.post("/verzet", data={"kvk": "10000001", "bron": "e-mail"})
    assert not _allowed(session, "10000001")

    page = client.post("/verwijder", data={"kvk": "10000001"}).text
    assert "is verwijderd" in page
    session.expire_all()
    assert _company(session, "10000001") is None
    assert "KvK 10000001" in page  # still in the objection register

    assert "Bewaartermijnen toegepast" in client.post("/opschonen").text


def test_web_upload_with_wrong_columns_shows_error(web):
    client, _ = web
    response = client.post(
        "/import", files={"bestand": ("x.csv", b"Kolom A;Kolom B\n1;2\n", "text/csv")}
    )
    assert response.status_code == 200
    assert "ontbreken in de CSV" in response.text

"""Excel and CSV lead files as they come from real exports. All data is fictional."""

import io

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import func, select

from salesagent.importer.csv_import import (
    ImportConfigError,
    ImportMapping,
    import_file,
    load_mapping,
)
from salesagent.importer.table import clean_kvk, match_columns
from salesagent.importer.template import build_template
from salesagent.models import (
    Brand,
    CampaignLead,
    Company,
    LeadStatus,
    LegalForm,
    Vehicle,
)
from tests.conftest import PROJECT_ROOT

MAPPING = load_mapping(PROJECT_ROOT / "config" / "import_mapping.yaml")


def _xlsx(path, rows, title_rows=()):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Export"
    for row in title_rows:
        sheet.append(row)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return path


FLEET_HEADER = [
    "Handelsnaam", "KvK", "Rechtsvorm", "Straat", "Huisnummer", "Postcode",
    "Vestigingsplaats", "Branche", "Telefoonnummer", "Kenteken", "Merk",
    "Datum eerste toelating", "Koop/lease", "Einddatum lease", "Aantal medewerkers",
]  # fmt: skip


def test_fleet_export_in_excel_merges_rows_per_company(session, tmp_path):
    path = _xlsx(
        tmp_path / "vloot.xlsx",
        [
            FLEET_HEADER,
            [
                "Proef Bouw B.V.",
                1234567,
                "BV",
                "Industrieweg",
                4,
                "8011 AB",
                "Zwolle",
                "Bouw",
                "038-1234567",
                "AB-123-C",
                "Fictiva",
                "2019-03-01",
                "lease",
                "31-12-2027",
                40,
            ],
            [
                "Proef Bouw B.V.",
                1234567,
                "BV",
                "Industrieweg",
                4,
                "8011 AB",
                "Zwolle",
                "Bouw",
                "038-1234567",
                "XY987Z",
                "Fictiva",
                2017,
                "koop",
                None,
                40,
            ],
            [
                "Proef Kapper",
                "87654321",
                "Eenmanszaak",
                "Dorpsstraat",
                1,
                "",
                "Epe",
                "Kappers",
                "",
                "GH456J",
                "Nordwagen",
                2021,
                "",
                None,
                1,
            ],
        ],  # fmt: skip
        title_rows=[["Wagenparkexport (verzonnen)"], []],
    )
    report = import_file(session, path, MAPPING)

    assert report.total == 3 and report.created == 2 and not report.rejected
    assert report.vehicles_created == 3
    company = session.scalar(select(Company).where(Company.kvk_number == "01234567"))
    assert company.legal_form is LegalForm.BV
    assert company.address == "Industrieweg 4, 8011 AB"
    assert company.city == "Zwolle" and company.phone == "+31381234567"
    plates = {v.license_plate: v for v in company.vehicles}
    assert set(plates) == {"AB123C", "XY987Z"}
    assert plates["AB123C"].build_year == 2019
    assert plates["AB123C"].contract_end_date.isoformat() == "2027-12-31"
    assert any("voorloopnul aangevuld" in w.reason for w in report.warnings)
    assert report.legal_forms == {"bv": 1, "eenmanszaak": 1} and report.callable_count == 1

    text = report.render()
    assert "kopregel op regel 3" in text
    assert "Bedrijfsnaam ← 'Handelsnaam'" in text
    assert "'Aantal medewerkers'" in text  # reported as not used
    assert "Proef" not in text  # no names in the report


def test_reimport_of_same_file_changes_nothing(session, tmp_path):
    path = _xlsx(
        tmp_path / "vloot.xlsx",
        [FLEET_HEADER, ["Proef B.V.", "12345678", "B.V.", "Weg", 1, "", "Ede", "Bouw", "",
                        "AB123C", "Fictiva", 2020, "lease", "2028-01-01", 5]],
    )  # fmt: skip
    import_file(session, path, MAPPING)
    second = import_file(session, path, MAPPING)
    assert second.created == 0 and second.unchanged == 1
    assert second.vehicles_created == 0 and second.vehicles_updated == 0
    assert session.scalar(select(func.count()).select_from(Vehicle)) == 1


def test_csv_from_excel_in_windows_encoding_with_commas(session, tmp_path):
    path = tmp_path / "leads.csv"
    path.write_bytes(
        "Bedrijfsnaam,KvK-nummer,Rechtsvorm,Plaats\nCafé Proef B.V.,30000001,B.V.,Utrecht\n".encode(
            "cp1252"
        )
    )
    report = import_file(session, path, MAPPING)
    assert report.created == 1
    assert "Windows-1252" in report.source and "komma" in report.source
    assert session.scalar(select(Company.name)) == "Café Proef B.V."


def test_old_excel_format_gives_clear_message(session, tmp_path):
    path = tmp_path / "oud.xls"
    path.write_bytes(b"not really excel")
    with pytest.raises(ImportConfigError, match="xlsx"):
        import_file(session, path, MAPPING)


def test_missing_required_column_lists_found_columns(session, tmp_path):
    path = _xlsx(tmp_path / "x.xlsx", [["Bedrijfsnaam", "Plaats"], ["Proef B.V.", "Ede"]])
    with pytest.raises(ImportConfigError, match="KvK-nummer.*Gevonden kolommen: 'Bedrijfsnaam'"):
        import_file(session, path, MAPPING)


def test_many_issues_are_grouped_in_the_report(session, tmp_path):
    rows = ["Bedrijfsnaam;KvK-nummer;Rechtsvorm"]
    rows += [f"Proef {i};{40000000 + i};" for i in range(60)]
    path = tmp_path / "leads.csv"
    path.write_text("\n".join(rows), encoding="utf-8")
    report = import_file(session, path, MAPPING, report_dir=tmp_path / "rapporten")
    text = report.render()
    assert "60x Rechtsvorm ontbreekt" in text
    assert report.report_file is not None and report.report_file.exists()
    assert len(report.report_file.read_text(encoding="utf-8-sig").splitlines()) == 61


def test_column_matching_prefers_configured_name_then_aliases():
    match = match_columns(
        ["KvK nr", "Naam", "Bedrijfsnaam"], {"name": "Bedrijfsnaam"}, ("name", "kvk_number")
    )
    assert match.used == {"kvk_number": "KvK nr", "name": "Bedrijfsnaam"}
    assert match.ignored == ["Naam"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12345678", ("12345678", False)), ("1234567", ("01234567", True)),
     ("12.345.678", ("12345678", False)), ("1234", (None, False)), ("", (None, False))],
)  # fmt: skip
def test_clean_kvk(raw, expected):
    assert clean_kvk(raw) == expected


def test_template_imports_cleanly(session, tmp_path):
    path = tmp_path / "sjabloon.xlsx"
    path.write_bytes(build_template())
    assert load_workbook(io.BytesIO(path.read_bytes())).sheetnames == ["Leads", "Uitleg"]
    report = import_file(session, path, MAPPING)
    assert report.created == 2 and report.vehicles_created == 2 and not report.rejected


def test_mapping_accepts_vehicle_columns_and_auto_settings():
    assert MAPPING.delimiter == "auto" and MAPPING.encoding == "auto"
    assert MAPPING.columns["license_plate"] == "Kenteken"


def test_import_mapping_object_still_works_with_csv(session, tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("Naam;KvK\nAlfa B.V.;30000009\n", encoding="utf-8")
    report = import_file(
        session, path, ImportMapping(columns={"name": "Naam", "kvk_number": "KvK"})
    )
    assert report.created == 1


# --- Large files: the round and research stay within bounds ---------------------------------


class CountingKvk:
    source_name = "mock-kvk"

    def __init__(self):
        self.calls = 0

    def get_legal_form(self, kvk_number):
        self.calls += 1
        return LegalForm.BV


def test_prepare_round_skips_kvk_lookups_for_forms_never_called(session, make_company):
    from salesagent.operations.campaigns import create_campaign, prepare_round

    brand = Brand(name="Testmerk", segment_rules={})
    session.add(brand)
    session.flush()
    make_company(LegalForm.BV)
    sole = make_company(LegalForm.EENMANSZAAK)
    make_company(LegalForm.UNKNOWN)
    campaign = create_campaign(session, brand, "R1")
    kvk = CountingKvk()
    report = prepare_round(session, campaign, kvk)
    assert kvk.calls == 2  # bv and unknown; not the eenmanszaak
    lead = next(lead for lead in campaign.leads if lead.company_id == sole.id)
    assert lead.status is LeadStatus.BLOCKED
    assert "eenmanszaak" in lead.reason  # the real reason, not "not checked this round"
    assert report.kvk_lookups == 2


def test_research_only_for_callable_leads_and_limited(session, make_company):
    from salesagent.enrichment.research import research_candidates
    from salesagent.operations.campaigns import create_campaign

    brand = Brand(name="Testmerk", segment_rules={})
    session.add(brand)
    session.flush()
    campaign = create_campaign(session, brand, "R1")
    companies = [make_company() for _ in range(5)]
    for i, company in enumerate(companies):
        status = LeadStatus.BLOCKED if i == 0 else LeadStatus.CALLABLE
        campaign.leads.append(CampaignLead(company_id=company.id, status=status))
    make_company()  # not in any round
    session.flush()
    found = research_candidates(session, limit=3)
    assert len(found) == 3
    assert companies[0] not in found


# --- Web ------------------------------------------------------------------------------------


@pytest.fixture
def web(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from salesagent.api import app as app_module
    from salesagent.api.app import app, get_session
    from salesagent.db import Base, make_engine, make_session_factory

    engine = make_engine(f"sqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    def override():
        with factory() as session:
            yield session

    monkeypatch.setattr(app_module, "IMPORT_DIR", tmp_path / "import")
    monkeypatch.setattr(app_module, "REPORT_DIR", tmp_path / "rapporten")
    app.dependency_overrides[get_session] = override
    yield TestClient(app), tmp_path
    app.dependency_overrides.clear()


def test_web_excel_upload_folder_import_and_template(web):
    client, tmp_path = web
    template = client.get("/import/sjabloon.xlsx")
    assert template.status_code == 200
    assert template.headers["content-type"].startswith("application/vnd.openxmlformats")

    page = client.post(
        "/import", files={"bestand": ("leads.xlsx", template.content, "application/octet-stream")}
    ).text
    assert "Leads geïmporteerd" in page and "Koud belbaar op rechtsvorm" in page

    folder = tmp_path / "import"
    folder.mkdir()
    (folder / "extra.csv").write_text("Bedrijfsnaam;KvK-nummer\nProef N.V.;30000002\n")
    page = client.get("/data").text
    assert "extra.csv" in page
    page = client.post("/import-map", data={"bestand": "extra.csv"}).text
    assert "Nieuw:            1" in page

    refused = client.post("/import-map", data={"bestand": "../web.db"}).text
    assert "niet gevonden in de importmap" in refused

    assert "Proef N.V." in client.get("/data", params={"q": "30000002"}).text

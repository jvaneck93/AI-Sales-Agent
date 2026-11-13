from pathlib import Path

import pytest
from sqlalchemy import select

from salesagent.importer.csv_import import (
    ImportConfigError,
    ImportMapping,
    import_csv,
    load_mapping,
)
from salesagent.models import Company, CompanyStatus, LegalForm
from salesagent.normalize import normalize_kvk, normalize_legal_form, normalize_phone
from tests.conftest import PROJECT_ROOT

SAMPLE_CSV = PROJECT_ROOT / "data" / "voorbeeld_bedrijven.csv"
MAPPING = PROJECT_ROOT / "config" / "import_mapping.yaml"


def _company(session, kvk: str) -> Company | None:
    return session.scalar(select(Company).where(Company.kvk_number == kvk))


def test_sample_csv_report(session):
    report = import_csv(session, SAMPLE_CSV, load_mapping(MAPPING))

    assert report.total == 13
    assert report.created == 10
    assert report.updated == 0

    rejected = {issue.line: issue.reason for issue in report.rejected}
    assert set(rejected) == {9, 10, 11}
    assert "Bedrijfsnaam ontbreekt" in rejected[9]
    assert "ongeldig" in rejected[10]
    assert "Dubbel KvK-nummer (eerder op regel 3)" in rejected[11]

    warnings = [(issue.line, issue.reason) for issue in report.warnings]
    assert any(line == 8 and "Rechtsvorm ontbreekt" in reason for line, reason in warnings)
    assert any(line == 14 and "Rechtsvorm niet herkend" in reason for line, reason in warnings)
    # Line 12 has an invalid phone, email and website, but is still imported.
    assert sum(1 for line, _ in warnings if line == 12) == 3
    bad = _company(session, "10000009")
    assert bad is not None
    assert (bad.phone, bad.email, bad.website) == (None, None, None)


def test_values_are_normalised(session):
    import_csv(session, SAMPLE_CSV, load_mapping(MAPPING))

    first = _company(session, "10000001")
    assert first.legal_form is LegalForm.BV
    assert first.legal_form_source == "csv"
    assert first.phone == "+31381234567"
    assert first.website == "https://www.voorbeeld-installatie.example"
    assert first.status is CompanyStatus.NEW
    assert _company(session, "10000002").legal_form is LegalForm.NV
    assert _company(session, "10000006").legal_form is LegalForm.COOPERATIE
    assert _company(session, "10000007").legal_form is LegalForm.UNKNOWN
    assert _company(session, "10000011").legal_form is LegalForm.UNKNOWN


def test_first_row_wins_for_duplicate_kvk(session):
    import_csv(session, SAMPLE_CSV, load_mapping(MAPPING))
    assert _company(session, "10000002").name == "Demo Bouwgroep N.V."


def test_reimport_does_not_duplicate(session):
    mapping = load_mapping(MAPPING)
    import_csv(session, SAMPLE_CSV, mapping)
    second = import_csv(session, SAMPLE_CSV, mapping)

    assert second.created == 0
    assert second.updated == 0
    assert second.unchanged == 10
    assert len(session.scalars(select(Company)).all()) == 10


def test_reimport_updates_changed_fields_but_keeps_status(session, tmp_path):
    mapping = ImportMapping(columns={"name": "Naam", "kvk_number": "KvK", "city": "Plaats"})
    first = tmp_path / "a.csv"
    first.write_text("Naam;KvK;Plaats\nAlfa B.V.;30000001;Zwolle\n", encoding="utf-8")
    import_csv(session, first, mapping)
    _company(session, "30000001").status = CompanyStatus.OBJECTION

    second = tmp_path / "b.csv"
    second.write_text("Naam;KvK;Plaats\nAlfa B.V.;30000001;Kampen\n", encoding="utf-8")
    report = import_csv(session, second, mapping)

    company = _company(session, "30000001")
    assert report.updated == 1
    assert company.city == "Kampen"
    assert company.status is CompanyStatus.OBJECTION


def test_unknown_legal_form_does_not_overwrite_known_one(session, tmp_path):
    mapping = ImportMapping(columns={"name": "Naam", "kvk_number": "KvK", "legal_form": "Vorm"})
    path = tmp_path / "a.csv"
    path.write_text("Naam;KvK;Vorm\nAlfa;30000001;B.V.\n", encoding="utf-8")
    import_csv(session, path, mapping)
    path.write_text("Naam;KvK;Vorm\nAlfa;30000001;\n", encoding="utf-8")
    import_csv(session, path, mapping)
    assert _company(session, "30000001").legal_form is LegalForm.BV


def test_other_column_names_via_mapping(session, tmp_path):
    mapping_file = tmp_path / "mapping.yaml"
    mapping_file.write_text(
        'delimiter: ","\ncolumns:\n  name: Company\n  kvk_number: CoC\n', encoding="utf-8"
    )
    csv_file = tmp_path / "export.csv"
    csv_file.write_text("Company,CoC\nBeta B.V.,30000002\n", encoding="utf-8")

    report = import_csv(session, csv_file, load_mapping(mapping_file))
    assert report.created == 1


def test_missing_csv_column_gives_clear_error(session, tmp_path):
    csv_file = tmp_path / "export.csv"
    csv_file.write_text("Bedrijfsnaam;Iets anders\nBeta;30000002\n", encoding="utf-8")
    with pytest.raises(ImportConfigError, match="KvK-nummer"):
        import_csv(session, csv_file, load_mapping(MAPPING))


def test_mapping_without_required_field_is_rejected(tmp_path):
    mapping_file = tmp_path / "mapping.yaml"
    mapping_file.write_text("columns:\n  name: Naam\n", encoding="utf-8")
    with pytest.raises(ImportConfigError, match="kvk_number"):
        load_mapping(mapping_file)


def test_report_contains_no_company_names(session):
    report = import_csv(session, SAMPLE_CSV, load_mapping(MAPPING))
    text = report.render()
    assert "Bakkerij" not in text
    assert "Demo Bouwgroep" not in text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("038-1234567", "+31381234567"),
        ("+31 (0)38 123 45 67", "+31381234567"),
        ("0031381234567", "+31381234567"),
        ("06 12345678", "+31612345678"),
        ("12345", None),
        ("geen", None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"), [("12345678", "12345678"), ("1234567", None), ("1234567a", None)]
)
def test_normalize_kvk(raw, expected):
    assert normalize_kvk(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("B.V.", LegalForm.BV),
        ("Besloten Vennootschap", LegalForm.BV),
        ("Coöperatie U.A.", LegalForm.COOPERATIE),
        ("C.V.", LegalForm.CV),
        ("V.O.F.", LegalForm.VOF),
        ("Vereniging met beperkte rechtsbevoegdheid", LegalForm.UNKNOWN),
        ("Limited", LegalForm.UNKNOWN),
        ("", LegalForm.UNKNOWN),
        (None, LegalForm.UNKNOWN),
    ],
)
def test_normalize_legal_form(raw, expected):
    assert normalize_legal_form(raw) is expected


def test_migration_creates_schema(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    from salesagent.db import make_engine

    url = f"sqlite:///{tmp_path / 'test.db'}"
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    tables = set(inspect(make_engine(url)).get_table_names())
    assert {"company", "contact", "vehicle", "consent", "profile", "call_attempt"} <= tables
    assert Path(tmp_path / "test.db").exists()

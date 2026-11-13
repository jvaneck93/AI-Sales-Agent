"""Command line interface. All user-facing text is Dutch."""

from datetime import date
from pathlib import Path

import typer
from sqlalchemy import select

from salesagent.compliance.gate import may_be_called
from salesagent.compliance.register import ConsentError, register_objection, register_opt_in
from salesagent.config import get_settings
from salesagent.db import make_session_factory
from salesagent.importer.csv_import import ImportConfigError, import_csv, load_mapping
from salesagent.models import Company
from salesagent.normalize import normalize_kvk

PROJECT_ROOT = Path(__file__).resolve().parents[2]

app = typer.Typer(help="Sales AI Agent: prospecting voor zakelijke lease.", no_args_is_help=True)
db_app = typer.Typer(help="Databasebeheer.", no_args_is_help=True)
compliance_app = typer.Typer(help="Compliance-poort en registers.", no_args_is_help=True)
app.add_typer(db_app, name="db")
privacy_app = typer.Typer(help="Verwijderen en bewaartermijnen.", no_args_is_help=True)
app.add_typer(compliance_app, name="compliance")
app.add_typer(privacy_app, name="privacy")


def _fail(message: str) -> None:
    typer.secho(f"Fout: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


@db_app.command("init")
def db_init() -> None:
    """Maak de database aan of werk hem bij naar de nieuwste versie."""
    from salesagent.db import upgrade_database

    upgrade_database()
    typer.echo(f"Database is bijgewerkt: {get_settings().database_url}")


@app.command("import")
def import_command(
    csv_file: Path = typer.Argument(..., exists=True, dir_okay=False, help="CSV-bestand."),
    mapping: Path = typer.Option(
        PROJECT_ROOT / "config" / "import_mapping.yaml", help="Kolomtoewijzing (YAML)."
    ),
) -> None:
    """Importeer bedrijven uit een CSV-bestand en toon het importrapport."""
    try:
        import_mapping = load_mapping(mapping)
        with make_session_factory()() as session:
            report = import_csv(session, csv_file, import_mapping)
    except ImportConfigError as exc:
        _fail(str(exc))
    typer.echo(report.render())


@compliance_app.command("check")
def compliance_check() -> None:
    """Toon per bedrijf of het gebeld mag worden en waarom."""
    with make_session_factory()() as session:
        companies = session.scalars(select(Company).order_by(Company.kvk_number)).all()
        if not companies:
            typer.echo("Geen bedrijven gevonden. Importeer eerst een CSV.")
            return
        for company in companies:
            decision = may_be_called(company, session)
            verdict = "JA " if decision.allowed else "NEE"
            typer.echo(
                f"{company.kvk_number}  {verdict}  {company.name}\n          {decision.reason}"
            )


def _company_by_kvk(session, kvk: str) -> Company:
    normalized = normalize_kvk(kvk)
    company = (
        session.scalar(select(Company).where(Company.kvk_number == normalized))
        if normalized
        else None
    )
    if company is None:
        _fail(f"Geen bedrijf gevonden met KvK-nummer {kvk}.")
    return company


@compliance_app.command("optin")
def compliance_opt_in(
    kvk: str = typer.Option(..., help="KvK-nummer van het bedrijf."),
    bron: str = typer.Option(..., help="Waar de toestemming vandaan komt."),
    datum: str = typer.Option(..., help="Datum van toestemming (JJJJ-MM-DD)."),
    bewijs: str = typer.Option(..., help="Verwijzing naar het bewijs."),
) -> None:
    """Leg aantoonbare toestemming vast voor een bedrijf."""
    try:
        consent_date = date.fromisoformat(datum)
    except ValueError:
        _fail("Datum moet de vorm JJJJ-MM-DD hebben.")
    with make_session_factory()() as session:
        company = _company_by_kvk(session, kvk)
        try:
            register_opt_in(
                session, company, source=bron, consent_date=consent_date, evidence=bewijs
            )
        except ConsentError as exc:
            _fail(str(exc))
        session.commit()
    typer.echo(f"Toestemming vastgelegd voor KvK {kvk}.")


@compliance_app.command("verzet")
def compliance_objection(
    bron: str = typer.Option(..., help="Waar het verzet vandaan komt."),
    kvk: str | None = typer.Option(None, help="KvK-nummer."),
    telefoon: str | None = typer.Option(None, help="Telefoonnummer."),
    bewijs: str = typer.Option("", help="Verwijzing naar het bewijs."),
) -> None:
    """Registreer verzet. Blokkeert direct en blijvend op KvK-nummer en/of telefoonnummer."""
    with make_session_factory()() as session:
        try:
            register_objection(
                session, source=bron, kvk_number=kvk, phone=telefoon, evidence=bewijs
            )
        except ConsentError as exc:
            _fail(str(exc))
        session.commit()
    typer.echo("Verzet geregistreerd. Dit bedrijf of nummer wordt niet meer gebeld.")


@app.command("import-voertuigen")
def import_vehicles_command(
    csv_file: Path = typer.Argument(..., exists=True, dir_okay=False, help="CSV-bestand."),
    mapping: Path = typer.Option(
        PROJECT_ROOT / "config" / "import_mapping.yaml", help="Kolomtoewijzing (YAML)."
    ),
) -> None:
    """Importeer voertuigen uit een CSV-bestand (gekoppeld op KvK-nummer)."""
    from salesagent.importer.vehicle_import import import_vehicles, load_vehicle_mapping

    try:
        vehicle_mapping = load_vehicle_mapping(mapping)
        with make_session_factory()() as session:
            report = import_vehicles(session, csv_file, vehicle_mapping)
    except ImportConfigError as exc:
        _fail(str(exc))
    typer.echo(report.render())


@compliance_app.command("rechtsvorm-ophalen")
def compliance_resolve_legal_forms() -> None:
    """Haal onbekende rechtsvormen op via de KvK API (vereist KVK_API_KEY)."""
    from salesagent.compliance.kvk import KvkError, kvk_client_from_settings, resolve_legal_form
    from salesagent.models import LegalForm

    client = kvk_client_from_settings(get_settings())
    if client is None:
        _fail("Geen KVK_API_KEY ingesteld in .env.")
    resolved = failed = 0
    with make_session_factory()() as session:
        unknown = session.scalars(
            select(Company).where(Company.legal_form == LegalForm.UNKNOWN)
        ).all()
        for company in unknown:
            try:
                if resolve_legal_form(company, client) is not LegalForm.UNKNOWN:
                    resolved += 1
            except KvkError as exc:
                failed += 1
                typer.echo(f"KvK {company.kvk_number}: {exc}", err=True)
        session.commit()
    typer.echo(f"Onbekend: {len(unknown)}, opgehaald: {resolved}, mislukt: {failed}")


@privacy_app.command("verwijder-bedrijf")
def privacy_delete_company(
    kvk: str = typer.Option(..., help="KvK-nummer van het bedrijf."),
    ja: bool = typer.Option(False, "--ja", help="Niet om bevestiging vragen."),
) -> None:
    """Verwijder een bedrijf met alle gekoppelde gegevens. Verzet blijft staan."""
    from salesagent.privacy import delete_company

    with make_session_factory()() as session:
        company = _company_by_kvk(session, kvk)
        if not ja:
            typer.confirm(f"KvK {kvk} met alle gekoppelde gegevens verwijderen?", abort=True)
        delete_company(session, company)
        session.commit()
    typer.echo(f"KvK {kvk} is verwijderd. Een eventueel verzet blijft geregistreerd.")


@privacy_app.command("verwijder-contact")
def privacy_delete_contact(
    contact_id: int = typer.Option(..., "--id", help="ID van de contactpersoon."),
) -> None:
    """Verwijder één contactpersoon."""
    from salesagent.models import Contact
    from salesagent.privacy import delete_contact

    with make_session_factory()() as session:
        contact = session.get(Contact, contact_id)
        if contact is None:
            _fail(f"Geen contactpersoon met ID {contact_id}.")
        delete_contact(session, contact)
        session.commit()
    typer.echo(f"Contactpersoon {contact_id} is verwijderd.")


@privacy_app.command("opschonen")
def privacy_purge() -> None:
    """Pas de bewaartermijnen toe op gespreksverslagen en contactpersonen."""
    from salesagent.privacy import purge_expired

    settings = get_settings()
    with make_session_factory()() as session:
        result = purge_expired(
            session,
            transcript_days=settings.retention_transcript_days,
            contact_days=settings.retention_contact_days,
        )
        session.commit()
    typer.echo(result.render())


@app.command("web")
def web(
    port: int = typer.Option(8000, help="Poort."),
    host: str = typer.Option("127.0.0.1", help="Adres om op te luisteren."),
) -> None:
    """Start de webpagina om import en compliance in de browser te proberen."""
    import uvicorn

    uvicorn.run("salesagent.api.app:app", host=host, port=port)


if __name__ == "__main__":
    app()

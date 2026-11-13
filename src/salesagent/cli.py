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
app.add_typer(compliance_app, name="compliance")


def _fail(message: str) -> None:
    typer.secho(f"Fout: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


@db_app.command("init")
def db_init() -> None:
    """Maak de database aan of werk hem bij naar de nieuwste versie."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(config, "head")
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


if __name__ == "__main__":
    app()

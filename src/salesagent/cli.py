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
    bestand: Path = typer.Argument(..., exists=True, dir_okay=False, help="Excel- of CSV-bestand."),
    mapping: Path = typer.Option(
        PROJECT_ROOT / "config" / "import_mapping.yaml", help="Kolomtoewijzing (YAML)."
    ),
) -> None:
    """Importeer leads (bedrijven, contactpersonen, voertuigen) uit Excel of CSV."""
    try:
        import_mapping = load_mapping(mapping)
        with make_session_factory()() as session:
            report = import_csv(
                session,
                bestand,
                import_mapping,
                report_dir=PROJECT_ROOT / "data" / "importrapporten",
            )
    except ImportConfigError as exc:
        _fail(str(exc))
    typer.echo(report.render())


@app.command("import-sjabloon")
def import_template_command(
    uit: Path = typer.Argument(Path("sjabloon-leads.xlsx"), help="Waar het sjabloon komt."),
) -> None:
    """Maak een Excel-sjabloon met de kolommen voor de leadimport."""
    from salesagent.importer.template import build_template

    uit.write_bytes(build_template())
    typer.echo(f"Sjabloon geschreven naar {uit}")


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
    csv_file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Excel- of CSV-bestand."
    ),
    mapping: Path = typer.Option(
        PROJECT_ROOT / "config" / "import_mapping.yaml", help="Kolomtoewijzing (YAML)."
    ),
) -> None:
    """Importeer alleen voertuigen uit een apart bestand (gekoppeld op KvK-nummer)."""
    from salesagent.importer.vehicle_import import import_vehicles, load_vehicle_mapping

    try:
        vehicle_mapping = load_vehicle_mapping(mapping)
        with make_session_factory()() as session:
            report = import_vehicles(
                session,
                csv_file,
                vehicle_mapping,
                report_dir=PROJECT_ROOT / "data" / "importrapporten",
            )
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


# --- Call centre ---------------------------------------------------------------------------

campaign_app = typer.Typer(help="Merken en belrondes.", no_args_is_help=True)
recordings_app = typer.Typer(
    help="CATI-opnames verwerken tot playbook en testset.", no_args_is_help=True
)
agent_app = typer.Typer(help="Voice-agent: prompt en evaluatie.", no_args_is_help=True)
app.add_typer(campaign_app, name="ronde")
app.add_typer(recordings_app, name="opnames")
app.add_typer(agent_app, name="agent")


def _mask_number(value: str) -> str:
    return (value[:3] + "*" * max(0, len(value) - 5) + value[-2:]) if value else ""


@app.command("demo")
def demo_command(
    bedrijven: int = typer.Option(160, help="Aantal verzonnen bedrijven."),
) -> None:
    """Vul de database met verzonnen demodata en draai een gesimuleerde belronde per merk."""
    from salesagent.demo import run_demo

    with make_session_factory()() as session:
        report = run_demo(session, companies=bedrijven)
        session.commit()
    typer.echo(report)
    typer.echo("\nKlaar. Start de webpagina met: salesagent web")


@campaign_app.command("merken")
def list_brands() -> None:
    """Toon de merken met hun belrondes."""
    from salesagent.models import Brand, Campaign

    with make_session_factory()() as session:
        brands = session.scalars(select(Brand).order_by(Brand.name)).all()
        if not brands:
            typer.echo("Nog geen merken. Voeg ze toe via de webpagina of draai: salesagent demo")
        for brand in brands:
            typer.echo(f"{brand.name}")
            campaigns = session.scalars(select(Campaign).where(Campaign.brand_id == brand.id))
            for campaign in campaigns:
                typer.echo(f"  ronde {campaign.id}: {campaign.name} ({campaign.status.value})")


@campaign_app.command("maak")
def campaign_create(
    merk: str = typer.Option(..., help="Naam van het merk."),
    naam: str = typer.Option(..., help="Naam van de belronde."),
    varianten: str = typer.Option("A", help="Agentvarianten voor A/B, bv. A,B."),
) -> None:
    """Maak een nieuwe belronde voor een merk."""
    from salesagent.models import Brand
    from salesagent.operations.campaigns import create_campaign

    with make_session_factory()() as session:
        brand = session.scalar(select(Brand).where(Brand.name == merk))
        if brand is None:
            _fail(f"Merk '{merk}' bestaat niet.")
        campaign = create_campaign(session, brand, naam, varianten)
        session.commit()
        typer.echo(
            f"Belronde {campaign.id} aangemaakt. Volgende stap: salesagent ronde "
            f"voorbereiden {campaign.id}"
        )


@campaign_app.command("voorbereiden")
def campaign_prepare(ronde: int = typer.Argument(..., help="ID van de belronde.")) -> None:
    """Selecteer bedrijven, controleer de rechtsvorm opnieuw en pas de compliance-poort toe."""
    from salesagent.compliance.kvk import kvk_client_from_settings
    from salesagent.models import Campaign
    from salesagent.operations.campaigns import MockKvkClient, prepare_round

    with make_session_factory()() as session:
        campaign = session.get(Campaign, ronde)
        if campaign is None:
            _fail(f"Geen belronde met ID {ronde}.")
        client = kvk_client_from_settings(get_settings()) or MockKvkClient(session)
        report = prepare_round(session, campaign, client)
        session.commit()
    live_kvk = bool(get_settings().kvk_api_key)
    source = "KvK API" if live_kvk else "sandbox (mock-KvK, niet geldig voor live)"
    typer.echo(f"Rechtsvorm gecontroleerd via {source}.")
    typer.echo(report.render())


@campaign_app.command("bellen")
def campaign_call(
    ronde: int = typer.Argument(..., help="ID van de belronde."),
    modus: str = typer.Option("dryrun", help="dryrun, simulator of live."),
    aantal: int = typer.Option(25, help="Maximaal aantal gesprekken."),
    buiten_beltijden: bool = typer.Option(
        False, help="Alleen simulator: ook buiten de beltijden draaien."
    ),
    toon_verzoeken: int = typer.Option(1, help="Dryrun: hoeveel verzoeken volledig tonen."),
) -> None:
    """Draai een belronde. Standaard een droge run die laat zien wat er verstuurd zou worden."""
    import json

    from salesagent.models import Campaign
    from salesagent.operations.dialer import run_round
    from salesagent.voice.providers import DryRunProvider, ElevenLabsProvider, SimulatorProvider

    settings = get_settings()
    if modus == "live":
        provider = ElevenLabsProvider(
            settings.elevenlabs_api_key,
            dry_run=settings.dry_run,
            blocked_numbers=set(filter(None, settings.test_block_numbers.split(","))),
        )
        ignore_window = False
    elif modus == "dryrun":
        provider, ignore_window = DryRunProvider(), True
    elif modus == "simulator":
        provider, ignore_window = SimulatorProvider(), buiten_beltijden
    else:
        _fail("Modus moet dryrun, simulator of live zijn.")
    with make_session_factory()() as session:
        campaign = session.get(Campaign, ronde)
        if campaign is None:
            _fail(f"Geen belronde met ID {ronde}.")
        report = run_round(
            session, campaign, provider, limit=max(1, aantal), ignore_call_window=ignore_window
        )
        session.commit()
    typer.echo(report.render())
    for request in report.dry_run_requests[: max(0, toon_verzoeken)]:
        shown = json.loads(json.dumps(request))
        shown["to_number"] = _mask_number(shown.get("to_number", ""))
        typer.echo(
            "\nZou versturen naar POST /v1/convai/sip-trunk/outbound-call (nummer gemaskeerd):"
        )
        typer.echo(json.dumps(shown, ensure_ascii=False, indent=2))


@app.command("research")
def research_command(
    kvk: list[str] = typer.Option(None, help="Alleen deze KvK-nummers (meerdere mogelijk)."),
    ronde: int = typer.Option(None, help="Alleen belbare leads uit deze belronde."),
    max_aantal: int = typer.Option(25, "--max", help="Maximaal aantal profielen per keer."),
) -> None:
    """Maak of ververs profielen van belbare leads, in porties. Zonder sleutel: sandbox."""
    from salesagent.enrichment.research import (
        research_candidates,
        research_provider_from_settings,
        run_research,
    )

    settings = get_settings()
    with make_session_factory()() as session:
        if kvk:
            companies = [_company_by_kvk(session, k) for k in kvk]
        else:
            companies = research_candidates(
                session,
                limit=max(1, max_aantal),
                campaign_id=ronde,
                max_age_days=settings.research_max_age_days,
            )
        if not companies:
            typer.echo("Geen belbare leads zonder actueel profiel. Bereid eerst een belronde voor.")
            return
        provider = research_provider_from_settings(settings)
        report = run_research(
            session, companies, provider, max_age_days=settings.research_max_age_days
        )
        session.commit()
    typer.echo(report.render())


@recordings_app.command("verwerken")
def recordings_process(
    export: Path = typer.Argument(..., exists=True, dir_okay=False, help="CATI-export (CSV)."),
    map_opnames: Path = typer.Option(None, "--map", help="Map met de mp4-bestanden."),
    whisper: bool = typer.Option(False, help="Transcriberen met faster-whisper (lokaal)."),
    claude: bool = typer.Option(False, help="Kenmerken laten extraheren door Claude."),
) -> None:
    """Verwerk opnames: pseudonimiseren, kenmerken, playbook, bezwarenbibliotheek, testset."""
    from salesagent.recordings.features import ClaudeExtractor, RuleBasedExtractor
    from salesagent.recordings.pipeline import (
        analyze,
        build_eval_cases,
        build_outputs,
        ingest_export,
    )
    from salesagent.recordings.transcribe import SidecarTranscriber, WhisperTranscriber
    from salesagent.voice.library import seed_objection_library

    settings = get_settings()
    if claude and not settings.anthropic_api_key:
        _fail("--claude vereist ANTHROPIC_API_KEY in .env.")
    transcriber = WhisperTranscriber() if whisper else SidecarTranscriber()
    extractor = (
        ClaudeExtractor(settings.anthropic_api_key, settings.anthropic_model)
        if claude
        else RuleBasedExtractor()
    )
    with make_session_factory()() as session:
        seed_objection_library(session)
        report = ingest_export(
            session,
            export,
            map_opnames or export.parent,
            transcriber=transcriber,
            extractor=extractor,
            workdir=PROJECT_ROOT / "data" / "opnames_werk",
        )
        playbook = build_outputs(session, analyze(session))
        cases = build_eval_cases(session)
        session.commit()
    typer.echo(report.render())
    typer.echo(f"Playbook {playbook.version} gemaakt, {cases} testgevallen toegevoegd.")


@recordings_app.command("demo")
def recordings_demo(aantal: int = typer.Option(600, help="Aantal verzonnen gesprekken.")) -> None:
    """Maak een verzonnen CATI-export met opnames om de pijplijn te proberen."""
    from salesagent.recordings.demo_data import generate

    target = PROJECT_ROOT / "data" / "demo_opnames"
    export = generate(target, n=aantal)
    typer.echo(
        f"Demo-export: {export}\nVerwerk met: salesagent opnames verwerken {export} "
        f"--map {target / 'opnames'}"
    )


@agent_app.command("prompt")
def agent_prompt(
    uit: Path = typer.Option(None, help="Schrijf naar dit bestand in plaats van het scherm."),
) -> None:
    """Exporteer de systeemprompt en bezwarenbibliotheek voor de ElevenLabs-agent."""
    from salesagent.voice.prompt import build_system_prompt

    with make_session_factory()() as session:
        text = build_system_prompt(session)
    if uit:
        uit.write_text(text, encoding="utf-8")
        typer.echo(f"Prompt geschreven naar {uit}")
    else:
        typer.echo(text)


@agent_app.command("eval")
def agent_eval() -> None:
    """Draai de testset tegen de referentie-agent (harde en zachte criteria)."""
    from salesagent.evals.harness import run_eval, seed_default_cases
    from salesagent.voice.library import seed_objection_library

    with make_session_factory()() as session:
        seed_objection_library(session)
        seed_default_cases(session)
        run = run_eval(session)
        session.commit()
        scores = run.scores
        failed = [r["case"] for r in run.results if not r["geslaagd_hard"]]
    typer.echo(
        f"Harde criteria: {scores['hard_ok']}/{scores['hard_totaal']}, "
        f"zachte criteria: {scores['zacht_ok']}/{scores['zacht_totaal']}"
    )
    if failed:
        typer.secho("Gezakt op harde criteria: " + ", ".join(failed), fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command("rapport")
def report_command(ronde: int = typer.Option(None, help="Alleen deze belronde.")) -> None:
    """Toon resultaten en kosten per adres en per afspraak."""
    from salesagent.operations.reporting import attention_list, compute_stats

    def eur(value):
        return (
            "n.v.t."
            if value is None
            else f"€ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        )

    def pct(value):
        return "n.v.t." if value is None else f"{value * 100:.1f}%".replace(".", ",")

    with make_session_factory()() as session:
        s = compute_stats(session, campaign_id=ronde)
        attention = attention_list(session)
    typer.echo(f"Adressen in selectie: {s.leads} (geblokkeerd: {s.blocked})")
    typer.echo(
        f"Adressen nagebeld: {s.addresses_called}, gesprekken: {s.attempts}, bereikt: {s.connected}"
    )
    typer.echo(f"Opgehangen na AI-melding: {pct(s.hangup_rate)}")
    typer.echo(f"Warm of beter per afgerond gesprek: {pct(s.warm_rate)}")
    typer.echo(
        f"Afspraken: {s.appointments} (goedgekeurd {s.appointments_approved}, "
        f"afgekeurd {s.appointments_rejected}, te controleren {s.appointments_pending})"
    )
    typer.echo(f"Verzet: merk {s.objections_brand}, alles {s.objections_all}")
    typer.echo(f"Kosten totaal: {eur(s.cost_total)}")
    for kind, amount in sorted(s.costs.items()):
        typer.echo(f"  {kind}: {eur(amount)}")
    typer.echo(
        f"Kosten per adres: {eur(s.cost_per_address)} "
        f"(benchmark {eur(s.benchmark_cost_per_address)})"
    )
    typer.echo(f"Kosten per afspraak: {eur(s.cost_per_appointment)}")
    typer.echo(f"Brutomarge: {pct(s.margin)}")
    if attention:
        typer.echo("\nAandacht nodig:")
        for item in attention[:20]:
            typer.echo(f"  {item['soort']}: {item['tekst']}")


@app.command("web")
def web(
    port: int = typer.Option(8000, help="Poort."),
    host: str = typer.Option("127.0.0.1", help="Adres om op te luisteren."),
    browser: bool = typer.Option(False, help="Open de webpagina in de browser."),
) -> None:
    """Start de webpagina (dashboard, CRM, belrondes, opnames)."""
    import threading
    import webbrowser

    import uvicorn

    url = f"http://{host}:{port}/"
    typer.echo(f"Webpagina: {url}  (stoppen met Ctrl+C)")
    if browser:
        threading.Timer(2.0, webbrowser.open, args=(url,)).start()
    uvicorn.run("salesagent.api.app:app", host=host, port=port)


if __name__ == "__main__":
    app()

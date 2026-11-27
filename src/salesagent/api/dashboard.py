"""Dashboard / CRM pages. All text is Dutch; all links relative to the root path."""

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from salesagent.api.common import get_session, render
from salesagent.compliance.gate import may_be_called
from salesagent.compliance.kvk import kvk_client_from_settings
from salesagent.config import get_settings
from salesagent.enrichment.fleet import summarize_fleet
from salesagent.enrichment.research import (
    latest_profile,
    research_provider_from_settings,
    run_research,
)
from salesagent.evals.harness import run_eval, seed_default_cases
from salesagent.models import (
    AccountManager,
    Appointment,
    Brand,
    CallAttempt,
    Campaign,
    CampaignLead,
    Company,
    Consent,
    CostEntry,
    EvalCase,
    EvalRun,
    LeadStatus,
    ObjectionEntry,
    OutboxMessage,
    PlaybookVersion,
    Recording,
)
from salesagent.operations.campaigns import MockKvkClient, create_campaign, prepare_round
from salesagent.operations.dialer import run_round
from salesagent.operations.qa import (
    QaError,
    approve_appointment,
    record_result,
    reject_appointment,
    review_call,
)
from salesagent.operations.reporting import attention_list, compute_stats
from salesagent.voice.library import seed_objection_library
from salesagent.voice.prompt import DATA_COLLECTION, build_system_prompt
from salesagent.voice.providers import DryRunProvider, ElevenLabsProvider, SimulatorProvider

router = APIRouter()
ROOT = Path(__file__).resolve().parents[3]


def _redirect(request: Request, path: str, **params) -> RedirectResponse:
    from urllib.parse import urlencode

    root = request.scope.get("root_path", "").rstrip("/")
    query = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(f"{root}/{path}{query}", status_code=303)


def _flash(request: Request) -> dict:
    return {
        "message": request.query_params.get("melding", ""),
        "error": request.query_params.get("fout", ""),
        "report": request.query_params.get("rapport", ""),
    }


# --- Overview -----------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def overview(request: Request, session: Session = Depends(get_session)):
    stats = compute_stats(session)
    brands = session.scalars(select(Brand).order_by(Brand.name)).all()
    per_brand = [(b, compute_stats(session, brand_id=b.id)) for b in brands]
    campaigns = session.scalars(select(Campaign).order_by(Campaign.id.desc())).all()
    empty = not session.scalar(select(func.count(Company.id)))
    funnel = [
        ("Leads in rondes", stats.leads),
        ("Belbaar na compliance", stats.callable),
        ("Nagebelde adressen", stats.addresses_called),
        ("Bereikt", stats.connected),
        ("Gesprek na AI-melding", stats.completed),
        ("Warm of beter", stats.warm),
        ("Afspraak", stats.appointments),
    ]
    order = ["data", "bellen", "research", "qa"]
    costs = [(k, stats.costs.get(k, 0.0)) for k in order if stats.costs.get(k)]
    return render(
        request,
        "overzicht.html",
        nav="",
        stats=stats,
        per_brand=per_brand,
        campaigns=campaigns,
        attention=attention_list(session),
        funnel=funnel,
        funnel_max=max([v for _, v in funnel] + [1]),
        costs=costs,
        empty=empty,
        **_flash(request),
    )


@router.get("/api/stats")
def stats_json(session: Session = Depends(get_session), campagne: int | None = None):
    s = compute_stats(session, campaign_id=campagne)
    data = {k: v for k, v in s.__dict__.items()}
    for prop in (
        "cost_per_address",
        "cost_per_completed",
        "cost_per_warm",
        "cost_per_appointment",
        "margin",
        "hangup_rate",
        "warm_rate",
        "appointment_rate",
        "qa_minutes_per_appointment",
    ):
        data[prop] = getattr(s, prop)
    return JSONResponse(data)


@router.post("/demo")
def demo(request: Request, session: Session = Depends(get_session)):
    from salesagent.demo import run_demo

    report = run_demo(session)
    session.commit()
    return _redirect(
        request,
        "",
        melding="Demodata geladen en een gesimuleerde belronde per merk gedraaid.",
        rapport=report[-1500:],
    )


# --- Brands and rounds ----------------------------------------------------------


@router.get("/merken", response_class=HTMLResponse)
def brands_page(request: Request, session: Session = Depends(get_session)):
    brands = session.scalars(select(Brand).order_by(Brand.name)).all()
    campaigns = session.scalars(select(Campaign).order_by(Campaign.id.desc())).all()
    campaign_stats = {c.id: compute_stats(session, campaign_id=c.id) for c in campaigns}
    settings = get_settings()
    return render(
        request,
        "merken.html",
        nav="merken",
        brands=brands,
        campaigns=campaigns,
        campaign_stats=campaign_stats,
        live_possible=bool(settings.elevenlabs_api_key) and not settings.dry_run,
        kvk_live=bool(settings.kvk_api_key),
        **_flash(request),
    )


@router.post("/merken")
def add_brand(
    request: Request,
    naam: str = Form(...),
    pitch: str = Form(""),
    vragen: str = Form(""),
    min_voertuigen: int = Form(0),
    prijs_adres: float = Form(11.0),
    prijs_afspraak: float = Form(0.0),
    session: Session = Depends(get_session),
):
    if session.scalar(select(Brand).where(Brand.name == naam.strip())):
        return _redirect(request, "merken", fout="Dit merk bestaat al.")
    brand = Brand(
        name=naam.strip(),
        pitch=pitch.strip(),
        qualification_questions=[q.strip() for q in vragen.splitlines() if q.strip()],
        segment_rules={"min_vehicles": min_voertuigen} if min_voertuigen else {},
        price_per_address_eur=prijs_adres,
        price_per_appointment_eur=prijs_afspraak,
    )
    session.add(brand)
    session.commit()
    return _redirect(request, "merken", melding=f"Merk {brand.name} toegevoegd.")


@router.post("/merken/{brand_id}/accountmanager")
def add_am(
    request: Request,
    brand_id: int,
    naam: str = Form(...),
    email: str = Form(...),
    regio: str = Form(""),
    session: Session = Depends(get_session),
):
    session.add(
        AccountManager(
            brand_id=brand_id,
            name=naam.strip(),
            email=email.strip(),
            regions=[r.strip() for r in regio.split(",") if r.strip()],
        )
    )
    session.commit()
    return _redirect(request, "merken", melding="Accountmanager toegevoegd.")


@router.post("/campagnes")
def add_campaign(
    request: Request,
    merk: int = Form(...),
    naam: str = Form(...),
    varianten: str = Form("A"),
    session: Session = Depends(get_session),
):
    brand = session.get(Brand, merk)
    if brand is None:
        return _redirect(request, "merken", fout="Onbekend merk.")
    campaign = create_campaign(session, brand, naam.strip(), varianten.strip() or "A")
    session.commit()
    return _redirect(
        request,
        f"campagnes/{campaign.id}",
        melding="Belronde aangemaakt. Bereid hem voor: selectie, rechtsvorm-check en compliance.",
    )


@router.post("/campagnes/{campaign_id}/voorbereiden")
def prepare(request: Request, campaign_id: int, session: Session = Depends(get_session)):
    campaign = session.get(Campaign, campaign_id)
    client = kvk_client_from_settings(get_settings()) or MockKvkClient(session)
    report = prepare_round(session, campaign, client)
    session.commit()
    source = "KvK API" if get_settings().kvk_api_key else "sandbox (mock-KvK)"
    return _redirect(
        request,
        f"campagnes/{campaign_id}",
        melding=f"Ronde voorbereid. Rechtsvorm gecontroleerd via {source}.",
        rapport=report.render(),
    )


@router.post("/research")
def research(request: Request, session: Session = Depends(get_session)):
    companies = session.scalars(select(Company)).all()
    provider = research_provider_from_settings(get_settings())
    report = run_research(
        session, list(companies), provider, max_age_days=get_settings().research_max_age_days
    )
    session.commit()
    return _redirect(request, "merken", melding="Research gedraaid.", rapport=report.render())


@router.post("/campagnes/{campaign_id}/bellen")
def call_round(
    request: Request,
    campaign_id: int,
    modus: str = Form("simulator"),
    aantal: int = Form(25),
    buiten_beltijden: str = Form(""),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    campaign = session.get(Campaign, campaign_id)
    if modus == "live":
        provider = ElevenLabsProvider(
            settings.elevenlabs_api_key,
            dry_run=settings.dry_run,
            blocked_numbers=set(filter(None, settings.test_block_numbers.split(","))),
        )
        ignore_window = False
    elif modus == "dryrun":
        provider, ignore_window = DryRunProvider(), True
    else:
        provider, ignore_window = SimulatorProvider(), bool(buiten_beltijden)
    report = run_round(
        session,
        campaign,
        provider,
        limit=max(1, min(aantal, 500)),
        ignore_call_window=ignore_window,
    )
    session.commit()
    label = {"live": "Echte belronde", "dryrun": "Droge run (niets verstuurd)"}.get(
        modus, "Gesimuleerde belronde (sandbox)"
    )
    return _redirect(
        request, f"campagnes/{campaign_id}", melding=f"{label} afgerond.", rapport=report.render()
    )


@router.get("/campagnes/{campaign_id}", response_class=HTMLResponse)
def campaign_page(
    request: Request, campaign_id: int, status: str = "", session: Session = Depends(get_session)
):
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        return _redirect(request, "merken", fout="Onbekende belronde.")
    query = select(CampaignLead).where(CampaignLead.campaign_id == campaign_id)
    if status:
        query = query.where(CampaignLead.status == status)
    leads = session.scalars(query.order_by(CampaignLead.id).limit(400)).all()
    counts = dict(
        session.execute(
            select(CampaignLead.status, func.count())
            .where(CampaignLead.campaign_id == campaign_id)
            .group_by(CampaignLead.status)
        ).all()
    )
    last_dry = session.scalar(
        select(CallAttempt)
        .where(CallAttempt.campaign_id == campaign_id, CallAttempt.outcome == "dry run")
        .order_by(CallAttempt.id.desc())
    )
    return render(
        request,
        "campagne.html",
        nav="merken",
        campaign=campaign,
        leads=leads,
        counts=counts,
        statuses=list(LeadStatus),
        status=status,
        stats=compute_stats(session, campaign_id=campaign_id),
        last_dry=last_dry,
        **_flash(request),
    )


# --- CRM ------------------------------------------------------------------------


@router.get("/bedrijven", response_class=HTMLResponse)
def companies_page(request: Request, q: str = "", session: Session = Depends(get_session)):
    query = select(Company).order_by(Company.name)
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(Company.name.ilike(like), Company.kvk_number.like(like), Company.city.ilike(like))
        )
    companies = session.scalars(query.limit(300)).all()
    rows = []
    for c in companies:
        attempts = session.scalars(
            select(CallAttempt)
            .where(CallAttempt.company_id == c.id)
            .order_by(CallAttempt.id.desc())
        ).all()
        costs = (
            session.scalar(
                select(func.sum(CostEntry.amount_eur)).where(CostEntry.company_id == c.id)
            )
            or 0
        )
        rows.append(
            {
                "c": c,
                "fleet": summarize_fleet(c),
                "attempts": len(attempts),
                "last": attempts[0] if attempts else None,
                "costs": costs,
            }
        )
    return render(request, "bedrijven.html", nav="bedrijven", rows=rows, q=q, **_flash(request))


@router.get("/bedrijven/{kvk}", response_class=HTMLResponse)
def company_page(request: Request, kvk: str, session: Session = Depends(get_session)):
    company = session.scalar(select(Company).where(Company.kvk_number == kvk))
    if company is None:
        return _redirect(request, "bedrijven", fout="Onbekend KvK-nummer.")
    brands = {b.id: b for b in session.scalars(select(Brand))}
    leads = session.scalars(select(CampaignLead).where(CampaignLead.company_id == company.id))
    attempts = session.scalars(
        select(CallAttempt)
        .where(CallAttempt.company_id == company.id)
        .order_by(CallAttempt.id.desc())
    ).all()
    appointments = session.scalars(
        select(Appointment)
        .where(Appointment.company_id == company.id)
        .order_by(Appointment.starts_at)
    ).all()
    consents = session.scalars(
        select(Consent).where(
            or_(Consent.kvk_number == company.kvk_number, Consent.company_id == company.id)
        )
    ).all()
    costs = session.execute(
        select(CostEntry.kind, func.sum(CostEntry.amount_eur))
        .where(CostEntry.company_id == company.id)
        .group_by(CostEntry.kind)
    ).all()
    decisions = [(b, may_be_called(company, session, brand=b)) for b in brands.values()]
    return render(
        request,
        "bedrijf.html",
        nav="bedrijven",
        c=company,
        fleet=summarize_fleet(company),
        profile=latest_profile(session, company),
        leads=list(leads),
        attempts=attempts,
        appointments=appointments,
        consents=consents,
        brands=brands,
        costs=costs,
        cost_total=sum(v or 0 for _, v in costs),
        decisions=decisions,
        **_flash(request),
    )


@router.get("/gesprekken/{attempt_id}", response_class=HTMLResponse)
def call_page(request: Request, attempt_id: int, session: Session = Depends(get_session)):
    attempt = session.get(CallAttempt, attempt_id)
    if attempt is None:
        return _redirect(request, "bedrijven", fout="Onbekend gesprek.")
    import json

    company = session.get(Company, attempt.company_id)
    return render(
        request,
        "gesprek.html",
        nav="bedrijven",
        a=attempt,
        c=company,
        payload=json.dumps(attempt.payload, indent=2, ensure_ascii=False)
        if attempt.payload
        else "",
        **_flash(request),
    )


@router.post("/gesprekken/{attempt_id}/qa")
def call_qa(
    request: Request,
    attempt_id: int,
    oordeel: str = Form(...),
    notitie: str = Form(""),
    minuten: float = Form(2),
    session: Session = Depends(get_session),
):
    attempt = session.get(CallAttempt, attempt_id)
    try:
        review_call(session, attempt, ok=oordeel == "goed", note=notitie, minutes=minuten)
    except QaError as exc:
        return _redirect(request, f"gesprekken/{attempt_id}", fout=str(exc))
    session.commit()
    return _redirect(request, "qa", melding=f"Gesprek {attempt_id} beoordeeld.")


# --- Appointments and QA ---------------------------------------------------------


@router.get("/afspraken", response_class=HTMLResponse)
def appointments_page(request: Request, session: Session = Depends(get_session)):
    rows = session.scalars(select(Appointment).order_by(Appointment.starts_at)).all()
    return render(request, "afspraken.html", nav="afspraken", rows=rows, **_flash(request))


@router.get("/afspraken/{appointment_id}", response_class=HTMLResponse)
def appointment_page(
    request: Request, appointment_id: int, session: Session = Depends(get_session)
):
    a = session.get(Appointment, appointment_id)
    if a is None:
        return _redirect(request, "afspraken", fout="Onbekende afspraak.")
    attempt = session.get(CallAttempt, a.call_attempt_id) if a.call_attempt_id else None
    return render(
        request, "afspraak.html", nav="afspraken", a=a, attempt=attempt, **_flash(request)
    )


@router.post("/afspraken/{appointment_id}/{action}")
def appointment_action(
    request: Request,
    appointment_id: int,
    action: str,
    notitie: str = Form(""),
    minuten: float = Form(4),
    resultaat: str = Form(""),
    session: Session = Depends(get_session),
):
    a = session.get(Appointment, appointment_id)
    try:
        if action == "goedkeuren":
            approve_appointment(session, a, minutes=minuten, note=notitie)
            msg = "Goedgekeurd. Briefing en bevestiging staan in de uitgaande mail."
        elif action == "afkeuren":
            reject_appointment(session, a, note=notitie, minutes=minuten)
            msg = "Afgekeurd. De lead staat weer als warm bij de accountmanager."
        elif action == "resultaat":
            record_result(session, a, resultaat)
            msg = "Terugmelding vastgelegd."
        else:
            raise QaError("Onbekende actie.")
    except QaError as exc:
        return _redirect(request, f"afspraken/{appointment_id}", fout=str(exc))
    session.commit()
    return _redirect(request, f"afspraken/{appointment_id}", melding=msg)


@router.get("/qa", response_class=HTMLResponse)
def qa_page(request: Request, session: Session = Depends(get_session)):
    pending = session.scalars(
        select(Appointment)
        .where(Appointment.qa_status == "te controleren")
        .order_by(Appointment.starts_at)
    ).all()
    sample = session.scalars(
        select(CallAttempt)
        .where(CallAttempt.qa_status == "steekproef")
        .order_by(CallAttempt.id)
        .limit(100)
    ).all()
    errors = session.scalars(
        select(CallAttempt)
        .where(CallAttempt.qa_status == "fout")
        .order_by(CallAttempt.id.desc())
        .limit(50)
    ).all()
    return render(
        request,
        "qa.html",
        nav="qa",
        pending=pending,
        sample=sample,
        errors=errors,
        stats=compute_stats(session),
        **_flash(request),
    )


@router.get("/outbox", response_class=HTMLResponse)
def outbox_page(request: Request, session: Session = Depends(get_session)):
    rows = session.scalars(select(OutboxMessage).order_by(OutboxMessage.id.desc()).limit(200)).all()
    return render(request, "outbox.html", nav="outbox", rows=rows, **_flash(request))


# --- Recordings, agent and eval -------------------------------------------------


@router.get("/opnames", response_class=HTMLResponse)
def recordings_page(request: Request, session: Session = Depends(get_session)):
    playbook = session.scalar(select(PlaybookVersion).order_by(PlaybookVersion.id.desc()))
    total = session.scalar(select(func.count(Recording.id))) or 0
    usable = (
        session.scalar(
            select(func.count(Recording.id)).where(
                Recording.features.is_not(None), Recording.excluded_reason.is_(None)
            )
        )
        or 0
    )
    proposals = session.scalars(
        select(ObjectionEntry).where(ObjectionEntry.approved.is_(False))
    ).all()
    sample = session.scalars(
        select(Recording).where(Recording.transcript.is_not(None)).order_by(Recording.id).limit(3)
    ).all()
    return render(
        request,
        "opnames.html",
        nav="opnames",
        playbook=playbook,
        total=total,
        usable=usable,
        proposals=proposals,
        sample=sample,
        stats=playbook.stats if playbook else None,
        **_flash(request),
    )


@router.post("/opnames/verwerken")
async def recordings_process(
    request: Request,
    export: UploadFile | None = None,
    map_opnames: str = Form(""),
    demo: str = Form(""),
    session: Session = Depends(get_session),
):
    from salesagent.recordings.features import RuleBasedExtractor
    from salesagent.recordings.pipeline import analyze, build_outputs, ingest_export
    from salesagent.recordings.transcribe import SidecarTranscriber

    workdir = ROOT / "data" / "opnames_werk"
    if demo:
        from salesagent.recordings.demo_data import generate

        export_path = generate(ROOT / "data" / "demo_opnames")
        recordings_dir = ROOT / "data" / "demo_opnames" / "opnames"
    else:
        if export is None or not export.filename:
            return _redirect(request, "opnames", fout="Kies een CATI-export (CSV).")
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as handle:
            handle.write(await export.read())
            export_path = Path(handle.name)
        recordings_dir = Path(map_opnames.strip()) if map_opnames.strip() else export_path.parent
    seed_objection_library(session)
    report = ingest_export(
        session,
        export_path,
        recordings_dir,
        transcriber=SidecarTranscriber(),
        extractor=RuleBasedExtractor(),
        workdir=workdir,
    )
    playbook = build_outputs(session, analyze(session))
    session.commit()
    if not demo:
        export_path.unlink(missing_ok=True)
    return _redirect(
        request,
        "opnames",
        melding=f"Opnames verwerkt; playbook {playbook.version} gemaakt.",
        rapport=report.render(),
    )


@router.post("/bezwaren/{entry_id}/goedkeuren")
def approve_objection(request: Request, entry_id: int, session: Session = Depends(get_session)):
    proposal = session.get(ObjectionEntry, entry_id)
    base_category = proposal.category.removesuffix("__voorstel")
    base = session.scalar(select(ObjectionEntry).where(ObjectionEntry.category == base_category))
    if base is not None:
        base.responses = proposal.responses + [
            r for r in base.responses if r not in proposal.responses
        ]
        base.source = "opnames"
        session.delete(proposal)
    else:
        proposal.approved = True
    session.commit()
    return _redirect(
        request,
        "agent",
        melding="Bezwaarreactie goedgekeurd. Draai de testset "
        "voordat je de nieuwe prompt live zet.",
    )


@router.get("/agent", response_class=HTMLResponse)
def agent_page(request: Request, session: Session = Depends(get_session)):
    seed_objection_library(session)
    seed_default_cases(session)
    session.commit()
    library = session.scalars(
        select(ObjectionEntry).order_by(ObjectionEntry.approved.desc(), ObjectionEntry.category)
    ).all()
    runs = session.scalars(select(EvalRun).order_by(EvalRun.id.desc()).limit(10)).all()
    cases = session.scalars(select(EvalCase).order_by(EvalCase.id)).all()
    return render(
        request,
        "agent.html",
        nav="agent",
        prompt=build_system_prompt(session),
        data_collection=DATA_COLLECTION,
        library=library,
        runs=runs,
        cases=cases,
        **_flash(request),
    )


@router.post("/agent/eval")
def agent_eval(request: Request, session: Session = Depends(get_session)):
    seed_default_cases(session)
    run = run_eval(session)
    session.commit()
    verdict = (
        "geslaagd op alle harde criteria"
        if run.passed_hard
        else "NIET geslaagd op de harde criteria: niet live zetten"
    )
    return _redirect(
        request,
        "agent",
        melding=f"Testset gedraaid: {verdict}. "
        f"Zacht: {run.scores['zacht_ok']}/{run.scores['zacht_totaal']}.",
    )


@router.get("/agent/eval/{run_id}", response_class=HTMLResponse)
def eval_detail(request: Request, run_id: int, session: Session = Depends(get_session)):
    return render(request, "eval.html", nav="agent", run=session.get(EvalRun, run_id))

"""Fictional demo data, so the whole flow can be tried without any keys.

All companies, people and numbers are made up (hard rule 7: no real personal
data in fixtures). Brand names are fictional too.
"""

import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from salesagent.enrichment.research import MockResearchProvider, run_research
from salesagent.models import (
    AccountManager,
    Appointment,
    Brand,
    Company,
    Contact,
    LegalForm,
    Vehicle,
    utcnow,
)
from salesagent.operations.campaigns import MockKvkClient, create_campaign, prepare_round
from salesagent.operations.dialer import run_round
from salesagent.operations.qa import approve_appointment, record_result, reject_appointment
from salesagent.voice.library import seed_objection_library
from salesagent.voice.providers import SimulatorProvider

TZ = ZoneInfo("Europe/Amsterdam")
CITIES = ["Zwolle", "Apeldoorn", "Deventer", "Amersfoort", "Utrecht", "Arnhem", "Ede", "Hengelo"]
SECTORS = [
    "Installatietechniek",
    "Bouw",
    "Transport",
    "Groothandel",
    "Schoonmaak",
    "Hoveniers",
    "Elektrotechniek",
    "Facilitair",
]
PREFIX = ["Noord", "Delta", "Vector", "Polder", "Kring", "Linde", "Brug", "Gaaf", "Zuidas", "Eik"]
SUFFIX = ["Techniek", "Bouw", "Logistiek", "Service", "Groep", "Montage", "Partners", "Werken"]
MAKES = ["Fictiva", "Voltera", "Nordwagen", "Kestrel"]
FORMS = [
    (LegalForm.BV, 0.74),
    (LegalForm.NV, 0.03),
    (LegalForm.EENMANSZAAK, 0.09),
    (LegalForm.VOF, 0.07),
    (LegalForm.STICHTING, 0.03),
    (LegalForm.UNKNOWN, 0.04),
]
ROLES = ["Directeur-eigenaar", "Wagenparkbeheerder", "Office manager", "Financieel directeur"]

BRANDS = [
    {
        "name": "Voltera Bedrijfswagens",
        "pitch": "Voltera helpt bedrijven hun bestelbussen stap voor stap elektrisch te maken.",
        "qualification_questions": [
            "Hoeveel bedrijfswagens heeft u nu ongeveer rijden?",
            "Staat er de komende twaalf maanden vervanging van voertuigen gepland?",
            "Speelt elektrisch rijden of een zero-emissiezone een rol bij u?",
        ],
        "segment_rules": {"min_vehicles": 3},
        "price_per_address_eur": 11.0,
        "price_per_appointment_eur": 0.0,
        "ams": [
            (
                "Accountmanager Oost",
                "am.oost@voltera.example",
                ["Zwolle", "Deventer", "Apeldoorn", "Hengelo"],
            ),
            ("Accountmanager Midden", "am.midden@voltera.example", []),
        ],
    },
    {
        "name": "Nordwagen Lease",
        "pitch": "Nordwagen Lease regelt zakelijke lease voor het MKB, inclusief onderhoud.",
        "qualification_questions": [
            "Hoe is uw wagenpark nu geregeld: koop of lease?",
            "Loopt er het komende jaar een contract af of moeten er auto's bij?",
        ],
        "segment_rules": {"min_vehicles": 2},
        "price_per_address_eur": 11.0,
        "price_per_appointment_eur": 0.0,
        "ams": [("Accountmanager Lease", "am@nordwagen.example", [])],
    },
]


def _draw_form(rng):
    forms, weights = zip(*FORMS, strict=True)
    return rng.choices(forms, weights)[0]


def seed_companies(session: Session, n: int = 160, seed: int = 7) -> int:
    rng = random.Random(seed)
    today = date.today()
    created = 0
    existing = set(session.scalars(select(Company.kvk_number)))
    used_names: set[str] = set()
    for i in range(n):
        kvk = f"{90000000 + i}"
        if kvk in existing:
            continue
        form = _draw_form(rng)
        suffix = {
            LegalForm.BV: " B.V.",
            LegalForm.NV: " N.V.",
            LegalForm.VOF: " V.O.F.",
            LegalForm.STICHTING: "",
        }.get(form, "")
        city = rng.choice(CITIES)
        base = f"{rng.choice(PREFIX)} {rng.choice(SUFFIX)}"
        if base in used_names:
            base = f"{base} {city}"
        used_names.add(base)
        name = f"{base}{suffix}"
        if form is LegalForm.STICHTING:
            name = "Stichting " + name
        company = Company(
            name=name,
            kvk_number=kvk,
            legal_form=form,
            legal_form_raw=form.value,
            legal_form_source="csv" if form is not LegalForm.UNKNOWN else None,
            legal_form_checked_at=utcnow() - timedelta(days=200)
            if form is not LegalForm.UNKNOWN
            else None,
            address=f"Industrieweg {rng.randint(1, 120)}",
            city=city,
            sector=rng.choice(SECTORS),
            website=f"www.demo{i + 1}.example",
            phone=f"+31850{i:06d}",
            email=f"info@demo{i + 1}.example",
        )
        session.add(company)
        session.flush()
        session.add(
            Contact(company_id=company.id, role=rng.choice(ROLES), name=f"Fictief Contact {i + 1}")
        )
        for v in range(rng.choice([1, 2, 3, 3, 4, 5, 6, 8, 12, 18, 25])):
            lease = rng.random() < 0.55
            end = today + timedelta(days=rng.randint(-60, 900)) if lease else None
            session.add(
                Vehicle(
                    company_id=company.id,
                    make=rng.choice(MAKES),
                    model=rng.choice(["Bestelbus L2", "Bestelbus L3", "Stationwagon", "Pick-up"]),
                    license_plate=f"D{i:03d}{v:02d}",
                    build_year=rng.randint(2014, 2025),
                    ownership="lease" if lease else "koop",
                    lease_company="Demo Lease B.V." if lease else None,
                    contract_end_date=end,
                )
            )
        created += 1
    session.flush()
    return created


def seed_brands(session: Session) -> list[Brand]:
    brands = []
    for spec in BRANDS:
        brand = session.scalar(select(Brand).where(Brand.name == spec["name"]))
        if brand is None:
            data = {k: v for k, v in spec.items() if k != "ams"}
            brand = Brand(**data)
            session.add(brand)
            session.flush()
            for name, email, regions in spec["ams"]:
                session.add(
                    AccountManager(brand_id=brand.id, name=name, email=email, regions=regions)
                )
        brands.append(brand)
    session.flush()
    return brands


def next_call_moment(now: datetime | None = None) -> datetime:
    local = (now or utcnow()).astimezone(TZ)
    day = local.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, 10, 0, tzinfo=TZ)


def run_demo(session: Session, *, companies: int = 160, seed: int = 7) -> str:
    """Seed data and run one simulated round per brand. Returns a Dutch report."""
    lines = []
    seed_objection_library(session)
    lines.append(f"Bedrijven aangemaakt: {seed_companies(session, companies, seed)}")
    brands = seed_brands(session)
    all_companies = list(session.scalars(select(Company)))
    lines.append(
        "Research (mock): " + run_research(session, all_companies, MockResearchProvider()).render()
    )
    moment = next_call_moment()
    kvk = MockKvkClient(session)
    for index, brand in enumerate(brands):
        campaign = create_campaign(session, brand, f"{brand.name} ronde 1 ({moment:%Y})", "A,B")
        report = prepare_round(session, campaign, kvk, now=moment)
        lines.append(f"\n{campaign.name}\n{report.render()}")
        provider = SimulatorProvider(seed=seed + index)
        for attempt_round in range(3):
            when = moment + timedelta(hours=2 * attempt_round, days=attempt_round)
            result = run_round(
                session, campaign, provider, now=when, limit=500, ignore_call_window=True
            )
            lines.append(f"Poging {attempt_round + 1}: {result.render()}")
    # Process part of the QA queue so the outbox and QA costs are visible.
    rng = random.Random(seed)
    pending = list(
        session.scalars(
            select(Appointment)
            .where(Appointment.qa_status == "te controleren")
            .order_by(Appointment.id)
        )
    )
    for appointment in pending[: max(1, len(pending) * 2 // 3)]:
        if rng.random() < 0.9:
            approve_appointment(session, appointment, minutes=rng.choice([3, 4, 5]))
            if rng.random() < 0.5:
                record_result(
                    session,
                    appointment,
                    rng.choice(["plaatsgevonden", "order", "geen order", "no-show"]),
                )
        else:
            reject_appointment(session, appointment, note="Beslisser niet bevestigd")
    session.flush()
    return "\n".join(lines)

"""Numbers for the dashboard: funnel, costs per unit, margin, A/B and QA."""

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from salesagent.models import (
    Appointment,
    Brand,
    CallAttempt,
    Campaign,
    CampaignLead,
    CostEntry,
    LeadStatus,
)
from salesagent.operations.costs import load_cost_model


def _div(a: float, b: float) -> float | None:
    return a / b if b else None


@dataclass
class Stats:
    leads: int = 0
    callable: int = 0
    blocked: int = 0
    addresses_called: int = 0
    attempts: int = 0
    connected: int = 0
    completed: int = 0
    hung_up_after_ai: int = 0
    outcomes: dict = field(default_factory=dict)
    warm: int = 0
    appointments: int = 0
    appointments_approved: int = 0
    appointments_rejected: int = 0
    appointments_pending: int = 0
    no_shows: int = 0
    orders: int = 0
    objections_brand: int = 0
    objections_all: int = 0
    disclosure_missing: int = 0
    costs: dict = field(default_factory=dict)
    cost_total: float = 0.0
    revenue: float = 0.0
    qa_minutes: float = 0.0
    call_minutes: float = 0.0
    variants: dict = field(default_factory=dict)
    benchmark_cost_per_address: float = 11.0

    @property
    def cost_per_address(self):
        return _div(self.cost_total, self.addresses_called)

    @property
    def cost_per_completed(self):
        return _div(self.cost_total, self.completed)

    @property
    def cost_per_warm(self):
        return _div(self.cost_total, self.warm)

    @property
    def cost_per_appointment(self):
        return _div(self.cost_total, self.appointments)

    @property
    def margin(self):
        return _div(self.revenue - self.cost_total, self.revenue)

    @property
    def hangup_rate(self):
        return _div(self.hung_up_after_ai, self.connected)

    @property
    def warm_rate(self):
        return _div(self.warm, self.completed)

    @property
    def appointment_rate(self):
        return _div(self.appointments, self.completed)

    @property
    def appointment_per_address(self):
        return _div(self.appointments, self.addresses_called)

    @property
    def qa_minutes_per_appointment(self):
        return _div(self.qa_minutes, self.appointments_approved + self.appointments_rejected)


def compute_stats(
    session: Session, *, campaign_id: int | None = None, brand_id: int | None = None
) -> Stats:
    stats = Stats(benchmark_cost_per_address=load_cost_model().benchmark_kosten_per_adres)
    campaigns = select(Campaign.id)
    if campaign_id:
        campaigns = campaigns.where(Campaign.id == campaign_id)
    if brand_id:
        campaigns = campaigns.where(Campaign.brand_id == brand_id)
    ids = list(session.scalars(campaigns))
    if not ids:
        return stats

    leads = session.scalars(select(CampaignLead).where(CampaignLead.campaign_id.in_(ids))).all()
    stats.leads = len(leads)
    stats.blocked = sum(1 for lead in leads if lead.status is LeadStatus.BLOCKED)
    stats.callable = stats.leads - stats.blocked
    stats.objections_brand = sum(1 for lead in leads if lead.status is LeadStatus.OBJECTION_BRAND)
    stats.objections_all = sum(1 for lead in leads if lead.status is LeadStatus.OBJECTION_ALL)
    variant_of = {(lead.campaign_id, lead.company_id): lead.variant for lead in leads}

    attempts = session.scalars(
        select(CallAttempt).where(
            CallAttempt.campaign_id.in_(ids),
            CallAttempt.dry_run.is_(False)
            | CallAttempt.outcome.not_in(["dry run", "niet gestart"]),
        )
    ).all()
    attempts = [a for a in attempts if a.outcome not in ("dry run", "niet gestart", None)]
    stats.attempts = len(attempts)
    stats.addresses_called = len({(a.campaign_id, a.company_id) for a in attempts})
    stats.connected = sum(1 for a in attempts if a.connected)
    stats.hung_up_after_ai = sum(1 for a in attempts if a.hung_up_after_disclosure)
    stats.completed = stats.connected - stats.hung_up_after_ai
    stats.outcomes = dict(Counter(a.outcome for a in attempts))
    stats.disclosure_missing = sum(1 for a in attempts if a.ai_disclosure_given is False)
    stats.call_minutes = sum((a.duration_s or 0) for a in attempts) / 60

    appointments = session.scalars(
        select(Appointment).where(Appointment.campaign_id.in_(ids))
    ).all()
    stats.appointments_approved = sum(1 for a in appointments if a.qa_status == "goedgekeurd")
    stats.appointments_rejected = sum(1 for a in appointments if a.qa_status == "afgekeurd")
    stats.appointments_pending = sum(1 for a in appointments if a.qa_status == "te controleren")
    stats.appointments = stats.appointments_approved + stats.appointments_pending
    stats.no_shows = sum(1 for a in appointments if a.result == "no-show")
    stats.orders = sum(1 for a in appointments if a.result == "order")
    stats.warm = stats.outcomes.get("warm", 0) + stats.appointments

    rows = session.execute(
        select(CostEntry.kind, func.sum(CostEntry.amount_eur))
        .where(CostEntry.campaign_id.in_(ids))
        .group_by(CostEntry.kind)
    ).all()
    stats.costs = {kind: float(total or 0) for kind, total in rows}
    # Research is booked per company, not per campaign: attribute it to called companies.
    called_companies = {a.company_id for a in attempts}
    if called_companies:
        research = session.scalar(
            select(func.sum(CostEntry.amount_eur)).where(
                CostEntry.kind == "research", CostEntry.company_id.in_(called_companies)
            )
        )
        if research:
            stats.costs["research"] = float(research)
    stats.cost_total = sum(stats.costs.values())
    qa_rate = load_cost_model().qa_per_minuut
    stats.qa_minutes = stats.costs.get("qa", 0) / qa_rate if qa_rate else 0

    brands = {b.id: b for b in session.scalars(select(Brand))}
    campaign_brand = dict(session.execute(select(Campaign.id, Campaign.brand_id)).all())
    for (cid, _company), _v in {(a.campaign_id, a.company_id): 1 for a in attempts}.items():
        brand = brands.get(campaign_brand.get(cid))
        stats.revenue += brand.price_per_address_eur if brand else 0
    approved_by_campaign = Counter(
        a.campaign_id for a in appointments if a.qa_status == "goedgekeurd"
    )
    for cid, n in approved_by_campaign.items():
        brand = brands.get(campaign_brand.get(cid))
        stats.revenue += n * (brand.price_per_appointment_eur if brand else 0)

    by_variant: dict[str, Counter] = {}
    for a in attempts:
        v = variant_of.get((a.campaign_id, a.company_id), "A")
        c = by_variant.setdefault(v, Counter())
        c["gesprekken"] += 1
        c["bereikt"] += int(a.connected)
        c["opgehangen_na_ai"] += int(bool(a.hung_up_after_disclosure))
        c["warm"] += int(a.outcome in ("warm", "afspraak"))
        c["afspraak"] += int(a.outcome == "afspraak")
    stats.variants = {k: dict(v) for k, v in sorted(by_variant.items())}
    return stats


def attention_list(session: Session) -> list[dict]:
    """What needs a human now."""
    items = []
    for a in session.scalars(
        select(Appointment)
        .where(Appointment.qa_status == "te controleren")
        .order_by(Appointment.starts_at)
    ):
        items.append(
            {
                "soort": "Afspraak controleren",
                "id": a.id,
                "tekst": f"{a.company.name}, {a.starts_at:%d-%m %H:%M}",
                "link": f"afspraken/{a.id}",
            }
        )
    for c in session.scalars(select(CallAttempt).where(CallAttempt.ai_disclosure_given.is_(False))):
        items.append(
            {
                "soort": "AI-melding ontbrak",
                "id": c.id,
                "tekst": f"gesprek {c.id}",
                "link": f"gesprekken/{c.id}",
            }
        )
    n = session.scalar(
        select(func.count(CallAttempt.id)).where(CallAttempt.qa_status == "steekproef")
    )
    if n:
        items.append(
            {"soort": "Steekproef nalopen", "id": 0, "tekst": f"{n} gesprekken", "link": "qa"}
        )
    return items

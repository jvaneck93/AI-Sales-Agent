"""Brands, calling rounds and lead selection.

Per round: select companies by the brand's segment rules, check the legal form
again via the KvK (decision: check per round), run the compliance gate with
brand context, and assign an A/B variant stratified by fleet size.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from salesagent.compliance.gate import callable_legal_forms, may_be_called
from salesagent.compliance.kvk import KvkClient, KvkError
from salesagent.compliance.register import find_valid_opt_in
from salesagent.enrichment.fleet import summarize_fleet
from salesagent.models import (
    Brand,
    Campaign,
    CampaignLead,
    CampaignStatus,
    Company,
    LeadStatus,
    LegalForm,
    utcnow,
)


class MockKvkClient:
    """Sandbox stand-in for the KvK API: confirms the legal form we already have.

    Marked as source "mock-kvk", which the gate refuses for real calls.
    """

    source_name = "mock-kvk"

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_legal_form(self, kvk_number: str) -> LegalForm | None:
        company = self._session.scalar(select(Company).where(Company.kvk_number == kvk_number))
        if company is None or company.legal_form is LegalForm.UNKNOWN:
            return None
        return company.legal_form


def segment_match(company: Company, rules: dict) -> tuple[bool, str]:
    fleet = summarize_fleet(company)
    minimum = rules.get("min_vehicles")
    if minimum and fleet.count < minimum:
        return False, f"Buiten segment: {fleet.count} voertuigen, minimaal {minimum}"
    makes = [m.lower() for m in rules.get("makes", [])]
    if makes and not any(m.lower() in makes for m in fleet.makes):
        return False, "Buiten segment: geen voertuigen van de gekozen merken"
    max_year = rules.get("max_avg_build_year")
    if max_year and fleet.avg_build_year and fleet.avg_build_year > max_year:
        return False, f"Buiten segment: gemiddeld bouwjaar {fleet.avg_build_year:.0f}"
    sectors = [s.lower() for s in rules.get("sectors", [])]
    if sectors and (company.sector or "").lower() not in sectors:
        return False, "Buiten segment: andere sector"
    return True, "Binnen segment"


def recheck_legal_form(company: Company, client: KvkClient, now: datetime) -> None:
    """Overwrite the legal form with the register's current answer.

    Not found means it may be dissolved or changed: unknown, so not called.
    """
    found = client.get_legal_form(company.kvk_number)
    company.legal_form = found or LegalForm.UNKNOWN
    company.legal_form_source = getattr(client, "source_name", "kvk")
    company.legal_form_checked_at = now


def _variant(company: Company, campaign: Campaign, variants: list[str]) -> str:
    fleet = summarize_fleet(company).count
    stratum = "klein" if fleet < 5 else "middel" if fleet < 20 else "groot"
    digest = hashlib.sha256(f"{campaign.id}:{stratum}:{company.kvk_number}".encode()).digest()
    return variants[digest[0] % len(variants)]


@dataclass
class PrepareReport:
    selected: int = 0
    callable: int = 0
    blocked: dict[str, int] = field(default_factory=dict)
    kvk_errors: int = 0
    kvk_lookups: int = 0

    def render(self) -> str:
        lines = [f"Geselecteerd: {self.selected}, belbaar: {self.callable}"]
        if self.kvk_lookups:
            lines.append(f"Rechtsvorm opgevraagd bij KvK: {self.kvk_lookups}")
        if self.kvk_errors:
            lines.append(f"KvK-fouten (niet gebeld): {self.kvk_errors}")
        lines += [f"  {n}x {reason}" for reason, n in sorted(self.blocked.items())]
        return "\n".join(lines)


def create_campaign(session: Session, brand: Brand, name: str, variants: str = "A") -> Campaign:
    campaign = Campaign(brand_id=brand.id, name=name, agent_variant=variants)
    session.add(campaign)
    session.flush()
    return campaign


def prepare_round(
    session: Session,
    campaign: Campaign,
    kvk_client: KvkClient | None,
    *,
    now: datetime | None = None,
) -> PrepareReport:
    now = now or utcnow()
    report = PrepareReport()
    campaign.legal_form_checked_since = now
    brand = campaign.brand
    variants = [v.strip() for v in campaign.agent_variant.split(",") if v.strip()] or ["A"]
    existing = {lead.company_id: lead for lead in campaign.leads}
    callable_forms = callable_legal_forms()
    companies = session.scalars(
        select(Company).options(selectinload(Company.vehicles)).order_by(Company.id)
    )
    # no_autoflush: the gate's lookups must not flush every changed company (slow at 40k+).
    with session.no_autoflush:
        for company in companies:
            in_segment, reason = segment_match(company, brand.segment_rules or {})
            if not in_segment:
                continue
            report.selected += 1
            # A known legal form we never cold-call, without consent, is blocked anyway:
            # no KvK lookup needed (saves API calls on large files).
            needs_check = (
                company.legal_form in callable_forms
                or company.legal_form is LegalForm.UNKNOWN
                or find_valid_opt_in(session, company) is not None
            )
            if kvk_client is not None and needs_check:
                try:
                    recheck_legal_form(company, kvk_client, now)
                    report.kvk_lookups += 1
                except KvkError:
                    report.kvk_errors += 1
            decision = may_be_called(company, session, campaign=campaign, now=now)
            lead = existing.get(company.id)
            if lead is None:
                lead = CampaignLead(company_id=company.id, status=LeadStatus.CALLABLE)
                lead.variant = _variant(company, campaign, variants)
                campaign.leads.append(lead)
            if decision.allowed:
                if lead.status is LeadStatus.BLOCKED:
                    lead.status = LeadStatus.CALLABLE
                lead.reason = decision.reason
                report.callable += 1
            else:
                lead.status = LeadStatus.BLOCKED
                lead.reason = decision.reason
                key = decision.reason.split(" (")[0].split(":")[0]
                report.blocked[key] = report.blocked.get(key, 0) + 1
    campaign.status = CampaignStatus.PREPARED
    session.flush()
    return report

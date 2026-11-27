"""Calling a round (phase 4): which leads are due, and placing the calls.

Every call goes through ``start_call_attempt`` (gate re-check right before the
call, evidence stored) and through ``check_disclosure`` (hard rule 4).
"""

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from salesagent.compliance.gate import CallNotAllowedError, start_call_attempt
from salesagent.config import get_settings
from salesagent.enrichment.research import latest_profile
from salesagent.models import (
    Campaign,
    CampaignLead,
    CampaignStatus,
    LeadStatus,
    utcnow,
)
from salesagent.operations.costs import add_cost, data_cost_booked, load_cost_model
from salesagent.operations.postcall import process_post_call
from salesagent.operations.tools import SessionCallTools
from salesagent.scheduling.calendar import LocalCalendar, pick_account_manager
from salesagent.voice.library import load_library
from salesagent.voice.payload import AgentConfigError, build_variables, elevenlabs_request
from salesagent.voice.providers import RealCallBlockedError, VoiceProvider

DUE_STATUSES = (LeadStatus.CALLABLE, LeadStatus.NOT_REACHED, LeadStatus.CALL_BACK)


def _hm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def within_call_window(now: datetime, settings=None) -> tuple[bool, str]:
    settings = settings or get_settings()
    local = now.astimezone(ZoneInfo(settings.call_timezone))
    if local.weekday() >= 5:
        return False, "Buiten beltijden: weekend"
    if not _hm(settings.call_window_start) <= local.time() < _hm(settings.call_window_end):
        return False, (
            f"Buiten beltijden ({settings.call_window_start} tot {settings.call_window_end})"
        )
    return True, "Binnen beltijden"


def due_leads(session: Session, campaign: Campaign, now: datetime, limit: int) -> list:
    settings = get_settings()
    return list(
        session.scalars(
            select(CampaignLead)
            .where(
                CampaignLead.campaign_id == campaign.id,
                CampaignLead.status.in_(DUE_STATUSES),
                CampaignLead.attempts < settings.max_call_attempts,
                or_(
                    CampaignLead.next_attempt_after.is_(None),
                    CampaignLead.next_attempt_after <= now,
                ),
            )
            .order_by(CampaignLead.attempts, CampaignLead.id)
            .limit(limit)
        )
    )


@dataclass
class RoundReport:
    called: int = 0
    blocked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    outcomes: dict[str, int] = field(default_factory=dict)
    dry_run_requests: list[dict] = field(default_factory=list)
    message: str = ""

    def render(self) -> str:
        lines = [self.message] if self.message else []
        lines.append(f"Gesprekken: {self.called}")
        lines += [f"  {k}: {v}" for k, v in sorted(self.outcomes.items())]
        if self.blocked:
            lines.append(f"Vlak voor het bellen geblokkeerd: {len(self.blocked)}")
            lines += [f"  {b}" for b in self.blocked[:10]]
        lines += [f"Fout: {e}" for e in self.errors[:10]]
        return "\n".join(lines)


def run_round(
    session: Session,
    campaign: Campaign,
    provider: VoiceProvider,
    *,
    now: datetime | None = None,
    limit: int = 50,
    ignore_call_window: bool = False,
) -> RoundReport:
    now = now or utcnow()
    settings = get_settings()
    report = RoundReport()
    ok, reason = within_call_window(now, settings)
    if not ok and not ignore_call_window:
        report.message = reason + ": er is niet gebeld."
        return report
    calendar = LocalCalendar(session)
    library = load_library(session)
    costs = load_cost_model()
    campaign.status = CampaignStatus.RUNNING
    for lead in due_leads(session, campaign, now, limit):
        company = lead.company
        try:
            attempt = start_call_attempt(session, company, now=now, campaign=campaign)
        except CallNotAllowedError as exc:
            lead.status = LeadStatus.BLOCKED
            lead.reason = exc.decision.reason
            report.blocked.append(f"KvK {company.kvk_number}: {exc.decision.reason}")
            continue
        lead.attempts += 1
        lead.last_attempt_at = now
        if not data_cost_booked(session, company.id, campaign.id):
            add_cost(
                session,
                "data",
                costs.data_per_adres,
                company_id=company.id,
                campaign_id=campaign.id,
                detail="data per nagebeld adres",
            )
        am = pick_account_manager(session, campaign.brand, company)
        tools = SessionCallTools(
            session,
            attempt=attempt,
            company=company,
            campaign=campaign,
            calendar=calendar,
            account_manager=am,
            now=now,
        )
        variables = build_variables(
            company=company,
            brand=campaign.brand,
            campaign=campaign,
            attempt=attempt,
            profile=latest_profile(session, company),
            slots=tools.slot_options(),
            account_manager=am.name if am else None,
            agent_name=settings.agent_name,
            company_name=settings.company_name,
            now=now,
        )
        try:
            request = elevenlabs_request(
                variables,
                agent_id=settings.elevenlabs_agent_id,
                phone_number_id=settings.elevenlabs_phone_number_id,
                to_number=company.phone or "",
            )
            attempt.payload = request
            attempt.dry_run = provider.name != "elevenlabs"
            start = provider.place_call(
                request, tools=tools, library=library, variables=variables, now=now
            )
        except (AgentConfigError, RealCallBlockedError) as exc:
            attempt.outcome = "niet gestart"
            attempt.summary = str(exc)
            report.errors.append(str(exc))
            lead.attempts -= 1
            continue
        attempt.external_call_id = start.conversation_id
        report.called += 1
        if start.status == "dry_run":
            attempt.outcome = "dry run"
            attempt.summary = "Droge run: niets verstuurd. Zie payload."
            lead.attempts -= 1
            report.dry_run_requests.append(request)
            outcome = "dry run"
        elif start.post_call is not None:
            outcome = process_post_call(
                session, start.post_call, now=now, calendar=calendar
            ).outcome
        else:
            outcome = "gestart (wacht op webhook)"
        report.outcomes[outcome] = report.outcomes.get(outcome, 0) + 1
    session.flush()
    return report

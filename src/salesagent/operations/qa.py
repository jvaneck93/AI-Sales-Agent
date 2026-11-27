"""Human QA (human-in-the-loop) before the client sees an appointment.

Approve: the briefing goes to the account manager and a confirmation to the
company (mock: into the outbox). Reject: the appointment is cancelled and the
lead becomes a warm lead for the AM to call. QA minutes are booked as cost,
so "human minutes per appointment" can be tracked and must go down.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from salesagent.models import (
    AccountManager,
    Appointment,
    CallAttempt,
    CampaignLead,
    LeadStatus,
    OutboxMessage,
)
from salesagent.operations.costs import add_cost, load_cost_model
from salesagent.scheduling.calendar import Slot


class QaError(Exception):
    pass


def _book_qa_cost(session, minutes, *, company_id, campaign_id, attempt_id, detail):
    rate = load_cost_model().qa_per_minuut
    add_cost(
        session,
        "qa",
        minutes * rate,
        company_id=company_id,
        campaign_id=campaign_id,
        call_attempt_id=attempt_id,
        detail=f"{minutes:g} min {detail}",
    )


def approve_appointment(
    session: Session, appointment: Appointment, *, minutes: float | None = None, note: str = ""
) -> None:
    if appointment.qa_status != "te controleren":
        raise QaError("Deze afspraak is al beoordeeld.")
    minutes = minutes if minutes is not None else load_cost_model().qa_minuten_per_afspraak
    appointment.qa_status = "goedgekeurd"
    appointment.qa_note = note or None
    appointment.qa_minutes = minutes
    attempt = (
        session.get(CallAttempt, appointment.call_attempt_id)
        if appointment.call_attempt_id
        else None
    )
    if attempt is not None:
        attempt.qa_status = "goedgekeurd"
    _book_qa_cost(
        session,
        minutes,
        company_id=appointment.company_id,
        campaign_id=appointment.campaign_id,
        attempt_id=appointment.call_attempt_id,
        detail="controle afspraak",
    )
    am = (
        session.get(AccountManager, appointment.account_manager_id)
        if appointment.account_manager_id
        else None
    )
    company = appointment.company
    when = Slot(0, appointment.starts_at, appointment.starts_at).spoken()
    if am is not None:
        session.add(
            OutboxMessage(
                to=am.email,
                kind="briefing",
                appointment_id=appointment.id,
                subject=f"Nieuwe afspraak: {company.name}, {when}",
                body=appointment.briefing or "",
            )
        )
    if company.email:
        session.add(
            OutboxMessage(
                to=company.email,
                kind="bevestiging",
                appointment_id=appointment.id,
                subject=f"Bevestiging afspraak {when}",
                body=(
                    f"Geachte heer/mevrouw,\n\nHierbij bevestigen we de afspraak op {when} met "
                    f"{appointment.account_manager or 'onze accountmanager'}"
                    f"{' op ' + appointment.location if appointment.location else ''}.\n\n"
                    "Deze afspraak is gemaakt in een telefoongesprek met onze AI-assistent. "
                    "Past het toch niet? Antwoord dan op deze mail. Wilt u niet meer gebeld "
                    "worden, laat het ons weten; we verwerken dat direct.\n\nMet vriendelijke groet"
                ),
            )
        )
    session.flush()


def reject_appointment(
    session: Session, appointment: Appointment, *, note: str, minutes: float | None = None
) -> None:
    if appointment.qa_status != "te controleren":
        raise QaError("Deze afspraak is al beoordeeld.")
    if not note.strip():
        raise QaError("Geef een reden op bij afkeuren.")
    minutes = minutes if minutes is not None else load_cost_model().qa_minuten_per_afspraak
    appointment.qa_status = "afgekeurd"
    appointment.status = "geannuleerd"
    appointment.qa_note = note
    appointment.qa_minutes = minutes
    _book_qa_cost(
        session,
        minutes,
        company_id=appointment.company_id,
        campaign_id=appointment.campaign_id,
        attempt_id=appointment.call_attempt_id,
        detail="controle afspraak (afgekeurd)",
    )
    if appointment.campaign_id:
        lead = session.scalar(
            select(CampaignLead).where(
                CampaignLead.campaign_id == appointment.campaign_id,
                CampaignLead.company_id == appointment.company_id,
            )
        )
        if lead is not None:
            lead.status = LeadStatus.WARM
            lead.reason = f"Afspraak afgekeurd in QA: {note}"
    session.flush()


def review_call(
    session: Session,
    attempt: CallAttempt,
    *,
    ok: bool,
    note: str = "",
    minutes: float | None = None,
) -> None:
    if attempt.qa_status not in ("steekproef", "te controleren"):
        raise QaError("Dit gesprek staat niet in de QA-wachtrij.")
    minutes = minutes if minutes is not None else load_cost_model().qa_minuten_per_steekproef
    attempt.qa_status = "goedgekeurd" if ok else "fout"
    analysis = dict(attempt.analysis or {})
    if note:
        analysis["qa_note"] = note
    attempt.analysis = analysis
    _book_qa_cost(
        session,
        minutes,
        company_id=attempt.company_id,
        campaign_id=attempt.campaign_id,
        attempt_id=attempt.id,
        detail="steekproef gesprek",
    )
    session.flush()


def record_result(session: Session, appointment: Appointment, result: str) -> None:
    """Feedback from the account manager: the basis for the success fee and learning."""
    if result not in ("plaatsgevonden", "no-show", "order", "geen order"):
        raise QaError("Onbekende uitkomst.")
    appointment.result = result
    if result == "no-show":
        appointment.status = "no-show"
    elif result in ("plaatsgevonden", "order", "geen order"):
        appointment.status = "plaatsgevonden"
    session.flush()

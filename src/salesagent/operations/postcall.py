"""Post-call processing (phase 5).

Input is the ElevenLabs ``post_call_transcription`` payload (the simulator
produces the same shape). Steps: find the attempt, store transcript and
duration, determine the outcome, handle objections (safety net: an opt-out in
the transcript without a registered objection blocks everything), book or
confirm the appointment, update lead and company, book costs, queue QA.
"""

import hashlib
import hmac
import time
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from salesagent.compliance.register import register_objection
from salesagent.config import get_settings
from salesagent.models import (
    Appointment,
    CallAttempt,
    Campaign,
    CampaignLead,
    Company,
    CompanyStatus,
    Consent,
    ConsentType,
    LeadStatus,
    utcnow,
)
from salesagent.operations.briefing import build_briefing
from salesagent.operations.costs import add_cost, load_cost_model
from salesagent.voice import nlu
from salesagent.voice.library import OPT_OUT, load_library

OUTCOMES = (
    "afspraak",
    "warm",
    "terugbellen",
    "geen interesse",
    "verzet",
    "niet bereikt",
    "verkeerde persoon",
    "opgehangen",
)
LEAD_STATUS = {
    "afspraak": LeadStatus.APPOINTMENT,
    "warm": LeadStatus.WARM,
    "terugbellen": LeadStatus.CALL_BACK,
    "geen interesse": LeadStatus.NO_INTEREST,
    "niet bereikt": LeadStatus.NOT_REACHED,
    "opgehangen": LeadStatus.DONE,
    "verkeerde persoon": LeadStatus.CALL_BACK,
}


class WebhookError(Exception):
    """Webhook rejected. Message is Dutch and contains no personal data."""


def verify_signature(
    raw_body: bytes,
    header: str | None,
    secret: str,
    *,
    tolerance_s: int = 1800,
    now: float | None = None,
) -> None:
    """ElevenLabs-Signature: ``t=<unix>,v0=<hex hmac_sha256(secret, f"{t}.{body}")>``."""
    if not secret:
        raise WebhookError("Geen ELEVENLABS_WEBHOOK_SECRET ingesteld")
    if not header:
        raise WebhookError("Handtekening ontbreekt")
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    timestamp, signature = parts.get("t"), parts.get("v0")
    if not timestamp or not signature or not timestamp.isdigit():
        raise WebhookError("Handtekening heeft een onbekend formaat")
    if abs((now or time.time()) - int(timestamp)) > tolerance_s:
        raise WebhookError("Handtekening is verlopen")
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookError("Handtekening klopt niet")


def sign(raw_body: bytes, secret: str, timestamp: int | None = None) -> str:
    """Test helper: produce a valid header."""
    timestamp = timestamp or int(time.time())
    digest = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v0={digest}"


def _turns(data: dict) -> list[dict]:
    turns = []
    for t in data.get("transcript") or []:
        role = "agent" if t.get("role") == "agent" else "user"
        turns.append(
            {"role": role, "message": t.get("message") or "", "t": t.get("time_in_call_secs", 0)}
        )
    return turns


def _structured(data: dict) -> dict:
    analysis = data.get("analysis") or {}
    if analysis.get("structured_result"):
        return dict(analysis["structured_result"])
    collected = analysis.get("data_collection_results") or {}
    result = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in collected.items()}
    if analysis.get("transcript_summary"):
        result["summary"] = analysis["transcript_summary"]
    return result


def classify_transcript(turns: list[dict], library: list[dict], brand: str, now: datetime) -> dict:
    """Fallback when the platform sent no structured result: rules on the transcript."""
    user = [t["message"] for t in turns if t["role"] == "user"]
    agent = [t["message"].lower() for t in turns if t["role"] == "agent"]
    result: dict = {"objections": [], "notes": []}
    if not user:
        result["outcome"] = "niet bereikt" if not turns else "opgehangen"
        return result
    categories = [c for c in (nlu.detect_objection(u, library) for u in user) if c]
    result["objections"] = categories
    if OPT_OUT in categories:
        index = next(i for i, u in enumerate(user) if nlu.detect_objection(u, library) == OPT_OUT)
        answer = user[index + 1] if index + 1 < len(user) else ""
        result.update(outcome="verzet", opt_out_scope=nlu.opt_out_scope(answer, brand))
    elif any("genoteerd:" in a and "bevestiging" in a for a in agent):
        result["outcome"] = "afspraak"
    elif "geen_beslisser" in categories:
        result.update(outcome="verkeerde persoon")
    elif "geen_tijd" in categories:
        moment = nlu.parse_callback(" ".join(user), now) or now + timedelta(days=7)
        result.update(outcome="terugbellen", callback_at=moment.isoformat())
    elif any(nlu.is_yes(u) for u in user[1:]) and len(user) >= 3:
        result["outcome"] = "warm"
    else:
        result["outcome"] = "geen interesse"
    return result


def _opt_out_consent(session: Session, attempt: CallAttempt) -> Consent | None:
    return session.scalar(
        select(Consent).where(
            Consent.type == ConsentType.OBJECTION, Consent.source == f"AI-gesprek {attempt.id}"
        )
    )


def _summary(result: dict, duration: float | None) -> str:
    parts = [f"Uitkomst: {result.get('outcome')}"]
    if duration:
        parts.append(f"duur {duration:.0f} s")
    if result.get("objections"):
        parts.append("bezwaren: " + ", ".join(result["objections"]))
    if result.get("opt_out_scope"):
        parts.append(f"verzet voor: {result['opt_out_scope']}")
    if result.get("callback_at"):
        parts.append(f"terugbellen: {result['callback_at'][:16].replace('T', ' ')}")
    return "; ".join(parts)


def _in_qa_sample(attempt_id: int, rate: float) -> bool:
    return int(hashlib.sha256(str(attempt_id).encode()).hexdigest(), 16) % 1000 < rate * 1000


def process_post_call(
    session: Session, payload: dict, *, now: datetime | None = None, calendar=None
) -> CallAttempt:
    if payload.get("type") != "post_call_transcription":
        raise WebhookError("Onbekend webhooktype")
    now = now or utcnow()
    data = payload.get("data") or {}
    variables = (data.get("conversation_initiation_client_data") or {}).get(
        "dynamic_variables"
    ) or {}
    attempt = None
    if variables.get("attempt_id"):
        attempt = session.get(CallAttempt, int(variables["attempt_id"]))
    if attempt is None and data.get("conversation_id"):
        attempt = session.scalar(
            select(CallAttempt).where(CallAttempt.external_call_id == data["conversation_id"])
        )
    if attempt is None:
        raise WebhookError("Onbekend gesprek")
    if attempt.outcome is not None:
        return attempt  # webhooks can be delivered twice

    company = session.get(Company, attempt.company_id)
    campaign = session.get(Campaign, attempt.campaign_id) if attempt.campaign_id else None
    brand_name = campaign.brand.name if campaign else ""
    library = load_library(session)
    costs = load_cost_model()
    metadata = data.get("metadata") or {}
    turns = _turns(data)

    attempt.external_call_id = attempt.external_call_id or data.get("conversation_id")
    attempt.transcript = turns
    attempt.duration_s = metadata.get("call_duration_secs")
    attempt.connected = bool(metadata.get("connected", any(t["role"] == "user" for t in turns)))

    result = _structured(data)
    if not result.get("outcome") or result["outcome"] not in OUTCOMES:
        fallback = classify_transcript(turns, library, brand_name, now)
        result = {**fallback, **{k: v for k, v in result.items() if v not in (None, "")}}
        result["outcome"] = fallback["outcome"]
        result["classified_by"] = "regels"

    first_agent = next((t["message"] for t in turns if t["role"] == "agent"), "")
    attempt.ai_disclosure_given = ("ai-assistent" in first_agent.lower()) if turns else None
    attempt.hung_up_after_disclosure = bool(result.get("hung_up_after_disclosure")) or (
        result.get("outcome") == "opgehangen"
    )
    if turns and not attempt.ai_disclosure_given:
        result.setdefault("notes", []).append("LET OP: AI-melding ontbrak in de eerste zin")
        attempt.qa_status = "te controleren"

    # Objections: register what the agent did not, and when in doubt block everything.
    user_said_opt_out = any(
        nlu.detect_objection(t["message"], library) == OPT_OUT for t in turns if t["role"] == "user"
    )
    if (result.get("outcome") == "verzet" or user_said_opt_out) and not _opt_out_consent(
        session, attempt
    ):
        scope = (
            result.get("opt_out_scope")
            if result.get("opt_out_scope") in ("merk", "alles")
            else "alles"
        )
        register_objection(
            session,
            source=f"AI-gesprek {attempt.id}",
            company=company,
            brand=campaign.brand if (scope == "merk" and campaign) else None,
            evidence=f"nabewerking gespreks-ID {attempt.external_call_id}; reikwijdte: {scope}",
        )
        result["outcome"], result["opt_out_scope"] = "verzet", scope

    outcome = result["outcome"]
    attempt.outcome = outcome
    attempt.analysis = result
    attempt.summary = _summary(result, attempt.duration_s)
    if result.get("callback_at"):
        attempt.callback_at = datetime.fromisoformat(result["callback_at"])

    # Lead and company status.
    lead = None
    if campaign:
        lead = session.scalar(
            select(CampaignLead).where(
                CampaignLead.campaign_id == campaign.id, CampaignLead.company_id == company.id
            )
        )
    settings = get_settings()
    if lead is not None:
        if outcome == "verzet":
            lead.status = (
                LeadStatus.OBJECTION_BRAND
                if result.get("opt_out_scope") == "merk"
                else LeadStatus.OBJECTION_ALL
            )
        else:
            lead.status = LEAD_STATUS.get(outcome, LeadStatus.CALLED)
        if outcome == "opgehangen":
            lead.reason = "Opgehangen na de AI-melding: niet opnieuw in deze ronde"
        if outcome == "niet bereikt":
            lead.next_attempt_after = now + timedelta(hours=settings.min_hours_between_attempts)
            if lead.attempts >= settings.max_call_attempts:
                lead.status = LeadStatus.DONE
                lead.reason = "Maximum aantal pogingen bereikt"
        elif attempt.callback_at:
            lead.next_attempt_after = attempt.callback_at
    if outcome == "verzet" and result.get("opt_out_scope") != "merk":
        company.status = CompanyStatus.OBJECTION
    elif outcome == "afspraak":
        company.status = CompanyStatus.APPOINTMENT
    elif outcome == "terugbellen":
        company.status = CompanyStatus.CALL_BACK
    elif outcome == "geen interesse" and company.status is not CompanyStatus.APPOINTMENT:
        company.status = CompanyStatus.NO_INTEREST

    # Appointment: booked live by the agent, or book now from the chosen slot.
    appointment = session.scalar(
        select(Appointment).where(Appointment.call_attempt_id == attempt.id)
    )
    if (
        outcome == "afspraak"
        and appointment is None
        and result.get("appointment_slot")
        and calendar is not None
        and campaign is not None
    ):
        from salesagent.operations.tools import SessionCallTools

        tools = SessionCallTools(
            session,
            attempt=attempt,
            company=company,
            campaign=campaign,
            calendar=calendar,
            account_manager=None,
            now=now,
        )
        if tools.book(result["appointment_slot"]):
            appointment = tools.booked
        else:
            result.setdefault("notes", []).append("Afspraak kon niet worden ingepland")
            attempt.outcome = outcome = "warm"
            if lead is not None:
                lead.status = LeadStatus.WARM
    if appointment is not None:
        appointment.briefing = build_briefing(session, appointment, attempt)
        appointment.qa_status = "te controleren"
        attempt.qa_status = "te controleren"
    elif (
        attempt.connected
        and attempt.qa_status is None
        and _in_qa_sample(attempt.id, costs.qa_steekproef)
    ):
        attempt.qa_status = "steekproef"

    minutes = (attempt.duration_s or 0) / 60
    cost = metadata.get("cost_eur")
    if cost is None:
        cost = minutes * costs.bellen_per_minuut
    attempt.cost_eur = round(float(cost), 4)
    add_cost(
        session,
        "bellen",
        attempt.cost_eur,
        company_id=company.id,
        campaign_id=attempt.campaign_id,
        call_attempt_id=attempt.id,
        detail=f"{minutes:.1f} min",
    )
    session.flush()
    return attempt

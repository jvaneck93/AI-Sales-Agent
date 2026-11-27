"""Endpoints for the live voice agent: post-call webhook and in-call tools.

Configure in ElevenLabs:
- post-call webhook -> POST /webhooks/elevenlabs (HMAC, ELEVENLABS_WEBHOOK_SECRET)
- server tools (header X-Tool-Secret = TOOL_SECRET), body includes attempt_id
  from the dynamic variables:
    POST /api/tools/afspraakmomenten   {attempt_id}
    POST /api/tools/afspraak-inplannen {attempt_id, slot_key}
    POST /api/tools/verzet-registreren {attempt_id, scope: "merk"|"alles"}
"""

import hmac
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from salesagent.api.common import get_session
from salesagent.config import get_settings
from salesagent.models import AccountManager, CallAttempt, Campaign, Company, utcnow
from salesagent.operations.postcall import WebhookError, process_post_call, verify_signature
from salesagent.operations.tools import SessionCallTools
from salesagent.scheduling.calendar import LocalCalendar, pick_account_manager

router = APIRouter()


def _check_tool_secret(x_tool_secret: str | None) -> None:
    secret = get_settings().tool_secret
    if not secret or not x_tool_secret or not hmac.compare_digest(secret, x_tool_secret):
        raise HTTPException(status_code=401, detail="Ongeldige of ontbrekende tool-sleutel")


def _tools(session: Session, attempt_id: int) -> SessionCallTools:
    attempt = session.get(CallAttempt, attempt_id)
    if attempt is None or attempt.campaign_id is None or attempt.outcome is not None:
        raise HTTPException(status_code=404, detail="Geen lopend gesprek met dit ID")
    company = session.get(Company, attempt.company_id)
    campaign = session.get(Campaign, attempt.campaign_id)
    am: AccountManager | None = pick_account_manager(session, campaign.brand, company)
    return SessionCallTools(
        session,
        attempt=attempt,
        company=company,
        campaign=campaign,
        calendar=LocalCalendar(session),
        account_manager=am,
        now=utcnow(),
    )


class ToolRequest(BaseModel):
    attempt_id: int
    slot_key: str | None = None
    scope: str | None = None


@router.post("/api/tools/afspraakmomenten")
def tool_slots(
    body: ToolRequest,
    session: Session = Depends(get_session),
    x_tool_secret: str | None = Header(None),
):
    _check_tool_secret(x_tool_secret)
    tools = _tools(session, body.attempt_id)
    return {"momenten": [{"sleutel": k, "tekst": t} for k, t in tools.slot_options()]}


@router.post("/api/tools/afspraak-inplannen")
def tool_book(
    body: ToolRequest,
    session: Session = Depends(get_session),
    x_tool_secret: str | None = Header(None),
):
    _check_tool_secret(x_tool_secret)
    if not body.slot_key:
        raise HTTPException(status_code=422, detail="slot_key ontbreekt")
    tools = _tools(session, body.attempt_id)
    ok = tools.book(body.slot_key)
    session.commit()
    return {
        "gelukt": ok,
        "melding": "Afspraak staat in de agenda"
        if ok
        else "Dit moment is niet meer vrij; kies een ander moment",
    }


@router.post("/api/tools/verzet-registreren")
def tool_opt_out(
    body: ToolRequest,
    session: Session = Depends(get_session),
    x_tool_secret: str | None = Header(None),
):
    _check_tool_secret(x_tool_secret)
    scope = body.scope if body.scope in ("merk", "alles") else "alles"  # when in doubt: all
    tools = _tools(session, body.attempt_id)
    tools.register_opt_out(scope)
    session.commit()
    return {"vastgelegd": True, "reikwijdte": scope}


@router.post("/webhooks/elevenlabs")
async def elevenlabs_webhook(
    request: Request,
    session: Session = Depends(get_session),
    elevenlabs_signature: str | None = Header(None),
):
    raw = await request.body()
    try:
        verify_signature(raw, elevenlabs_signature, get_settings().elevenlabs_webhook_secret)
        payload = json.loads(raw)
        if payload.get("type") != "post_call_transcription":
            return {"genegeerd": payload.get("type")}
        attempt = process_post_call(session, payload, calendar=LocalCalendar(session))
    except WebhookError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Geen geldige JSON") from exc
    session.commit()
    return {"verwerkt": True, "uitkomst": attempt.outcome}

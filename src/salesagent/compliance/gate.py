"""Compliance gate: may this company be called, and why (hard rules 1 to 3)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session

from salesagent.compliance.register import find_objection, find_valid_opt_in
from salesagent.config import get_settings
from salesagent.models import (
    Brand,
    CallAttempt,
    CallBasis,
    Campaign,
    Company,
    LegalForm,
    utcnow,
)

# Hard rule 1: only entities with legal personality may ever be cold-called.
LEGAL_PERSONALITY_FORMS = frozenset(
    {
        LegalForm.BV,
        LegalForm.NV,
        LegalForm.STICHTING,
        LegalForm.VERENIGING,
        LegalForm.COOPERATIE,
    }
)


def callable_legal_forms() -> frozenset[LegalForm]:
    """Forms we cold-call. Decision 27-11-2026: bv and nv only (setting
    CALLABLE_LEGAL_FORMS). Can never widen beyond legal personality."""
    configured = {f for f in LegalForm if f.value in get_settings().callable_forms}
    return frozenset(configured & LEGAL_PERSONALITY_FORMS)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@dataclass(frozen=True)
class CallDecision:
    allowed: bool
    reason: str  # Dutch, shown to users and stored as evidence
    basis: CallBasis | None = None


class CallNotAllowedError(Exception):
    """Raised when a call attempt is started for a company that may not be called."""

    def __init__(self, decision: CallDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


def may_be_called(
    company: Company,
    session: Session | None = None,
    *,
    brand: Brand | None = None,
    campaign: Campaign | None = None,
    now: datetime | None = None,
) -> CallDecision:
    """Decide whether a company may be called, with the reason.

    Order matters: an objection always wins, also over an opt-in. With a brand,
    only objections for that brand or for all brands count; without one, every
    objection blocks (conservative). With a campaign, the legal form must have
    been checked in this round, and call fatigue across brands is enforced.
    """
    session = session or object_session(company)
    if session is None:
        raise RuntimeError("Company is not attached to a session")
    if campaign is not None and brand is None:
        brand = campaign.brand

    objection = find_objection(session, company, brand=brand)
    if objection is not None:
        scope = "alle merken" if objection.brand_id is None else "dit merk"
        return CallDecision(
            False,
            f"Verzet ({scope}) geregistreerd op {objection.date.isoformat()} "
            f"(bron: {objection.source})",
        )

    decision = _basis(session, company)
    # Round checks only matter for a company that could be called at all; a legal-form
    # refusal keeps its own, more precise reason.
    if decision.allowed and campaign is not None:
        blocked = _round_checks(session, company, campaign, now or utcnow())
        if blocked is not None:
            return blocked
    return decision


def _basis(session: Session, company: Company) -> CallDecision:
    """Legal form or recorded consent: the basis for calling (hard rules 1 and 3)."""
    if company.legal_form in callable_legal_forms():
        checked = (
            company.legal_form_checked_at.date().isoformat()
            if company.legal_form_checked_at
            else "onbekend"
        )
        return CallDecision(
            True,
            f"Rechtsvorm {company.legal_form.value} heeft rechtspersoonlijkheid "
            f"(bron: {company.legal_form_source or 'onbekend'}, vastgesteld: {checked})",
            CallBasis.LEGAL_FORM,
        )

    opt_in = find_valid_opt_in(session, company)
    if opt_in is not None:
        return CallDecision(
            True,
            f"Toestemming vastgelegd op {opt_in.date.isoformat()} "
            f"(bron: {opt_in.source}, bewijs: {opt_in.evidence})",
            CallBasis.CONSENT,
        )

    if company.legal_form is LegalForm.UNKNOWN:
        return CallDecision(False, "Rechtsvorm onbekend: niet bellen")
    if company.legal_form in LEGAL_PERSONALITY_FORMS:
        return CallDecision(
            False,
            f"Rechtsvorm {company.legal_form.value} valt buiten het besluit om alleen "
            "bv en nv te bellen",
        )
    return CallDecision(
        False,
        f"Rechtsvorm {company.legal_form.value} heeft geen rechtspersoonlijkheid "
        "en er is geen aantoonbare toestemming",
    )


def _round_checks(
    session: Session, company: Company, campaign: Campaign, now: datetime
) -> CallDecision | None:
    settings = get_settings()
    since = campaign.legal_form_checked_since
    checked = company.legal_form_checked_at
    if since is not None and (checked is None or _aware(checked) < _aware(since)):
        return CallDecision(False, "Rechtsvorm is in deze belronde nog niet gecontroleerd")
    if not settings.dry_run and company.legal_form_source != "kvk":
        return CallDecision(
            False, "Voor echte gesprekken moet de rechtsvorm via de KvK zijn gecontroleerd"
        )
    contacts = session.scalar(
        select(func.count(CallAttempt.id)).where(
            CallAttempt.company_id == company.id,
            CallAttempt.connected.is_(True),
            CallAttempt.started_at >= now - timedelta(days=90),
        )
    )
    if contacts >= settings.max_contacts_per_quarter:
        return CallDecision(
            False,
            f"Belmoeheid: al {contacts} gesprekken in 90 dagen (over alle merken), "
            f"maximum {settings.max_contacts_per_quarter}",
        )
    return None


# Dutch alias: this is the name used in CLAUDE.md.
mag_gebeld_worden = may_be_called


def start_call_attempt(
    session: Session,
    company: Company,
    *,
    now: datetime | None = None,
    campaign: Campaign | None = None,
) -> CallAttempt:
    """Re-check the gate right before a call and record the attempt with its basis.

    Every call must go through this function. It checks the objection register
    at this moment (hard rule 2) and stores why the call was allowed (hard rule 3).
    """
    session.flush()
    decision = may_be_called(company, session, campaign=campaign, now=now)
    if not decision.allowed or decision.basis is None:
        raise CallNotAllowedError(decision)
    attempt = CallAttempt(
        company_id=company.id,
        campaign_id=campaign.id if campaign else None,
        brand_id=campaign.brand_id if campaign else None,
        started_at=now or utcnow(),
        basis=decision.basis,
        basis_detail=decision.reason,
    )
    session.add(attempt)
    session.flush()
    return attempt


def complete_call_attempt(
    session: Session, attempt: CallAttempt, *, outcome: str, summary: str | None = None
) -> None:
    attempt.outcome = outcome
    attempt.summary = summary
    session.flush()

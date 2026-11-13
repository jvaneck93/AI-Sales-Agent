"""Compliance gate: may this company be called, and why (hard rules 1 to 3)."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session, object_session

from salesagent.compliance.register import find_objection, find_valid_opt_in
from salesagent.models import CallAttempt, CallBasis, Company, LegalForm, utcnow

# Hard rule 1: only entities with legal personality may be cold-called.
CALLABLE_LEGAL_FORMS = frozenset(
    {
        LegalForm.BV,
        LegalForm.NV,
        LegalForm.STICHTING,
        LegalForm.VERENIGING,
        LegalForm.COOPERATIE,
    }
)


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


def may_be_called(company: Company, session: Session | None = None) -> CallDecision:
    """Decide whether a company may be called, with the reason.

    Order matters: an objection always wins, also over an opt-in.
    """
    session = session or object_session(company)
    if session is None:
        raise RuntimeError("Company is not attached to a session")

    objection = find_objection(session, company)
    if objection is not None:
        return CallDecision(
            False,
            f"Verzet geregistreerd op {objection.date.isoformat()} (bron: {objection.source})",
        )

    if company.legal_form in CALLABLE_LEGAL_FORMS:
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
    return CallDecision(
        False,
        f"Rechtsvorm {company.legal_form.value} heeft geen rechtspersoonlijkheid "
        "en er is geen aantoonbare toestemming",
    )


# Dutch alias: this is the name used in CLAUDE.md.
mag_gebeld_worden = may_be_called


def start_call_attempt(
    session: Session, company: Company, *, now: datetime | None = None
) -> CallAttempt:
    """Re-check the gate right before a call and record the attempt with its basis.

    Every call must go through this function. It checks the objection register
    at this moment (hard rule 2) and stores why the call was allowed (hard rule 3).
    """
    session.flush()
    decision = may_be_called(company, session)
    if not decision.allowed or decision.basis is None:
        raise CallNotAllowedError(decision)
    attempt = CallAttempt(
        company_id=company.id,
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

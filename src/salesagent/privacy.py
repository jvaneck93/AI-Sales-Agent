"""Deletion and retention of personal data (hard rule 7).

An objection is never deleted: it must keep blocking calls (hard rule 2). It
holds only a KvK number and/or phone number, source and date.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from salesagent.models import (
    CallAttempt,
    Company,
    CompanyStatus,
    Consent,
    ConsentType,
    Contact,
    utcnow,
)

CLOSED_STATUSES = (CompanyStatus.NO_INTEREST, CompanyStatus.OBJECTION, CompanyStatus.BLOCKED)


@dataclass
class PurgeResult:
    transcripts_cleared: int = 0
    contacts_deleted: int = 0

    def render(self) -> str:
        return (
            f"Gespreksverslagen en transcriptverwijzingen gewist: {self.transcripts_cleared}\n"
            f"Contactpersonen verwijderd: {self.contacts_deleted}"
        )


def delete_company(session: Session, company: Company) -> None:
    """Delete a company with its contacts, vehicles, profiles, call attempts,
    appointments and opt-ins. Objections stay in the register."""
    session.execute(
        delete(Consent).where(
            Consent.type == ConsentType.OPT_IN, Consent.kvk_number == company.kvk_number
        )
    )
    session.delete(company)
    session.flush()


def delete_contact(session: Session, contact: Contact) -> None:
    session.delete(contact)
    session.flush()


def purge_expired(
    session: Session,
    *,
    transcript_days: int,
    contact_days: int,
    now: datetime | None = None,
) -> PurgeResult:
    """Apply the retention periods.

    - Summaries and transcript references of call attempts older than
      ``transcript_days`` are cleared. Time, basis and outcome stay as evidence.
    - Contact persons of companies in a closed status (no interest, objection,
      blocked) that have not changed for ``contact_days`` are deleted.
    """
    now = now or utcnow()
    result = PurgeResult()

    cutoff = now - timedelta(days=transcript_days)
    attempts = session.scalars(
        select(CallAttempt).where(
            CallAttempt.started_at < cutoff,
            CallAttempt.summary.is_not(None) | CallAttempt.transcript_ref.is_not(None),
        )
    )
    for attempt in attempts:
        attempt.summary = None
        attempt.transcript_ref = None
        result.transcripts_cleared += 1

    cutoff = now - timedelta(days=contact_days)
    contacts = session.scalars(
        select(Contact)
        .join(Company)
        .where(Company.status.in_(CLOSED_STATUSES), Company.updated_at < cutoff)
    ).all()
    for contact in contacts:
        session.delete(contact)
        result.contacts_deleted += 1

    session.flush()
    return result

"""Consent (opt-in) and objection (verzet) register."""

from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from salesagent.models import Brand, Company, CompanyStatus, Consent, ConsentType
from salesagent.normalize import normalize_kvk, normalize_phone


class ConsentError(ValueError):
    """Consent or objection could not be registered. Message is in Dutch."""


def register_opt_in(
    session: Session, company: Company, *, source: str, consent_date: date, evidence: str
) -> Consent:
    """Record demonstrable consent. Source, date and evidence are all mandatory."""
    if not source.strip() or not evidence.strip():
        raise ConsentError("Toestemming vereist een bron en bewijs.")
    if consent_date > date.today():
        raise ConsentError("De datum van toestemming kan niet in de toekomst liggen.")
    consent = Consent(
        company_id=company.id,
        kvk_number=company.kvk_number,
        phone=company.phone,
        type=ConsentType.OPT_IN,
        source=source.strip(),
        date=consent_date,
        evidence=evidence.strip(),
    )
    session.add(consent)
    session.flush()
    return consent


def register_objection(
    session: Session,
    *,
    source: str,
    company: Company | None = None,
    kvk_number: str | None = None,
    phone: str | None = None,
    objection_date: date | None = None,
    evidence: str = "",
    brand: Brand | None = None,
) -> Consent:
    """Record an objection. Takes effect immediately and permanently (hard rule 2).

    ``brand=None`` means the objection applies to every brand. Only pass a brand
    when the person clearly said it is about that brand only.
    """
    if not source.strip():
        raise ConsentError("Verzet vereist een bron.")
    kvk = normalize_kvk(kvk_number) if kvk_number else None
    normalized_phone = normalize_phone(phone) if phone else None
    if company is not None:
        kvk = kvk or company.kvk_number
        normalized_phone = normalized_phone or company.phone
    if kvk is None and normalized_phone is None:
        raise ConsentError("Verzet vereist een geldig KvK-nummer of telefoonnummer.")

    consent = Consent(
        company_id=company.id if company is not None else None,
        kvk_number=kvk,
        phone=normalized_phone,
        type=ConsentType.OBJECTION,
        source=source.strip(),
        date=objection_date or date.today(),
        evidence=evidence.strip(),
        brand_id=brand.id if brand is not None else None,
    )
    session.add(consent)
    if brand is not None:
        session.flush()
        return consent

    matches = []
    if kvk:
        matches.append(Company.kvk_number == kvk)
    if normalized_phone:
        matches.append(Company.phone == normalized_phone)
    for match in session.scalars(select(Company).where(or_(*matches))):
        match.status = CompanyStatus.OBJECTION
    session.flush()
    return consent


def find_objection(
    session: Session, company: Company, *, brand: Brand | None = None
) -> Consent | None:
    """Find an objection for this company by record, KvK number or phone number.

    With a brand: objections for all brands or for this brand. Without: any.
    """
    matches = [Consent.kvk_number == company.kvk_number]
    if company.id is not None:
        matches.append(Consent.company_id == company.id)
    if company.phone:
        matches.append(Consent.phone == company.phone)
    query = select(Consent).where(Consent.type == ConsentType.OBJECTION, or_(*matches))
    if brand is not None:
        query = query.where(or_(Consent.brand_id.is_(None), Consent.brand_id == brand.id))
    return session.scalar(query.order_by(Consent.brand_id.is_not(None), Consent.date).limit(1))


def find_valid_opt_in(session: Session, company: Company) -> Consent | None:
    """Find the most recent opt-in with source, date and evidence."""
    candidates = session.scalars(
        select(Consent)
        .where(Consent.type == ConsentType.OPT_IN, Consent.kvk_number == company.kvk_number)
        .order_by(Consent.date.desc())
    )
    for consent in candidates:
        if consent.source.strip() and consent.evidence.strip() and consent.date <= date.today():
            return consent
    return None

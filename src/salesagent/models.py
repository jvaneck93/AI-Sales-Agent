"""Data model.

Enum *values* are Dutch because they are stored and shown to users;
identifiers are English.
"""

from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from salesagent.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Stores every moment as UTC and returns it timezone-aware (UTC).

    SQLite drops time zones silently; without this an Amsterdam 10:00 and a
    UTC 10:00 would be stored the same. Naive input is taken as UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class LegalForm(StrEnum):
    BV = "bv"
    NV = "nv"
    STICHTING = "stichting"
    VERENIGING = "vereniging"
    COOPERATIE = "cooperatie"
    EENMANSZAAK = "eenmanszaak"
    VOF = "vof"
    CV = "cv"
    MAATSCHAP = "maatschap"
    UNKNOWN = "onbekend"


class CompanyStatus(StrEnum):
    NEW = "nieuw"
    BLOCKED = "geblokkeerd"
    RESEARCH_DONE = "research klaar"
    CALLABLE = "belbaar"
    IN_PROGRESS = "in behandeling"
    CALL_BACK = "terugbellen"
    APPOINTMENT = "afspraak"
    NO_INTEREST = "geen interesse"
    OBJECTION = "verzet"


class ConsentType(StrEnum):
    OPT_IN = "opt-in"
    OBJECTION = "verzet"


class CallBasis(StrEnum):
    LEGAL_FORM = "rechtsvorm"
    CONSENT = "toestemming"


def _enum(enum_cls: type[StrEnum]) -> SAEnum:
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=32,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Company(Base):
    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    kvk_number: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    legal_form: Mapped[LegalForm] = mapped_column(_enum(LegalForm), default=LegalForm.UNKNOWN)
    legal_form_raw: Mapped[str | None] = mapped_column(String(255))
    # Where the legal form came from ("csv" or "kvk") and when: evidence for hard rule 3.
    legal_form_source: Mapped[str | None] = mapped_column(String(32))
    legal_form_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    address: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(255))
    sector: Mapped[str | None] = mapped_column(String(255))
    website: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32), index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[CompanyStatus] = mapped_column(_enum(CompanyStatus), default=CompanyStatus.NEW)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    vehicles: Mapped[list["Vehicle"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class Contact(Base):
    __tablename__ = "contact"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"))
    role: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))

    company: Mapped[Company] = relationship(back_populates="contacts")


class Vehicle(Base):
    __tablename__ = "vehicle"
    __table_args__ = (UniqueConstraint("license_plate", name="uq_vehicle_license_plate"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"))
    make: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    license_plate: Mapped[str | None] = mapped_column(String(16))
    build_year: Mapped[int | None]
    ownership: Mapped[str | None] = mapped_column(String(16))  # "koop" or "lease"
    lease_company: Mapped[str | None] = mapped_column(String(255))
    contract_end_date: Mapped[date | None] = mapped_column(Date)

    company: Mapped[Company] = relationship(back_populates="vehicles")


class Consent(Base):
    """Opt-in or objection (verzet).

    KvK number and phone are stored on the record itself, so an objection keeps
    blocking even after the company record is deleted and imported again.
    """

    __tablename__ = "consent"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id", ondelete="SET NULL"))
    kvk_number: Mapped[str | None] = mapped_column(String(8), index=True)
    phone: Mapped[str | None] = mapped_column(String(32), index=True)
    type: Mapped[ConsentType] = mapped_column(_enum(ConsentType))
    source: Mapped[str] = mapped_column(String(255))
    date: Mapped[date] = mapped_column(Date)
    evidence: Mapped[str] = mapped_column(Text, default="")
    # Objection scope: None = applies to every brand (the safe default when unclear).
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brand.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Profile(Base):
    __tablename__ = "profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"))
    data: Mapped[dict] = mapped_column(JSON)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    prompt_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class CallAttempt(Base):
    """One call attempt, with the reason it was allowed (hard rule 3)."""

    __tablename__ = "call_attempt"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    basis: Mapped[CallBasis] = mapped_column(_enum(CallBasis))
    basis_detail: Mapped[str] = mapped_column(Text)
    external_call_id: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[str | None] = mapped_column(String(32))
    summary: Mapped[str | None] = mapped_column(Text)
    transcript_ref: Mapped[str | None] = mapped_column(String(255))
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaign.id", ondelete="SET NULL"), index=True
    )
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brand.id", ondelete="SET NULL"))
    # What was (or in dry run: would have been) sent to the voice platform.
    payload: Mapped[dict | None] = mapped_column(JSON)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_s: Mapped[float | None] = mapped_column(Float)
    # Transcript turns: [{"role": "agent"|"user", "message": str, "t": float}]
    transcript: Mapped[list | None] = mapped_column(JSON)
    analysis: Mapped[dict | None] = mapped_column(JSON)
    ai_disclosure_given: Mapped[bool | None] = mapped_column(Boolean)
    hung_up_after_disclosure: Mapped[bool | None] = mapped_column(Boolean)
    callback_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cost_eur: Mapped[float] = mapped_column(Float, default=0.0)
    qa_status: Mapped[str | None] = mapped_column(String(32))


class Appointment(Base):
    __tablename__ = "appointment"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"))
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime())
    location: Mapped[str | None] = mapped_column(String(255))
    account_manager: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="gepland")
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brand.id", ondelete="SET NULL"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaign.id", ondelete="SET NULL"))
    account_manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("account_manager.id", ondelete="SET NULL"), index=True
    )
    call_attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("call_attempt.id", ondelete="SET NULL")
    )
    contact_name: Mapped[str | None] = mapped_column(String(255))
    contact_role: Mapped[str | None] = mapped_column(String(255))
    # Human QA before the customer sees it: "te controleren", "goedgekeurd", "afgekeurd".
    qa_status: Mapped[str] = mapped_column(String(32), default="te controleren")
    qa_note: Mapped[str | None] = mapped_column(Text)
    qa_minutes: Mapped[float | None] = mapped_column(Float)
    briefing: Mapped[str | None] = mapped_column(Text)
    # Reported back by the account manager: "plaatsgevonden", "no-show", "order", "geen order".
    result: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    company: Mapped[Company] = relationship()


# --- Brands, rounds and leads -------------------------------------------------


class Brand(Base):
    """A client brand we call on behalf of (for example a car make or lease company)."""

    __tablename__ = "brand"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    # What the agent offers and asks, in Dutch. Used to build the call variables.
    pitch: Mapped[str] = mapped_column(Text, default="")
    qualification_questions: Mapped[list] = mapped_column(JSON, default=list)
    # Segment rules: {"min_vehicles": 3, "makes": [...], "max_avg_build_year": 2020}
    segment_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    price_per_address_eur: Mapped[float] = mapped_column(Float, default=11.0)
    price_per_appointment_eur: Mapped[float] = mapped_column(Float, default=0.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    account_managers: Mapped[list["AccountManager"]] = relationship(back_populates="brand")


class AccountManager(Base):
    __tablename__ = "account_manager"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255))
    # Cities or region words this person covers; empty = everywhere.
    regions: Mapped[list] = mapped_column(JSON, default=list)
    work_start: Mapped[str] = mapped_column(String(5), default="09:00")
    work_end: Mapped[str] = mapped_column(String(5), default="17:00")
    slot_minutes: Mapped[int] = mapped_column(Integer, default=60)

    brand: Mapped[Brand] = relationship(back_populates="account_managers")


class CampaignStatus(StrEnum):
    DRAFT = "concept"
    PREPARED = "voorbereid"
    RUNNING = "loopt"
    DONE = "afgerond"


class Campaign(Base):
    """One calling round for one brand (for example "Merk A, ronde 2026-1")."""

    __tablename__ = "campaign"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[CampaignStatus] = mapped_column(
        _enum(CampaignStatus), default=CampaignStatus.DRAFT
    )
    # Legal form must have been checked on or after this moment (check per round).
    legal_form_checked_since: Mapped[datetime | None] = mapped_column(UTCDateTime())
    agent_variant: Mapped[str] = mapped_column(String(64), default="A")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    brand: Mapped[Brand] = relationship()
    leads: Mapped[list["CampaignLead"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class LeadStatus(StrEnum):
    CALLABLE = "belbaar"
    BLOCKED = "geblokkeerd"
    CALLED = "gebeld"
    NOT_REACHED = "niet bereikt"
    CALL_BACK = "terugbellen"
    APPOINTMENT = "afspraak"
    WARM = "warm"
    NO_INTEREST = "geen interesse"
    WRONG_PERSON = "verkeerde persoon"
    OBJECTION_BRAND = "verzet merk"
    OBJECTION_ALL = "verzet alles"
    DONE = "afgerond"


class CampaignLead(Base):
    __tablename__ = "campaign_lead"
    __table_args__ = (UniqueConstraint("campaign_id", "company_id", name="uq_campaign_company"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaign.id", ondelete="CASCADE"))
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"))
    status: Mapped[LeadStatus] = mapped_column(_enum(LeadStatus), default=LeadStatus.CALLABLE)
    reason: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_attempt_after: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # A/B variant, randomised per company and stratified by segment.
    variant: Mapped[str] = mapped_column(String(16), default="A")

    campaign: Mapped[Campaign] = relationship(back_populates="leads")
    company: Mapped[Company] = relationship()


class CostEntry(Base):
    """Every euro spent, so cost per address and per appointment can be computed."""

    __tablename__ = "cost_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # data, research, bellen, qa
    amount_eur: Mapped[float] = mapped_column(Float)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id", ondelete="SET NULL"))
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaign.id", ondelete="SET NULL"), index=True
    )
    call_attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("call_attempt.id", ondelete="SET NULL")
    )
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class OutboxMessage(Base):
    """Emails that would be (or were) sent. Mock mode keeps them here only."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    to: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32))  # briefing, bevestiging
    appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointment.id", ondelete="SET NULL")
    )
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# --- Learning from historical recordings -------------------------------------


class Recording(Base):
    """One historical CATI call. Holds only pseudonymised text, never names."""

    __tablename__ = "recording"

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[str] = mapped_column(String(64), unique=True)
    file_name: Mapped[str | None] = mapped_column(String(512))
    kvk_number: Mapped[str | None] = mapped_column(String(8))
    agent_token: Mapped[str | None] = mapped_column(String(16))
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    duration_s: Mapped[float | None] = mapped_column(Float)
    disposition_code: Mapped[str | None] = mapped_column(String(64))
    # Label from the CATI disposition: the ground truth (decision Jeroen).
    outcome: Mapped[str | None] = mapped_column(String(32))
    campaign: Mapped[str | None] = mapped_column(String(128))
    segment: Mapped[dict] = mapped_column(JSON, default=dict)
    audio_info: Mapped[dict | None] = mapped_column(JSON)
    transcript: Mapped[list | None] = mapped_column(JSON)  # pseudonymised turns
    quality_flag: Mapped[str | None] = mapped_column(String(32))
    excluded_reason: Mapped[str | None] = mapped_column(String(255))
    features: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ObjectionEntry(Base):
    """Objection library: recognition, approved responses and when to give up."""

    __tablename__ = "objection_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(255))
    triggers: Mapped[list] = mapped_column(JSON, default=list)
    responses: Mapped[list] = mapped_column(JSON, default=list)
    give_up_when: Mapped[str] = mapped_column(Text, default="")
    # Evidence from the recordings: {"n": 120, "appointment_rate": 0.04, "by_response": {...}}
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32), default="standaard")


class PlaybookVersion(Base):
    __tablename__ = "playbook_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class EvalCase(Base):
    """A test situation for the agent. Never contains the historical outcome."""

    __tablename__ = "eval_case"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    category: Mapped[str] = mapped_column(String(64))
    prospect_turns: Mapped[list] = mapped_column(JSON)
    expectations: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(64), default="handmatig")


class EvalRun(Base):
    __tablename__ = "eval_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_version: Mapped[str] = mapped_column(String(64))
    passed_hard: Mapped[bool] = mapped_column(Boolean)
    scores: Mapped[dict] = mapped_column(JSON)
    results: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

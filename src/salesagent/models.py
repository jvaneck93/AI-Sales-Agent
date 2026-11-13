"""Data model.

Enum *values* are Dutch because they are stored and shown to users;
identifiers are English.
"""

from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import JSON, Date, DateTime, ForeignKey, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from salesagent.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


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
    legal_form_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    address: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(255))
    sector: Mapped[str | None] = mapped_column(String(255))
    website: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32), index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[CompanyStatus] = mapped_column(_enum(CompanyStatus), default=CompanyStatus.NEW)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Profile(Base):
    __tablename__ = "profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"))
    data: Mapped[dict] = mapped_column(JSON)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    prompt_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CallAttempt(Base):
    """One call attempt, with the reason it was allowed (hard rule 3)."""

    __tablename__ = "call_attempt"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    basis: Mapped[CallBasis] = mapped_column(_enum(CallBasis))
    basis_detail: Mapped[str] = mapped_column(Text)
    external_call_id: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[str | None] = mapped_column(String(32))
    summary: Mapped[str | None] = mapped_column(Text)
    transcript_ref: Mapped[str | None] = mapped_column(String(255))


class Appointment(Base):
    __tablename__ = "appointment"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    location: Mapped[str | None] = mapped_column(String(255))
    account_manager: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="gepland")

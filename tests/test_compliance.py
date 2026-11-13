"""Tests for hard rules 1 to 3 from CLAUDE.md."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from salesagent.compliance.gate import (
    CallNotAllowedError,
    complete_call_attempt,
    mag_gebeld_worden,
    may_be_called,
    start_call_attempt,
)
from salesagent.compliance.kvk import FakeKvkClient, resolve_legal_form
from salesagent.compliance.register import ConsentError, register_objection, register_opt_in
from salesagent.models import (
    CallAttempt,
    CallBasis,
    Company,
    CompanyStatus,
    Consent,
    ConsentType,
    LegalForm,
)

TODAY = date.today()


def _opt_in(session, company, **overrides):
    kwargs = {"source": "webformulier", "consent_date": TODAY, "evidence": "formulier #123"}
    return register_opt_in(session, company, **(kwargs | overrides))


# --- Hard rule 1: legal form filter -------------------------------------------------


@pytest.mark.parametrize(
    "legal_form",
    [LegalForm.BV, LegalForm.NV, LegalForm.STICHTING, LegalForm.VERENIGING, LegalForm.COOPERATIE],
)
def test_legal_entities_may_be_called(make_company, legal_form):
    decision = may_be_called(make_company(legal_form))
    assert decision.allowed
    assert decision.basis is CallBasis.LEGAL_FORM
    assert legal_form.value in decision.reason


@pytest.mark.parametrize(
    "legal_form", [LegalForm.EENMANSZAAK, LegalForm.VOF, LegalForm.CV, LegalForm.MAATSCHAP]
)
def test_non_legal_entities_are_not_called_without_consent(make_company, legal_form):
    decision = may_be_called(make_company(legal_form))
    assert not decision.allowed
    assert decision.basis is None
    assert "geen aantoonbare toestemming" in decision.reason


def test_unknown_legal_form_is_not_called(make_company):
    decision = may_be_called(make_company(LegalForm.UNKNOWN))
    assert not decision.allowed
    assert "onbekend" in decision.reason


def test_every_legal_form_has_an_explicit_outcome(make_company):
    """A newly added legal form must never become callable by accident."""
    allowed = {form for form in LegalForm if may_be_called(make_company(form)).allowed}
    assert allowed == {
        LegalForm.BV,
        LegalForm.NV,
        LegalForm.STICHTING,
        LegalForm.VERENIGING,
        LegalForm.COOPERATIE,
    }


def test_opt_in_allows_non_legal_entity(session, make_company):
    company = make_company(LegalForm.EENMANSZAAK)
    _opt_in(session, company)
    decision = may_be_called(company)
    assert decision.allowed
    assert decision.basis is CallBasis.CONSENT
    assert "webformulier" in decision.reason
    assert TODAY.isoformat() in decision.reason


def test_opt_in_allows_unknown_legal_form(session, make_company):
    company = make_company(LegalForm.UNKNOWN)
    _opt_in(session, company)
    assert may_be_called(company).basis is CallBasis.CONSENT


@pytest.mark.parametrize(
    "overrides",
    [{"source": " "}, {"evidence": ""}, {"consent_date": TODAY + timedelta(days=1)}],
)
def test_opt_in_requires_source_date_and_evidence(session, make_company, overrides):
    company = make_company(LegalForm.VOF)
    with pytest.raises(ConsentError):
        _opt_in(session, company, **overrides)
    assert not may_be_called(company).allowed


def test_opt_in_record_without_evidence_does_not_count(session, make_company):
    """Also when a record got into the database some other way."""
    company = make_company(LegalForm.VOF)
    session.add(
        Consent(
            company_id=company.id,
            kvk_number=company.kvk_number,
            type=ConsentType.OPT_IN,
            source="onbekend",
            date=TODAY,
            evidence="",
        )
    )
    session.flush()
    assert not may_be_called(company).allowed


def test_opt_in_of_other_company_does_not_count(session, make_company):
    _opt_in(session, make_company(LegalForm.VOF))
    assert not may_be_called(make_company(LegalForm.VOF)).allowed


def test_dutch_alias_is_the_same_function():
    assert mag_gebeld_worden is may_be_called


# --- Legal form via KvK interface ---------------------------------------------------


def test_kvk_client_fills_in_unknown_legal_form(make_company):
    company = make_company(LegalForm.UNKNOWN)
    client = FakeKvkClient({company.kvk_number: LegalForm.BV})

    assert resolve_legal_form(company, client) is LegalForm.BV
    assert company.legal_form_source == "kvk"
    decision = may_be_called(company)
    assert decision.allowed
    assert "bron: kvk" in decision.reason


def test_kvk_client_without_result_keeps_unknown(make_company):
    company = make_company(LegalForm.UNKNOWN)
    assert resolve_legal_form(company, FakeKvkClient()) is LegalForm.UNKNOWN
    assert resolve_legal_form(company, None) is LegalForm.UNKNOWN
    assert not may_be_called(company).allowed


def test_kvk_client_does_not_overwrite_known_legal_form(make_company):
    company = make_company(LegalForm.EENMANSZAAK)
    client = FakeKvkClient({company.kvk_number: LegalForm.BV})
    assert resolve_legal_form(company, client) is LegalForm.EENMANSZAAK


# --- Hard rule 2: objection register ------------------------------------------------


def test_objection_blocks_legal_entity(session, make_company):
    company = make_company(LegalForm.BV)
    register_objection(session, company=company, source="telefoongesprek")

    decision = may_be_called(company)
    assert not decision.allowed
    assert "Verzet" in decision.reason
    assert company.status is CompanyStatus.OBJECTION


def test_objection_wins_over_opt_in_even_if_opt_in_is_newer(session, make_company):
    company = make_company(LegalForm.EENMANSZAAK)
    register_objection(
        session, company=company, source="e-mail", objection_date=TODAY - timedelta(days=30)
    )
    _opt_in(session, company)
    assert not may_be_called(company).allowed


def test_objection_by_phone_number_blocks_company(session, make_company):
    company = make_company(LegalForm.BV, phone="+31381234567")
    register_objection(session, source="telefoongesprek", phone="038-1234567")
    assert not may_be_called(company).allowed
    assert company.status is CompanyStatus.OBJECTION


def test_objection_survives_delete_and_reimport(session, make_company):
    company = make_company(LegalForm.BV)
    kvk = company.kvk_number
    register_objection(session, company=company, source="telefoongesprek")
    session.delete(company)
    session.flush()

    again = make_company(LegalForm.BV, kvk_number=kvk, phone=None)
    assert not may_be_called(again).allowed


def test_objection_requires_source_and_identifier(session):
    with pytest.raises(ConsentError):
        register_objection(session, source="", kvk_number="20000001")
    with pytest.raises(ConsentError):
        register_objection(session, source="e-mail", phone="12345")


def test_objection_is_checked_again_right_before_the_call(session, make_company):
    """Allowed at planning time, objection arrives, call must not start."""
    company = make_company(LegalForm.BV)
    assert may_be_called(company).allowed

    register_objection(session, company=company, source="e-mail")

    with pytest.raises(CallNotAllowedError, match="Verzet"):
        start_call_attempt(session, company)
    assert session.scalars(select(CallAttempt)).all() == []


def test_call_attempt_refused_for_non_callable_company(session, make_company):
    with pytest.raises(CallNotAllowedError):
        start_call_attempt(session, make_company(LegalForm.EENMANSZAAK))
    with pytest.raises(CallNotAllowedError):
        start_call_attempt(session, make_company(LegalForm.UNKNOWN))


# --- Hard rule 3: burden of proof ---------------------------------------------------


def test_call_attempt_records_legal_form_basis_time_and_outcome(session, make_company):
    company = make_company(LegalForm.BV)
    attempt = start_call_attempt(session, company)

    assert attempt.basis is CallBasis.LEGAL_FORM
    assert "bv" in attempt.basis_detail
    assert "bron: csv" in attempt.basis_detail
    assert attempt.started_at is not None

    complete_call_attempt(session, attempt, outcome="niet bereikt")
    stored = session.scalar(select(CallAttempt).where(CallAttempt.company_id == company.id))
    assert stored.outcome == "niet bereikt"


def test_call_attempt_records_consent_basis_with_source_and_date(session, make_company):
    company = make_company(LegalForm.VOF)
    _opt_in(session, company)
    attempt = start_call_attempt(session, company)

    assert attempt.basis is CallBasis.CONSENT
    assert "webformulier" in attempt.basis_detail
    assert TODAY.isoformat() in attempt.basis_detail
    assert "formulier #123" in attempt.basis_detail


def test_call_attempt_cannot_be_stored_without_basis(session, make_company):
    from sqlalchemy.exc import IntegrityError

    from salesagent.models import utcnow

    company = make_company(LegalForm.BV)
    session.add(CallAttempt(company_id=company.id, started_at=utcnow()))
    with pytest.raises(IntegrityError):
        session.flush()


def test_objection_stays_in_register_as_evidence(session, make_company):
    company = make_company(LegalForm.BV)
    register_objection(session, company=company, source="telefoongesprek", evidence="gesprek 42")
    stored = session.scalar(select(Consent).where(Consent.type == ConsentType.OBJECTION))
    assert (stored.kvk_number, stored.phone) == (company.kvk_number, company.phone)
    assert stored.source == "telefoongesprek"
    assert stored.date == TODAY
    assert session.get(Company, company.id) is not None

"""Brands, rounds, calls, post-call processing, QA and reporting."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from salesagent.compliance.gate import may_be_called, start_call_attempt
from salesagent.compliance.kvk import FakeKvkClient
from salesagent.compliance.register import register_objection
from salesagent.config import get_settings
from salesagent.models import (
    AccountManager,
    Appointment,
    Brand,
    CallAttempt,
    CampaignLead,
    Consent,
    CostEntry,
    LeadStatus,
    LegalForm,
    OutboxMessage,
    Vehicle,
)
from salesagent.operations.campaigns import MockKvkClient, create_campaign, prepare_round
from salesagent.operations.dialer import run_round, within_call_window
from salesagent.operations.postcall import (
    WebhookError,
    process_post_call,
    sign,
    verify_signature,
)
from salesagent.operations.qa import approve_appointment, reject_appointment
from salesagent.operations.reporting import compute_stats
from salesagent.scheduling.calendar import LocalCalendar, Slot, SlotTakenError
from salesagent.voice.payload import AgentConfigError, check_disclosure
from salesagent.voice.providers import (
    DryRunProvider,
    ElevenLabsProvider,
    RealCallBlockedError,
    SimulatorProvider,
)

# Monday 10:00 Amsterdam (09:00 UTC), inside the call window.
NOW = datetime(2026, 11, 30, 9, 0, tzinfo=UTC)


@pytest.fixture
def brand(session):
    b = Brand(
        name="Testmerk",
        pitch="Testmerk helpt.",
        qualification_questions=["Hoeveel bedrijfswagens heeft u?", "Staat vervanging gepland?"],
        segment_rules={"min_vehicles": 1},
    )
    session.add(b)
    session.flush()
    session.add(AccountManager(brand_id=b.id, name="AM Test", email="am@test.example"))
    session.flush()
    return b


@pytest.fixture
def other_brand(session):
    b = Brand(name="Andermerk", segment_rules={})
    session.add(b)
    session.flush()
    return b


def _with_fleet(session, company, n=3):
    for i in range(n):
        session.add(
            Vehicle(
                company_id=company.id,
                make="Fictiva",
                build_year=2018,
                license_plate=f"T{company.id:03d}{i}",
            )
        )
    session.flush()
    session.refresh(company)
    return company


# --- Compliance with brand context ----------------------------------------------


def test_brand_objection_blocks_only_that_brand(session, make_company, brand, other_brand):
    company = make_company()
    register_objection(session, source="gesprek", company=company, brand=brand)
    assert not may_be_called(company, session, brand=brand).allowed
    assert may_be_called(company, session, brand=other_brand).allowed
    # Without brand context every objection blocks (conservative).
    assert not may_be_called(company, session).allowed


def test_all_brands_objection_blocks_everything(session, make_company, brand, other_brand):
    company = make_company()
    register_objection(session, source="gesprek", company=company)
    assert not may_be_called(company, session, brand=brand).allowed
    assert not may_be_called(company, session, brand=other_brand).allowed


def test_round_requires_fresh_legal_form_check(session, make_company, brand):
    company = _with_fleet(session, make_company(legal_form_checked_at=NOW - timedelta(days=300)))
    campaign = create_campaign(session, brand, "R1")
    campaign.legal_form_checked_since = NOW
    decision = may_be_called(company, session, campaign=campaign, now=NOW)
    assert not decision.allowed and "deze belronde" in decision.reason


def test_prepare_round_rechecks_legal_form_and_segment(session, make_company, brand):
    still_bv = _with_fleet(session, make_company())
    now_vof = _with_fleet(session, make_company())
    dissolved = _with_fleet(session, make_company())
    no_fleet = make_company()
    kvk = FakeKvkClient({still_bv.kvk_number: LegalForm.BV, now_vof.kvk_number: LegalForm.VOF})
    campaign = create_campaign(session, brand, "R1")
    report = prepare_round(session, campaign, kvk, now=NOW)
    leads = {lead.company_id: lead for lead in campaign.leads}
    assert report.selected == 3 and no_fleet.id not in leads
    assert leads[still_bv.id].status is LeadStatus.CALLABLE
    assert leads[now_vof.id].status is LeadStatus.BLOCKED
    assert dissolved.legal_form is LegalForm.UNKNOWN
    assert leads[dissolved.id].status is LeadStatus.BLOCKED


def test_call_fatigue_across_brands(session, make_company, brand, other_brand):
    company = _with_fleet(session, make_company())
    for i in range(get_settings().max_contacts_per_quarter):
        session.add(
            CallAttempt(
                company_id=company.id,
                started_at=NOW - timedelta(days=i + 1),
                basis="rechtsvorm",
                basis_detail="x",
                connected=True,
            )
        )
    session.flush()
    campaign = create_campaign(session, other_brand, "R1")
    prepare_round(session, campaign, MockKvkClient(session), now=NOW)
    decision = may_be_called(company, session, campaign=campaign, now=NOW)
    assert not decision.allowed and "Belmoeheid" in decision.reason


def test_real_calls_need_kvk_source(session, make_company, brand, monkeypatch):
    company = _with_fleet(session, make_company())
    campaign = create_campaign(session, brand, "R1")
    prepare_round(session, campaign, MockKvkClient(session), now=NOW)
    monkeypatch.setattr(get_settings(), "dry_run", False)
    decision = may_be_called(company, session, campaign=campaign, now=NOW)
    assert not decision.allowed and "KvK" in decision.reason


# --- Calling ------------------------------------------------------------------------


@pytest.fixture
def prepared(session, make_company, brand):
    companies = [_with_fleet(session, make_company()) for _ in range(40)]
    campaign = create_campaign(session, brand, "R1", "A,B")
    prepare_round(session, campaign, MockKvkClient(session), now=NOW)
    return campaign, companies


def test_call_window():
    assert within_call_window(NOW)[0]
    assert not within_call_window(NOW.replace(hour=18))[0]
    assert not within_call_window(NOW + timedelta(days=5))[0]  # Saturday


def test_round_outside_call_window_does_not_call(session, prepared):
    campaign, _ = prepared
    report = run_round(session, campaign, SimulatorProvider(seed=1), now=NOW.replace(hour=19))
    assert report.called == 0 and "Buiten beltijden" in report.message


def test_simulated_round_full_path(session, prepared):
    campaign, _ = prepared
    report = run_round(session, campaign, SimulatorProvider(seed=3), now=NOW, limit=100)
    assert report.called == 40
    attempts = session.scalars(select(CallAttempt)).all()
    for a in attempts:
        assert a.basis_detail and a.outcome  # evidence and outcome (hard rule 3)
        assert a.payload["conversation_initiation_client_data"]["dynamic_variables"]["merk"]
        if a.connected:
            assert a.ai_disclosure_given is True
            assert "ai-assistent" in a.transcript[0]["message"].lower()
            assert "opgenomen" in a.transcript[0]["message"].lower()
        for turn in a.transcript or []:
            if turn["role"] == "agent":
                assert "€" not in turn["message"] and "korting van" not in turn["message"]
    data_costs = session.scalars(select(CostEntry).where(CostEntry.kind == "data")).all()
    assert len(data_costs) == 40  # once per called address
    for appointment in session.scalars(select(Appointment)):
        assert appointment.qa_status == "te controleren"
        assert appointment.briefing and "Briefing" in appointment.briefing
        assert appointment.starts_at > NOW


def test_second_round_attempt_and_max_attempts(session, prepared):
    campaign, _ = prepared
    provider = SimulatorProvider(seed=5)
    for day in range(5):
        run_round(session, campaign, provider, now=NOW + timedelta(days=day), limit=100)
    for lead in session.scalars(select(CampaignLead)):
        assert lead.attempts <= get_settings().max_call_attempts


def test_dry_run_provider_sends_nothing(session, prepared):
    campaign, _ = prepared
    report = run_round(session, campaign, DryRunProvider(), now=NOW, limit=3)
    assert report.outcomes == {"dry run": 3}
    assert len(report.dry_run_requests) == 3
    lead = session.scalars(select(CampaignLead)).first()
    assert lead.attempts == 0  # a dry run is not an attempt


def test_elevenlabs_provider_refuses_in_dry_run():
    provider = ElevenLabsProvider("key", dry_run=True, blocked_numbers=set())
    with pytest.raises(RealCallBlockedError):
        provider.place_call(
            {"to_number": "+31850000001"}, tools=None, library=[], variables={}, now=NOW
        )
    provider = ElevenLabsProvider("key", dry_run=False, blocked_numbers={"+31850000001"})
    with pytest.raises(RealCallBlockedError):
        provider.place_call(
            {"to_number": "+31850000001"}, tools=None, library=[], variables={}, now=NOW
        )


def test_elevenlabs_provider_posts_request():
    import httpx

    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers["xi-api-key"]
        return httpx.Response(200, json={"success": True, "conversation_id": "conv_1"})

    provider = ElevenLabsProvider(
        "secret",
        dry_run=False,
        blocked_numbers=set(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    start = provider.place_call(
        {"to_number": "+31850000009"}, tools=None, library=[], variables={}, now=NOW
    )
    assert start.conversation_id == "conv_1"
    assert seen["url"].endswith("/v1/convai/sip-trunk/outbound-call")
    assert seen["key"] == "secret"


def test_disclosure_is_required():
    check_disclosure(
        "Goedemorgen, u spreekt met Robin, een AI-assistent namens X. Dit gesprek wordt opgenomen."
    )
    with pytest.raises(AgentConfigError):
        check_disclosure("Goedemorgen, u spreekt met Robin van X. Dit gesprek wordt opgenomen.")
    with pytest.raises(AgentConfigError):
        check_disclosure("Goedemorgen, u spreekt met Robin, een AI-assistent namens X.")


# --- Post-call ------------------------------------------------------------------------


def test_signature():
    body = b'{"type":"post_call_transcription"}'
    header = sign(body, "s3cret", timestamp=1_000_000)
    verify_signature(body, header, "s3cret", now=1_000_010)
    with pytest.raises(WebhookError):
        verify_signature(body + b" ", header, "s3cret", now=1_000_010)
    with pytest.raises(WebhookError):
        verify_signature(body, header, "s3cret", now=1_000_000 + 3600)
    with pytest.raises(WebhookError):
        verify_signature(body, None, "s3cret")


def _live_payload(attempt, transcript, collected=None):
    return {
        "type": "post_call_transcription",
        "data": {
            "conversation_id": "conv_x",
            "transcript": [
                {"role": r, "message": m, "time_in_call_secs": i * 4}
                for i, (r, m) in enumerate(transcript)
            ],
            "metadata": {"call_duration_secs": 61},
            "analysis": {
                "data_collection_results": {k: {"value": v} for k, v in (collected or {}).items()}
            },
            "conversation_initiation_client_data": {
                "dynamic_variables": {"attempt_id": attempt.id}
            },
        },
    }


def _attempt(session, campaign, company):
    return start_call_attempt(session, company, now=NOW, campaign=campaign)


OPENING = (
    "Goedemorgen, u spreekt met Robin, een AI-assistent namens Testmerk. Dit gesprek "
    "wordt opgenomen."
)


def test_live_payload_with_brand_scope(session, prepared, brand):
    campaign, companies = prepared
    attempt = _attempt(session, campaign, companies[0])
    payload = _live_payload(
        attempt,
        [
            ("agent", OPENING),
            ("user", "Bel me niet meer."),
            ("agent", "Alleen voor Testmerk of alles?"),
            ("user", "Alleen voor Testmerk."),
        ],
        {"outcome": "verzet", "opt_out_scope": "merk"},
    )
    process_post_call(session, payload, now=NOW)
    consent = session.scalars(select(Consent)).one()
    assert consent.brand_id == brand.id
    lead = session.scalar(select(CampaignLead).where(CampaignLead.company_id == companies[0].id))
    assert lead.status is LeadStatus.OBJECTION_BRAND
    assert attempt.cost_eur > 0


def test_opt_out_in_transcript_without_result_blocks_everything(session, prepared):
    campaign, companies = prepared
    attempt = _attempt(session, campaign, companies[1])
    payload = _live_payload(
        attempt,
        [("agent", OPENING), ("user", "Haal ons uit uw bestand, en snel.")],
        {"outcome": "geen interesse"},
    )
    process_post_call(session, payload, now=NOW)
    consent = session.scalars(select(Consent)).one()
    assert consent.brand_id is None  # when in doubt: everything
    assert attempt.outcome == "verzet"
    assert not may_be_called(companies[1], session).allowed


def test_fallback_classification_and_idempotency(session, prepared):
    campaign, companies = prepared
    attempt = _attempt(session, campaign, companies[2])
    payload = _live_payload(
        attempt,
        [
            ("agent", OPENING),
            ("user", "Ik heb geen tijd."),
            ("agent", "Wanneer kan ik terugbellen?"),
            ("user", "Bel donderdag maar."),
        ],
    )
    process_post_call(session, payload, now=NOW)
    assert attempt.outcome == "terugbellen"
    assert attempt.callback_at.weekday() == 3
    assert attempt.analysis["classified_by"] == "regels"
    process_post_call(session, payload, now=NOW)  # delivered twice
    assert len(session.scalars(select(CostEntry).where(CostEntry.kind == "bellen")).all()) == 1


def test_missing_disclosure_is_flagged(session, prepared):
    campaign, companies = prepared
    attempt = _attempt(session, campaign, companies[3])
    process_post_call(
        session, _live_payload(attempt, [("agent", "Hallo met Robin."), ("user", "Ja?")]), now=NOW
    )
    assert attempt.ai_disclosure_given is False
    assert attempt.qa_status == "te controleren"


def test_live_appointment_slot_is_booked(session, prepared):
    campaign, companies = prepared
    attempt = _attempt(session, campaign, companies[4])
    am = session.scalars(select(AccountManager)).first()
    key = Slot(
        am.id, datetime(2026, 12, 1, 9, tzinfo=UTC), datetime(2026, 12, 1, 10, tzinfo=UTC)
    ).key
    payload = _live_payload(
        attempt,
        [("agent", OPENING), ("user", "Ja dinsdag is goed.")],
        {"outcome": "afspraak", "appointment_slot": key},
    )
    process_post_call(session, payload, now=NOW, calendar=LocalCalendar(session))
    appointment = session.scalars(select(Appointment)).one()
    assert appointment.call_attempt_id == attempt.id
    assert appointment.starts_at == datetime(2026, 12, 1, 9, tzinfo=UTC)


# --- Calendar and QA --------------------------------------------------------------------


def test_calendar_no_double_booking_and_no_weekend(session, make_company, brand):
    am = session.scalars(select(AccountManager)).first()
    calendar = LocalCalendar(session)
    friday = datetime(2026, 12, 3, 12, tzinfo=UTC)
    slots = calendar.free_slots(am, friday, days=4)
    assert all(s.starts_at.weekday() < 5 for s in slots)
    assert slots[0].starts_at.date().isoformat() == "2026-12-04"
    company = make_company()
    calendar.book(slots[0], company=company)
    with pytest.raises(SlotTakenError):
        calendar.book(slots[0], company=company)


def test_qa_approve_and_reject(session, prepared):
    campaign, _ = prepared
    run_round(session, campaign, SimulatorProvider(seed=3), now=NOW, limit=100)
    appointments = session.scalars(select(Appointment)).all()
    if len(appointments) < 2:
        pytest.skip("seed produced fewer than two appointments")
    approve_appointment(session, appointments[0], minutes=4)
    kinds = {m.kind for m in session.scalars(select(OutboxMessage))}
    assert "briefing" in kinds
    reject_appointment(session, appointments[1], note="beslisser niet bevestigd")
    assert appointments[1].status == "geannuleerd"
    lead = session.scalar(
        select(CampaignLead).where(CampaignLead.company_id == appointments[1].company_id)
    )
    assert lead.status is LeadStatus.WARM
    stats = compute_stats(session)
    assert stats.costs["qa"] > 0
    assert stats.cost_per_address and stats.cost_per_address < 11
    assert stats.appointments_rejected == 1


def test_stats_json_roundtrip(session, prepared):
    campaign, _ = prepared
    run_round(session, campaign, SimulatorProvider(seed=9), now=NOW, limit=100)
    stats = compute_stats(session, campaign_id=campaign.id)
    assert stats.addresses_called == 40
    assert stats.connected + stats.outcomes.get("niet bereikt", 0) == stats.attempts
    json.dumps(stats.__dict__)

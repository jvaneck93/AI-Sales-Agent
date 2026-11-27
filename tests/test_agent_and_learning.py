"""Agent policy, NLU, enrichment, recordings pipeline, eval set and web endpoints."""

import json
import shutil
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from salesagent.enrichment.research import (
    MockResearchProvider,
    ResearchError,
    parse_profile_json,
    run_research,
)
from salesagent.enrichment.scraper import WebsiteScraper
from salesagent.evals.harness import run_eval, seed_default_cases
from salesagent.models import EvalCase, ObjectionEntry, Profile, Recording, Vehicle
from salesagent.recordings.demo_data import generate
from salesagent.recordings.features import RuleBasedExtractor
from salesagent.recordings.pipeline import analyze, build_outputs, ingest_export
from salesagent.recordings.pseudonymize import pseudonymize
from salesagent.recordings.transcribe import SidecarTranscriber
from salesagent.voice import nlu
from salesagent.voice.agent import Agent
from salesagent.voice.library import load_library, seed_objection_library

NOW = datetime(2026, 11, 27, 10, tzinfo=UTC)  # a Friday
VARS = {
    "groet": "Goedemorgen",
    "agent_naam": "Robin",
    "merk": "Testmerk",
    "bedrijfsnaam": "Test B.V.",
    "pitch": "",
    "kwalificatievragen": ["Vraag 1?", "Vraag 2?"],
    "gespreksopener": "",
    "accountmanager": "AM",
    "attempt_id": 1,
    "campaign_id": 1,
}


class Tools:
    def __init__(self):
        self.booked, self.opt_out = None, None

    def slot_options(self):
        return [
            ("1@2026-12-01T10:00:00+01:00", "dinsdag 1 december om 10:00"),
            ("1@2026-12-02T14:00:00+01:00", "woensdag 2 december om 14:00"),
        ]

    def book(self, key):
        self.booked = key
        return True

    def register_opt_out(self, scope):
        self.opt_out = scope


def _talk(lines):
    tools = Tools()
    agent = Agent(dict(VARS), load_library(None), tools, NOW)
    said = [agent.opening()]
    for line in lines:
        turn = agent.respond(line)
        if turn is None:
            break
        said.append(turn)
    return agent, tools, said


def test_first_sentence_is_ai_disclosure():
    _, _, said = _talk([])
    first = said[0].text
    assert "AI-assistent" in first.split(". ")[0]
    assert "opgenomen" in first


@pytest.mark.parametrize(
    ("answer", "scope"),
    [
        ("Alleen voor Testmerk.", "merk"),
        ("Helemaal niet meer, niemand.", "alles"),
        ("Weet ik veel, gewoon niet.", "alles"),
    ],
)
def test_opt_out_asks_scope_and_stops(answer, scope):
    agent, tools, said = _talk(["Bel me niet meer.", answer, "nog iets"])
    assert said[1].act == "scope"
    assert tools.opt_out == scope
    assert agent.result["outcome"] == "verzet" and agent.ended
    assert len(said) == 3  # nothing after the closing


def test_price_question_gets_no_price():
    agent, _, said = _talk(["Ja.", "Wat kost dat per maand?"])
    assert "geen uitspraken" in said[2].text
    assert "prijs" in agent.result["objections"]


def test_interested_prospect_gets_booked():
    agent, tools, _ = _talk(
        [
            "Ja, dat ben ik.",
            "Ja, een paar zijn aan vervanging toe.",
            "Ja, dit jaar nog.",
            "Woensdag is goed.",
        ]
    )
    assert agent.result["outcome"] == "afspraak"
    assert tools.booked == "1@2026-12-02T14:00:00+01:00"


def test_hang_up_after_disclosure_is_recorded():
    agent, _, _ = _talk([nlu.HANG_UP])
    assert agent.result["hung_up_after_disclosure"]
    assert agent.result["outcome"] == "opgehangen"


def test_wrong_person_gives_callback():
    agent, _, _ = _talk(["Daar ga ik niet over.", "De directeur, die is er dinsdag."])
    assert agent.result["outcome"] == "verkeerde persoon"
    assert datetime.fromisoformat(agent.result["callback_at"]).weekday() == 1


def test_parse_callback():
    assert nlu.parse_callback("bel volgende week", NOW).weekday() == 0
    assert nlu.parse_callback("over twee weken", NOW).day == 11
    assert nlu.parse_callback("na de zomer", NOW).month == 9
    assert nlu.parse_callback("in maart", NOW).year == 2027
    assert nlu.parse_callback("geen idee", NOW) is None


# --- Enrichment ------------------------------------------------------------------------


def test_scraper_respects_robots_and_sources_signals():
    pages = {
        "/robots.txt": "User-agent: *\nDisallow: /contact",
        "/": "<html><head><title>Demo</title><meta name='description' content='Installateur'>"
        "</head><body><nav>menu</nav><p>Wij zoeken een servicemonteur voor de regio.</p>"
        "</body></html>",
        "/vacatures": "<p>Vacature: chauffeur bestelbus</p>",
    }

    def handler(request):
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://demo.example")
    result = WebsiteScraper(client).scrape("demo.example")
    assert any("robots" in e for e in result.errors)
    labels = {s["signaal"] for s in result.signals}
    assert "servicemonteurs" in labels or "monteurs (servicebussen)" in labels
    assert all(s["bron"].startswith("https://demo.example") for s in result.signals)
    assert "menu" not in result.pages[0].text


def test_parse_profile_json():
    profile = parse_profile_json('Hier: ```json\n{"profiel": "x", "gespreksopener": "y"}\n```')
    assert profile.gespreksopener == "y" and profile.bronnen == []
    with pytest.raises(ResearchError):
        parse_profile_json("geen json")


def test_mock_research_batch_and_freshness(session, make_company):
    company = make_company()
    session.add(Vehicle(company_id=company.id, make="X", build_year=2017, license_plate="Q1"))
    session.flush()
    session.refresh(company)
    report = run_research(session, [company], MockResearchProvider())
    assert report.done == 1
    profile = session.scalars(select(Profile)).one()
    assert "oudere bedrijfswagens" in profile.data["gespreksopener"]
    assert "2017" not in profile.data["gespreksopener"]  # fleet data is not revealed
    assert run_research(session, [company], MockResearchProvider()).skipped_fresh == 1


# --- Recordings ---------------------------------------------------------------------------


def test_pseudonymize():
    text = (
        "Met Pieter de Vries, mail naar p.vries@bedrijf.nl of bel 06-12345678, "
        "kenteken 12-ABC-3, postcode 1234 AB, KvK 12345678, en collega Jansen."
    )
    out = pseudonymize(text, ["Sanne Proefmans"])
    for leaked in ("Pieter", "Vries", "bedrijf.nl", "12345678", "12-ABC-3", "1234 AB", "Jansen"):
        assert leaked not in out
    assert (
        pseudonymize("Goedemorgen, u spreekt met Sanne.", ["Sanne Proefmans"]).count("[NAAM]") == 1
    )


class SpyExtractor(RuleBasedExtractor):
    name = "spy"

    def __init__(self):
        super().__init__()
        self.calls = []

    def extract(self, turns):
        self.calls.append(turns)
        return super().extract(turns)


@pytest.fixture
def processed(session, tmp_path):
    seed_objection_library(session)
    export = generate(tmp_path / "demo", n=400, mp4=bool(shutil.which("ffmpeg")))
    spy = SpyExtractor()
    report = ingest_export(
        session,
        export,
        tmp_path / "demo" / "opnames",
        transcriber=SidecarTranscriber(),
        extractor=spy,
        workdir=tmp_path / "werk",
    )
    return report, spy, tmp_path


def test_pipeline_never_gives_outcome_to_extractor(processed):
    report, spy, _ = processed
    assert spy.calls and report.transcribed == len(spy.calls)
    for turns in spy.calls:
        blob = json.dumps(turns).lower()
        for label in ('"afspraak"', "dispositie", '"warm"', "outcome"):
            assert label not in blob


def test_pipeline_reports_and_links(session, processed):
    report, _, tmp_path = processed
    assert "XX9" in report.unknown_codes or report.rows == 400
    links = json.loads((tmp_path / "werk" / "koppeltabel.json").read_text())
    assert links["agents"] and links["calls"]
    for r in session.scalars(select(Recording)):
        assert r.kvk_number is None  # KvK only in the link table
        assert r.agent_token is None or r.agent_token.startswith("A")
        for turn in r.transcript or []:
            assert "Proefmans" not in turn["message"]
            assert "06-12345678" not in turn["message"]
    if shutil.which("ffprobe"):
        assert any("kanaal" in k for k in report.audio)


def test_guardrail_flags_optimistic_agent(session, processed):
    report, _, tmp_path = processed
    links = json.loads((tmp_path / "werk" / "koppeltabel.json").read_text())["agents"]
    assert links["Sanne Proefmans"] in report.guardrail_agents


def test_outputs_playbook_library_and_cases(session, processed):
    playbook = build_outputs(session, analyze(session, min_cell=20))
    assert "Playbook" in playbook.content and "Hard" in playbook.content
    cases = session.scalars(select(EvalCase).where(EvalCase.source == "opnames")).all()
    assert cases
    for case in cases:
        assert "afspraak" not in json.dumps(case.expectations)
    proposals = session.scalars(
        select(ObjectionEntry).where(ObjectionEntry.approved.is_(False))
    ).all()
    for p in proposals:
        assert all("[NAAM]" not in r for r in p.responses)


def test_eval_set_passes_hard_criteria(session):
    seed_objection_library(session)
    seed_default_cases(session)
    run = run_eval(session)
    assert run.passed_hard, [r for r in run.results if not r["geslaagd_hard"]]
    assert run.scores["zacht_ok"] == run.scores["zacht_totaal"]


# --- Web -----------------------------------------------------------------------------------


@pytest.fixture
def web(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from salesagent.api.app import app, get_session
    from salesagent.db import Base, make_engine, make_session_factory

    monkeypatch.chdir(tmp_path)  # demo recordings are written under the project data dir
    engine = make_engine(f"sqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    def override():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()
    engine.dispose()


def test_dashboard_pages_after_demo(web):
    assert "Nog geen data" in web.get("/").text
    assert web.post("/demo", follow_redirects=False).status_code == 303
    for path in (
        "/",
        "/merken",
        "/campagnes/1",
        "/bedrijven",
        "/bedrijven/90000001",
        "/gesprekken/1",
        "/afspraken",
        "/afspraken/1",
        "/qa",
        "/outbox",
        "/agent",
        "/opnames",
        "/data",
    ):
        response = web.get(path)
        assert response.status_code == 200, path
    assert "kosten per afspraak" in web.get("/").text
    assert web.post("/agent/eval", follow_redirects=False).status_code == 303
    assert "hard geslaagd" in web.get("/agent/eval/1").text
    stats = web.get("/api/stats").json()
    assert stats["addresses_called"] > 0 and stats["cost_per_address"] < 11


def test_webhook_and_tools_need_secrets(web, monkeypatch):
    from salesagent.config import get_settings
    from salesagent.operations.postcall import sign

    body = json.dumps({"type": "post_call_transcription", "data": {}}).encode()
    monkeypatch.setattr(get_settings(), "elevenlabs_webhook_secret", "geheim")
    assert web.post("/webhooks/elevenlabs", content=body).status_code == 400
    bad = sign(body, "fout")
    assert (
        web.post(
            "/webhooks/elevenlabs", content=body, headers={"ElevenLabs-Signature": bad}
        ).status_code
        == 400
    )
    good = sign(body, "geheim")
    response = web.post(
        "/webhooks/elevenlabs", content=body, headers={"ElevenLabs-Signature": good}
    )
    assert response.status_code == 400 and "Onbekend gesprek" in response.text
    assert web.post("/api/tools/afspraakmomenten", json={"attempt_id": 1}).status_code == 401
    monkeypatch.setattr(get_settings(), "tool_secret", "t")
    assert (
        web.post(
            "/api/tools/afspraakmomenten", json={"attempt_id": 999}, headers={"X-Tool-Secret": "t"}
        ).status_code
        == 404
    )

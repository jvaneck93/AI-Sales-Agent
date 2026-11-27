"""Research module (phase 3): a short, sourced company profile per company.

Two implementations behind one interface:
- ``MockResearchProvider``: no network, no costs. Uses only our own data and,
  optionally, the company website via ``WebsiteScraper``. For demos and tests.
- ``ClaudeResearchProvider``: Claude with web search and web fetch, using
  ``prompts/research_prompt.md``. Needs ANTHROPIC_API_KEY.

Hard rule 9: a provider must leave a field empty rather than guess.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from salesagent.enrichment.fleet import summarize_fleet
from salesagent.enrichment.scraper import WebsiteScraper
from salesagent.models import Company, CompanyStatus, CostEntry, Profile, utcnow

PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "research_prompt.md"
EXTRA_FIELDS_INSTRUCTION = """

AANVULLENDE VELDEN (verplicht, in hetzelfde JSON-object)
Voeg naast "profiel" en "gespreksopener" deze korte velden toe, voor gebruik door
een voice-agent. Laat een veld leeg ("" of []) als je het niet met een bron kunt
onderbouwen:
  "kernactiviteit": "<max 15 woorden>",
  "omvang": "<bijv. 'circa 40 medewerkers, 2 vestigingen' of ''>",
  "wagenparksignalen": ["<kort signaal met bron-URL tussen haakjes>"],
  "recent_nieuws": ["<kort, met datum en bron-URL>"],
  "vacatures": ["<functietitel (bron-URL)>"],
  "bronnen": ["<elke URL die je gebruikt hebt>"]
"""


class ResearchProfile(BaseModel):
    profiel: str
    gespreksopener: str
    kernactiviteit: str = ""
    omvang: str = ""
    wagenparksignalen: list[str] = Field(default_factory=list)
    recent_nieuws: list[str] = Field(default_factory=list)
    vacatures: list[str] = Field(default_factory=list)
    bronnen: list[str] = Field(default_factory=list)


@dataclass
class ResearchResult:
    profile: ResearchProfile
    cost_eur: float
    prompt_version: str
    detail: str = ""


class ResearchError(Exception):
    """Research failed for one company. Message is Dutch and contains no personal data."""


class ResearchProvider(Protocol):
    def research(self, company: Company) -> ResearchResult: ...


def parse_profile_json(text: str) -> ResearchProfile:
    """Parse the model's JSON, tolerating a code fence or text around it."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ResearchError("Antwoord bevat geen JSON-object")
    try:
        return ResearchProfile.model_validate(json.loads(match.group(0)))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ResearchError(f"Ongeldige profiel-JSON: {type(exc).__name__}") from exc


class MockResearchProvider:
    """Builds a profile from our own data (and the website if a scraper is given)."""

    prompt_version = "mock-1"

    def __init__(self, scraper: WebsiteScraper | None = None) -> None:
        self._scraper = scraper

    def research(self, company: Company) -> ResearchResult:
        fleet = summarize_fleet(company)
        signals: list[str] = []
        sources: list[str] = []
        core = company.sector or ""
        if self._scraper and company.website:
            scraped = self._scraper.scrape(company.website)
            sources = scraped.sources
            signals = [f"{s['signaal']} ({s['bron']})" for s in scraped.signals]
            if scraped.pages and scraped.pages[0].description:
                core = scraped.pages[0].description[:120]
        if fleet.count:
            signals.insert(0, f"{fleet.as_text()} (bron: eigen wagenparkdata)")
        opener = (
            "Geen specifieke aanleiding gevonden — open met een algemene vraag naar het wagenpark."
        )
        if fleet.next_contract_end:
            # Do not reveal what our fleet data says; ask so they can tell us themselves.
            opener = (
                "Veel bedrijven kijken opnieuw naar hun wagenpark als er een leasecontract "
                "afloopt; speelt dat bij u de komende tijd?"
            )
        elif fleet.oldest_build_year and fleet.oldest_build_year <= 2019:
            opener = (
                "Bij oudere bedrijfswagens komt vervanging vaak in beeld; speelt dat bij u "
                "binnenkort?"
            )
        text = (
            "Beperkte publieke informatie beschikbaar over dit bedrijf. "
            f"{company.name} is gevestigd in {company.city or 'een onbekende plaats'}"
            f"{' en actief in de sector ' + company.sector.lower() if company.sector else ''}. "
            f"Wagenpark volgens onze data: {fleet.as_text()}"
        )
        return ResearchResult(
            ResearchProfile(
                profiel=text,
                gespreksopener=opener,
                kernactiviteit=core,
                omvang="",
                wagenparksignalen=signals,
                bronnen=sources,
            ),
            cost_eur=0.0,
            prompt_version=self.prompt_version,
            detail="mock",
        )


# USD per million tokens and per 1,000 searches; converted at a fixed rate.
# Check https://platform.claude.com/docs/en/about-claude/pricing before relying on it.
PRICES_USD = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
WEB_SEARCH_USD_PER_1000 = 10.0
USD_TO_EUR = 0.92


class ClaudeResearchProvider:
    def __init__(self, api_key: str, model: str, *, client=None, max_searches: int = 6) -> None:
        import anthropic

        self._client = client or anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_searches = max_searches
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        self._system = prompt + EXTRA_FIELDS_INSTRUCTION
        # Stable across runs (str hash() is randomised per process).
        digest = hashlib.sha256(self._system.encode("utf-8")).hexdigest()[:8]
        self.prompt_version = f"research-{digest}"

    def research(self, company: Company) -> ResearchResult:
        import anthropic

        user = (
            f"Bedrijfsnaam: {company.name}\nKvK-nummer: {company.kvk_number}\n"
            f"Plaats/vestigingsadres: {company.address or ''} {company.city or ''}\n"
            f"Sector: {company.sector or 'onbekend'}\nWebsite: {company.website or 'onbekend'}"
        )
        messages = [{"role": "user", "content": user}]
        tools = [
            {
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": self._max_searches,
                "user_location": {"type": "approximate", "country": "NL"},
            },
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 4},
        ]
        try:
            for _ in range(4):  # server tools may pause a long turn; resume it
                with self._client.messages.stream(
                    model=self._model,
                    max_tokens=16000,
                    system=self._system,
                    output_config={"effort": "medium"},
                    tools=tools,
                    messages=messages,
                ) as stream:
                    message = stream.get_final_message()
                if message.stop_reason != "pause_turn":
                    break
                messages = [*messages, {"role": "assistant", "content": message.content}]
        except anthropic.RateLimitError as exc:
            raise ResearchError("Anthropic: limiet bereikt, probeer later opnieuw") from exc
        except anthropic.APIStatusError as exc:
            raise ResearchError(f"Anthropic gaf status {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise ResearchError("Anthropic is niet bereikbaar") from exc
        if message.stop_reason == "refusal":
            raise ResearchError("Model weigerde dit verzoek")
        text = "".join(b.text for b in message.content if b.type == "text")
        profile = parse_profile_json(text)
        usage = message.usage
        price_in, price_out = PRICES_USD.get(self._model, (4.0, 20.0))
        searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
        usd = (
            usage.input_tokens * price_in
            + (getattr(usage, "cache_read_input_tokens", 0) or 0) * price_in * 0.1
            + usage.output_tokens * price_out
        ) / 1_000_000 + searches * WEB_SEARCH_USD_PER_1000 / 1000
        return ResearchResult(
            profile,
            cost_eur=round(usd * USD_TO_EUR, 4),
            prompt_version=self.prompt_version,
            detail=f"{usage.input_tokens} in, {usage.output_tokens} uit, {searches} zoekopdrachten",
        )


def latest_profile(session: Session, company: Company) -> Profile | None:
    return session.scalar(
        select(Profile)
        .where(Profile.company_id == company.id)
        .order_by(Profile.created_at.desc(), Profile.id.desc())
        .limit(1)
    )


@dataclass
class BatchReport:
    done: int = 0
    skipped_fresh: int = 0
    failed: list[tuple[str, str]] = None  # (kvk, reason)
    cost_eur: float = 0.0

    def __post_init__(self):
        self.failed = self.failed or []

    def render(self) -> str:
        lines = [
            f"Onderzocht: {self.done}, nog vers (overgeslagen): {self.skipped_fresh}, "
            f"mislukt: {len(self.failed)}, kosten: EUR {self.cost_eur:.2f}"
        ]
        lines += [f"  KvK {kvk}: {reason}" for kvk, reason in self.failed]
        return "\n".join(lines)


def run_research(
    session: Session,
    companies: list[Company],
    provider: ResearchProvider,
    *,
    max_age_days: int = 30,
    force: bool = False,
) -> BatchReport:
    """Research companies in batch. Fresh profiles are kept; failures are reported."""
    report = BatchReport()
    cutoff = utcnow() - timedelta(days=max_age_days)
    for company in companies:
        current = latest_profile(session, company)
        if current and not force:
            created = current.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=cutoff.tzinfo)
            if created >= cutoff:
                report.skipped_fresh += 1
                continue
        try:
            result = provider.research(company)
        except ResearchError as exc:
            report.failed.append((company.kvk_number, str(exc)))
            continue
        session.add(
            Profile(
                company_id=company.id,
                data=result.profile.model_dump(),
                sources=result.profile.bronnen,
                prompt_version=result.prompt_version,
            )
        )
        session.add(
            CostEntry(
                kind="research",
                amount_eur=result.cost_eur,
                company_id=company.id,
                detail=result.detail,
            )
        )
        if company.status is CompanyStatus.NEW:
            company.status = CompanyStatus.RESEARCH_DONE
        report.done += 1
        report.cost_eur += result.cost_eur
    session.flush()
    return report


def research_candidates(
    session: Session,
    *,
    limit: int,
    campaign_id: int | None = None,
    max_age_days: int = 30,
) -> list[Company]:
    """Callable leads without a fresh profile, at most ``limit``.

    Research costs money per company with a real key, so it only runs for leads that
    passed the compliance gate in a prepared round, and never for a whole file at once.
    """
    from salesagent.models import CampaignLead, LeadStatus

    cutoff = utcnow() - timedelta(days=max_age_days)
    fresh = select(Profile.company_id).where(Profile.created_at >= cutoff)
    query = (
        select(Company)
        .join(CampaignLead, CampaignLead.company_id == Company.id)
        .where(CampaignLead.status == LeadStatus.CALLABLE, Company.id.not_in(fresh))
        .order_by(CampaignLead.id)
        .distinct()
        .limit(limit)
    )
    if campaign_id is not None:
        query = query.where(CampaignLead.campaign_id == campaign_id)
    return list(session.scalars(query))


def research_provider_from_settings(settings) -> ResearchProvider:
    if settings.anthropic_api_key:
        return ClaudeResearchProvider(settings.anthropic_api_key, settings.anthropic_model)
    return MockResearchProvider()

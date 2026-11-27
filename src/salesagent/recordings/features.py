"""Per-call features, extracted WITHOUT the outcome.

The extractor only ever receives the (pseudonymised) transcript. Giving the
model the outcome would make it rationalise the call afterwards ("the agent
closed strongly") and we would measure our own label back. Talk share and
interruptions come from timestamps, not from a model.
"""

import json
import re
from typing import Protocol

from salesagent.voice import nlu
from salesagent.voice.library import load_library

FLEET_WORDS = (
    "wagenpark",
    "bus",
    "bussen",
    "lease",
    "contract",
    "auto's",
    "voertuig",
    "bestelwagen",
    "elektrisch",
)
VALUE_WORDS = (
    "voordeel",
    "besparen",
    "bespaart",
    "goedkoper",
    "voordeliger",
    "fiscaal",
    "bijtelling",
    "onderhoud",
    "zorgeloos",
    "subsidie",
)
DAY_WORDS = tuple(nlu.WEEKDAYS) + ("morgen", "volgende week", "uur", "ochtend", "middag")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "opening_type",
        "mentions_brand",
        "mentions_fleet",
        "decision_maker_confirmed",
        "objections",
        "closing_type",
        "closing_attempts",
    ],
    "properties": {
        "opening_type": {
            "enum": ["aanleiding_wagenpark", "referentie", "vraag", "pitch", "onbekend"]
        },
        "mentions_brand": {"type": "boolean"},
        "mentions_fleet": {"type": "boolean"},
        "decision_maker_confirmed": {"type": "boolean"},
        "objections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["category", "response_type", "continued"],
                "properties": {
                    "category": {"type": "string"},
                    "response_type": {
                        "enum": [
                            "doorvragen",
                            "waarde",
                            "afspraakvoorstel",
                            "accepteren",
                            "herhalen_pitch",
                            "onbekend",
                        ]
                    },
                    "continued": {"type": "boolean"},
                },
            },
        },
        "closing_type": {"enum": ["concreet_voorstel", "open_vraag", "geen", "onbekend"]},
        "closing_attempts": {"type": "integer"},
    },
}


class FeatureExtractor(Protocol):
    name: str

    def extract(self, turns: list[dict]) -> dict: ...


def response_type(agent_text: str) -> str:
    t = nlu.normalize(agent_text)
    if any(d in t for d in DAY_WORDS) and ("?" in t or "past" in t or "voorstel" in t):
        return "afspraakvoorstel"
    if any(w in t for w in VALUE_WORDS):
        return "waarde"
    if t.endswith("?") or "?" in t:
        return "doorvragen"
    if any(w in t for w in ("begrijp", "prima", "geen probleem", "jammer", "fijne dag")):
        return "accepteren"
    if any(w in t for w in FLEET_WORDS):
        return "herhalen_pitch"
    return "onbekend"


def timing_features(turns: list[dict], duration_s: float | None) -> dict:
    """Computed from timestamps: talk share agent, interruptions, length."""
    durations = {"agent": 0.0, "user": 0.0}
    interruptions = 0
    for index, turn in enumerate(turns):
        spoken = len(turn["message"].split()) / 2.2 + 1.0
        end = turns[index + 1]["t"] if index + 1 < len(turns) else turn["t"] + spoken
        length = max(0.0, min(end - turn["t"], spoken))
        durations[turn["role"] if turn["role"] in durations else "user"] += length
        if index and turn["role"] != turns[index - 1]["role"] and length < 0.4:
            interruptions += 1
    total = sum(durations.values()) or 1
    return {
        "talk_share_agent": round(durations["agent"] / total, 2),
        "interruptions": interruptions,
        "longer_than_60s": bool(duration_s and duration_s > 60),
    }


class RuleBasedExtractor:
    name = "regels-v1"

    def __init__(self, library: list[dict] | None = None) -> None:
        self.library = library or load_library(None)

    def extract(self, turns: list[dict]) -> dict:
        agent = [t for t in turns if t["role"] == "agent"]
        first = nlu.normalize(agent[0]["message"]) if agent else ""
        if any(w in first for w in FLEET_WORDS):
            opening = "aanleiding_wagenpark"
        elif "klant" in first or "collega" in first or "andere bedrijven" in first:
            opening = "referentie"
        elif first.endswith("?"):
            opening = "vraag"
        elif first:
            opening = "pitch"
        else:
            opening = "onbekend"
        objections = []
        for index, turn in enumerate(turns):
            if turn["role"] != "user":
                continue
            category = nlu.detect_objection(turn["message"], self.library)
            if not category:
                continue
            reply = next((t["message"] for t in turns[index + 1 :] if t["role"] == "agent"), "")
            later_user = [t for t in turns[index + 1 :] if t["role"] == "user"]
            objections.append(
                {
                    "category": category,
                    "response_type": response_type(reply) if reply else "onbekend",
                    "continued": len(later_user) >= 2,
                }
            )
        decision = False
        for index, turn in enumerate(turns[:-1]):
            if turn["role"] == "agent" and re.search(
                r"beslist|gaat over|verantwoordelijk", turn["message"].lower()
            ):
                answer = turns[index + 1]
                if answer["role"] == "user" and nlu.is_yes(answer["message"]):
                    decision = True
        closings = [
            t for t in agent[1:] if any(d in nlu.normalize(t["message"]) for d in DAY_WORDS)
        ]
        if closings:
            closing = "concreet_voorstel"
        elif any("afspraak" in nlu.normalize(t["message"]) for t in agent):
            closing = "open_vraag"
        else:
            closing = "geen"
        return {
            "opening_type": opening,
            "mentions_brand": "namens" in first or "merk" in first,
            "mentions_fleet": any(w in first for w in FLEET_WORDS),
            "decision_maker_confirmed": decision,
            "objections": objections,
            "closing_type": closing,
            "closing_attempts": len(closings),
        }


class ClaudeExtractor:
    """Same schema, filled by Claude with structured outputs. Transcript only."""

    name = "claude"

    def __init__(self, api_key: str, model: str, client=None) -> None:
        import anthropic

        self._client = client or anthropic.Anthropic(api_key=api_key)
        self._model = model

    def extract(self, turns: list[dict]) -> dict:
        text = "\n".join(
            f"{'AGENT' if t['role'] == 'agent' else 'KLANT'}: {t['message']}" for t in turns
        )
        response = self._client.messages.create(
            model=self._model,
            max_tokens=2000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=(
                "Je analyseert een gepseudonimiseerd transcript van een zakelijk telefoongesprek. "
                "Vul alleen in wat letterlijk uit het transcript blijkt; kies anders 'onbekend'. "
                "Verzin geen citaten. Bezwaarcategorieën: verzet, geen_interesse, net_vernieuwd, "
                "contract_loopt, geen_beslisser, stuur_mail, geen_tijd, tevreden_huidige, prijs, "
                "ai_bezwaar, of een korte eigen omschrijving."
            ),
            messages=[{"role": "user", "content": text}],
        )
        if response.stop_reason == "refusal":
            raise ValueError("Model weigerde")
        return json.loads(next(b.text for b in response.content if b.type == "text"))

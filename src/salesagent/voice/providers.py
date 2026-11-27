"""Voice providers: dry run, sandbox simulator and ElevenLabs.

Hard rule 5: only ``ElevenLabsProvider`` can place a real call, and only when
DRY_RUN=false and the number is not on the test block list.
"""

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

from salesagent.voice.agent import Agent, CallTools
from salesagent.voice.prospect import draw_prospect

ELEVENLABS_URL = "https://api.elevenlabs.io/v1/convai/sip-trunk/outbound-call"


@dataclass
class CallStart:
    conversation_id: str | None
    status: str  # "dry_run", "gestart", "afgerond"
    # For the simulator: the post-call payload in the ElevenLabs webhook shape.
    post_call: dict | None = None


class VoiceProvider(Protocol):
    name: str

    def place_call(
        self,
        request: dict,
        *,
        tools: CallTools,
        library: list[dict],
        variables: dict,
        now: datetime,
    ) -> CallStart: ...


class RealCallBlockedError(Exception):
    """A real call was refused by hard rule 5. Message is Dutch."""


class DryRunProvider:
    """Shows exactly what would be sent. Nothing leaves the machine."""

    name = "dry-run"

    def place_call(self, request, *, tools, library, variables, now) -> CallStart:
        return CallStart(None, "dry_run")


class SimulatorProvider:
    """Sandbox telephony: runs the agent against a simulated prospect.

    Produces the same post-call payload ElevenLabs would send, so the whole
    processing path (classification, objections, booking, costs) is exercised.
    """

    name = "simulator"

    def __init__(self, seed: int | None = None, cost_per_minute_eur: float = 0.20) -> None:
        self.rng = random.Random(seed)
        self.cost_per_minute = cost_per_minute_eur

    def place_call(self, request, *, tools, library, variables, now) -> CallStart:
        conversation_id = f"sim-{variables['attempt_id']}-{self.rng.randrange(10**6):06d}"
        prospect = draw_prospect(self.rng, variables["merk"])
        transcript: list[dict] = []
        clock = 0.0
        if prospect is None:
            analysis = {"outcome": "niet bereikt", "ai_disclosure_given": False}
            duration = self.rng.uniform(20, 35)  # ringing / voicemail
            return CallStart(
                conversation_id,
                "afgerond",
                self._payload(
                    conversation_id, variables, transcript, duration, analysis, connected=False
                ),
            )
        agent = Agent(variables, library, tools, now)
        turn = agent.opening()
        while turn is not None:
            transcript.append({"role": "agent", "message": turn.text, "time_in_call_secs": clock})
            clock += len(turn.text.split()) / 2.6 + 0.6
            reply = prospect.reply(turn)
            if turn.act == "close":
                break
            if reply:
                transcript.append({"role": "user", "message": reply, "time_in_call_secs": clock})
                clock += len(reply.split()) / 2.4 + 0.8
            turn = agent.respond(reply)
        analysis = dict(agent.result, persona=prospect.kind)
        return CallStart(
            conversation_id,
            "afgerond",
            self._payload(
                conversation_id, variables, transcript, clock + 2, analysis, connected=True
            ),
        )

    def _payload(self, conversation_id, variables, transcript, duration, analysis, connected):
        return {
            "type": "post_call_transcription",
            "event_timestamp": 0,
            "data": {
                "agent_id": "simulator",
                "conversation_id": conversation_id,
                "status": "done",
                "transcript": transcript,
                "metadata": {
                    "call_duration_secs": round(duration, 1),
                    "cost_eur": round(duration / 60 * self.cost_per_minute, 4),
                    "connected": connected,
                },
                "analysis": {"structured_result": analysis},
                "conversation_initiation_client_data": {
                    "dynamic_variables": {
                        "attempt_id": variables["attempt_id"],
                        "campaign_id": variables["campaign_id"],
                    }
                },
            },
        }


class ElevenLabsProvider:
    name = "elevenlabs"

    def __init__(
        self,
        api_key: str,
        *,
        dry_run: bool,
        blocked_numbers: set[str],
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._dry_run = dry_run
        self._blocked = blocked_numbers
        self._client = http_client or httpx.Client(timeout=20)

    def place_call(self, request, *, tools, library, variables, now) -> CallStart:
        if self._dry_run:
            raise RealCallBlockedError("DRY_RUN staat aan: er wordt niet echt gebeld.")
        if request["to_number"] in self._blocked:
            raise RealCallBlockedError("Dit nummer staat op de testblokkade.")
        if not self._api_key:
            raise RealCallBlockedError("Geen ELEVENLABS_API_KEY ingesteld.")
        response = self._client.post(
            ELEVENLABS_URL, json=request, headers={"xi-api-key": self._api_key}
        )
        if response.status_code != 200:
            raise RealCallBlockedError(f"ElevenLabs gaf status {response.status_code}")
        body = response.json()
        if not body.get("success", False):
            raise RealCallBlockedError("ElevenLabs kon het gesprek niet starten")
        return CallStart(body.get("conversation_id"), "gestart")

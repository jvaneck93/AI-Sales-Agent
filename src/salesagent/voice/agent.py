"""The call policy of the AI agent, as testable code.

The live agent runs on ElevenLabs with a prompt generated from the same rules
(see ``prompt.py``). This module is the reference implementation: it drives
the sandbox simulator and the offline eval set, so every rule here can be
tested before a prompt change goes live.

Hard rules built in:
- the first sentence says it is an AI and that the call is recorded;
- never a price, discount or delivery time;
- on an opt-out: ask whether it is for this brand only or for everything,
  record the answer, and when unclear block everything;
- appointments only in free slots of the account manager's calendar.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from salesagent.voice import nlu
from salesagent.voice.library import OPT_OUT

ORIGIN_TRIGGERS = (
    "waar heeft u",
    "hoe komt u aan",
    "waar haalt u",
    "hoe kom je aan",
    "waar heb je mijn",
)
WARM_TIMING_WORDS = (
    "binnenkort",
    "dit jaar",
    "komend jaar",
    "volgend jaar",
    "half jaar",
    "aan vervanging toe",
    "zijn we mee bezig",
    "staat gepland",
    "oriënteren",
    "orienteren",
    "uitbreiden",
    "erbij",
)


class CallTools(Protocol):
    """What the agent can do during the call. Mirrors the API tool endpoints."""

    def slot_options(self) -> list[tuple[str, str]]:
        """[(slot_key, spoken text)]"""
        ...

    def book(self, slot_key: str) -> bool: ...

    def register_opt_out(self, scope: str) -> None: ...


@dataclass
class Turn:
    text: str
    act: str  # opening, hook, question:N, objection:<cat>, slots, scope, close, info


@dataclass
class AgentState:
    stage: str = "opening"
    question_index: int = 0
    objections: list[str] = field(default_factory=list)
    pending: str | None = None  # objection category waiting for a follow-up answer
    answers: dict[str, str] = field(default_factory=dict)
    interest: int = 0
    slot_options: list[tuple[str, str]] = field(default_factory=list)
    slot_tries: int = 0
    turns: int = 0


def disclosure_sentence(v: dict) -> str:
    return (
        f"{v['groet']}, u spreekt met {v['agent_naam']}, een AI-assistent die belt namens "
        f"{v['merk']}. Dit gesprek wordt opgenomen voor kwaliteitsdoeleinden."
    )


def first_message(v: dict) -> str:
    return (
        f"{disclosure_sentence(v)} Ik bel kort over het wagenpark van "
        f"{v['bedrijfsnaam'].rstrip('.')}. "
        "Spreek ik met degene die over het wagenpark beslist?"
    )


class Agent:
    MAX_TURNS = 16

    def __init__(self, variables: dict, library: list[dict], tools: CallTools, now: datetime):
        self.v = variables
        self.library = {e["category"]: e for e in library}
        self.library_list = library
        self.tools = tools
        self.now = now
        self.s = AgentState()
        self.result: dict = {
            "outcome": None,
            "ai_disclosure_given": False,
            "decision_maker_reached": False,
            "hung_up_after_disclosure": False,
            "objections": [],
            "opt_out_scope": None,
            "callback_at": None,
            "appointment_slot": None,
            "qualification": {},
            "notes": [],
        }
        self.ended = False

    # --- helpers --------------------------------------------------------
    def _say(self, text: str, act: str) -> Turn:
        self.s.turns += 1
        return Turn(text.replace("{merk}", self.v["merk"]), act)

    def _end(self, outcome: str, text: str, **extra) -> Turn:
        self.result["outcome"] = outcome
        self.result.update(extra)
        self.ended = True
        return self._say(text + " Fijne dag verder!", "close")

    def _questions(self) -> list[str]:
        return list(self.v.get("kwalificatievragen") or [])

    def _callback(self, text: str, default_days: int = 7) -> str:
        moment = nlu.parse_callback(text, self.now) or (self.now + timedelta(days=default_days))
        return moment.isoformat()

    # --- conversation ---------------------------------------------------
    def opening(self) -> Turn:
        self.result["ai_disclosure_given"] = True
        return self._say(first_message(self.v), "opening")

    def respond(self, prospect: str) -> Turn | None:
        if self.ended:
            return None
        text = nlu.normalize(prospect)
        if not text or text == nlu.HANG_UP:
            if self.s.stage == "opening":
                self.result["hung_up_after_disclosure"] = True
            self.result["outcome"] = self.result["outcome"] or (
                "geen interesse" if self.s.stage != "opening" else "opgehangen"
            )
            self.ended = True
            return None
        if self.s.turns >= self.MAX_TURNS:
            return self._end(
                "warm" if self.s.interest > 0 else "geen interesse",
                "Ik wil uw tijd niet langer vragen. Dank u voor het gesprek.",
            )

        if self.s.stage == "scope":
            scope = nlu.opt_out_scope(text, self.v["merk"])
            self.tools.register_opt_out(scope)
            where = f"voor {self.v['merk']}" if scope == "merk" else "helemaal"
            return self._end(
                "verzet",
                f"Dat is genoteerd: we bellen u {where} niet meer. Excuses voor de moeite.",
                opt_out_scope=scope,
            )

        objection = nlu.detect_objection(text, self.library_list)
        if objection == OPT_OUT:
            self._note_objection(objection)
            self.s.stage = "scope"
            return self._say(self.library[OPT_OUT]["responses"][0], "scope")

        if any(t in text for t in ORIGIN_TRIGGERS):
            return self._say(
                "We gebruiken zakelijke gegevens uit het KvK-handelsregister en zakelijke "
                "voertuiggegevens. U kunt altijd aangeven dat u niet meer gebeld wilt worden. "
                + self._resume_line(),
                "info",
            )

        if self.s.pending:
            return self._follow_up(self.s.pending, text, objection)

        if (
            objection
            and objection != "geen_interesse"
            or (objection == "geen_interesse" and self.s.stage in ("opening", "hook", "question"))
        ):
            return self._handle_objection(objection, text)

        if self.s.stage == "slots":
            return self._handle_slot_answer(text)
        if self.s.stage == "opening":
            self.result["decision_maker_reached"] = True
            return self._hook()
        if self.s.stage in ("hook", "question"):
            return self._record_answer(text)
        return self._end("geen interesse", "Dank u wel voor uw tijd.")

    def _resume_line(self) -> str:
        if self.s.stage == "opening":
            return "Spreek ik met degene die over het wagenpark beslist?"
        questions = self._questions()
        if self.s.stage in ("hook", "question") and self.s.question_index < len(questions):
            return questions[self.s.question_index]
        if self.s.stage == "slots" and self.s.slot_options:
            return "Welk moment komt u het best uit?"
        return "Mag ik nog één vraag stellen?"

    def _note_objection(self, category: str) -> None:
        self.s.objections.append(category)
        self.result["objections"].append(category)

    def _hook(self) -> Turn:
        self.s.stage = "question"
        opener = self.v.get("gespreksopener") or ""
        if opener.startswith("Geen specifieke aanleiding"):
            opener = ""
        questions = self._questions()
        pitch = self.v.get("pitch") or ""
        first_q = questions[0] if questions else "Hoe is uw wagenpark nu geregeld?"
        return self._say(" ".join(p for p in (opener, pitch, first_q) if p), "hook")

    def _record_answer(self, text: str) -> Turn:
        questions = self._questions()
        if self.s.question_index < len(questions):
            self.result["qualification"][questions[self.s.question_index]] = text
        if nlu.is_yes(text) or any(w in text for w in WARM_TIMING_WORDS):
            self.s.interest += 1
        moment = nlu.parse_callback(text, self.now)
        if moment and moment - self.now < timedelta(days=365):
            self.s.interest += 1
        self.s.question_index += 1
        if self.s.question_index < len(questions):
            return self._say(questions[self.s.question_index], f"question:{self.s.question_index}")
        if self.s.interest >= 2 or (self.s.interest >= 1 and len(questions) <= 1):
            return self._propose_slots()
        return self._end(
            "geen interesse",
            "Dank u, dan noteer ik dat het nu niet speelt. Bij een volgend vervangmoment "
            "nemen we graag weer contact op.",
        )

    def _propose_slots(self) -> Turn:
        self.s.stage = "slots"
        self.s.slot_options = self.tools.slot_options()[:3]
        am = self.v.get("accountmanager") or "onze accountmanager"
        if not self.s.slot_options:
            return self._end("warm", f"Dan vraag ik {am} om u te bellen voor een afspraak.")
        spoken = [s for _, s in self.s.slot_options[:2]]
        return self._say(
            f"Dan stel ik voor dat {am} langskomt voor een gesprek van een half uur. "
            f"Past {spoken[0]}{' of ' + spoken[1] if len(spoken) > 1 else ''}?",
            "slots",
        )

    def _handle_slot_answer(self, text: str) -> Turn:
        options = self.s.slot_options[:2] if self.s.slot_tries == 0 else self.s.slot_options
        choice = nlu.pick_slot(text, [s for _, s in options])
        if choice is not None:
            key, spoken = options[choice]
            if self.tools.book(key):
                am = self.v.get("accountmanager") or "de accountmanager"
                return self._end(
                    "afspraak",
                    f"Genoteerd: {spoken}, met {am}. U ontvangt een bevestiging per mail.",
                    appointment_slot=key,
                )
            self.result["notes"].append("gekozen moment was net bezet")
        self.s.slot_tries += 1
        if self.s.slot_tries == 1 and len(self.s.slot_options) > 2 and not nlu.is_no(text):
            return self._say(f"Of past {self.s.slot_options[2][1]} beter?", "slots")
        if nlu.is_no(text) and self.s.slot_tries > 1:
            return self._end(
                "warm",
                "Geen probleem. Dan neemt de accountmanager zelf contact met u op om een moment "
                "te kiezen.",
            )
        if self.s.slot_tries >= 2:
            return self._end(
                "warm", "Dan laat ik de accountmanager u bellen om een moment te prikken."
            )
        return self._say("Welke dag zou u wel uitkomen?", "slots")

    def _handle_objection(self, category: str, text: str) -> Turn:
        self._note_objection(category)
        entry = self.library.get(category)
        if category == "prijs":
            return self._say(
                entry["responses"][0] + " " + self._resume_line(), f"objection:{category}"
            )
        if self.s.objections.count(category) > 1:
            return self._give_up(category, text)
        self.s.pending = category
        return self._say(entry["responses"][0], f"objection:{category}")

    def _follow_up(self, category: str, text: str, new_objection: str | None) -> Turn:
        self.s.pending = None
        if new_objection and new_objection not in (category, "geen_interesse"):
            return self._handle_objection(new_objection, text)
        if category == "geen_beslisser":
            self.result["notes"].append(f"juiste persoon volgens gesprek: {text[:120]}")
            return self._end(
                "verkeerde persoon",
                "Dank u, dan probeer ik het op dat moment.",
                callback_at=self._callback(text, 2),
            )
        if category == "geen_tijd":
            return self._end(
                "terugbellen", "Prima, dan bel ik u dan terug.", callback_at=self._callback(text)
            )
        if category in ("net_vernieuwd", "contract_loopt", "tevreden_huidige"):
            moment = nlu.parse_callback(text, self.now)
            if moment or nlu.is_yes(text):
                when = moment or (self.now + timedelta(days=180))
                if when - self.now < timedelta(days=120):
                    self.s.interest += 1
                    return self._propose_slots()
                callback = when - timedelta(days=60) if category == "contract_loopt" else when
                return self._end(
                    "terugbellen",
                    "Dan nemen we rond dat moment weer contact op.",
                    callback_at=max(callback, self.now + timedelta(days=7)).isoformat(),
                )
            return self._end("geen interesse", "Helder, dank u wel voor uw tijd.")
        if category == "stuur_mail":
            if nlu.is_yes(text):
                self.s.interest += 1
                return self._propose_slots()
            self.result["notes"].append("wil informatie per mail")
            return self._end("warm", "Dan stuurt de accountmanager u een korte mail.")
        if category == "ai_bezwaar":
            if nlu.is_yes(text):
                self.result["notes"].append("wil gebeld worden door een mens")
                return self._end("warm", "Dan belt de accountmanager u persoonlijk.")
            return self._end("geen interesse", "Begrepen, dank u wel.")
        if category == "geen_interesse":
            if new_objection == "geen_interesse" or nlu.is_no(text) and not nlu.is_yes(text):
                return self._end("geen interesse", "Dat begrijp ik, dank u wel voor uw tijd.")
            if self.s.stage == "opening":
                self.result["decision_maker_reached"] = True
                return self._hook()
            return self._say(self._resume_line(), "question")
        return self._end("geen interesse", "Dank u wel voor uw tijd.")

    def _give_up(self, category: str, text: str) -> Turn:
        if category in ("geen_tijd", "geen_beslisser"):
            return self._end(
                "terugbellen",
                "Dan probeer ik het een andere keer.",
                callback_at=self._callback(text),
            )
        return self._end("geen interesse", "Dat begrijp ik. Dank u wel voor uw tijd.")

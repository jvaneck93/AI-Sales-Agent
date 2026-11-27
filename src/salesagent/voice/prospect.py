"""Simulated prospects for the sandbox (no real phone calls).

Each persona reacts to the agent's act. The mix is an [AANNAME], tuned so a
round lands near the callcenter benchmark *after* a drop from the AI
disclosure: about 55% reached, of whom roughly 15-20% hang up after the AI
sentence, ~10% warm or better and ~4-5% an appointment. Replace these numbers
with measured ones once real calls run.
"""

import random
from dataclasses import dataclass

from salesagent.voice.agent import Turn
from salesagent.voice.nlu import HANG_UP

REACH_RATE = 0.55
PERSONAS: dict[str, float] = {
    "ophangen_na_ai": 0.17,
    "verzet_merk": 0.03,
    "verzet_alles": 0.02,
    "verzet_onduidelijk": 0.02,
    "verkeerde_persoon": 0.14,
    "druk_terugbellen": 0.11,
    "geen_interesse": 0.20,
    "net_vernieuwd": 0.07,
    "contract_loopt": 0.06,
    "stuur_mail": 0.05,
    "wil_mens": 0.02,
    "geinteresseerd_plant": 0.05,
    "prijsvraag_plant": 0.01,
    "geinteresseerd_geen_moment": 0.05,
}


@dataclass
class Prospect:
    kind: str
    brand: str
    rng: random.Random

    def reply(self, turn: Turn) -> str:
        act, kind = turn.act, self.kind
        if act == "close":
            return ""
        if act == "opening":
            return {
                "ophangen_na_ai": HANG_UP,
                "verzet_merk": f"Nee, voor {self.brand} hoeft u mij niet meer te bellen.",
                "verzet_alles": "Bel me niet meer, ik wil hier niet meer voor gebeld worden.",
                "verzet_onduidelijk": "Bel me niet meer alsjeblieft.",
                "verkeerde_persoon": "Daar ga ik niet over, dat doet mijn collega.",
                "druk_terugbellen": "Ik heb nu echt geen tijd.",
                "geen_interesse": "Ja, dat ben ik, maar ik heb geen interesse.",
                "wil_mens": "Ben jij een robot? Ik praat liever met een echt persoon.",
            }.get(kind, "Ja, dat ben ik. Waar gaat het over?")
        if act == "scope":
            return {
                "verzet_merk": f"Alleen voor {self.brand}.",
                "verzet_alles": "Helemaal niet meer, niemand.",
            }.get(kind, "Ja dat weet ik niet, gewoon niet meer bellen.")
        if act.startswith("objection:"):
            category = act.split(":", 1)[1]
            if category == "geen_beslisser":
                return "Dat is onze directeur, die is volgende week dinsdag weer op kantoor."
            if category == "geen_tijd":
                return self.rng.choice(
                    ["Bel volgende week maar.", "Probeer het donderdag.", "Over twee weken."]
                )
            if category == "geen_interesse":
                return "Nee, het is gewoon goed geregeld zo."
            if category == "ai_bezwaar":
                return "Ja, laat die maar bellen."
            if category == "stuur_mail":
                return "Nee, stuur eerst maar een mail."
            if category in ("net_vernieuwd", "contract_loopt", "tevreden_huidige"):
                return self.rng.choice(
                    [
                        "Over een jaar of twee pas.",
                        "Pas in september.",
                        "Geen idee, niet binnenkort.",
                    ]
                )
            return "Hm, oké."
        if act == "hook":
            return {
                "net_vernieuwd": "We hebben net nieuwe bussen, pas vervangen.",
                "contract_loopt": "Ons contract loopt nog tot 2028.",
                "stuur_mail": "Stuur maar een mailtje.",
                "druk_terugbellen": "Ik heb nu geen tijd, bel later.",
                "verkeerde_persoon": "Dat moet u bij mijn collega vragen.",
                "prijsvraag_plant": "Wat kost dat dan per maand?",
            }.get(kind, self._answer())
        if act.startswith("question") or act == "info":
            return self._answer()
        if act == "slots":
            if kind in ("geinteresseerd_plant", "prijsvraag_plant"):
                day = turn.text.split("Past ")[-1].split()[0] if "Past " in turn.text else ""
                return f"{day.capitalize()} is goed." if day else "De eerste is goed."
            return "Nee, dat komt niet uit. Laat hem maar bellen."
        return "Oké."

    def _answer(self) -> str:
        if self.kind in ("geinteresseerd_plant", "prijsvraag_plant", "geinteresseerd_geen_moment"):
            return self.rng.choice(
                [
                    "Ja, we hebben er zo'n twaalf, een paar zijn aan vervanging toe.",
                    "Ja, dit jaar moeten er drie bussen vervangen worden.",
                    "Ja, elektrisch rijden staat gepland, binnenkort.",
                ]
            )
        if self.kind == "geen_interesse":
            return "Nee, het is goed geregeld."
        return self.rng.choice(["Nee, niet echt.", "Weet ik niet.", "Nee."])


def draw_prospect(rng: random.Random, brand: str) -> Prospect | None:
    """None means nobody picked up (voicemail, no answer)."""
    if rng.random() > REACH_RATE:
        return None
    kinds, weights = zip(*PERSONAS.items(), strict=True)
    return Prospect(rng.choices(kinds, weights)[0], brand, rng)

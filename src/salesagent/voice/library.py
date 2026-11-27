"""Default objection library.

The recordings pipeline replaces the ``evidence`` and can propose better
responses; a person approves them in the dashboard before the agent uses them.
Triggers are lower-case Dutch fragments.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from salesagent.models import ObjectionEntry

OPT_OUT = "verzet"

DEFAULT_OBJECTIONS: list[dict] = [
    {
        "category": OPT_OUT,
        "label": "Wil niet meer gebeld worden",
        "triggers": [
            "bel me niet meer",
            "bel ons niet meer",
            "niet meer bellen",
            "niet meer gebeld",
            "uit uw bestand",
            "uit je bestand",
            "verwijder ons",
            "verwijder mij",
            "haal ons",
            "haal mij",
            "geen telefoontjes",
            "stop met bellen",
            "niet meer benaderen",
        ],
        "responses": [
            "Dat respecteer ik. Mag ik nog één ding vragen: geldt dit alleen voor gesprekken "
            "namens {merk}, of wilt u helemaal niet meer door ons gebeld worden?"
        ],
        "give_up_when": "Altijd direct stoppen na de vraag over de reikwijdte. Bij twijfel: alles.",
    },
    {
        "category": "geen_interesse",
        "label": "Geen interesse",
        "triggers": [
            "geen interesse",
            "hoeft niet",
            "niet nodig",
            "nee dank",
            "niet geïnteresseerd",
        ],
        "responses": [
            "Dat begrijp ik. Mag ik vragen of dat is omdat het wagenpark nu goed geregeld is, "
            "of omdat het gewoon geen goed moment is?",
        ],
        "give_up_when": "Na één doorvraag nog steeds nee: vriendelijk afsluiten.",
    },
    {
        "category": "net_vernieuwd",
        "label": "Net vernieuwd",
        "triggers": [
            "net nieuw",
            "pas vervangen",
            "net geleased",
            "net vervangen",
            "pas nieuwe",
            "net nieuwe",
            "vorig jaar vernieuwd",
        ],
        "responses": [
            "Fijn dat dat net geregeld is. Wanneer verwacht u het volgende vervangmoment? "
            "Dan zorg ik dat we u op een beter moment benaderen.",
        ],
        "give_up_when": "Geen vervangmoment binnen 12 maanden: terugbellen rond dat moment.",
    },
    {
        "category": "contract_loopt",
        "label": "Contract loopt nog",
        "triggers": ["contract loopt", "zit vast", "vast tot", "loopt nog", "looptijd"],
        "responses": [
            "Dat is helder. Juist vóór het einde van een contract loont het om te vergelijken. "
            "Zal de accountmanager een half jaar voor de einddatum contact opnemen?",
        ],
        "give_up_when": "Wil niet eerder praten: terugbellen een half jaar voor einddatum.",
    },
    {
        "category": "geen_beslisser",
        "label": "Niet de beslisser",
        "triggers": [
            "ga ik niet over",
            "niet over",
            "collega",
            "directeur beslist",
            "mijn baas",
            "moet u bij",
            "niet de juiste",
            "verkeerde persoon",
        ],
        "responses": [
            "Dank u. Wie gaat er bij u over het wagenpark, en wanneer is die persoon het best "
            "bereikbaar?",
        ],
        "give_up_when": "Functie en moment genoteerd: afsluiten en terugbellen.",
    },
    {
        "category": "stuur_mail",
        "label": "Stuur maar een mail",
        "triggers": [
            "stuur maar",
            "mail maar",
            "mailtje",
            "per mail",
            "stuur me informatie",
            "stuur informatie",
        ],
        "responses": [
            "Dat kan. Een mail is vaak algemeen; in een kort gesprek van twintig minuten kan de "
            "accountmanager het op uw situatie afstemmen. Zal ik een moment voorstellen?",
        ],
        "give_up_when": "Blijft bij mail: warm met voorkeur mail; accountmanager mailt.",
    },
    {
        "category": "geen_tijd",
        "label": "Geen tijd nu",
        "triggers": [
            "geen tijd",
            "druk",
            "bel later",
            "komt niet uit",
            "slecht moment",
            "andere keer",
            "bel volgende week",
            "bel me later",
        ],
        "responses": [
            "Natuurlijk. Wanneer kan ik u beter terugbellen?",
        ],
        "give_up_when": "Moment genoemd: terugbellen inplannen. Geen moment: over een week.",
    },
    {
        "category": "tevreden_huidige",
        "label": "Tevreden met huidige partij",
        "triggers": [
            "tevreden",
            "vaste dealer",
            "eigen leverancier",
            "vaste partij",
            "goed geregeld",
            "al een leasemaatschappij",
        ],
        "responses": [
            "Goed om te horen. Veel klanten van {merk} waren dat ook en vergeleken alleen bij het "
            "volgende vervangmoment. Wanneer staat dat bij u gepland?",
        ],
        "give_up_when": "Geen moment binnen 12 maanden: afsluiten, volgende ronde opnieuw.",
    },
    {
        "category": "prijs",
        "label": "Vraagt naar prijs of korting",
        "triggers": ["wat kost", "prijs", "te duur", "korting", "tarief", "hoeveel kost"],
        "responses": [
            "Daar kan ik geen uitspraken over doen; prijzen en voorwaarden bespreekt de "
            "accountmanager persoonlijk met u, afgestemd op uw situatie.",
        ],
        "give_up_when": "Nooit een prijs, korting of levertijd noemen.",
    },
    {
        "category": "ai_bezwaar",
        "label": "Wil geen AI / wil een mens",
        "triggers": [
            "robot",
            "computer",
            "met een mens",
            "echt persoon",
            "echte persoon",
            "ben jij een ai",
            "bent u een ai",
            "geen ai",
        ],
        "responses": [
            "Ja, ik ben een AI-assistent. Liever een mens? Dan laat ik de accountmanager u "
            "persoonlijk terugbellen. Is dat goed?",
        ],
        "give_up_when": "Wil ook geen terugbelverzoek: vriendelijk afsluiten.",
    },
]


def seed_objection_library(session: Session) -> int:
    """Insert missing default categories. Existing entries are left alone."""
    existing = set(session.scalars(select(ObjectionEntry.category)))
    added = 0
    for entry in DEFAULT_OBJECTIONS:
        if entry["category"] in existing:
            continue
        session.add(ObjectionEntry(**entry, approved=True, source="standaard"))
        added += 1
    session.flush()
    return added


def load_library(session: Session | None) -> list[dict]:
    """Approved entries from the database, falling back to the defaults."""
    if session is not None:
        rows = session.scalars(
            select(ObjectionEntry).where(ObjectionEntry.approved.is_(True))
        ).all()
        if rows:
            return [
                {
                    "category": r.category,
                    "label": r.label,
                    "triggers": r.triggers,
                    "responses": r.responses,
                    "give_up_when": r.give_up_when,
                }
                for r in rows
            ]
    return [dict(e) for e in DEFAULT_OBJECTIONS]

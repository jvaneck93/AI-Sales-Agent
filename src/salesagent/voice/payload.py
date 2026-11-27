"""Dynamic variables and the outbound-call request for the voice platform.

Hard rule 4: no call starts unless the first message contains the AI
disclosure and the recording notice.
"""

from datetime import datetime

from salesagent.enrichment.fleet import summarize_fleet
from salesagent.models import Brand, CallAttempt, Campaign, Company, Profile
from salesagent.voice.agent import first_message

REQUIRED_DISCLOSURE = ("ai-assistent",)
REQUIRED_RECORDING = ("opgenomen",)


class AgentConfigError(Exception):
    """The agent configuration breaks hard rule 4. Message is Dutch."""


def greeting(now: datetime) -> str:
    hour = now.hour
    return "Goedemorgen" if hour < 12 else "Goedemiddag" if hour < 18 else "Goedenavond"


def build_variables(
    *,
    company: Company,
    brand: Brand,
    campaign: Campaign,
    attempt: CallAttempt,
    profile: Profile | None,
    slots: list[tuple[str, str]],
    account_manager: str | None,
    agent_name: str,
    company_name: str,
    now: datetime,
) -> dict:
    fleet = summarize_fleet(company, now.date())
    data = profile.data if profile else {}
    contact_roles = [c.role for c in company.contacts if c.role]
    variables = {
        "attempt_id": attempt.id,
        "campaign_id": campaign.id,
        "groet": greeting(now),
        "agent_naam": agent_name,
        "belbureau": company_name,
        "merk": brand.name,
        "pitch": brand.pitch,
        "kwalificatievragen": list(brand.qualification_questions or []),
        "bedrijfsnaam": company.name,
        "plaats": company.city or "",
        "gewenste_functie": contact_roles[0].lower() if contact_roles else "",
        "gespreksopener": data.get("gespreksopener", ""),
        "kernactiviteit": data.get("kernactiviteit", ""),
        "wagenpark_samenvatting": fleet.as_text(),
        "aantal_voertuigen": fleet.count,
        "eerstvolgend_contract_einde": (
            fleet.next_contract_end.isoformat() if fleet.next_contract_end else ""
        ),
        "afspraakmomenten": [{"sleutel": k, "tekst": t} for k, t in slots],
        "accountmanager": account_manager or "",
        "bronnen": data.get("bronnen", []),
    }
    variables["ai_melding"] = first_message(variables).split(" Ik bel kort")[0]
    variables["eerste_bericht"] = first_message(variables)
    return variables


def check_disclosure(first: str) -> None:
    text = first.lower()
    first_sentences = " ".join(text.split(". ")[:2])
    if not any(word in first_sentences for word in REQUIRED_DISCLOSURE):
        raise AgentConfigError("De eerste zin van het gesprek moet melden dat het een AI is.")
    if not any(word in text for word in REQUIRED_RECORDING):
        raise AgentConfigError("Het begin van het gesprek moet melden dat het wordt opgenomen.")


def elevenlabs_request(
    variables: dict, *, agent_id: str, phone_number_id: str, to_number: str
) -> dict:
    """Body for POST /v1/convai/sip-trunk/outbound-call (see docs/koppelingen.md)."""
    check_disclosure(variables["eerste_bericht"])
    spoken_vars = {
        k: (v if isinstance(v, str | int | float | bool) else _flatten(v))
        for k, v in variables.items()
    }
    return {
        "agent_id": agent_id or "<ELEVENLABS_AGENT_ID>",
        "agent_phone_number_id": phone_number_id or "<ELEVENLABS_PHONE_NUMBER_ID>",
        "to_number": to_number,
        "conversation_initiation_client_data": {
            "dynamic_variables": spoken_vars,
            "conversation_config_override": {
                "agent": {"first_message": variables["eerste_bericht"], "language": "nl"}
            },
        },
    }


def _flatten(value) -> str:
    if isinstance(value, list):
        return " | ".join(v["tekst"] if isinstance(v, dict) else str(v) for v in value)
    return str(value)

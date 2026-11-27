"""One-page briefing for the account manager (phase 6)."""

from salesagent.enrichment.fleet import summarize_fleet
from salesagent.enrichment.research import latest_profile
from salesagent.models import Appointment, CallAttempt, Company
from salesagent.scheduling.calendar import Slot


def build_briefing(session, appointment: Appointment, attempt: CallAttempt | None) -> str:
    company: Company = appointment.company
    fleet = summarize_fleet(company)
    profile = latest_profile(session, company)
    data = profile.data if profile else {}
    slot = Slot(appointment.account_manager_id or 0, appointment.starts_at, appointment.starts_at)
    analysis = (attempt.analysis or {}) if attempt else {}
    lines = [
        f"# Briefing: {company.name}",
        "",
        f"**Afspraak:** {slot.spoken()} · {appointment.location or 'locatie nog af te stemmen'}",
        f"**Gesprekspartner:** {appointment.contact_role or 'beslisser wagenpark'}"
        + (f" ({appointment.contact_name})" if appointment.contact_name else ""),
        f"**KvK:** {company.kvk_number} · {company.legal_form.value} · {company.city or ''}",
        "",
        "## Wagenpark (eigen data)",
        fleet.as_text(),
    ]
    if fleet.lease_companies:
        lines.append(f"Leasemaatschappijen: {', '.join(fleet.lease_companies)}".rstrip(".") + ".")
    lines += ["", "## Profiel"]
    lines.append(data.get("profiel") or "Nog geen profiel beschikbaar.")
    if data.get("wagenparksignalen"):
        lines += ["", "**Signalen:**"] + [f"- {s}" for s in data["wagenparksignalen"]]
    lines += ["", "## Gesprek met de AI-assistent"]
    if analysis.get("qualification"):
        lines += [f'- {q} → "{a}"' for q, a in analysis["qualification"].items()]
    if analysis.get("objections"):
        lines.append(f"- Bezwaren in het gesprek: {', '.join(analysis['objections'])}")
    for note in analysis.get("notes", []):
        lines.append(f"- {note}")
    lines += [
        "",
        "## Voorgestelde insteek",
        _approach(fleet, data, analysis),
        "",
        "## Bronnen",
    ]
    lines += [f"- {u}" for u in (profile.sources if profile else [])] or ["- eigen wagenparkdata"]
    lines += ["", "_De AI-assistent heeft geen prijzen, kortingen of levertijden toegezegd._"]
    return "\n".join(lines)


def _approach(fleet, data, analysis) -> str:
    if fleet.next_contract_end:
        return (
            f"Het eerstvolgende contract loopt af op {fleet.next_contract_end:%d-%m-%Y}. "
            "Begin bij de vervangplanning en vergelijk met de huidige voorwaarden."
        )
    if fleet.oldest_build_year and fleet.oldest_build_year <= 2019:
        return f"Oudste voertuig uit {fleet.oldest_build_year}: start bij vervanging en TCO."
    if data.get("gespreksopener") and not data["gespreksopener"].startswith("Geen specifieke"):
        return data["gespreksopener"]
    return "Vraag naar de plannen met het wagenpark de komende 12 maanden."

"""Facts about a company's fleet, computed from our own vehicle data."""

from collections import Counter
from dataclasses import dataclass
from datetime import date

from salesagent.models import Company


@dataclass
class FleetSummary:
    count: int
    makes: dict[str, int]
    avg_build_year: float | None
    oldest_build_year: int | None
    lease_count: int
    lease_companies: list[str]
    next_contract_end: date | None

    def as_text(self) -> str:
        if not self.count:
            return "Geen voertuigen bekend in onze data."
        makes = ", ".join(f"{n}x {m}" for m, n in self.makes.items())
        parts = [f"{self.count} voertuigen ({makes})"]
        if self.avg_build_year:
            parts.append(f"gemiddeld bouwjaar {self.avg_build_year:.0f}")
        if self.lease_count:
            parts.append(f"{self.lease_count} geleased")
        if self.next_contract_end:
            parts.append(f"eerstvolgende contract loopt af op {self.next_contract_end:%d-%m-%Y}")
        return ", ".join(parts) + "."


def summarize_fleet(company: Company, today: date | None = None) -> FleetSummary:
    today = today or date.today()
    vehicles = company.vehicles
    years = [v.build_year for v in vehicles if v.build_year]
    ends = [
        v.contract_end_date
        for v in vehicles
        if v.contract_end_date and v.contract_end_date >= today
    ]
    return FleetSummary(
        count=len(vehicles),
        makes=dict(Counter(v.make or "onbekend" for v in vehicles).most_common()),
        avg_build_year=sum(years) / len(years) if years else None,
        oldest_build_year=min(years) if years else None,
        lease_count=sum(1 for v in vehicles if (v.ownership or "").lower() == "lease"),
        lease_companies=sorted({v.lease_company for v in vehicles if v.lease_company}),
        next_contract_end=min(ends) if ends else None,
    )

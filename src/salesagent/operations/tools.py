"""What the agent can do during a call, bound to one call attempt.

The simulator calls this directly; the live ElevenLabs agent reaches the same
code through the tool endpoints in the API.
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from salesagent.compliance.register import register_objection
from salesagent.models import (
    AccountManager,
    Brand,
    CallAttempt,
    Campaign,
    Company,
    Consent,
)
from salesagent.scheduling.calendar import (
    CalendarAdapter,
    Slot,
    SlotTakenError,
    parse_slot_key,
    proposed_slots,
)


class SessionCallTools:
    def __init__(
        self,
        session: Session,
        *,
        attempt: CallAttempt,
        company: Company,
        campaign: Campaign,
        calendar: CalendarAdapter,
        account_manager: AccountManager | None,
        now: datetime,
    ) -> None:
        self.session = session
        self.attempt = attempt
        self.company = company
        self.campaign = campaign
        self.calendar = calendar
        self.am = account_manager
        self.now = now
        self.booked = None

    def slot_options(self) -> list[tuple[str, str]]:
        if self.am is None:
            return []
        return [(s.key, s.spoken()) for s in proposed_slots(self.calendar, self.am, self.now)]

    def book(self, slot_key: str) -> bool:
        if self.booked is not None:
            return True
        am_id, starts = parse_slot_key(slot_key)
        am = self.session.get(AccountManager, am_id)
        if am is None or am.brand_id != self.campaign.brand_id:
            return False
        slot = Slot(am_id, starts, starts + timedelta(minutes=am.slot_minutes))
        contact = self.company.contacts[0] if self.company.contacts else None
        try:
            self.booked = self.calendar.book(
                slot,
                company=self.company,
                campaign_id=self.campaign.id,
                call_attempt_id=self.attempt.id,
                contact_role=contact.role if contact else None,
                contact_name=contact.name if contact else None,
            )
        except SlotTakenError:
            return False
        return True

    def register_opt_out(self, scope: str) -> Consent:
        brand: Brand | None = self.campaign.brand if scope == "merk" else None
        return register_objection(
            self.session,
            source=f"AI-gesprek {self.attempt.id}",
            company=self.company,
            brand=brand,
            evidence=f"gespreks-ID {self.attempt.external_call_id or self.attempt.id}; "
            f"reikwijdte: {'alleen ' + brand.name if brand else 'alle merken'}",
        )

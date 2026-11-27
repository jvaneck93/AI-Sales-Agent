"""Calendar adapter for account managers (phase 6).

The agenda/CRM of the client is still an open decision, so this is an
interface with a local, database-backed implementation. A Google or Microsoft
adapter only needs ``free_slots`` and ``book``.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from salesagent.models import AccountManager, Appointment, Brand, Company

TZ = ZoneInfo("Europe/Amsterdam")
DAYS_NL = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MONTHS_NL = [
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
]


@dataclass(frozen=True)
class Slot:
    account_manager_id: int
    starts_at: datetime  # timezone-aware, Europe/Amsterdam
    ends_at: datetime

    @property
    def key(self) -> str:
        return f"{self.account_manager_id}@{self.starts_at.isoformat()}"

    def spoken(self) -> str:
        """Dutch text the agent can read out: 'dinsdag 2 december om 10:00'."""
        s = self.starts_at.astimezone(TZ)
        return f"{DAYS_NL[s.weekday()]} {s.day} {MONTHS_NL[s.month - 1]} om {s:%H:%M}"


class SlotTakenError(Exception):
    """The slot is no longer free. Message is Dutch."""


class CalendarAdapter(Protocol):
    def free_slots(self, am: AccountManager, start: datetime, days: int = 10) -> list[Slot]: ...

    def book(self, slot: Slot, *, company: Company, **fields) -> Appointment: ...


def _hm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=TZ)


class LocalCalendar:
    """Working days within the AM's hours, minus booked appointments.

    The earliest slot is the next working day, so the AM has time to prepare
    and QA can check the call first.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _busy(self, am: AccountManager) -> list[tuple[datetime, datetime]]:
        rows = self._session.scalars(
            select(Appointment).where(
                Appointment.account_manager_id == am.id,
                Appointment.status.not_in(["geannuleerd"]),
            )
        )
        return [
            (_aware(a.starts_at), _aware(a.ends_at or a.starts_at + timedelta(hours=1)))
            for a in rows
        ]

    def free_slots(self, am: AccountManager, start: datetime, days: int = 10) -> list[Slot]:
        busy = self._busy(am)
        local = _aware(start).astimezone(TZ)
        first_day: date = local.date() + timedelta(days=1)
        length = timedelta(minutes=am.slot_minutes)
        slots = []
        for offset in range(days):
            day = first_day + timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            cursor = datetime.combine(day, _hm(am.work_start), TZ)
            end = datetime.combine(day, _hm(am.work_end), TZ)
            while cursor + length <= end:
                slot_end = cursor + length
                if not any(cursor < b_end and slot_end > b_start for b_start, b_end in busy):
                    slots.append(Slot(am.id, cursor, slot_end))
                cursor = slot_end
        return slots

    def book(self, slot: Slot, *, company: Company, **fields) -> Appointment:
        am = self._session.get(AccountManager, slot.account_manager_id)
        if am is None:
            raise SlotTakenError("Onbekende accountmanager")
        free = {s.starts_at for s in self.free_slots(am, slot.starts_at - timedelta(days=1), 2)}
        if slot.starts_at not in free:
            raise SlotTakenError("Dit moment is niet meer vrij")
        appointment = Appointment(
            company_id=company.id,
            starts_at=slot.starts_at,
            ends_at=slot.ends_at,
            account_manager_id=am.id,
            account_manager=am.name,
            brand_id=am.brand_id,
            location=fields.pop("location", None)
            or ", ".join(p for p in (company.address, company.city) if p)
            or None,
            **fields,
        )
        self._session.add(appointment)
        self._session.flush()
        return appointment


def pick_account_manager(session: Session, brand: Brand, company: Company) -> AccountManager | None:
    """The AM of this brand whose regions cover the company's city, else the first one."""
    ams = list(
        session.scalars(
            select(AccountManager)
            .where(AccountManager.brand_id == brand.id)
            .order_by(AccountManager.id)
        )
    )
    city = (company.city or "").lower()
    for am in ams:
        if city and any(region.lower() == city for region in am.regions):
            return am
    for am in ams:
        if not am.regions:
            return am
    return ams[0] if ams else None


def proposed_slots(
    calendar: CalendarAdapter, am: AccountManager, now: datetime, count: int = 3
) -> list[Slot]:
    """A few spread-out options: different days, morning and afternoon."""
    free = calendar.free_slots(am, now)
    chosen: list[Slot] = []
    for slot in free:
        if all(slot.starts_at.date() != c.starts_at.date() for c in chosen) and (
            not chosen or (slot.starts_at.hour < 12) != (chosen[-1].starts_at.hour < 12)
        ):
            chosen.append(slot)
        if len(chosen) == count:
            break
    for slot in free:
        if len(chosen) == count:
            break
        if slot not in chosen:
            chosen.append(slot)
    return sorted(chosen, key=lambda s: s.starts_at)


def parse_slot_key(key: str) -> tuple[int, datetime]:
    am_id, iso = key.split("@", 1)
    return int(am_id), datetime.fromisoformat(iso)

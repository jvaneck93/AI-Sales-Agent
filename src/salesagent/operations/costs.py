"""Cost model and cost bookkeeping."""

from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from salesagent.models import CostEntry

COSTS_PATH = Path(__file__).resolve().parents[3] / "config" / "kosten.yaml"


@dataclass(frozen=True)
class CostModel:
    data_per_adres: float = 1.23
    bellen_per_minuut: float = 0.20
    qa_uurtarief: float = 35.0
    qa_minuten_per_afspraak: float = 4
    qa_steekproef: float = 0.10
    qa_minuten_per_steekproef: float = 2
    benchmark_kosten_per_adres: float = 11.0

    @property
    def qa_per_minuut(self) -> float:
        return self.qa_uurtarief / 60


def load_cost_model(path: Path = COSTS_PATH) -> CostModel:
    if not path.exists():
        return CostModel()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = CostModel.__dataclass_fields__
    return CostModel(**{k: float(v) for k, v in raw.items() if k in known})


def add_cost(session: Session, kind: str, amount: float, **fields) -> CostEntry:
    entry = CostEntry(kind=kind, amount_eur=round(amount, 4), **fields)
    session.add(entry)
    return entry


def data_cost_booked(session: Session, company_id: int, campaign_id: int) -> bool:
    return (
        session.scalar(
            select(CostEntry.id).where(
                CostEntry.kind == "data",
                CostEntry.company_id == company_id,
                CostEntry.campaign_id == campaign_id,
            )
        )
        is not None
    )

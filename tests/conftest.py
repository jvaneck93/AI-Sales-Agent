from pathlib import Path

import pytest

from salesagent.db import Base, make_engine, make_session_factory
from salesagent.models import Company, LegalForm, utcnow

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        yield session


@pytest.fixture
def make_company(session):
    counter = iter(range(1, 1000))

    def _make(legal_form: LegalForm = LegalForm.BV, **kwargs) -> Company:
        n = next(counter)
        defaults = {
            "name": f"Testbedrijf {n}",
            "kvk_number": f"{20000000 + n}",
            "phone": f"+3110000{n:04d}",
            "legal_form": legal_form,
        }
        if legal_form is not LegalForm.UNKNOWN:
            defaults |= {"legal_form_source": "csv", "legal_form_checked_at": utcnow()}
        company = Company(**(defaults | kwargs))
        session.add(company)
        session.flush()
        return company

    return _make

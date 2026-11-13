"""KvK lookup behind an interface, so the real API can be plugged in later."""

from typing import Protocol

from salesagent.models import Company, LegalForm, utcnow


class KvkClient(Protocol):
    def get_legal_form(self, kvk_number: str) -> LegalForm | None:
        """Return the legal form for a KvK number, or None when not found."""
        ...


class FakeKvkClient:
    """Test implementation backed by a dictionary."""

    def __init__(self, legal_forms: dict[str, LegalForm] | None = None) -> None:
        self._legal_forms = legal_forms or {}

    def get_legal_form(self, kvk_number: str) -> LegalForm | None:
        return self._legal_forms.get(kvk_number)


def resolve_legal_form(company: Company, client: KvkClient | None) -> LegalForm:
    """Fill in an unknown legal form via the KvK client. A known one is left alone."""
    if company.legal_form is not LegalForm.UNKNOWN or client is None:
        return company.legal_form
    found = client.get_legal_form(company.kvk_number)
    if found is not None and found is not LegalForm.UNKNOWN:
        company.legal_form = found
        company.legal_form_source = "kvk"
        company.legal_form_checked_at = utcnow()
    return company.legal_form

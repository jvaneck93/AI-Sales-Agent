"""KvK lookup behind an interface, so the real API can be plugged in later."""

from typing import Protocol

import httpx

from salesagent.models import Company, LegalForm, utcnow
from salesagent.normalize import normalize_legal_form


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


class KvkError(Exception):
    """The KvK API could not be reached or gave an unexpected answer."""


class KvkApiClient:
    """KvK Basisprofiel API client. See docs/koppelingen.md.

    An unrecognised legal form gives None, so the company stays "unknown" and
    is not called.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.kvk.nl/api/v1",
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = http_client or httpx.Client(timeout=10)
        self._headers = {"apikey": api_key}

    def get_legal_form(self, kvk_number: str) -> LegalForm | None:
        url = f"{self._base_url}/basisprofielen/{kvk_number}"
        try:
            response = self._client.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise KvkError("KvK API is niet bereikbaar") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise KvkError(f"KvK API gaf status {response.status_code}")
        try:
            owner = (response.json().get("_embedded") or {}).get("eigenaar") or {}
        except ValueError as exc:
            raise KvkError("KvK API gaf geen geldige JSON") from exc
        for key in ("rechtsvorm", "uitgebreideRechtsvorm"):
            legal_form = normalize_legal_form(owner.get(key))
            if legal_form is not LegalForm.UNKNOWN:
                return legal_form
        return None


def kvk_client_from_settings(settings) -> KvkClient | None:
    """Return the real client when an API key is configured, else None."""
    if not settings.kvk_api_key:
        return None
    return KvkApiClient(settings.kvk_api_key, settings.kvk_api_base_url)

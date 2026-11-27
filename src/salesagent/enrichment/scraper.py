"""Small, polite website scraper for company information.

Only the company's own website is read (home, about, vacancies, news). No
personal profiles, no LinkedIn. robots.txt is respected. Everything found is
returned with the URL it came from, so nothing ends up in a briefing without
a source (hard rule 9).
"""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

USER_AGENT = "SalesAgentResearch/0.1 (+bedrijfsinformatie; respecteert robots.txt)"
CANDIDATE_PATHS = ("", "over-ons", "about", "vacatures", "werken-bij", "nieuws", "contact")

# Signals that a company needs vehicles. Dutch words, matched on word starts.
FLEET_KEYWORDS = {
    "monteur": "monteurs (servicebussen)",
    "servicemonteur": "servicemonteurs",
    "buitendienst": "buitendienstmedewerkers",
    "vertegenwoordiger": "vertegenwoordigers",
    "accountmanager": "accountmanagers in de buitendienst",
    "chauffeur": "chauffeurs",
    "bezorg": "bezorgdienst",
    "installateur": "installateurs",
    "storingsdienst": "storingsdienst",
    "wagenpark": "eigen wagenpark genoemd",
    "bedrijfsbus": "bedrijfsbussen",
    "bestelbus": "bestelbussen",
    "elektrisch rijden": "interesse in elektrisch rijden",
    "laadpa": "laadpalen",
    "nieuwe vestiging": "nieuwe vestiging",
    "uitbreiding": "uitbreiding",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.links: list[str] = []
        self.title = ""
        self.description = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript", "nav", "footer", "header"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        elif tag == "meta" and attrs.get("name", "").lower() == "description":
            self.description = attrs.get("content", "") or ""

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "nav", "footer", "header") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.parts.append(data.strip())


@dataclass
class PageInfo:
    url: str
    title: str
    description: str
    text: str


@dataclass
class ScrapeResult:
    pages: list[PageInfo] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)  # {"signaal", "bron", "citaat"}
    errors: list[str] = field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        return [p.url for p in self.pages]


def parse_html(url: str, html: str) -> tuple[PageInfo, list[str]]:
    parser = _TextExtractor()
    parser.feed(html)
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    page = PageInfo(url, parser.title.strip(), parser.description.strip(), text[:20000])
    return page, parser.links


def find_signals(page: PageInfo) -> list[dict]:
    found = []
    lower = page.text.lower()
    for keyword, label in FLEET_KEYWORDS.items():
        match = re.search(rf"\b{re.escape(keyword)}", lower)
        if match:
            start = max(0, match.start() - 60)
            quote = page.text[start : match.end() + 60].strip()
            found.append({"signaal": label, "bron": page.url, "citaat": quote})
    return found


class WebsiteScraper:
    def __init__(self, http_client: httpx.Client | None = None, max_pages: int = 5) -> None:
        self._client = http_client or httpx.Client(
            timeout=10, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )
        self._max_pages = max_pages

    def _allowed(self, base: str, url: str) -> bool:
        robots = RobotFileParser()
        try:
            response = self._client.get(urljoin(base, "/robots.txt"))
        except httpx.HTTPError:
            return True
        if response.status_code != 200:
            return True
        robots.parse(response.text.splitlines())
        return robots.can_fetch(USER_AGENT, url)

    def scrape(self, website: str) -> ScrapeResult:
        result = ScrapeResult()
        base = website if website.startswith("http") else f"https://{website}"
        host = urlparse(base).netloc
        seen: set[str] = set()
        for path in CANDIDATE_PATHS:
            if len(result.pages) >= self._max_pages:
                break
            url = urljoin(base.rstrip("/") + "/", path)
            if url in seen:
                continue
            seen.add(url)
            if not self._allowed(base, url):
                result.errors.append(f"robots.txt staat {url} niet toe")
                continue
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                result.errors.append(f"{url}: niet bereikbaar ({type(exc).__name__})")
                continue
            if response.status_code != 200 or "html" not in response.headers.get(
                "content-type", "text/html"
            ):
                continue
            if urlparse(str(response.url)).netloc not in (host, ""):
                continue  # redirected off-site: not the company's own page
            page, _links = parse_html(str(response.url), response.text)
            if any(p.text == page.text for p in result.pages):
                continue
            result.pages.append(page)
            for signal in find_signals(page):
                if signal["signaal"] not in {s["signaal"] for s in result.signals}:
                    result.signals.append(signal)
        return result

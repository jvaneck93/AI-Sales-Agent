"""Build a static, single-file preview of the dashboard with fictional demo data.

Usage: python scripts/build_preview.py [output.html]

Runs the demo on a temporary database, renders the pages with FastAPI's TestClient,
and bundles them as hash-routed views in one HTML file. Forms are disabled in the
preview. Nothing is called for real and no real personal data is involved.
"""

import os
import re
import sys
import tempfile
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LIMITS = {"bedrijven/": 30, "gesprekken/": 30, "afspraken/": 12, "agent/eval/": 2}
MAX_PAGES = 140
SKIP = ("api/", "webhooks/", "demo", "static/")


def _slug(path: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
    return slug or "overzicht"


def _bucket(path: str) -> str | None:
    return next((prefix for prefix in LIMITS if path.startswith(prefix)), None)


def build(output: Path) -> None:
    work = Path(tempfile.mkdtemp(prefix="preview-"))
    os.environ["DATABASE_URL"] = f"sqlite:///{work / 'preview.db'}"
    os.environ["DRY_RUN"] = "true"
    for key in ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "KVK_API_KEY"):
        os.environ[key] = ""

    from fastapi.testclient import TestClient

    from salesagent.api.app import app
    from salesagent.config import get_settings
    from salesagent.db import make_session_factory, upgrade_database
    from salesagent.demo import run_demo
    from salesagent.evals.harness import run_eval, seed_default_cases
    from salesagent.recordings.demo_data import generate
    from salesagent.recordings.features import RuleBasedExtractor
    from salesagent.recordings.pipeline import (
        analyze,
        build_eval_cases,
        build_outputs,
        ingest_export,
    )
    from salesagent.recordings.transcribe import SidecarTranscriber

    get_settings.cache_clear()
    upgrade_database()
    with make_session_factory()() as session:
        run_demo(session)
        export = generate(work / "opnames", n=600)
        ingest_export(
            session,
            export,
            work / "opnames" / "opnames",
            transcriber=SidecarTranscriber(),
            extractor=RuleBasedExtractor(),
            workdir=work / "opnames_werk",
        )
        build_outputs(session, analyze(session))
        build_eval_cases(session)
        seed_default_cases(session)
        run_eval(session)
        session.commit()

    client = TestClient(app)
    pages: dict[str, str] = {}
    counts: dict[str, int] = {}
    queue = deque([""])
    seen = {""}
    href_re = re.compile(r'href="/([^"#]*)"')
    while queue and len(pages) < MAX_PAGES:
        path = queue.popleft()
        response = client.get("/" + path)
        if response.status_code != 200 or "text/html" not in response.headers["content-type"]:
            continue
        pages[path] = response.text
        for link in href_re.findall(response.text):
            link = link.replace("&amp;", "&")
            if link in seen or link.startswith(SKIP):
                continue
            bucket = _bucket(link)
            if bucket:
                if counts.get(bucket, 0) >= LIMITS[bucket]:
                    continue
                counts[bucket] = counts.get(bucket, 0) + 1
            seen.add(link)
            queue.append(link)

    style = re.search(r"<style>(.*?)</style>", pages[""], re.S).group(1)
    style = _theme_aware(style)
    views = []
    for path, html in pages.items():
        title = re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()
        body = re.search(r"<body>(.*?)</body>", html, re.S).group(1)

        def relink(match):
            target = match.group(1).replace("&amp;", "&")
            if target in pages:
                return f'href="#{_slug(target)}"'
            return 'href="#niet-in-preview"'

        body = href_re.sub(relink, body)
        body = body.replace("<form", "<form data-preview")
        views.append(f'<template id="v-{_slug(path)}" data-title="{title}">{body}</template>')
    stamp = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%d-%m-%Y %H:%M")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        PAGE.replace("/*STYLE*/", style)
        .replace("<!--VIEWS-->", "\n".join(views))
        .replace("{{stamp}}", stamp)
        .replace("{{pages}}", str(len(pages))),
        encoding="utf-8",
    )
    print(f"Preview: {output} ({len(pages)} pagina's)")


def _theme_aware(css: str) -> str:
    """Follow the viewer's data-theme as well as the system setting."""

    def repl(match):
        inner = match.group(1)
        forced = inner.replace(":root", ":root[data-theme=dark]")
        system = inner.replace(":root", ":root:not([data-theme=light])")
        return f"@media (prefers-color-scheme: dark) {{{system}}}\n{forced}"

    return re.sub(r"@media \(prefers-color-scheme: dark\) \{(.*?\})\s*\}", repl, css, flags=re.S)


PAGE = """<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI-belbureau preview</title>
<style>
/*STYLE*/
:root{color-scheme:light dark}
:root[data-theme=light]{color-scheme:light}:root[data-theme=dark]{color-scheme:dark}
.preview-bar{background:var(--warn-bg);color:var(--warn);font-size:13px;padding:8px 16px;
  text-align:center;border-bottom:1px solid var(--line)}
.preview-toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);max-width:92vw;
  background:var(--text);color:var(--bg);padding:10px 16px;border-radius:8px;font-size:14px;
  opacity:0;transition:opacity .2s;pointer-events:none}
.preview-toast.show{opacity:1}
@media (prefers-reduced-motion: reduce){.preview-toast{transition:none}}
</style>
</head>
<body>
<div class="preview-bar">Preview met verzonnen demodata, momentopname {{stamp}}
({{pages}} pagina's).
Knoppen en formulieren werken alleen in de lokale app (start.bat).</div>
<div id="app"></div>
<div class="preview-toast" id="toast" role="status"></div>
<template id="v-niet-in-preview" data-title="Niet in de preview"><main>
<h1>Niet in de preview</h1><p class="muted">Deze pagina zit niet in de momentopname. Start de
app lokaal om alles te zien.</p><p><a href="#overzicht">Terug naar het overzicht</a></p>
</main></template>
<!--VIEWS-->
<script>
{
  const app = document.getElementById("app");
  const toast = document.getElementById("toast");
  let timer = null;
  const show = () => {
    const name = (location.hash || "#overzicht").slice(1);
    const view = document.getElementById("v-" + name) || document.getElementById("v-overzicht");
    app.replaceChildren(view.content.cloneNode(true));
    document.title = view.dataset.title;
    window.scrollTo(0, 0);
  };
  document.addEventListener("submit", (event) => {
    event.preventDefault();
    toast.textContent = "In de preview werkt dit niet. Start de app lokaal met start.bat.";
    toast.classList.add("show");
    clearTimeout(timer);
    timer = setTimeout(() => toast.classList.remove("show"), 2600);
  });
  window.addEventListener("hashchange", show);
  show();
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("preview/index.html"))

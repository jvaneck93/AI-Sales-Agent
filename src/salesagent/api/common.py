"""Shared web helpers: session dependency, templates and page rendering."""

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from salesagent.config import get_settings
from salesagent.db import make_session_factory

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_session_factory = None


def get_session():
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory()
    with _session_factory() as session:
        yield session


def eur(value) -> str:
    if value is None:
        return "–"
    return "€ " + f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(value, digits: int = 1) -> str:
    return "–" if value is None else f"{value * 100:.{digits}f}%".replace(".", ",")


def dt(value) -> str:
    if value is None:
        return "–"
    from zoneinfo import ZoneInfo

    if value.tzinfo is None:
        from datetime import UTC

        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo("Europe/Amsterdam")).strftime("%d-%m-%Y %H:%M")


templates.env.filters.update(eur=eur, pct=pct, dt=dt)


def render(request: Request, template: str, *, nav: str = "", **context):
    settings = get_settings()
    base = {
        "root": request.scope.get("root_path", "").rstrip("/"),
        "nav": nav,
        "dry_run": settings.dry_run,
        "message": "",
        "error": "",
        "report": "",
    }
    return templates.TemplateResponse(request, template, base | context)


def markdown(text: str | None):
    """Tiny, safe markdown: headings, bullets, bold, italics. Input is escaped first."""
    import re

    from markupsafe import Markup, escape

    if not text:
        return Markup("")
    html, in_list = [], False
    for raw in str(text).splitlines():
        line = str(escape(raw))
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"(?<![\w*])_(.+?)_(?!\w)", r"<i>\1</i>", line)
        bullet = re.match(r"^(\s*)- (.*)$", line)
        if bullet:
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{bullet.group(2)}</li>")
            continue
        if in_list:
            html.append("</ul>")
            in_list = False
        heading = re.match(r"^(#{1,3}) (.*)$", line)
        if heading:
            level = len(heading.group(1))
            html.append(f"<h{level}>{heading.group(2)}</h{level}>")
        elif line.strip():
            html.append(f"<p>{line}</p>")
    if in_list:
        html.append("</ul>")
    return Markup("\n".join(html))


templates.env.filters["md"] = markdown

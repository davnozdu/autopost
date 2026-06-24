"""Рендер страницы статьи из шаблона сайта (Jinja2) + сборка meta.json.

render_page рендерит конкретную языковую версию (контент уже на нужном языке).
"""

import re
from datetime import datetime, timezone

from jinja2 import Environment, select_autoescape

from app.db.models import Site

_MONTHS = {
    "ru": ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"],
    "uk": ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня",
           "серпня", "вересня", "жовтня", "листопада", "грудня"],
    "cs": ["ledna", "února", "března", "dubna", "května", "června", "července",
           "srpna", "září", "října", "listopadu", "prosince"],
    "en": ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"],
}

_env = Environment(autoescape=select_autoescape(["html", "xml"]))


def _date_human(d: datetime, lang: str) -> str:
    months = _MONTHS.get(lang, _MONTHS["cs"])
    if lang == "en":
        return f"{months[d.month - 1]} {d.day}, {d.year}"
    return f"{d.day} {months[d.month - 1]} {d.year}"


def _strip_scripts(html: str) -> str:
    html = re.sub(r"<\s*(script|style)\b[\s\S]*?<\s*/\s*\1\s*>", "", html, flags=re.I)
    html = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html, flags=re.I)
    return html


def render_page(
    site: Site,
    *,
    lang: str,
    slug: str,
    content: dict,
    image_url: str | None,
    publish_at: datetime | None,
    alternates: list[str],
) -> tuple[str, dict, str]:
    """Вернуть (html, meta_dict, path_base) для языковой версии статьи."""
    when = publish_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    date_iso = when.date().isoformat()
    date_human = _date_human(when, lang)
    url = f"/{lang}/blog/{slug}/"
    annotation = content.get("annotation", "")

    ctx = {
        "lang": lang,
        "slug": slug,
        "title": content.get("title", ""),
        "meta_description": content.get("meta_description") or annotation,
        "keywords": content.get("keywords", ""),
        "tag": content.get("tag", ""),
        "image_url": image_url or "",
        "body_html": _strip_scripts(content.get("body_html", "")),
        "date_iso": date_iso,
        "date_human": date_human,
        "annotation": annotation,
        "alternates": alternates or [],
    }
    html = _env.from_string(site.template).render(**ctx)

    meta = {
        "title": content.get("title", ""),
        "slug": slug,
        "url": url,
        "date": date_iso,
        "date_human": date_human,
        "tag": content.get("tag", ""),
        "annotation": annotation,
        "image": image_url or "",
    }
    path_base = (site.path_pattern or "{lang}/blog/{slug}").format(lang=lang, slug=slug)
    return html, meta, path_base

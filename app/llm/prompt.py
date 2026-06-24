"""Сборка промпта для написания статьи и разбор JSON-ответа модели.

DeepSeek возвращает СТРОГИЙ JSON с контентом. HTML-обвязку (SEO, бэклинк,
JSON-LD) собирает Python-шаблонизатор — модель разметку не трогает.
"""

import json
import re

from app.db.models import AppConfig

ALLOWED_TAGS = "p, h2, h3, ul, ol, li, strong, em, a"


def build_prompt(config: AppConfig, news: dict) -> tuple[str, str]:
    rules = [
        f"Piš výhradně v jazyce: {config.language}.",
        f"Délka článku přibližně {config.chars_per_news} znaků.",
    ]
    if config.images_from_source_only:
        rules.append(
            "Jako obrázek použij VÝHRADNĚ URL ze zdroje (pole 'Obrázek ze zdroje'). "
            "Negeneruj a neměň URL; pokud chybí, vrať prázdný řetězec."
        )
    if config.llm_instructions.strip():
        rules.append(config.llm_instructions.strip())

    rules.append(
        "Vrať POUZE validní JSON bez dalšího textu, přesně v tomto tvaru:\n"
        '{"title": "...", "slug": "...", "annotation": "...", '
        '"meta_description": "...", "keywords": ["...", "..."], '
        '"tag": "...", "body_html": "...", "image_url": "..."}\n'
        "Pravidla polí:\n"
        "- slug: krátký, latinkou, malá písmena, slova oddělená pomlčkou (a-z 0-9 -);\n"
        f"- body_html: HTML těla článku pouze s tagy {ALLOWED_TAGS}; bez <html>/<head>/<h1>; "
        "podnadpisy h2/h3, odstavce p;\n"
        "- annotation: 1–2 věty pro náhled; meta_description: do 160 znaků;\n"
        "- keywords: 4–8 klíčových slov; tag: jedno krátké označení rubriky;\n"
        "- NEzmiňuj zdroj a nevkládej odkaz na zdroj do body_html."
    )

    system = "Jsi profesionální autor SEO článků. " + " ".join(rules)
    user = (
        f"Titulek zdroje: {news.get('title', '')}\n"
        f"URL zdroje: {news.get('link', '')}\n"
        f"Obrázek ze zdroje: {news.get('image') or ''}\n\n"
        f"Text zdroje:\n{news.get('text', '')}"
    )
    return system, user


def parse_article(raw: str, fallback_image: str | None = None) -> dict:
    data = _extract_json(raw) or {}
    body = (data.get("body_html") or data.get("body") or "").strip()
    if not body and not data:
        body = (raw or "").strip()
    title = data.get("title") or "(bez názvu)"
    kw = data.get("keywords")
    if isinstance(kw, list):
        keywords = ", ".join(str(k).strip() for k in kw if str(k).strip())
    else:
        keywords = (kw or "").strip()
    annotation = data.get("annotation") or _text_excerpt(body, 300)
    return {
        "title": title,
        "slug": (data.get("slug") or "").strip(),
        "annotation": annotation,
        "meta_description": (data.get("meta_description") or annotation)[:200],
        "keywords": keywords,
        "tag": (data.get("tag") or "").strip(),
        "body_html": body,
        "image_url": data.get("image_url") or fallback_image,
    }


def _text_excerpt(html: str, n: int) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n]


def _extract_json(raw: str) -> dict | None:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw.strip(), re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None

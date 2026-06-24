"""Сборка промпта для написания статьи и разбор JSON-ответа модели."""

import json
import re

from app.db.models import AppConfig


def build_prompt(config: AppConfig, news: dict) -> tuple[str, str]:
    rules = [
        f"Piš výhradně v jazyce: {config.language}.",
        f"Délka článku přibližně {config.chars_per_news} znaků.",
    ]
    if config.images_from_source_only:
        rules.append(
            "Jako obrázek použij VÝHRADNĚ URL ze zdroje (pole 'Obrázek ze zdroje'). "
            "Negeneruj, neměň ani nevymýšlej URL obrázku; pokud chybí, vrať prázdný řetězec."
        )
    if config.llm_instructions.strip():
        rules.append(config.llm_instructions.strip())
    rules.append(
        'Vrať POUZE validní JSON bez dalšího textu ve tvaru: '
        '{"title": "...", "body": "...", "image_url": "..."}.'
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
    data = _extract_json(raw)
    if not data:
        return {
            "title": "(bez názvu)",
            "body": raw.strip(),
            "image_url": fallback_image,
        }
    return {
        "title": data.get("title") or "(bez názvu)",
        "body": data.get("body") or "",
        "image_url": data.get("image_url") or fallback_image,
    }


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

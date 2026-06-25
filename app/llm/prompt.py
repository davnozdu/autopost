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


def build_select_prompt(
    items: list[dict], limit: int, context: str, avoid: list[str] | None = None
) -> tuple[str, str]:
    """Промпт отбора самых стоящих новостей из списка кандидатов."""
    lines = [f"{it['i']}. {it['title']} — {it['summary'][:160]}" for it in items]
    system = (
        f"Jsi editor zpravodajství webu '{context}'. Z níže uvedených zpráv vyber "
        f"{limit} nejzajímavějších a nejrelevantnějších k publikaci (aktuálnost, "
        f"přínos pro čtenáře). Vyhni se duplicitám mezi vybranými a NEvybírej zprávy, "
        f"které se tématem překrývají s již publikovanými. "
        'Vrať POUZE JSON: {"selected":[čísla zpráv]} bez dalšího textu.'
    )
    user = "Zprávy:\n" + "\n".join(lines)
    if avoid:
        user += "\n\nJiž publikováno (nevybírej podobné):\n" + "\n".join(
            f"- {t}" for t in avoid[:30]
        )
    return system, user


def parse_selection(raw: str, n: int, limit: int) -> list[int] | None:
    data = _extract_json(raw)
    if not data or "selected" not in data:
        return None
    idx = []
    for x in data["selected"]:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if 0 <= i < n and i not in idx:
            idx.append(i)
    return idx[:limit] or None


def build_ig_prompt(config: AppConfig, news: dict) -> tuple[str, str]:
    """Промпт подписи для Instagram-поста по внешней новости (группа rss).

    Возвращает строгий JSON {caption, hashtags}. Тон — живой, как у человека,
    по тем же общим инструкциям, но в формате соцсети (коротко, с эмодзи).
    """
    rules = [
        f"Piš výhradně v jazyce: {config.language}.",
        "Formát: poutavý popisek pro Instagram, 2–4 krátké odstavce, "
        "lehké a lidské podání, klidně 1–3 vhodné emoji (ne přehnaně).",
        "Bez klišé a marketingového balastu; první věta musí zaujmout.",
        "NEzmiňuj zdroj a nevkládej do textu žádné odkazy.",
    ]
    if config.llm_instructions.strip():
        rules.append(config.llm_instructions.strip())
    rules.append(
        "Vrať POUZE validní JSON bez dalšího textu, přesně v tomto tvaru:\n"
        '{"caption": "...", "hashtags": ["...", "..."]}\n'
        "- caption: text popisku bez hashtagů;\n"
        "- hashtags: 5–12 relevantních hashtagů bez znaku #, malými písmeny."
    )
    system = "Jsi zkušený social media manažer. " + " ".join(rules)
    user = (
        f"Titulek zdroje: {news.get('title', '')}\n\n"
        f"Text zdroje:\n{news.get('text', '')}"
    )
    return system, user


def parse_ig_caption(raw: str, fallback_text: str = "") -> str:
    """Собрать готовую подпись (текст + хэштеги) из JSON-ответа модели."""
    data = _extract_json(raw) or {}
    caption = (data.get("caption") or "").strip()
    if not caption:
        caption = (fallback_text or raw or "").strip()
    tags = data.get("hashtags")
    if isinstance(tags, list):
        clean = []
        for t in tags:
            t = str(t).strip().lstrip("#").replace(" ", "")
            if t:
                clean.append("#" + t)
        if clean:
            caption = caption.rstrip() + "\n\n" + " ".join(clean[:12])
    return caption


def build_translate_prompt(content: dict, target_name: str) -> tuple[str, str]:
    """Промпт перевода статьи в целевой язык с сохранением HTML-структуры."""
    system = (
        f"You are a professional translator and SEO editor. Translate the article into "
        f"{target_name}. Keep it natural and idiomatic (not literal), preserve meaning, "
        f"facts and tone. In body_html translate ONLY the text and keep the HTML structure "
        f"and tags ({ALLOWED_TAGS}) intact. Do not mention or add any source. "
        'Return ONLY valid JSON: {"title":"...","annotation":"...","meta_description":"...",'
        '"tag":"...","keywords":["...","..."],"body_html":"..."}.'
    )
    user = json.dumps(
        {
            "title": content.get("title", ""),
            "annotation": content.get("annotation", ""),
            "meta_description": content.get("meta_description", ""),
            "tag": content.get("tag", ""),
            "keywords": content.get("keywords", ""),
            "body_html": content.get("body_html", ""),
        },
        ensure_ascii=False,
    )
    return system, user


def parse_translation(raw: str, fallback: dict) -> dict:
    data = _extract_json(raw) or {}
    kw = data.get("keywords")
    if isinstance(kw, list):
        keywords = ", ".join(str(k).strip() for k in kw if str(k).strip())
    else:
        keywords = (kw or fallback.get("keywords", "")).strip()
    return {
        "title": (data.get("title") or fallback.get("title", "")).strip(),
        "annotation": (data.get("annotation") or fallback.get("annotation", "")).strip(),
        "meta_description": (data.get("meta_description") or fallback.get("meta_description", "")).strip(),
        "tag": (data.get("tag") or fallback.get("tag", "")).strip(),
        "keywords": keywords,
        "body_html": (data.get("body_html") or fallback.get("body_html", "")).strip(),
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

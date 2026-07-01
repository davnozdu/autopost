"""Сборка промпта для написания статьи и разбор JSON-ответа модели.

DeepSeek возвращает СТРОГИЙ JSON с контентом. HTML-обвязку (SEO, бэклинк,
JSON-LD) собирает Python-шаблонизатор — модель разметку не трогает.
"""

import json
import re

from app.db.models import AppConfig

ALLOWED_TAGS = "p, h2, h3, ul, ol, li, strong, em, a"

# Названия языков для жёсткой инструкции (код → как назвать модели).
LANG_NAMES = {
    "ru": "ruštině (русский язык)",
    "cs": "češtině",
    "sk": "slovenčině",
    "en": "angličtině (English)",
    "uk": "ukrajinštině (українська мова)",
    "de": "němčině",
    "pl": "polštině",
    "es": "španělštině",
    "fr": "francouzštině",
}


# Инструкция на САМОМ целевом языке — самый надёжный «замок» против дрейфа
# модели в чешский/английский (язык системного промпта). Дублируем её в начале
# (system) и в конце (user), чтобы язык вывода был зафиксирован с двух сторон.
LANG_NATIVE = {
    "ru": "Пиши весь ответ строго на русском языке. Не используй другие языки.",
    "cs": "Celou odpověď piš výhradně v češtině. Nepoužívej jiný jazyk.",
    "sk": "Celú odpoveď píš výhradne v slovenčine. Nepoužívaj iný jazyk.",
    "en": "Write the entire response strictly in English. Do not use any other language.",
    "uk": "Пиши всю відповідь виключно українською мовою. Не використовуй інші мови.",
    "de": "Schreibe die gesamte Antwort ausschließlich auf Deutsch. Verwende keine andere Sprache.",
    "pl": "Całą odpowiedź pisz wyłącznie po polsku. Nie używaj innego języka.",
    "es": "Escribe toda la respuesta únicamente en español. No uses ningún otro idioma.",
    "fr": "Écris toute la réponse uniquement en français. N'utilise aucune autre langue.",
}


def _lang_code(code: str) -> str:
    return (code or "").strip().lower()[:2]


def _lang_name(code: str) -> str:
    return LANG_NAMES.get(_lang_code(code), code or "ruštině")


def _lang_native(code: str) -> str:
    """Императив «пиши только на этом языке» — на самом целевом языке."""
    return LANG_NATIVE.get(_lang_code(code), LANG_NATIVE["ru"])


def _lang_rule(code: str) -> str:
    """Жёсткое правило языка вывода (чтобы не проскакивала латиница/английский)."""
    name = _lang_name(code)
    return (
        f"KRITICKY DŮLEŽITÉ: celý výstup piš VÝHRADNĚ v jazyce {name}. "
        f"Nepoužívej žádný jiný jazyk — ani angličtinu, ani češtinu (pokud to není "
        f"cílový jazyk). Vše, včetně názvů a hashtagů, musí být v jazyce {name}. "
        # та же инструкция на самом целевом языке — сильнее всего фиксирует язык
        f"{_lang_native(code)}"
    )


def _lang_reminder(code: str) -> str:
    # напоминание в конце user-сообщения — на целевом языке (последнее, что видит модель)
    return f"\n\n{_lang_native(code)}"


def build_prompt(config: AppConfig, news: dict) -> tuple[str, str]:
    rules = [
        _lang_rule(config.language),
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
        + _lang_reminder(config.language)
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


def build_ig_prompt(config: AppConfig, news: dict, language: str | None = None) -> tuple[str, str]:
    """Промпт подписи для Instagram-поста.

    Возвращает строгий JSON {caption, hashtags}. Тон — живой, как у человека,
    по тем же общим инструкциям, но в формате соцсети (коротко, с эмодзи).
    `language` (язык публикации аккаунта) переопределяет глобальный config.language.
    """
    lang = (language or config.language or "ru").strip()
    rules = [
        _lang_rule(lang),
        "Formát: poutavý popisek pro Instagram, 2–4 krátké odstavce, "
        "lehké a lidské podání, klidně 1–3 vhodné emoji (ne přehnaně).",
        "Bez klišé a marketingového balastu; první věta musí zaujmout.",
        "NEzmiňuj zdroj a nevkládej do textu žádné odkazy.",
    ]
    if config.llm_instructions.strip():
        rules.append(config.llm_instructions.strip())
    rules.append(
        "Vrať POUZE validní JSON bez dalšího textu, přesně v tomto tvaru:\n"
        '{"caption": "...", "hashtags": ["...", "..."], "gif_query": "..."}\n'
        "- caption: text popisku bez hashtagů;\n"
        "- hashtags: 5–12 relevantních hashtagů bez znaku #, malými písmeny;\n"
        "- gif_query: JEDNO výstižné ANGLICKÉ klíčové slovo pro vyhledání tematické "
        "animované GIF nálepky k tématu příspěvku (např. horror, comedy, football, "
        "cinema, premiere). Vždy anglicky, malými písmeny, bez #."
    )
    system = "Jsi zkušený social media manažer. " + " ".join(rules)
    user = (
        f"Titulek zdroje: {news.get('title', '')}\n\n"
        f"Text zdroje:\n{news.get('text', '')}"
        + _lang_reminder(lang)
    )
    return system, user


def build_tg_prompt(config: AppConfig, news: dict, language: str | None = None) -> tuple[str, str]:
    """Промпт подписи для Telegram-поста. JSON {caption, hashtags}.

    Telegram позволяет чуть более развёрнутый текст, чем Instagram, и меньше
    тяготеет к хэштегам. `language` — язык публикации аккаунта.
    """
    lang = (language or config.language or "ru").strip()
    rules = [
        _lang_rule(lang),
        "Formát: poutavý příspěvek pro Telegram, 2–5 krátkých odstavců, lidský tón, "
        "klidně 1–2 emoji. První věta musí zaujmout.",
        "Bez klišé. NEzmiňuj zdroj a nevkládej do textu žádné odkazy.",
    ]
    if config.llm_instructions.strip():
        rules.append(config.llm_instructions.strip())
    rules.append(
        "Vrať POUZE validní JSON bez dalšího textu, přesně v tomto tvaru:\n"
        '{"caption": "...", "hashtags": ["...", "..."]}\n'
        "- caption: text příspěvku bez hashtagů;\n"
        "- hashtags: 2–5 relevantních hashtagů bez znaku #, malými písmeny."
    )
    system = "Jsi zkušený editor Telegram kanálu. " + " ".join(rules)
    user = (
        f"Titulek zdroje: {news.get('title', '')}\n\n"
        f"Text zdroje:\n{news.get('text', '')}"
        + _lang_reminder(lang)
    )
    return system, user


def build_x_prompt(config: AppConfig, news: dict, language: str | None = None) -> tuple[str, str]:
    """Промпт твита для X/Twitter. JSON {caption, hashtags}.

    Жёсткий лимит: твит ≤280 символов вместе со ссылкой (она ~23) и хэштегами,
    поэтому просим очень короткий текст. `language` — язык публикации.
    """
    lang = (language or config.language or "ru").strip()
    rules = [
        _lang_rule(lang),
        "Formát: JEDEN tweet pro X (Twitter). Text MAX 200 znaků (zbytek místa je "
        "na odkaz a hashtagy). Úderné, lidské, klidně 1 emoji.",
        "Bez klišé. NEzmiňuj zdroj a nevkládej do textu žádné odkazy.",
    ]
    if config.llm_instructions.strip():
        rules.append(config.llm_instructions.strip())
    rules.append(
        "Vrať POUZE validní JSON bez dalšího textu, přesně v tomto tvaru:\n"
        '{"caption": "...", "hashtags": ["...", "..."]}\n'
        "- caption: text tweetu bez hashtagů, max 200 znaků;\n"
        "- hashtags: 1–3 krátké relevantní hashtagy bez znaku #, malými písmeny."
    )
    system = "Jsi zkušený social media manažer pro X (Twitter). " + " ".join(rules)
    user = (
        f"Titulek zdroje: {news.get('title', '')}\n\n"
        f"Text zdroje:\n{news.get('text', '')}"
        + _lang_reminder(lang)
    )
    return system, user


def build_digest_prompt(
    items: list[dict],
    instructions: str,
    language: str,
    *,
    max_chars: int = 0,
) -> tuple[str, str]:
    """Промпт ОДНОГО итогового поста-дайджеста из списка новостей дня.

    На вход подаём только заголовки + короткие аннотации (не полные тексты) —
    это и есть весь расход токенов на дайджест за день. `instructions` —
    редактируемая пользователем инструкция «что мне нужно получить в итоге».
    Возвращает JSON {caption, hashtags}; разбирается через parse_ig_parts.
    """
    lang = (language or "ru").strip()
    lines = []
    for i, it in enumerate(items, 1):
        title = (it.get("title") or "").strip()
        summary = (it.get("summary") or "").strip()[:220]
        lines.append(f"{i}. {title}" + (f" — {summary}" if summary else ""))

    rules = [
        _lang_rule(lang),
        "Sestav JEDEN souhrnný příspěvek (denní digest) z níže uvedených zpráv pro "
        "sociální síť. Lidský, živý tón, bez klišé a vaty. NEvymýšlej fakta — "
        "vycházej VÝHRADNĚ z dodaných zpráv. NEzmiňuj zdroj a nevkládej do textu odkazy.",
    ]
    if max_chars:
        rules.append(f"Text příspěvku (caption) musí být MAX {max_chars} znaků.")
    if (instructions or "").strip():
        rules.append(
            "Pokyny editora (DODRŽ je co nejpřesněji, mají přednost před formátem výše):\n"
            + instructions.strip()
        )
    rules.append(
        "Vrať POUZE validní JSON bez dalšího textu, přesně v tomto tvaru:\n"
        '{"caption": "...", "hashtags": ["...", "..."]}\n'
        "- caption: text příspěvku bez hashtagů;\n"
        "- hashtags: 3–8 relevantních hashtagů bez znaku #, malými písmeny."
    )
    system = (
        "Jsi zkušený editor, který připravuje denní souhrn (digest) pro sociální sítě. "
        + " ".join(rules)
    )
    user = "Zprávy dne (seřazené od nejaktuálnějších):\n" + "\n".join(lines) + _lang_reminder(lang)
    return system, user


def parse_ig_parts(raw: str, fallback_text: str = "") -> tuple[str, list[str]]:
    """Разобрать ответ модели на (текст подписи, список хэштегов без '#')."""
    data = _extract_json(raw) or {}
    caption = (data.get("caption") or "").strip()
    if not caption:
        caption = (fallback_text or raw or "").strip()
    tags = data.get("hashtags")
    clean: list[str] = []
    if isinstance(tags, list):
        for t in tags:
            t = str(t).strip().lstrip("#").replace(" ", "")
            if t:
                clean.append(t)
    return caption, clean


def build_movie_digest_prompt(
    items: list[dict],
    instructions: str,
    language: str,
    *,
    max_chars: int = 0,
) -> tuple[str, str]:
    """Промпт подборки «Что посмотреть вечером» из торрент-новинок (movies-дайджест).

    На вход — только название/год/тип/рейтинг/жанр (не magnet, не полный текст).
    Один вызов LLM. JSON {caption, hashtags}; разбирается parse_ig_parts.
    """
    lang = (language or "ru").strip()
    lines = []
    for i, it in enumerate(items, 1):
        kind = "сериал" if it.get("is_series") or it.get("omdb_type") == "series" else "фильм"
        year = f" ({it['year']})" if it.get("year") else ""
        rating = f", рейтинг {it['rating']}" if it.get("rating") else ""
        genre = f", жанр: {it['genre']}" if it.get("genre") else ""
        title = it.get("omdb_title") or it.get("title") or it.get("raw_title", "")
        lines.append(f"{i}. {title}{year} — {kind}{rating}{genre}")

    rules = [
        _lang_rule(lang),
        "Napiš KRÁTKÝ poutavý úvod (1 věta, klidně 1 emoji) k večernímu výběru filmových "
        "novinek pro Telegram — sám seznam filmů sestaví aplikace, ty jen úvodní větu. "
        "Lidský, živý tón, bez klišé. NEvypisuj filmy, NEvkládej odkazy.",
    ]
    if (instructions or "").strip():
        rules.append("Pokyny editora (zohledni tón):\n" + instructions.strip())
    rules.append(
        "Vrať POUZE validní JSON: {\"caption\": \"...\", \"hashtags\": [\"...\"]} — "
        "caption = jen úvodní věta bez filmů a bez hashtagů; hashtags 3–6 bez #, malými písmeny."
    )
    system = "Jsi filmový redaktor, který dělá večerní výběr novinek pro Telegram. " + " ".join(rules)
    user = "Dnešní novinky (pro kontext úvodu):\n" + "\n".join(lines) + _lang_reminder(lang)
    return system, user


def parse_gif_query(raw: str) -> str:
    """Достать англ. ключевое слово для GIF из JSON-ответа модели (или '')."""
    data = _extract_json(raw) or {}
    q = str(data.get("gif_query") or "").strip().lstrip("#").lower()
    # оставляем ОДНО латинское слово (лучше всего ищется в Giphy)
    m = re.search(r"[a-z][a-z0-9]{2,}", q)
    return m.group(0) if m else ""


def build_shorten_prompt(text: str, max_chars: int, language: str) -> tuple[str, str]:
    """Промпт сокращения текста подписи под лимит символов (без JSON)."""
    system = (
        f"Zkrať následující text do max {max_chars} znaků. Zachovej smysl, hlavní "
        f"fakta a přirozený, lidský tón. {_lang_rule(language)} "
        f"Vrať POUZE zkrácený text, bez uvozovek a komentářů."
    )
    return system, text + _lang_reminder(language)


def build_translate_prompt(content: dict, target_name: str,
                           lang_code: str = "") -> tuple[str, str]:
    """Промпт перевода статьи в целевой язык с сохранением HTML-структуры."""
    native = _lang_native(lang_code) if lang_code else ""
    system = (
        f"You are a professional translator and SEO editor. Translate the article into "
        f"{target_name}. Keep it natural and idiomatic (not literal), preserve meaning, "
        f"facts and tone. In body_html translate ONLY the text and keep the HTML structure "
        f"and tags ({ALLOWED_TAGS}) intact. Do not mention or add any source. "
        f"CRITICAL: every output field must be ENTIRELY in {target_name}; do not leave "
        f"any sentence in the original language. {native} "
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

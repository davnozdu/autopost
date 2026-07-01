"""Поиск анимированных стикеров Giphy по теме — для GIF-стикеров на сториз.

Instagram-овые GIF-стикеры — это стикеры Giphy (по их id). Берём прозрачные
анимированные стикеры (endpoint /stickers/search), чтобы они органично ложились
поверх фото, а не прямоугольным гифом с фоном. Ключ — бесплатный (api.giphy.com).

Возвращаем СПИСОК id (перемешанный), чтобы стикер каждый раз был разным; «по теме»
обеспечивает поисковый запрос (бренд/тех-термин из хэштегов или заголовка).
"""

import random
import re

import httpx

GIPHY_SEARCH = "https://api.giphy.com/v1/stickers/search"

# запасные темы, если из материала не удалось вытащить осмысленный запрос
_FALLBACK = ["technology", "smartphone", "gadget", "phone", "tech", "news"]
# слова-пустышки, которые не годятся как запрос к Giphy
_STOP = {
    "the", "and", "for", "with", "this", "that", "from", "your", "you",
    "новый", "новая", "новые", "как", "для", "что", "это", "его", "все",
    "nový", "nová", "nové", "jak", "pro", "vše",
}


def _ascii_terms(text: str) -> list[str]:
    """Латинские слова (бренды/тех-термины) из текста — лучше всего ищутся в Giphy."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text or "")
    out = []
    for w in words:
        lw = w.lower()
        if lw in _STOP or lw in out:
            continue
        out.append(lw)
    return out


def build_query(hashtags: list[str] | None, title: str = "", fallback: str = "") -> str:
    """Запрос к Giphy «по теме»: латинский хэштег/термин, иначе — из заголовка,
    иначе — из заданной темы аккаунта (`fallback`, напр. «cinema movie»), а в
    последнюю очередь — общий запасной список."""
    for t in hashtags or []:
        t = (t or "").lstrip("#").strip().lower()
        if t and re.fullmatch(r"[a-z0-9]{3,}", t) and t not in _STOP:
            return t
    terms = _ascii_terms(title)
    if terms:
        return terms[0]
    theme_terms = _ascii_terms(fallback)
    if theme_terms:
        return random.choice(theme_terms)
    return random.choice(_FALLBACK)


def search_sticker_ids(api_key: str, query: str, lang: str = "en",
                       limit: int = 25) -> list[str]:
    """Найти id анимированных стикеров Giphy по запросу; вернуть перемешанный список.

    Пустой ключ/запрос или сетевой сбой → пустой список (гифки просто не добавим).
    """
    api_key = (api_key or "").strip()
    query = (query or "").strip()
    if not api_key or not query:
        return []
    params = {
        "api_key": api_key, "q": query, "limit": limit,
        "rating": "pg-13", "lang": (lang or "en")[:2] or "en",
        "bundle": "messaging_non_clips",
    }
    try:
        r = httpx.get(GIPHY_SEARCH, params=params, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", []) or []
    except Exception:
        return []
    ids = [d.get("id") for d in data if d.get("id")]
    random.shuffle(ids)
    return ids

"""Клиент Brave Search (News API) — ранжирование новостей по актуальности.

Используется дайджестом, чтобы НЕ тратить токены LLM на отбор: Brave по теме
дайджеста отдаёт самые свежие/релевантные заголовки, по которым мы и ранжируем
собранный из RSS пул. Один HTTP-запрос на прогон, токены LLM не расходуются.

Бесплатный ключ: https://api.search.brave.com/ (план «Free»). Заголовок
аутентификации — X-Subscription-Token.
"""

import httpx

BRAVE_NEWS_URL = "https://api.search.brave.com/res/v1/news/search"

# Brave принимает 2-буквенные коды; нашу метку UA приводим к uk.
_LANG_MAP = {"ua": "uk"}


def _blang(code: str) -> str:
    code = (code or "ru").strip().lower()[:2]
    return _LANG_MAP.get(code, code)


def search_titles(
    api_key: str,
    query: str,
    *,
    count: int = 20,
    freshness: str = "pd",
    lang: str = "ru",
    country: str = "cz",
    timeout: int = 15,
) -> list[str]:
    """Вернуть список «заголовок + описание» свежих новостей по теме (или []).

    Пустой ключ/запрос или любая ошибка → пустой список (вызывающий код тогда
    ранжирует пул только по свежести). Токены LLM здесь не задействованы.
    """
    if not api_key.strip() or not query.strip():
        return []
    if freshness not in ("pd", "pw", "pm", "py"):
        freshness = "pd"
    params = {
        "q": query.strip(),
        "count": max(1, min(int(count or 20), 50)),
        "freshness": freshness,
        "search_lang": _blang(lang),
        "country": (country or "cz").lower()[:2],
        "spellcheck": "0",
    }
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key.strip(),
    }
    try:
        resp = httpx.get(BRAVE_NEWS_URL, params=params, headers=headers, timeout=timeout)
        if resp.status_code >= 400:
            return []
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []

    out: list[str] = []
    for item in (data.get("results") or []):
        title = (item.get("title") or "").strip()
        desc = (item.get("description") or "").strip()
        blob = (title + " " + desc).strip()
        if blob:
            out.append(blob)
    return out

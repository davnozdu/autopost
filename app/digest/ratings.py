"""Обогащение фильма/сериала рейтингом и постером через OMDb (omdbapi.com).

Бесплатный ключ. Ищем по imdbid (если Torznab его дал) или по названию+году.
Токены LLM не тратятся. Любая ошибка — молча пропускаем (останется без рейтинга).
"""

import httpx

OMDB_URL = "https://www.omdbapi.com/"


def enrich(item: dict, api_key: str, timeout: int = 15) -> None:
    """Дополнить item полями rating/poster/omdb_title/omdb_type (на месте)."""
    api_key = (api_key or "").strip()
    if not api_key:
        return
    params = {"apikey": api_key}
    if item.get("imdbid"):
        params["i"] = item["imdbid"]
    elif item.get("title"):
        params["t"] = item["title"]
        if item.get("year"):
            params["y"] = item["year"]
    else:
        return
    try:
        data = httpx.get(OMDB_URL, params=params, timeout=timeout).json()
    except (httpx.HTTPError, ValueError):
        return
    if not isinstance(data, dict) or data.get("Response") != "True":
        return
    rating = (data.get("imdbRating") or "").strip()
    if rating and rating != "N/A":
        item["rating"] = rating
    poster = (data.get("Poster") or "").strip()
    if poster and poster != "N/A":
        item["poster"] = poster
    title = (data.get("Title") or "").strip()
    if title:
        item["omdb_title"] = title
    typ = (data.get("Type") or "").strip()
    if typ:
        item["omdb_type"] = typ  # movie | series | episode
    genre = (data.get("Genre") or "").strip()
    if genre and genre != "N/A":
        item["genre"] = genre

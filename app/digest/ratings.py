"""Обогащение фильма/сериала рейтингом и постером.

Основной источник — TMDb (многоязычный: находит и русские, и новые фильмы, есть
постеры). Запасной — OMDb (англоязычный). Оба ключа бесплатные. Токены LLM не
тратятся. Любая ошибка — молча пропускаем (останется без постера/рейтинга).
"""

import httpx

OMDB_URL = "https://www.omdbapi.com/"
TMDB_URL = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w500"


def enrich(item: dict, omdb_key: str = "", tmdb_key: str = "", timeout: int = 15) -> None:
    """Дополнить item полями rating/poster/omdb_title/omdb_type (на месте)."""
    if (tmdb_key or "").strip() and _tmdb(item, tmdb_key.strip(), timeout):
        return
    if (omdb_key or "").strip():
        _omdb(item, omdb_key.strip(), timeout)


def _tmdb(item: dict, key: str, timeout: int) -> bool:
    """Поиск в TMDb (по типу и году) → постер + рейтинг. True, если что-то нашли."""
    query = (item.get("en_title") or item.get("title") or "").strip()
    if not query:
        return False
    is_series = bool(item.get("is_series"))
    endpoint = "tv" if is_series else "movie"
    params = {"api_key": key, "query": query, "language": "ru-RU", "include_adult": "false"}
    if item.get("year"):
        params["first_air_date_year" if is_series else "year"] = item["year"]
    try:
        data = httpx.get(f"{TMDB_URL}/search/{endpoint}", params=params, timeout=timeout).json()
        results = data.get("results") or []
        if not results:  # запас: мультипоиск без года/типа
            data = httpx.get(f"{TMDB_URL}/search/multi",
                             params={"api_key": key, "query": query, "language": "ru-RU"},
                             timeout=timeout).json()
            results = [r for r in (data.get("results") or [])
                       if r.get("media_type") in ("movie", "tv")]
    except (httpx.HTTPError, ValueError):
        return False
    if not results:
        return False
    r = results[0]
    got = False
    poster = r.get("poster_path")
    if poster:
        item["poster"] = TMDB_IMG + poster
        got = True
    vote = r.get("vote_average")
    try:
        if vote and float(vote) > 0:
            item["rating"] = f"{float(vote):.1f}"
            got = True
    except (TypeError, ValueError):
        pass
    title = r.get("title") or r.get("name")
    if title:
        item["omdb_title"] = title
    if r.get("media_type") == "tv" or is_series:
        item["omdb_type"] = "series"
    return got


def _omdb(item: dict, key: str, timeout: int) -> None:
    params = {"apikey": key}
    if item.get("imdbid"):
        params["i"] = item["imdbid"]
    elif item.get("en_title") or item.get("title"):
        params["t"] = item.get("en_title") or item["title"]
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
    if (data.get("Title") or "").strip():
        item["omdb_title"] = data["Title"].strip()
    if (data.get("Type") or "").strip():
        item["omdb_type"] = data["Type"].strip()

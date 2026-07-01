"""Обогащение фильма/сериала: постер, рейтинг, описание, жанр, тип.

TMDb — основной (многоязычный: русские и новые фильмы, постеры, описание ru-RU).
OMDb — даёт настоящий рейтинг IMDb (если находит, обычно для англоязычных).
Итог: постер и описание — из TMDb; рейтинг — IMDb (OMDb) при наличии, иначе TMDb.
Оба ключа бесплатные, токены LLM не тратятся; любая ошибка — молча пропускаем.
"""

import httpx

OMDB_URL = "https://www.omdbapi.com/"
TMDB_URL = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w500"

# TMDb genre_id → русское название (частые жанры, фильмы + сериалы).
_TMDB_GENRES = {
    28: "боевик", 12: "приключения", 16: "мультфильм", 35: "комедия", 80: "криминал",
    99: "документальный", 18: "драма", 10751: "семейный", 14: "фэнтези", 36: "история",
    27: "ужасы", 10402: "музыка", 9648: "детектив", 10749: "мелодрама", 878: "фантастика",
    53: "триллер", 10752: "военный", 37: "вестерн", 10759: "боевик", 10762: "детский",
    10763: "новости", 10764: "реалити", 10765: "фантастика", 10766: "драма",
    10767: "ток-шоу", 10768: "война",
}


def enrich(item: dict, omdb_key: str = "", tmdb_key: str = "", timeout: int = 15) -> None:
    """Дополнить item: poster, rating, rating_src, genre, overview, omdb_title, omdb_type."""
    if (tmdb_key or "").strip():
        _tmdb(item, tmdb_key.strip(), timeout)
    if (omdb_key or "").strip():
        _omdb(item, omdb_key.strip(), timeout)  # даёт настоящий рейтинг IMDb


def _tmdb(item: dict, key: str, timeout: int) -> None:
    query = (item.get("en_title") or item.get("title") or "").strip()
    if not query:
        return
    is_series = bool(item.get("is_series"))
    endpoint = "tv" if is_series else "movie"
    params = {"api_key": key, "query": query, "language": "ru-RU", "include_adult": "false"}
    if item.get("year"):
        params["first_air_date_year" if is_series else "year"] = item["year"]
    try:
        data = httpx.get(f"{TMDB_URL}/search/{endpoint}", params=params, timeout=timeout).json()
        results = data.get("results") or []
        if not results:
            data = httpx.get(f"{TMDB_URL}/search/multi",
                             params={"api_key": key, "query": query, "language": "ru-RU"},
                             timeout=timeout).json()
            results = [r for r in (data.get("results") or [])
                       if r.get("media_type") in ("movie", "tv")]
    except (httpx.HTTPError, ValueError):
        return
    if not results:
        return
    r = results[0]
    if r.get("poster_path"):
        item["poster"] = TMDB_IMG + r["poster_path"]
    try:
        va = float(r.get("vote_average") or 0)
        if va > 0:
            item["rating"] = f"{va:.1f}"
            item["rating_src"] = "TMDb"
    except (TypeError, ValueError):
        pass
    if r.get("overview"):
        item["overview"] = r["overview"].strip()
    if r.get("title") or r.get("name"):
        item["omdb_title"] = r.get("title") or r.get("name")
    if r.get("media_type") == "tv" or is_series:
        item["omdb_type"] = "series"
    gids = r.get("genre_ids") or []
    for gid in gids:
        if gid in _TMDB_GENRES:
            item["genre"] = _TMDB_GENRES[gid]
            break


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
        item["rating"] = rating              # настоящий IMDb — приоритет
        item["rating_src"] = "IMDb"
    if not item.get("poster"):
        poster = (data.get("Poster") or "").strip()
        if poster and poster != "N/A":
            item["poster"] = poster
    if not item.get("genre"):
        genre = (data.get("Genre") or "").strip()
        if genre and genre != "N/A":
            item["genre"] = genre.split(",")[0].strip().lower()
    if not item.get("omdb_title") and (data.get("Title") or "").strip():
        item["omdb_title"] = data["Title"].strip()
    if (data.get("Type") or "").strip():
        item["omdb_type"] = data["Type"].strip()

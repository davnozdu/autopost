"""Обогащение фильма/сериала: постер, рейтинг, описание, жанр, тип.

TMDb — основной (многоязычный: русские и новые фильмы, постеры, описание ru-RU).
OMDb — даёт настоящий рейтинг IMDb (если находит, обычно для англоязычных).
Итог: постер и описание — из TMDb; рейтинг — IMDb (OMDb) при наличии, иначе TMDb.
Оба ключа бесплатные, токены LLM не тратятся; любая ошибка — молча пропускаем.
"""

import re

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


def check_tmdb(tmdb_key: str, timeout: int = 15) -> dict:
    """Проверка ключа TMDb на тестовом фильме → {ok, reason}."""
    key = (tmdb_key or "").strip()
    if not key:
        return {"ok": False, "reason": "ключ TMDb не задан"}
    auth, headers = _tmdb_auth(key)
    try:
        r = httpx.get(f"{TMDB_URL}/search/movie",
                      params={**auth, "query": "The Matrix", "year": "1999", "language": "ru-RU"},
                      headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        return {"ok": False, "reason": f"не удалось подключиться к TMDb ({type(exc).__name__})"}
    if r.status_code == 401:
        return {"ok": False, "reason": "неверный ключ (401) — нужен API key v3 или Read Access Token v4"}
    if r.status_code >= 400:
        return {"ok": False, "reason": f"TMDb вернул HTTP {r.status_code}"}
    try:
        res = (r.json().get("results") or [])
    except ValueError:
        return {"ok": False, "reason": "TMDb вернул не JSON"}
    if not res:
        return {"ok": False, "reason": "TMDb ответил, но без результатов"}
    top = res[0]
    return {"ok": True, "reason": f"OK — {top.get('title')} · рейтинг {top.get('vote_average')} · "
            f"постер {'есть' if top.get('poster_path') else 'нет'}"}


def _tmdb_auth(key: str) -> tuple[dict, dict]:
    """TMDb принимает два ключа: v3 (короткий, ?api_key=) и v4 (длинный JWT, Bearer).
    Определяем по виду ключа и возвращаем (extra_params, headers)."""
    key = key.strip()
    if key.startswith("eyJ") or len(key) > 60:      # v4 Read Access Token (JWT)
        return {}, {"Authorization": f"Bearer {key}"}
    return {"api_key": key}, {}                       # v3 API key


def _query_variants(item: dict) -> list[str]:
    """Кандидаты-запросы к TMDb по названию релиза.

    Название бывает «Русское / English» (или наоборот, или обе части русские).
    Пробуем: латинскую часть → кириллическую часть → целиком/en_title. Порядок
    важен: латиница обычно матчится точнее в международной базе TMDb.
    """
    out: list[str] = []

    def add(x: str) -> None:
        x = (x or "").strip(" -–—/")
        if x and x not in out:
            out.append(x)

    title = item.get("title") or ""
    if "/" in title:
        parts = [p.strip() for p in title.split("/") if p.strip()]
        lat = [p for p in parts if re.search(r"[A-Za-z]", p)]
        cyr = [p for p in parts if re.search(r"[А-Яа-яЁё]", p)]
        for p in lat + cyr:
            add(p)
    else:
        add(title)
    add(item.get("en_title"))
    return out


def _tmdb_find_by_imdb(imdbid: str, is_series: bool, auth: dict, headers: dict,
                       timeout: int) -> dict | None:
    """Точный матч по IMDb id (не зависит от языка названия). TMDb /find."""
    try:
        data = httpx.get(f"{TMDB_URL}/find/{imdbid}",
                         params={**auth, "external_source": "imdb_id", "language": "ru-RU"},
                         headers=headers, timeout=timeout).json()
    except (httpx.HTTPError, ValueError):
        return None
    movie = data.get("movie_results") or []
    tv = data.get("tv_results") or []
    picks = (tv + movie) if is_series else (movie + tv)
    return picks[0] if picks else None


def _tmdb_search(query: str, year, is_series: bool, auth: dict, headers: dict,
                 timeout: int) -> dict | None:
    """Поиск по названию: /search/{movie|tv} (+год), фолбэк на /search/multi."""
    query = (query or "").strip()
    if not query:
        return None
    endpoint = "tv" if is_series else "movie"
    params = {**auth, "query": query, "language": "ru-RU", "include_adult": "false"}
    if year:
        params["first_air_date_year" if is_series else "year"] = year
    try:
        results = (httpx.get(f"{TMDB_URL}/search/{endpoint}", params=params,
                             headers=headers, timeout=timeout).json().get("results") or [])
        if not results:
            results = [r for r in (httpx.get(
                f"{TMDB_URL}/search/multi",
                params={**auth, "query": query, "language": "ru-RU"},
                headers=headers, timeout=timeout).json().get("results") or [])
                if r.get("media_type") in ("movie", "tv")]
    except (httpx.HTTPError, ValueError):
        return None
    return results[0] if results else None


def _tmdb_overview_en(rid, media_type: str, auth: dict, headers: dict,
                      timeout: int) -> str:
    """Английское описание из карточки (когда у TMDb нет русского перевода)."""
    if not rid:
        return ""
    path = "tv" if media_type == "tv" else "movie"
    try:
        d = httpx.get(f"{TMDB_URL}/{path}/{rid}", params={**auth, "language": "en-US"},
                      headers=headers, timeout=timeout).json()
        return (d.get("overview") or "").strip()
    except (httpx.HTTPError, ValueError):
        return ""


def _tmdb(item: dict, key: str, timeout: int) -> None:
    auth, headers = _tmdb_auth(key)
    is_series = bool(item.get("is_series"))

    r: dict | None = None
    # 1) точный матч по IMDb id (если трекер его отдал) — самый надёжный путь
    imdbid = (item.get("imdbid") or "").strip()
    if imdbid:
        r = _tmdb_find_by_imdb(imdbid, is_series, auth, headers, timeout)
    # 2) поиск по названию (несколько вариантов) с фильтром по году
    if r is None:
        for q in _query_variants(item):
            r = _tmdb_search(q, item.get("year"), is_series, auth, headers, timeout)
            if r:
                break
    # 3) без года — год мог мешать (у TMDb иная дата первого эфира/релиза)
    if r is None:
        for q in _query_variants(item):
            r = _tmdb_search(q, None, is_series, auth, headers, timeout)
            if r:
                break
    if not r:
        return

    if r.get("poster_path"):
        item["poster"] = TMDB_IMG + r["poster_path"]
    try:
        va = float(r.get("vote_average") or 0)
        if va > 0:
            item["rating"] = f"{va:.1f}"
            item["rating_src"] = "TMDb"
    except (TypeError, ValueError):
        pass
    overview = (r.get("overview") or "").strip()
    media_type = r.get("media_type") or ("tv" if is_series else "movie")
    if not overview:  # нет русского перевода → берём английское описание
        overview = _tmdb_overview_en(r.get("id"), media_type, auth, headers, timeout)
    if overview:
        item["overview"] = overview
    if r.get("title") or r.get("name"):
        item["omdb_title"] = r.get("title") or r.get("name")
    if media_type == "tv" or is_series:
        item["omdb_type"] = "series"
    for gid in (r.get("genre_ids") or []):
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

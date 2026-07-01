"""Клиент Prowlarr — источники берутся АВТОМАТИЧЕСКИ из его базы.

Prowlarr `/api/v1/search` агрегирует ВСЕ настроенные индексаторы за один запрос
и отдаёт JSON (title, seeders, downloadUrl, infoUrl, categories, publishDate,
imdbId…). Пользователь задаёт только URL+API-ключ и выбирает категории (кино/
сериалы/…) — список трекеров вести вручную не нужно. Токены LLM не тратятся.

magnet/infoHash Prowlarr обычно не отдаёт (как и Torznab) — magnet добываем из
.torrent по downloadUrl (см. app/digest/torrentfile.py).
"""

from urllib.parse import quote

import httpx

from app.digest import release


def _headers(api_key: str) -> dict:
    return {"X-Api-Key": api_key.strip(), "Accept": "application/json"}


def _to_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def search(base_url: str, api_key: str, categories: str,
           limit: int = 100, timeout: int = 40) -> list[dict]:
    """Свежие релизы из ВСЕХ индексаторов Prowlarr по выбранным категориям."""
    base = (base_url or "").strip().rstrip("/")
    key = (api_key or "").strip()
    if not base or not key:
        return []
    cats = [c.strip() for c in (categories or "").split(",") if c.strip()]
    # categories — повторяющийся параметр (не через запятую!)
    params = [("query", ""), ("type", "search"), ("limit", str(max(1, min(limit, 300))))]
    for c in cats:
        params.append(("categories", c))
    try:
        resp = httpx.get(f"{base}/api/v1/search", params=params,
                         headers=_headers(key), timeout=timeout)
        if resp.status_code >= 400:
            return []
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    if not isinstance(data, list):
        return []

    out: list[dict] = []
    for x in data:
        raw = (x.get("title") or "").strip()
        if not raw:
            continue
        title, year = release.clean_title(raw)
        imdb = _to_int(x.get("imdbId"))
        magnet = x.get("magnetUrl") or ""
        if not str(magnet).startswith("magnet:"):
            magnet = ""
        infohash = (x.get("infoHash") or "").strip()
        if not magnet and len(infohash) in (40, 32):
            magnet = f"magnet:?xt=urn:btih:{infohash}&dn={quote(raw[:120])}"
        out.append({
            "raw_title": raw,
            "title": title,
            "year": year,
            "season": release.season_of(raw),
            "is_series": release.is_series(raw),
            "seeders": _to_int(x.get("seeders")),
            "size": _to_int(x.get("size")),
            "imdbid": f"tt{imdb:07d}" if imdb > 0 else "",
            "magnet": magnet,
            "infohash": infohash,
            "download_url": x.get("downloadUrl") or "",
            "page_url": x.get("infoUrl") or x.get("guid") or "",
            "pubdate": x.get("publishDate") or "",
            "categories": [c.get("id") for c in (x.get("categories") or []) if isinstance(c, dict)],
            "indexer": x.get("indexer") or "",
        })
    return out


def list_indexers(base_url: str, api_key: str, timeout: int = 20) -> list[dict]:
    """Список настроенных индексаторов (для кнопки «Проверить Prowlarr»)."""
    base = (base_url or "").strip().rstrip("/")
    key = (api_key or "").strip()
    if not base or not key:
        return []
    try:
        resp = httpx.get(f"{base}/api/v1/indexer", headers=_headers(key), timeout=timeout)
        if resp.status_code >= 400:
            return []
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [{"id": x.get("id"),
             "name": x.get("name") or x.get("definitionName") or "?",
             "enable": bool(x.get("enable"))}
            for x in data]

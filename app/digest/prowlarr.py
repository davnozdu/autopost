"""Клиент Prowlarr — источники берутся АВТОМАТИЧЕСКИ из его базы.

Prowlarr `/api/v1/search` агрегирует ВСЕ настроенные индексаторы за один запрос
и отдаёт JSON (title, seeders, downloadUrl, infoUrl, categories, publishDate,
imdbId…). Пользователь задаёт только URL+API-ключ и выбирает категории (кино/
сериалы/…) — список трекеров вести вручную не нужно. Токены LLM не тратятся.

magnet/infoHash Prowlarr обычно не отдаёт (как и Torznab) — magnet добываем из
.torrent по downloadUrl (см. app/digest/torrentfile.py).
"""

import re
from urllib.parse import quote

import httpx

from app.digest import release


def _headers(api_key: str) -> dict:
    return {"X-Api-Key": api_key.strip(), "Accept": "application/json"}


def _base(url: str) -> str:
    """Нормализовать адрес: добавить http:// если забыли, убрать хвостовой /."""
    u = (url or "").strip().rstrip("/")
    if u and not u.lower().startswith(("http://", "https://")):
        u = "http://" + u
    return u


def _to_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        pass
    # строковый вид с мусором/префиксом (напр. imdbId="tt1234567") → цифры
    digits = re.sub(r"[^\d]", "", str(v or ""))
    return int(digits) if digits else 0


def search(base_url: str, api_key: str, categories: str,
           limit: int = 100, timeout: int = 40) -> list[dict]:
    """Свежие релизы из ВСЕХ индексаторов Prowlarr по выбранным категориям."""
    base = _base(base_url)
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
    return check(base_url, api_key, timeout).get("indexers", [])


def check(base_url: str, api_key: str, timeout: int = 20) -> dict:
    """Диагностика подключения к Prowlarr → {ok, reason, indexers}."""
    base = _base(base_url)
    key = (api_key or "").strip()
    if not base or not key:
        return {"ok": False, "reason": "не задан адрес или API-ключ Prowlarr", "indexers": []}
    try:
        resp = httpx.get(f"{base}/api/v1/indexer", headers=_headers(key), timeout=timeout)
    except httpx.HTTPError as exc:
        return {"ok": False, "indexers": [],
                "reason": f"не удалось подключиться к {base} — контейнер не видит Prowlarr "
                          f"или адрес неверный ({type(exc).__name__})"}
    if resp.status_code == 401:
        return {"ok": False, "reason": "неверный API-ключ Prowlarr (401)", "indexers": []}
    if resp.status_code >= 400:
        return {"ok": False, "reason": f"Prowlarr вернул HTTP {resp.status_code}", "indexers": []}
    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "reason": "Prowlarr вернул не JSON", "indexers": []}
    if not isinstance(data, list) or not data:
        return {"ok": False, "reason": "подключились, но индексаторов нет — добавьте их в Prowlarr",
                "indexers": []}
    idx = [{"id": x.get("id"),
            "name": x.get("name") or x.get("definitionName") or "?",
            "enable": bool(x.get("enable"))}
           for x in data]
    return {"ok": True, "reason": "", "indexers": idx}

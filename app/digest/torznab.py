"""Клиент Torznab (Jackett / Prowlarr) — свежие релизы для movies-дайджеста.

Torznab — это RSS/XML с namespace-атрибутами (seeders, magneturl, imdbid,
category, size). Работает с ЛЮБЫМ Torznab-эндпоинтом: пользователь вставляет
Torznab feed URL из Jackett или Prowlarr (в нём уже есть apikey), а категории и
лимит добавляем параметрами. Токены LLM не задействованы.
"""

import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import httpx

from app.digest import release

# Атрибуты приходят в одном из двух namespace (torznab или newznab).
_ATTR_TAGS = (
    "{http://torznab.com/schemas/2015/feed}attr",
    "{http://www.newznab.com/DTD/2010/feeds/attributes/}attr",
)


def _int(v) -> int:
    try:
        return int(re.sub(r"[^\d]", "", str(v)))
    except (TypeError, ValueError):
        return 0


def _imdb(v: str) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    digits = re.sub(r"[^\d]", "", v)
    return f"tt{digits}" if digits else ""


def _build_url(base: str, categories: str, limit: int) -> str:
    sep = "&" if "?" in base else "?"
    params = {"t": "search", "limit": str(max(1, min(limit, 200)))}
    cats = (categories or "").replace(" ", "")
    if cats:
        params["cat"] = cats
    # q не задаём → индексатор отдаёт свежие релизы (recent)
    return base + sep + urlencode(params)


def fetch(base_url: str, categories: str = "", limit: int = 100,
          timeout: int = 25) -> list[dict]:
    """Свежие релизы из Torznab-эндпоинта. Возвращает список словарей.

    Поля: raw_title, title, year, season, is_series, seeders, size, imdbid,
    magnet, link, categories. Ошибка/пустой URL → [].
    """
    base_url = (base_url or "").strip()
    if not base_url:
        return []
    url = _build_url(base_url, categories, limit)
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={"User-Agent": "autopost/1.0"})
        if resp.status_code >= 400:
            return []
        root = ET.fromstring(resp.content)
    except (httpx.HTTPError, ET.ParseError):
        return []

    items: list[dict] = []
    for it in root.iter("item"):
        raw = (it.findtext("title") or "").strip()
        if not raw:
            continue
        d = {
            "raw_title": raw,
            "link": (it.findtext("link") or "").strip(),
            "pubdate": (it.findtext("pubDate") or "").strip(),
            "seeders": 0, "size": _int(it.findtext("size") or ""),
            "imdbid": "", "magnet": "", "categories": [],
        }
        enc = it.find("enclosure")
        if enc is not None:
            href = enc.get("url", "") or ""
            if href.startswith("magnet:"):
                d["magnet"] = href
            elif not d["link"]:
                d["link"] = href
        # torznab/newznab-атрибуты
        for tag in _ATTR_TAGS:
            for attr in it.findall(tag):
                name = (attr.get("name") or "").lower()
                val = attr.get("value", "")
                if name == "seeders":
                    d["seeders"] = _int(val)
                elif name == "size" and not d["size"]:
                    d["size"] = _int(val)
                elif name in ("magneturl", "magnet") and val.startswith("magnet:"):
                    d["magnet"] = val
                elif name in ("imdb", "imdbid"):
                    d["imdbid"] = _imdb(val)
                elif name == "category":
                    d["categories"].append(val)
        if not d["magnet"] and d["link"].startswith("magnet:"):
            d["magnet"] = d["link"]
        title, year = release.clean_title(raw)
        d["title"] = title
        d["year"] = year
        d["season"] = release.season_of(raw)
        d["is_series"] = release.is_series(raw)
        items.append(d)
    return items


def dedup_best(items: list[dict]) -> list[dict]:
    """Свернуть раздачи одного фильма/сезона в одну запись (лучшая по сидам)."""
    best: dict[str, dict] = {}
    for it in items:
        key = release.norm_key(it.get("title", ""), it.get("year", ""), it.get("season", ""))
        if not it.get("title"):
            continue
        cur = best.get(key)
        if cur is None or it.get("seeders", 0) > cur.get("seeders", 0):
            best[key] = it
    return list(best.values())

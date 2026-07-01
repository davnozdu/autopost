"""Собрать magnet из .torrent-файла: скачать по download-ссылке (в т.ч. через
прокси Prowlarr) → вынуть infohash (SHA1 от bencoded словаря `info`) → magnet.

Нужно для трекеров, которые отдают только .torrent (напр. NNM-Club), а magnet
в Torznab нет. Контейнер тянет .torrent через Prowlarr (внутренний доступ), а
наружу уходит уже чистый публичный magnet — без apikey/адреса сервера.
"""

import hashlib
from urllib.parse import quote

import httpx


def _decode(data: bytes, i: int):
    """Минимальный bdecode → (значение, следующий_индекс)."""
    c = data[i : i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1 : j]), j + 1
    if c.isdigit():
        colon = data.index(b":", i)
        n = int(data[i:colon])
        s = colon + 1
        return data[s : s + n], s + n
    if c == b"l":
        i += 1
        out = []
        while data[i : i + 1] != b"e":
            v, i = _decode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i : i + 1] != b"e":
            k, i = _decode(data, i)
            v, i = _decode(data, i)
            out[k] = v
        return out, i + 1
    raise ValueError(f"bad bencode at {i}")


def infohash_from_torrent(data: bytes) -> str | None:
    """SHA1(bencoded info) в hex — btih v1. None, если это не .torrent."""
    try:
        if data[0:1] != b"d":
            return None
        i = 1
        while data[i : i + 1] != b"e":
            key, i = _decode(data, i)
            start = i
            _, i = _decode(data, i)
            if key == b"info":
                return hashlib.sha1(data[start:i]).hexdigest()
        return None
    except (ValueError, IndexError):
        return None


def magnet_from_url(url: str, name: str = "", timeout: int = 25) -> str | None:
    """Скачать .torrent по ссылке и собрать magnet (btih). None при любой ошибке."""
    url = (url or "").strip()
    if not url:
        return None
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={"User-Agent": "autopost/1.0"})
        if resp.status_code >= 400:
            return None
        # если ссылка сама редиректнула на magnet — вернём его
        final = str(resp.url)
        if final.startswith("magnet:"):
            return final
        ih = infohash_from_torrent(resp.content)
    except httpx.HTTPError:
        return None
    if not ih:
        return None
    magnet = f"magnet:?xt=urn:btih:{ih}"
    if name:
        magnet += "&dn=" + quote(name[:120])
    return magnet

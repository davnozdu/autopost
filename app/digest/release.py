"""Разбор имён торрент-релизов: «Movie.Name.2026.1080p.BluRay.x264-GRP» → (название, год).

Лёгкий парсер на регэкспах (без внешних зависимостей). Цель — вытащить чистое
название и год для дедупа раздач и красивого текста подборки.
"""

import re

_YEAR = re.compile(r"[.\s(\[]((?:19|20)\d\d)(?:[.,\s)\]]|$)")
_SERIES = re.compile(r"\bS(\d{1,2})(?:[.\s]?E\d{1,3})?\b|\bСезон\b|\bSeason\b", re.I)
# технические теги качества/кодеков/групп — всё, что после них, отбрасываем
_TAGS = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|hdr10?|dv|hevc|x264|x265|h\.?264|h\.?265|"
    r"web[-\s]?dl|web[-\s]?rip|webrip|blu[-\s]?ray|bdrip|brrip|dvdrip|hdrip|remux|"
    r"proper|repack|amzn|nf|hulu|dsnp|atvp|max|hdtv|aac|ac3|dts(?:[-\s]?hd)?|"
    r"ddp?[.\s]?5[.\s]?1|truehd|atmos|10bit|imax|extended|unrated|multi|dual|"
    r"dubbed|subbed|rus|eng|ukr)\b",
    re.I,
)


def clean_title(name: str) -> tuple[str, str]:
    """Имя релиза → (Название, Год). Год может быть пустым."""
    s = (name or "").strip()
    year = ""
    m = _YEAR.search(s)
    ms = _SERIES.search(s)
    cut = len(s)
    if m:
        year = m.group(1)
        cut = min(cut, m.start())
    if ms:
        cut = min(cut, ms.start())
    if not m and not ms:
        # нет ни года, ни признака сериала — режем по первому тех-тегу
        mt = _TAGS.search(s)
        if mt:
            cut = mt.start()
    title = s[:cut]
    title = re.sub(r"[._]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -–—_.[](){}")
    return title, year


def season_of(name: str) -> str:
    """Номер сезона в формате «S3» (для сериалов) или ''."""
    m = re.search(r"\bS(\d{1,2})\b", name or "", re.I)
    return f"S{int(m.group(1))}" if m else ""


def is_series(name: str) -> bool:
    return bool(_SERIES.search(name or ""))


def norm_key(title: str, year: str, season: str = "") -> str:
    """Ключ дедупа: одинаковый фильм/сезон из разных раздач сворачивается в один."""
    base = re.sub(r"[^0-9a-zа-яё]+", "", (title or "").lower())
    return f"{base}|{year}|{season}"

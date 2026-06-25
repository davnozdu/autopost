"""Вспомогательные утилиты."""

import re
import unicodedata

_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e", "ё": "e",
    "є": "ie", "ж": "zh", "з": "z", "и": "i", "і": "i", "ї": "i", "й": "i", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "iu", "я": "ia",
}


def slugify_latin(s: str, maxlen: int = 70) -> str:
    """Латинский slug: транслит кириллицы + снятие диакритики + [a-z0-9-]."""
    s = (s or "").strip().lower()
    s = "".join(_CYR.get(ch, ch) for ch in s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s[:maxlen].strip("-")


# Язык статьи (как пишет LLM) → сегмент пути на сайте.
LANG_PATH = {"cz": "cs", "cs": "cs", "ru": "ru", "ua": "uk", "uk": "uk",
             "en": "en", "de": "de"}


def lang_segment(lang: str) -> str:
    return LANG_PATH.get((lang or "cs").strip().lower(), (lang or "cs").strip().lower())


def normalize_repo(s: str) -> str:
    """Привести репозиторий к виду owner/name (принимает и полный URL, и .git)."""
    s = (s or "").strip()
    s = re.sub(r"^https?://github\.com/", "", s, flags=re.I)
    s = re.sub(r"^git@github\.com:", "", s, flags=re.I)
    s = re.sub(r"\.git$", "", s, flags=re.I)
    return s.strip("/")


# Сегмент языка → человекочитаемое название для промпта перевода.
LANG_NAME = {
    "cs": "Czech (čeština)",
    "ru": "Russian (русский)",
    "en": "English",
    "uk": "Ukrainian (українська)",
    "de": "German (Deutsch)",
}


def lang_name(seg: str) -> str:
    return LANG_NAME.get(seg, seg)


def clean_image_url(url: str | None) -> str | None:
    """Починить «склеенный» URL вида https://domainhttps://real → https://real.

    Так ломается картинка, если к уже абсолютному адресу ошибочно приклеили
    домен сайта (без слэша между ними). Обычный относительный/абсолютный URL
    не трогаем. CDN-прокси (…/?u=https://…) не задеваем — там есть слэш до схемы.
    """
    if not url:
        return url
    u = url.strip()
    m = re.match(r"^(https?://[^/]*?)(https?://.+)$", u)
    return m.group(2) if m else u

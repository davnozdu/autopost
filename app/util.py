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

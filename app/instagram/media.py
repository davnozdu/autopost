"""Подготовка изображения к публикации в Instagram.

Скачивает картинку по URL и приводит к нужному формату:
  • пост в ленту — 1080×1350 (4:5, портрет, максимум площади в ленте);
  • сториз      — 1080×1920 (9:16).
Чтобы не обрезать важное, картинка вписывается целиком на размытый фон
того же изображения (cover-blur). Возвращает путь к JPEG или None при сбое.
"""

from io import BytesIO
from pathlib import Path

import httpx

# Целевые размеры (ширина, высота) под формат Instagram.
FEED_SIZE = (1080, 1350)
STORY_SIZE = (1080, 1920)


def _target(kind: str) -> tuple[int, int]:
    return STORY_SIZE if kind == "story" else FEED_SIZE


def prepare(url: str | None, out_path: Path, kind: str) -> Path | None:
    """Скачать `url` и сохранить подготовленный JPEG в `out_path`.

    kind: "post" | "story". При любой ошибке/пустом url → None.
    """
    from app.util import clean_image_url

    url = clean_image_url(url)
    if not url:
        return None
    try:
        from PIL import Image, ImageFilter, ImageOps
    except Exception:
        return None
    try:
        resp = httpx.get(url, timeout=30, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 autopost"})
        resp.raise_for_status()
        src = Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception:
        return None

    tw, th = _target(kind)
    # фон: то же фото, заполняющее кадр, сильно размытое
    bg = ImageOps.fit(src, (tw, th), Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
    # передний план: фото целиком, вписанное в кадр
    fg = src.copy()
    fg.thumbnail((tw, th), Image.LANCZOS)
    bg.paste(fg, ((tw - fg.width) // 2, (th - fg.height) // 2))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        bg.save(out_path, "JPEG", quality=88)
    except Exception:
        return None
    return out_path

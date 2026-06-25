"""Подготовка изображения к публикации в Instagram.

Скачивает картинку по URL и приводит к нужному формату:
  • пост в ленту — 1080×1350 (4:5, портрет, максимум площади в ленте);
  • сториз      — 1080×1920 (9:16).
Чтобы не обрезать важное, картинка вписывается целиком на размытый фон
того же изображения (cover-blur).

Для сториз дополнительно НАКЛАДЫВАЕТ текст (пара предложений + хэштеги) на
аккуратную полупрозрачную плашку снизу — Instagram не показывает caption как
видимый текст на сторис. Возвращает путь к JPEG или None при сбое.
"""

from io import BytesIO
from pathlib import Path

import httpx

FEED_SIZE = (1080, 1350)
STORY_SIZE = (1080, 1920)
# Шрифты с кириллицей (есть в образе через fonts-dejavu-core); запасной — встроенный.
_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _target(kind: str) -> tuple[int, int]:
    return STORY_SIZE if kind == "story" else FEED_SIZE


def _font(size: int):
    from PIL import ImageFont

    for p in _FONT_PATHS:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int, max_lines: int) -> list[str]:
    words = (text or "").split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) >= max_lines and (cur or words):
        # пометить обрезку многоточием
        lines[-1] = lines[-1].rstrip(" .,;:") + "…"
    return lines[:max_lines]


def _draw_story_text(img, title: str) -> None:
    """Наложить только текст (1–3 строки) на плашку снизу. Хэштеги НЕ рисуем."""
    from PIL import ImageDraw

    if not title:
        return
    W, H = img.size
    draw = ImageDraw.Draw(img, "RGBA")
    margin = int(W * 0.06)
    title_font = _font(54)

    title_lines = _wrap(draw, title, title_font, W - 2 * margin, 3)
    if not title_lines:
        return

    line_h = int(54 * 1.25)
    block_h = len(title_lines) * line_h
    pad = int(W * 0.05)
    # плашка занимает только нижнюю часть, не перекрывая всю картинку
    top = H - block_h - pad * 2 - int(H * 0.06)
    top = max(top, int(H * 0.45))
    draw.rectangle([0, top, W, H], fill=(0, 0, 0, 140))  # затемнение для читаемости

    y = top + pad
    for ln in title_lines:
        draw.text((margin, y), ln, font=title_font, fill=(255, 255, 255, 255))
        y += line_h


def prepare(url: str | None, out_path: Path, kind: str,
            overlay_title: str = "") -> Path | None:
    """Скачать `url`, привести к формату и (для сториз) наложить текст. → JPEG или None."""
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
    bg = ImageOps.fit(src, (tw, th), Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
    fg = src.copy()
    fg.thumbnail((tw, th), Image.LANCZOS)
    bg.paste(fg, ((tw - fg.width) // 2, (th - fg.height) // 2))

    if kind == "story" and overlay_title:
        try:
            _draw_story_text(bg, overlay_title)
        except Exception:
            pass  # текст — не критично; сторис всё равно опубликуем

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        bg.save(out_path, "JPEG", quality=88)
    except Exception:
        return None
    return out_path

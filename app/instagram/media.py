"""Подготовка изображения к публикации в Instagram.

Скачивает картинку по URL и приводит к нужному формату:
  • пост в ленту — 1080×1350 (4:5, портрет, максимум площади в ленте);
  • сториз      — 1080×1920 (9:16).
Чтобы не обрезать важное, картинка вписывается целиком на размытый фон
того же изображения (cover-blur).

Для сториз снизу рисуется БЕЛАЯ ПЛАШКА (~30% высоты): на ней тёмный текст
(1–2 коротких предложения, шрифт авто-подгоняется) и «пилюля» со ссылкой на
сайт. Само фото вписывается в верхние 70%, чтобы плашка его не перекрывала.
Кликабельная ссылка добавляется отдельно стикером Instagram (см. client.py).
Возвращает путь к JPEG или None при сбое.
"""

from io import BytesIO
from pathlib import Path

import httpx

import random

FEED_SIZE = (1080, 1350)
STORY_SIZE = (1080, 1920)
PANEL_RATIO = 0.30  # доля высоты сториз под белую плашку с текстом
# Палитра акцентов — выбирается СЛУЧАЙНО на каждую сториз, чтобы оформление
# не было однообразным (как было с музыкой). Текст всегда тёмный на белом.
ACCENTS = [
    (214, 41, 118, 255),   # розовый (Instagram)
    (29, 155, 240, 255),   # голубой
    (245, 133, 41, 255),   # оранжевый
    (123, 79, 224, 255),   # фиолетовый
    (16, 163, 127, 255),   # бирюзовый
    (233, 64, 87, 255),    # красный
    (0, 122, 255, 255),    # синий
]
ACCENT = ACCENTS[0]  # значение по умолчанию
# Шрифты с кириллицей (есть в образе через fonts-dejavu-core); запасной — встроенный.
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _target(kind: str) -> tuple[int, int]:
    return STORY_SIZE if kind == "story" else FEED_SIZE


def _font(size: int, bold: bool = True):
    from PIL import ImageFont

    for p in ([_FONT_BOLD, _FONT_REG] if bold else [_FONT_REG, _FONT_BOLD]):
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
        lines[-1] = lines[-1].rstrip(" .,;:") + "…"
    return lines[:max_lines]


def _domain(link: str) -> str:
    d = (link or "").strip()
    d = d.split("//", 1)[-1]          # убрать схему
    d = d.split("/", 1)[0]            # только хост
    return d[4:] if d.startswith("www.") else d


def _draw_grabber(draw, W: int, top: int, accent, style: str) -> None:
    """Маленький акцентный декор сверху плашки — стиль выбирается случайно."""
    cx, y = W // 2, top + 22
    if style == "dots":
        r, gap = 9, 34
        for i in (-1, 0, 1):
            x = cx + i * gap
            draw.ellipse([x - r, y - r, x + r, y + r], fill=accent)
    elif style == "line":
        hw = int(W * 0.22)
        draw.rounded_rectangle([cx - hw // 2, y - 5, cx + hw // 2, y + 5],
                               radius=5, fill=accent)
    else:  # "bar"
        hw = int(W * 0.10)
        draw.rounded_rectangle([cx - hw // 2, top + 16, cx + hw // 2, top + 28],
                               radius=6, fill=accent)


def _draw_story_panel(img, text: str, link: str = "", accent=None) -> None:
    """Белая плашка снизу (~30%): тёмный авто-подгоняемый текст + пилюля ссылки.

    Акцентный цвет и мотив «грабера» выбираются СЛУЧАЙНО — чтобы сториз не
    выглядели одинаково. Текст всегда тёмный на белом (читаемость).
    """
    from PIL import ImageDraw

    accent = tuple(accent) if accent else random.choice(ACCENTS)
    W, H = img.size
    draw = ImageDraw.Draw(img, "RGBA")
    panel_h = int(H * PANEL_RATIO)
    top = H - panel_h
    radius = int(W * 0.055)
    # белая «карточка» со скруглённым верхом (низ уходит за край → ровный)
    draw.rounded_rectangle([0, top, W, H + radius], radius=radius,
                           fill=(255, 255, 255, 255))
    _draw_grabber(draw, W, top, accent, random.choice(["bar", "dots", "line"]))

    margin = int(W * 0.07)
    inner_w = W - 2 * margin
    pad_top = int(panel_h * 0.13)
    pad_bottom = int(panel_h * 0.10)

    dom = _domain(link)
    link_block = int(panel_h * 0.24) if dom else 0
    text_area_h = panel_h - pad_top - pad_bottom - link_block

    # авто-подгонка: уменьшаем шрифт, пока ВЕСЬ текст влезет (без обрезки),
    # допускаем до 5 строк; натуральную разбивку берём с запасом (max_lines=8)
    size, lines, line_h = 62, [], 0
    while size >= 26:
        f = _font(size, bold=True)
        lines = _wrap(draw, text, f, inner_w, 8)
        line_h = int(size * 1.2)
        if lines and len(lines) * line_h <= text_area_h and len(lines) <= 5:
            break
        size -= 3
    f = _font(size, bold=True)

    y = top + pad_top
    for ln in lines:
        draw.text((margin, y), ln, font=f, fill=(20, 22, 26, 255))
        y += line_h

    if dom:
        lf = _font(34, bold=True)
        tw_ = draw.textlength(dom, font=lf)
        px, py = 22, 13
        pill_w, pill_h = int(tw_ + px * 2), 34 + py * 2
        x0 = margin
        y0 = H - pad_bottom - pill_h
        draw.rounded_rectangle([x0, y0, x0 + pill_w, y0 + pill_h],
                               radius=pill_h // 2, fill=accent)
        draw.text((x0 + px, y0 + py), dom, font=lf, fill=(255, 255, 255, 255))


def prepare(url: str | None, out_path: Path, kind: str,
            overlay_title: str = "", overlay_link: str = "") -> Path | None:
    """Скачать `url`, привести к формату и (для сториз) наложить плашку. → JPEG или None."""
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

    if kind == "story":
        # фото — в верхние 70%, чтобы белая плашка снизу его не перекрывала
        area_h = th - int(th * PANEL_RATIO)
        fg = src.copy()
        fg.thumbnail((tw, area_h), Image.LANCZOS)
        bg.paste(fg, ((tw - fg.width) // 2, (area_h - fg.height) // 2))
        if overlay_title:
            try:
                _draw_story_panel(bg, overlay_title, overlay_link)
            except Exception:
                pass  # текст — не критично; сторис всё равно опубликуем
    else:
        fg = src.copy()
        fg.thumbnail((tw, th), Image.LANCZOS)
        bg.paste(fg, ((tw - fg.width) // 2, (th - fg.height) // 2))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        bg.save(out_path, "JPEG", quality=88)
    except Exception:
        return None
    return out_path

"""Сбор новостей из RSS-ленты в файловую структуру для анализа.

Структура: <analysis>/<поток>/<партия>/<новость>/{article.txt, meta.json}
- одна папка на RSS-поток;
- каждый запуск сбора создаёт новую партию (подпапку);
- старые партии сверх KEEP_BATCHES удаляются;
- в партии — по папке на новость.
"""

import copy
import json
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
from bs4 import BeautifulSoup

from app.scraper.extract import extract_image, extract_text, fetch_html

SNIPPET_THRESHOLD = 800  # короче — догружаем полную статью
MAX_ITEMS = 15           # сколько записей из ленты брать за один прогон
KEEP_BATCHES = 10        # лимит подпапок-партий на поток
PEEK_TTL = 300           # кэш предпросмотра ленты, сек (IG/TG/X с одним RSS не качают повторно)

_peek_cache: dict[tuple[str, int], tuple[float, dict]] = {}
_peek_lock = threading.Lock()


def slugify(s: str, maxlen: int = 60) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^\w\-]", "", s, flags=re.UNICODE)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:maxlen] or "item"


def _strip_html(s: str) -> str:
    return BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True)


# Ссылки-«пустышки»: трекинг-пиксели feedburner/feedsportal, 1×1, рекламные сети.
# Их НЕ берём как картинку поста (иначе вместо фото уйдёт невидимый пиксель).
_IMG_TRACKER_RE = re.compile(
    r"feedburner|feedsportal|doubleclick|googleads|/~[0-9a-z]+/|~r/|"
    r"1x1|pixel|spacer|blank\.(?:gif|png)|/stat[/.]|/track",
    re.I,
)


def _img_from_html(html: str) -> str | None:
    """Первый осмысленный <img> из HTML описания/контента (без трекинг-пикселей)."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        src = (img.get("src") or img.get("data-src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        if _IMG_TRACKER_RE.search(src):
            continue
        if str(img.get("width")) in ("0", "1") or str(img.get("height")) in ("0", "1"):
            continue  # пиксель-трекер по размеру
        return src
    return None


def _entry_image(entry) -> str | None:
    """Картинка записи ленты: media/enclosure → <img> в описании/контенте."""
    # 1) media:content / media:thumbnail (список или одиночный объект)
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key)
        if isinstance(media, list):
            for m in media:
                if m.get("url") and not _IMG_TRACKER_RE.search(m["url"]):
                    return m["url"]
        elif isinstance(media, dict) and media.get("url"):
            return media["url"]
    # 2) enclosures / links с типом image
    for enc in (entry.get("enclosures", []) or []) + (entry.get("links", []) or []):
        href = enc.get("href") or enc.get("url")
        typ = (enc.get("type") or "")
        rel = (enc.get("rel") or "")
        if href and ("image" in typ or (rel == "enclosure" and "image" in typ)):
            return href
    # 3) <img> внутри content:encoded / summary (частый случай — film.ru и др.)
    htmls = [c.get("value", "") for c in (entry.get("content") or [])]
    htmls.append(entry.get("summary", ""))
    for html in htmls:
        found = _img_from_html(html)
        if found:
            return found
    return None


def peek_feed(feed_url: str, limit: int = MAX_ITEMS) -> dict:
    """Предпросмотр ленты с кэшем на PEEK_TTL секунд (экономия сетевых загрузок).

    Возвращает глубокую копию, чтобы вызывающий код мог безопасно дополнять
    записи своими полями (_src_id и т.п.), не портя общий кэш.
    """
    key = (feed_url, limit)
    now = time.monotonic()
    with _peek_lock:
        hit = _peek_cache.get(key)
        if hit and now - hit[0] < PEEK_TTL:
            return copy.deepcopy(hit[1])

    result = _peek_feed_uncached(feed_url, limit)
    # кэшируем только удачные ответы (с записями) — ошибки не «залипают»
    if result.get("entries"):
        with _peek_lock:
            _peek_cache[key] = (now, result)
    return copy.deepcopy(result)


def _peek_feed_uncached(feed_url: str, limit: int = MAX_ITEMS) -> dict:
    parsed = feedparser.parse(feed_url)
    entries = []
    for entry in parsed.entries[:limit]:
        entries.append(
            {
                "title": entry.get("title", "(bez názvu)"),
                # feedburner отдаёт редирект-ссылку (feeds.*/~r/...), которая часто
                # не открывается (501) → берём оригинальную ссылку статьи.
                "link": entry.get("feedburner_origlink") or entry.get("link", ""),
                "published": entry.get("published", ""),
                "summary": _strip_html(entry.get("summary", ""))[:300],
                "image": _entry_image(entry),
            }
        )
    return {
        "feed_title": (parsed.feed or {}).get("title", ""),
        "count": len(parsed.entries),
        "error": str(parsed.get("bozo_exception", "")) if parsed.get("bozo") else "",
        "entries": entries,
    }


def collect_feed(feed_name: str, feed_url: str, analysis_dir: Path) -> tuple[list[dict], str]:
    parsed = feedparser.parse(feed_url)
    feed_dir = analysis_dir / slugify(feed_name)
    batch_dir = feed_dir / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    items: list[dict] = []
    for i, entry in enumerate(parsed.entries[:MAX_ITEMS]):
        link = entry.get("feedburner_origlink") or entry.get("link", "")
        title = entry.get("title", "(bez názvu)")
        text = _strip_html(entry.get("summary", ""))
        image = _entry_image(entry)

        # Догрузка полной статьи, если в ленте только кусок.
        if len(text) < SNIPPET_THRESHOLD and link:
            try:
                html = fetch_html(link)
                full = extract_text(html)
                if len(full) > len(text):
                    text = full
                if not image:
                    image = extract_image(html, link)
            except Exception:
                pass  # остаётся то, что было в ленте

        news_dir = batch_dir / f"{i:02d}-{slugify(title)}"
        news_dir.mkdir(parents=True, exist_ok=True)
        (news_dir / "article.txt").write_text(text, encoding="utf-8")
        meta = {
            "title": title,
            "link": link,
            "image": image,
            "published": entry.get("published", ""),
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        (news_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        items.append({"path": str(news_dir), "text": text, **meta})

    _prune_batches(feed_dir, KEEP_BATCHES)
    return items, str(batch_dir)


def _prune_batches(feed_dir: Path, keep: int) -> None:
    if not feed_dir.exists():
        return
    batches = sorted(p for p in feed_dir.iterdir() if p.is_dir())
    for old in batches[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def iter_news_dirs(analysis_dir: Path):
    """Все папки-новости, в которых есть article.txt."""
    for feed_dir in sorted(analysis_dir.iterdir()):
        if not feed_dir.is_dir():
            continue
        for batch_dir in sorted(feed_dir.iterdir()):
            if not batch_dir.is_dir():
                continue
            for news_dir in sorted(batch_dir.iterdir()):
                if (news_dir / "article.txt").exists():
                    yield feed_dir.name, news_dir


def read_news(news_dir: Path) -> dict:
    meta = json.loads((news_dir / "meta.json").read_text(encoding="utf-8"))
    text = (news_dir / "article.txt").read_text(encoding="utf-8")
    return {"path": str(news_dir), "text": text, **meta}

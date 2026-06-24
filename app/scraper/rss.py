"""Сбор новостей из RSS-ленты в файловую структуру для анализа.

Структура: <analysis>/<поток>/<партия>/<новость>/{article.txt, meta.json}
- одна папка на RSS-поток;
- каждый запуск сбора создаёт новую партию (подпапку);
- старые партии сверх KEEP_BATCHES удаляются;
- в партии — по папке на новость.
"""

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import feedparser
from bs4 import BeautifulSoup

from app.scraper.extract import extract_image, extract_text, fetch_html

SNIPPET_THRESHOLD = 800  # короче — догружаем полную статью
MAX_ITEMS = 15           # сколько записей из ленты брать за один прогон
KEEP_BATCHES = 10        # лимит подпапок-партий на поток


def slugify(s: str, maxlen: int = 60) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^\w\-]", "", s, flags=re.UNICODE)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:maxlen] or "item"


def _strip_html(s: str) -> str:
    return BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True)


def _entry_image(entry) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key)
        if isinstance(media, list) and media and media[0].get("url"):
            return media[0]["url"]
    for enc in entry.get("enclosures", []) or []:
        if enc.get("href") and "image" in (enc.get("type") or ""):
            return enc["href"]
    return None


def peek_feed(feed_url: str, limit: int = MAX_ITEMS) -> dict:
    """Живой предпросмотр ленты без сохранения — чтобы увидеть, что поток отдаёт."""
    parsed = feedparser.parse(feed_url)
    entries = []
    for entry in parsed.entries[:limit]:
        entries.append(
            {
                "title": entry.get("title", "(bez názvu)"),
                "link": entry.get("link", ""),
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
        link = entry.get("link", "")
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

"""Бизнес-логика: сбор со всех источников сайта, генерация через LLM,
распределение дат публикации и сама публикация (через заглушку publisher).

Используется и планировщиком (автопилот), и ручными кнопками в админке.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import AppConfig, Article, Site, Source
from app.db.session import engine
from app.llm.client import LLMClient, LLMError
from app.llm.prompt import (
    build_prompt,
    build_select_prompt,
    parse_article,
    parse_selection,
)
from app.scraper.extract import extract_image, extract_text, fetch_html
from app.scraper.rss import peek_feed
from app.util import lang_segment, slugify_latin

MAX_PER_COLLECT = 10  # верхний предел статей за прогон (защита)
CANDIDATE_POOL = 40   # сколько кандидатов рассматривать для отбора
COLLECT_WORKERS = 4   # параллельных потоков загрузки+генерации

_WEEKDAY_IDX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_days(value: str) -> set[int]:
    return {_WEEKDAY_IDX[d.strip()] for d in (value or "").split(",") if d.strip() in _WEEKDAY_IDX}


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hh, mm = (value or "09:00").split(":")
        return int(hh), int(mm)
    except ValueError:
        return 9, 0


def upcoming_publish_datetimes(site: Site, n_slots: int) -> list[datetime]:
    """Ближайшие даты публикации (в UTC) по дням/времени сайта."""
    tz = ZoneInfo(get_settings().tz)
    days = _parse_days(site.publish_days) or {2}  # по умолчанию среда
    hh, mm = _parse_hhmm(site.publish_time)
    after_local = _now().astimezone(tz)
    slots: list[datetime] = []
    start_date = after_local.date()
    for i in range(120):
        cand_date = start_date + timedelta(days=i)
        if cand_date.weekday() in days:
            cand = datetime.combine(cand_date, time(hh, mm), tzinfo=tz)
            if cand > after_local:
                slots.append(cand.astimezone(timezone.utc))
                if len(slots) >= n_slots:
                    break
    return slots


def generate_article(
    config: AppConfig,
    client: LLMClient,
    *,
    site: Site,
    title: str,
    link: str,
    image: str | None,
    text: str,
) -> Article:
    """Прогнать материал через LLM и собрать Article (status=draft, без сохранения).

    Поднимает LLMError при сбое модели.
    """
    news = {"title": title, "link": link, "image": image, "text": text}
    system, user = build_prompt(config, news)
    result = client.chat(
        system, user, json_mode=True, temperature=0.7, model=(config.llm_model or None)
    )
    art = parse_article(result.text, fallback_image=image)
    slug = slugify_latin(art["slug"]) or slugify_latin(art["title"]) or "article"
    return Article(
        site_id=site.id,
        site_name=site.name,
        source_title=title,
        source_url=link,
        image_url=art["image_url"],
        title=art["title"],
        slug=slug,
        annotation=art["annotation"],
        meta_description=art["meta_description"],
        keywords=art["keywords"],
        tag=art["tag"],
        body=art["body_html"],
        lang=lang_segment(config.language),
        languages=site.languages,
        status="draft",
    )


def _fetch_full(link: str, image: str | None, summary: str) -> tuple[str, str | None]:
    text = ""
    img = image or None
    try:
        html = fetch_html(link)
        text = extract_text(html)
        if not img:
            img = extract_image(html, link)
    except Exception:
        pass
    if not text.strip():
        text = summary
    return text, img


def _norm_title(s: str) -> str:
    return re.sub(r"[^0-9a-zа-яёіїєґ]+", " ", (s or "").lower()).strip()


def _select_candidates(client, config, candidates: list[dict], limit: int,
                       context: str, avoid: list[str] | None = None) -> list[dict]:
    """Выбрать limit самых стоящих новостей через LLM; при сбое — первые limit."""
    items = [
        {"i": i, "title": e.get("title", ""), "summary": e.get("summary", "")}
        for i, e in enumerate(candidates)
    ]
    try:
        system, user = build_select_prompt(items, limit, context, avoid)
        res = client.chat(system, user, json_mode=True, temperature=0.2,
                          model=(config.llm_model or None))
        idx = parse_selection(res.text, len(candidates), limit)
    except LLMError:
        idx = None
    if not idx:
        return candidates[:limit]
    return [candidates[i] for i in idx]


def collect_and_generate(site_id: int) -> dict:
    """Собрать со всех источников сайта, сгенерировать статьи, разнести по датам."""
    with Session(engine) as s:
        site = s.get(Site, site_id)
        if not site:
            return {"created": 0, "error": "no site"}
        sources = s.exec(
            select(Source).where(Source.site_id == site_id, Source.enabled == True)  # noqa: E712
        ).all()
        config = s.get(AppConfig, 1) or AppConfig(id=1)
        client = LLMClient()

        # 1) кандидаты со всех лент, дедуп по ссылке
        seen: set[str] = set()
        candidates: list[dict] = []
        for src in sources:
            try:
                data = peek_feed(src.url)
            except Exception:
                continue
            for e in data["entries"]:
                link = e.get("link")
                if not link or link in seen:
                    continue
                seen.add(link)
                if s.exec(select(Article).where(Article.source_url == link)).first():
                    continue
                candidates.append(e)

        # дедуп против уже опубликованного (по нормализованному заголовку) — бесплатно
        pub_titles = s.exec(
            select(Article.title).where(
                Article.site_id == site_id, Article.status == "published"
            )
        ).all()
        pub_norm = {_norm_title(t) for t in pub_titles}
        candidates = [e for e in candidates if _norm_title(e.get("title", "")) not in pub_norm]
        candidates = candidates[:CANDIDATE_POOL]

        # 2) отбор: если кандидатов больше лимита — выбрать лучшие через LLM
        limit = max(1, site.collect_limit)
        if len(candidates) > limit:
            batch = _select_candidates(client, config, candidates, limit, site.name,
                                       avoid=list(pub_titles))
        else:
            batch = candidates

        # 3) генерация — параллельно (загрузка статьи + вызов LLM по сети)

        def _make(e: dict):
            text, img = _fetch_full(e["link"], e.get("image"), e.get("summary", ""))
            try:
                art = generate_article(
                    config, client, site=site, title=e.get("title", ""),
                    link=e["link"], image=img, text=text,
                )
            except LLMError:
                return None
            art.status = "scheduled"
            return art

        created: list[Article] = []
        if batch:
            workers = min(COLLECT_WORKERS, len(batch))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for art in pool.map(_make, batch):  # порядок сохраняется
                    if art is None:
                        continue
                    s.add(art)
                    s.commit()
                    s.refresh(art)
                    created.append(art)

        # 3) распределение дат публикации по слотам
        if created:
            per = max(1, site.publish_per_run)
            n_slots = len(created) // per + 2
            slots = upcoming_publish_datetimes(site, n_slots)
            for idx, art in enumerate(created):
                si = idx // per
                if slots:
                    art.publish_at = slots[si] if si < len(slots) else slots[-1]
                s.add(art)
            s.commit()
        return {"created": len(created)}


def run_publish(site_id: int) -> dict:
    """Опубликовать нужное число статей (publish_per_run), лишние черновики удалить.

    Берём свежие неопубликованные (scheduled/draft), публикуем первые N; остальные
    (сверх нужного количества) удаляем. Те из N, что не опубликовались (ошибка),
    остаются с пометкой — чтобы видеть причину и повторить.
    """
    from app.publisher import publish

    with Session(engine) as s:
        site = s.get(Site, site_id)
        if not site:
            return {"published": 0, "deleted": 0, "error": "no site"}
        per = max(1, site.publish_per_run)
        pending = s.exec(
            select(Article)
            .where(
                Article.site_id == site_id,
                Article.status.in_(["scheduled", "draft"]),
            )
            .order_by(Article.created_at.desc())
        ).all()

        to_publish, surplus = pending[:per], pending[per:]
        now = _now()
        published = 0
        errors = []
        for art in to_publish:
            result = publish(art)
            art.publish_note = result.get("note", "")
            if result.get("published"):
                art.status = "published"
                art.published_at = now
                published += 1
            else:
                errors.append(result.get("note", ""))
            s.add(art)
        s.commit()

        # лишние сверх нужного количества — очищаем
        deleted = 0
        for art in surplus:
            s.delete(art)
            deleted += 1
        s.commit()

        note = f"опубликовано {published}, удалено лишних {deleted}"
        if errors:
            note += " | ошибки: " + "; ".join(e[:80] for e in errors[:3])
        return {"published": published, "deleted": deleted, "note": note}

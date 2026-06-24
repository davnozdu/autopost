"""Маршруты WEB-админки: источники, настройки, сбор, обработка, превью."""

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.requests import Request

from app.db.models import AppConfig, Article, Feed
from app.db.session import analysis_dir, engine
from app.llm.client import LLMClient, LLMError
from app.llm.prompt import build_prompt, parse_article
from app.scraper.rss import collect_feed, iter_news_dirs, peek_feed, read_news

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)

PROCESS_LIMIT = 20  # сколько новостей обрабатывать LLM за один прогон


def _redirect(path: str, msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"{path}?msg={msg}", status_code=303)


# ── Источники ─────────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def feeds_page(request: Request, msg: str = "") -> HTMLResponse:
    with Session(engine) as s:
        feeds = s.exec(select(Feed).order_by(Feed.id)).all()
    return templates.TemplateResponse(
        request, "feeds.html", {"feeds": feeds, "msg": msg}
    )


@router.post("/feeds")
def add_feed(name: str = Form(...), url: str = Form(...)) -> RedirectResponse:
    with Session(engine) as s:
        s.add(Feed(name=name.strip(), url=url.strip()))
        s.commit()
    return _redirect("/", "Источник добавлен")


@router.get("/feeds/{feed_id}/preview", response_class=HTMLResponse)
def feed_preview(request: Request, feed_id: int) -> HTMLResponse:
    with Session(engine) as s:
        feed = s.get(Feed, feed_id)
    if not feed:
        return _redirect("/", "Источник не найден")
    data = peek_feed(feed.url)
    return templates.TemplateResponse(
        request, "feed_preview.html", {"feed": feed, "data": data}
    )


@router.post("/feeds/{feed_id}/delete")
def delete_feed(feed_id: int) -> RedirectResponse:
    with Session(engine) as s:
        feed = s.get(Feed, feed_id)
        if feed:
            s.delete(feed)
            s.commit()
    return _redirect("/", "Источник удалён")


# ── Сбор новостей ─────────────────────────────────────────────────────
@router.post("/collect")
def collect() -> RedirectResponse:
    adir = analysis_dir()
    total = 0
    with Session(engine) as s:
        feeds = s.exec(select(Feed).where(Feed.enabled == True)).all()  # noqa: E712
    for feed in feeds:
        try:
            items, _ = collect_feed(feed.name, feed.url, adir)
            total += len(items)
        except Exception:
            continue
    return _redirect("/", f"Собрано новостей: {total}")


# ── Обработка LLM ─────────────────────────────────────────────────────
@router.post("/process")
def process() -> RedirectResponse:
    adir = analysis_dir()
    client = LLMClient()
    created = 0
    skipped_err = 0
    with Session(engine) as s:
        config = s.get(AppConfig, 1) or AppConfig(id=1)
        for feed_name, news_dir in iter_news_dirs(adir):
            if created >= PROCESS_LIMIT:
                break
            path = str(news_dir)
            exists = s.exec(
                select(Article).where(Article.source_path == path)
            ).first()
            if exists:
                continue
            news = read_news(news_dir)
            system, user = build_prompt(config, news)
            try:
                result = client.chat(
                    system, user, json_mode=True, temperature=0.7
                )
            except LLMError:
                skipped_err += 1
                continue
            art = parse_article(result.text, fallback_image=news.get("image"))
            s.add(
                Article(
                    feed_name=feed_name,
                    source_title=news.get("title", ""),
                    source_url=news.get("link", ""),
                    source_path=path,
                    image_url=art["image_url"],
                    title=art["title"],
                    body=art["body"],
                    status="prepared",
                )
            )
            s.commit()
            created += 1
    msg = f"Подготовлено: {created}"
    if skipped_err:
        msg += f", ошибок LLM: {skipped_err}"
    return _redirect("/preview", msg)


# ── Настройки ─────────────────────────────────────────────────────────
@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = "") -> HTMLResponse:
    with Session(engine) as s:
        config = s.get(AppConfig, 1) or AppConfig(id=1)
    return templates.TemplateResponse(
        request, "settings.html", {"config": config, "msg": msg}
    )


@router.post("/settings")
def save_settings(
    language: str = Form("cs"),
    chars_per_news: int = Form(1500),
    images_from_source_only: bool = Form(False),
    llm_instructions: str = Form(""),
) -> RedirectResponse:
    with Session(engine) as s:
        config = s.get(AppConfig, 1) or AppConfig(id=1)
        config.language = language.strip() or "cs"
        config.chars_per_news = chars_per_news
        config.images_from_source_only = images_from_source_only
        config.llm_instructions = llm_instructions
        s.add(config)
        s.commit()
    return _redirect("/settings", "Настройки сохранены")


# ── Превью и одобрение ────────────────────────────────────────────────
@router.get("/preview", response_class=HTMLResponse)
def preview_page(request: Request, msg: str = "") -> HTMLResponse:
    with Session(engine) as s:
        articles = s.exec(
            select(Article)
            .where(Article.status == "prepared")
            .order_by(Article.created_at.desc())
        ).all()
    return templates.TemplateResponse(
        request, "preview.html", {"articles": articles, "msg": msg}
    )


@router.post("/articles/{article_id}/approve")
def approve(article_id: int) -> RedirectResponse:
    from app.publisher import publish

    with Session(engine) as s:
        art = s.get(Article, article_id)
        if not art:
            return _redirect("/preview", "Не найдено")
        art.status = "approved"
        art.approved_at = datetime.now(timezone.utc)
        result = publish(art)
        if result.get("published"):
            art.status = "published"
        art.publish_note = result.get("note", "")
        s.add(art)
        s.commit()
    return _redirect("/preview", "Одобрено")


@router.post("/articles/{article_id}/reject")
def reject(article_id: int) -> RedirectResponse:
    with Session(engine) as s:
        art = s.get(Article, article_id)
        if art:
            art.status = "rejected"
            s.add(art)
            s.commit()
    return _redirect("/preview", "Отклонено")

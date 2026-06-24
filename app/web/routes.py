"""Маршруты WEB-админки: источники, настройки, сбор, обработка, превью, статьи."""

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.requests import Request

from app.db.models import LANGUAGES, AppConfig, Article, Feed
from app.db.session import analysis_dir, engine
from app.llm.client import LLMClient, LLMError
from app.llm.prompt import build_prompt, parse_article
from app.scraper.extract import extract_image, extract_text, fetch_html
from app.scraper.rss import (
    collect_feed,
    iter_news_dirs,
    peek_feed,
    read_news,
    slugify,
)

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)

PROCESS_LIMIT = 20  # сколько новостей обрабатывать LLM за один прогон (bulk)

# Опции модели DeepSeek для переключателя в настройках.
DEEPSEEK_MODELS = [
    ("deepseek-v4-flash", "DeepSeek V4 Flash — быстрая и дешёвая"),
    ("deepseek-v4-pro", "DeepSeek V4 Pro — мощнее, качественнее"),
]


def _redirect(path: str, msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"{path}?msg={msg}", status_code=303)


def _generate_article(
    config: AppConfig,
    client: LLMClient,
    *,
    feed_name: str,
    title: str,
    link: str,
    image: str | None,
    text: str,
    source_path: str = "",
) -> Article:
    """Прогнать одну новость через LLM и собрать объект Article (без сохранения).

    Поднимает LLMError при сбое вызова модели.
    """
    news = {"title": title, "link": link, "image": image, "text": text}
    system, user = build_prompt(config, news)
    result = client.chat(
        system,
        user,
        json_mode=True,
        temperature=0.7,
        model=(config.llm_model or None),
    )
    art = parse_article(result.text, fallback_image=image)
    return Article(
        feed_name=feed_name,
        source_title=title,
        source_url=link,
        source_path=source_path,
        image_url=art["image_url"],
        title=art["title"],
        annotation=art["annotation"],
        body=art["body"],
        status="prepared",
    )


# ── Источники ─────────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def feeds_page(request: Request, msg: str = "") -> HTMLResponse:
    with Session(engine) as s:
        feeds = s.exec(select(Feed).order_by(Feed.id)).all()
    return templates.TemplateResponse(
        request, "feeds.html", {"feeds": feeds, "msg": msg, "languages": LANGUAGES}
    )


@router.post("/feeds/{feed_id}/settings")
def feed_settings(
    feed_id: int,
    dest_site: str = Form(""),
    langs: list[str] = Form(default=[]),
) -> RedirectResponse:
    allowed = {code for code, _ in LANGUAGES}
    selected = [c for c in langs if c in allowed]
    with Session(engine) as s:
        feed = s.get(Feed, feed_id)
        if feed:
            feed.dest_site = dest_site.strip()
            feed.languages = ",".join(selected)
            s.add(feed)
            s.commit()
    return _redirect("/", "Настройки источника сохранены")


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
        # отметим, какие ссылки уже обработаны, чтобы не дублировать
        links = [e["link"] for e in data["entries"] if e["link"]]
        done = set()
        if links:
            rows = s.exec(
                select(Article.source_url).where(Article.source_url.in_(links))
            ).all()
            done = set(rows)
    for e in data["entries"]:
        e["processed"] = e["link"] in done
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


# ── Ручная обработка одной новости из предпросмотра ───────────────────
@router.post("/process-one")
def process_one(
    feed_id: int = Form(0),
    feed_name: str = Form(""),
    title: str = Form(""),
    link: str = Form(...),
    image: str = Form(""),
    summary: str = Form(""),
) -> RedirectResponse:
    with Session(engine) as s:
        exists = s.exec(
            select(Article).where(Article.source_url == link)
        ).first()
        if exists:
            return _redirect(f"/articles/{exists.id}", "Уже обработано")

        feed = s.get(Feed, feed_id) if feed_id else None

        # парсинг полной статьи со страницы источника
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

        config = s.get(AppConfig, 1) or AppConfig(id=1)
        try:
            art = _generate_article(
                config,
                LLMClient(),
                feed_name=feed_name,
                title=title,
                link=link,
                image=img,
                text=text,
            )
        except LLMError as exc:
            return _redirect("/preview", f"Ошибка LLM: {str(exc)[:120]}")
        if feed:
            art.dest_site = feed.dest_site
            art.languages = feed.languages
        s.add(art)
        s.commit()
        s.refresh(art)
    return _redirect(f"/articles/{art.id}", "Новость обработана")


# ── Сбор новостей (bulk, в папки анализа) ─────────────────────────────
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


@router.post("/process")
def process() -> RedirectResponse:
    adir = analysis_dir()
    client = LLMClient()
    created = 0
    skipped_err = 0
    with Session(engine) as s:
        config = s.get(AppConfig, 1) or AppConfig(id=1)
        # сопоставление папки-потока (slug) → источник, чтобы взять настройки
        feed_by_slug = {
            slugify(f.name): f for f in s.exec(select(Feed)).all()
        }
        for feed_name, news_dir in iter_news_dirs(adir):
            if created >= PROCESS_LIMIT:
                break
            path = str(news_dir)
            if s.exec(select(Article).where(Article.source_path == path)).first():
                continue
            news = read_news(news_dir)
            try:
                art = _generate_article(
                    config,
                    client,
                    feed_name=feed_name,
                    title=news.get("title", ""),
                    link=news.get("link", ""),
                    image=news.get("image"),
                    text=news.get("text", ""),
                    source_path=path,
                )
            except LLMError:
                skipped_err += 1
                continue
            feed = feed_by_slug.get(feed_name)
            if feed:
                art.dest_site = feed.dest_site
                art.languages = feed.languages
            s.add(art)
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
        request,
        "settings.html",
        {"config": config, "msg": msg, "models": DEEPSEEK_MODELS},
    )


@router.post("/settings")
def save_settings(
    language: str = Form("cs"),
    chars_per_news: int = Form(1500),
    images_from_source_only: bool = Form(False),
    llm_model: str = Form(""),
    llm_instructions: str = Form(""),
) -> RedirectResponse:
    with Session(engine) as s:
        config = s.get(AppConfig, 1) or AppConfig(id=1)
        config.language = language.strip() or "cs"
        config.chars_per_news = chars_per_news
        config.images_from_source_only = images_from_source_only
        config.llm_model = llm_model.strip()
        config.llm_instructions = llm_instructions
        s.add(config)
        s.commit()
    return _redirect("/settings", "Настройки сохранены")


# ── Превью и статьи ───────────────────────────────────────────────────
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


@router.get("/articles/{article_id}", response_class=HTMLResponse)
def article_detail(request: Request, article_id: int, msg: str = "") -> HTMLResponse:
    with Session(engine) as s:
        art = s.get(Article, article_id)
    if not art:
        return _redirect("/preview", "Статья не найдена")
    return templates.TemplateResponse(
        request, "article.html", {"a": art, "msg": msg}
    )


@router.post("/articles/{article_id}/publish")
def publish_article(article_id: int) -> RedirectResponse:
    from app.publisher import publish

    with Session(engine) as s:
        art = s.get(Article, article_id)
        if not art:
            return _redirect("/preview", "Не найдено")
        art.approved_at = datetime.now(timezone.utc)
        result = publish(art)
        art.status = "published" if result.get("published") else "approved"
        art.publish_note = result.get("note", "")
        s.add(art)
        s.commit()
    return _redirect(f"/articles/{article_id}", "Отправлено на публикацию")


@router.post("/articles/{article_id}/delete")
def delete_article(article_id: int) -> RedirectResponse:
    with Session(engine) as s:
        art = s.get(Article, article_id)
        if art:
            s.delete(art)
            s.commit()
    return _redirect("/preview", "Статья удалена")

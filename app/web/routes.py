"""Маршруты WEB-админки: сайты, источники, превью по сайтам, статьи, настройки."""

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.requests import Request

from app import scheduler, services
from app.config import get_settings
from app.db.models import (
    LANGUAGES,
    WEEKDAYS,
    AppConfig,
    Article,
    Site,
    Source,
)
from app.db.session import engine
from app.llm.client import LLMClient, LLMError

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DEEPSEEK_MODELS = [
    ("deepseek-v4-flash", "DeepSeek V4 Flash — быстрая и дешёвая"),
    ("deepseek-v4-pro", "DeepSeek V4 Pro — мощнее, качественнее"),
]
STATUS_LABELS = {
    "draft": "Черновики",
    "scheduled": "Запланировано",
    "published": "Опубликовано",
    "failed": "Ошибки",
}


def _redirect(path: str, msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"{path}?msg={msg}", status_code=303)


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().tz)


def _to_local_str(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_tz()).strftime("%Y-%m-%dT%H:%M")


def _parse_local(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        naive = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=_tz()).astimezone(timezone.utc)


# ── Сайты ─────────────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def sites_page(request: Request, msg: str = "") -> HTMLResponse:
    with Session(engine) as s:
        sites = s.exec(select(Site).order_by(Site.id)).all()
    return templates.TemplateResponse(request, "sites.html", {"sites": sites, "msg": msg})


@router.post("/sites")
def add_site(name: str = Form(...)) -> RedirectResponse:
    with Session(engine) as s:
        site = Site(name=name.strip())
        s.add(site)
        s.commit()
        s.refresh(site)
    scheduler.reload_jobs()
    return _redirect(f"/sites/{site.id}", "Сайт создан — заполните настройки")


@router.get("/sites/{site_id}", response_class=HTMLResponse)
def site_page(request: Request, site_id: int, msg: str = "") -> HTMLResponse:
    with Session(engine) as s:
        site = s.get(Site, site_id)
        if not site:
            return _redirect("/", "Сайт не найден")
        sources = s.exec(
            select(Source).where(Source.site_id == site_id).order_by(Source.id)
        ).all()
    runs = [j for j in scheduler.jobs_info() if j["id"].endswith(f"-{site_id}")]
    return templates.TemplateResponse(
        request,
        "site.html",
        {
            "site": site,
            "sources": sources,
            "languages": LANGUAGES,
            "weekdays": WEEKDAYS,
            "site_langs": site.languages.split(","),
            "collect_days": site.collect_days.split(","),
            "publish_days": site.publish_days.split(","),
            "runs": runs,
            "msg": msg,
        },
    )


@router.post("/sites/{site_id}")
def save_site(
    site_id: int,
    name: str = Form(...),
    repo: str = Form(""),
    branch: str = Form("main"),
    articles_path: str = Form(""),
    template_ref: str = Form(""),
    langs: list[str] = Form(default=[]),
    collect_days: list[str] = Form(default=[]),
    collect_time: str = Form("09:00"),
    publish_days: list[str] = Form(default=[]),
    publish_time: str = Form("09:00"),
    publish_per_run: int = Form(3),
    enabled: bool = Form(False),
) -> RedirectResponse:
    allowed_l = {c for c, _ in LANGUAGES}
    allowed_d = {c for c, _ in WEEKDAYS}
    with Session(engine) as s:
        site = s.get(Site, site_id)
        if not site:
            return _redirect("/", "Сайт не найден")
        site.name = name.strip()
        site.repo = repo.strip()
        site.branch = branch.strip() or "main"
        site.articles_path = articles_path.strip()
        site.template_ref = template_ref.strip()
        site.languages = ",".join(c for c in langs if c in allowed_l)
        site.collect_days = ",".join(c for c in collect_days if c in allowed_d)
        site.collect_time = collect_time.strip() or "09:00"
        site.publish_days = ",".join(c for c in publish_days if c in allowed_d)
        site.publish_time = publish_time.strip() or "09:00"
        site.publish_per_run = max(1, publish_per_run)
        site.enabled = enabled
        s.add(site)
        s.commit()
    scheduler.reload_jobs()
    return _redirect(f"/sites/{site_id}", "Настройки сайта сохранены")


@router.post("/sites/{site_id}/delete")
def delete_site(site_id: int) -> RedirectResponse:
    with Session(engine) as s:
        site = s.get(Site, site_id)
        if site:
            for src in s.exec(select(Source).where(Source.site_id == site_id)).all():
                s.delete(src)
            s.delete(site)
            s.commit()
    scheduler.reload_jobs()
    return _redirect("/", "Сайт удалён")


@router.post("/sites/{site_id}/collect")
def collect_now(site_id: int) -> RedirectResponse:
    res = services.collect_and_generate(site_id)
    return _redirect(f"/sites/{site_id}", f"Собрано и подготовлено: {res.get('created', 0)}")


@router.post("/sites/{site_id}/publish-now")
def publish_now(site_id: int) -> RedirectResponse:
    res = services.run_publish(site_id)
    return _redirect(f"/sites/{site_id}", f"Опубликовано: {res.get('published', 0)}")


# ── Источники ─────────────────────────────────────────────────────────
@router.post("/sites/{site_id}/sources")
def add_source(site_id: int, name: str = Form(...), url: str = Form(...)) -> RedirectResponse:
    with Session(engine) as s:
        s.add(Source(site_id=site_id, name=name.strip(), url=url.strip()))
        s.commit()
    return _redirect(f"/sites/{site_id}", "Источник добавлен")


@router.post("/sources/{source_id}/delete")
def delete_source(source_id: int) -> RedirectResponse:
    with Session(engine) as s:
        src = s.get(Source, source_id)
        site_id = src.site_id if src else 0
        if src:
            s.delete(src)
            s.commit()
    return _redirect(f"/sites/{site_id}", "Источник удалён")


@router.get("/sources/{source_id}/preview", response_class=HTMLResponse)
def source_preview(request: Request, source_id: int) -> HTMLResponse:
    from app.scraper.rss import peek_feed

    with Session(engine) as s:
        src = s.get(Source, source_id)
        if not src:
            return _redirect("/", "Источник не найден")
        data = peek_feed(src.url)
        links = [e["link"] for e in data["entries"] if e["link"]]
        done = set()
        if links:
            done = set(
                s.exec(select(Article.source_url).where(Article.source_url.in_(links))).all()
            )
    for e in data["entries"]:
        e["processed"] = e["link"] in done
    return templates.TemplateResponse(
        request, "feed_preview.html", {"src": src, "data": data}
    )


@router.post("/process-one")
def process_one(
    site_id: int = Form(...),
    title: str = Form(""),
    link: str = Form(...),
    image: str = Form(""),
    summary: str = Form(""),
) -> RedirectResponse:
    with Session(engine) as s:
        existing = s.exec(select(Article).where(Article.source_url == link)).first()
        if existing:
            return _redirect(f"/articles/{existing.id}", "Уже обработано")
        site = s.get(Site, site_id)
        if not site:
            return _redirect("/", "Сайт не найден")
        config = s.get(AppConfig, 1) or AppConfig(id=1)
        text, img = services._fetch_full(link, image or None, summary)
        try:
            art = services.generate_article(
                config, LLMClient(), site=site, title=title, link=link, image=img, text=text
            )
        except LLMError as exc:
            return _redirect("/preview", f"Ошибка LLM: {str(exc)[:120]}")
        s.add(art)
        s.commit()
        s.refresh(art)
    return _redirect(f"/articles/{art.id}", "Новость обработана (черновик)")


# ── Превью по сайтам ──────────────────────────────────────────────────
@router.get("/preview", response_class=HTMLResponse)
def preview_page(request: Request, msg: str = "") -> HTMLResponse:
    groups = []
    with Session(engine) as s:
        sites = s.exec(select(Site).order_by(Site.id)).all()
        for site in sites:
            arts = s.exec(
                select(Article)
                .where(Article.site_id == site.id)
                .order_by(Article.created_at.desc())
            ).all()
            sections = {"draft": [], "scheduled": [], "published": [], "failed": []}
            for a in arts:
                sections.get(a.status, sections["draft"]).append(a)
            groups.append({"site": site, "sections": sections})
    return templates.TemplateResponse(
        request,
        "preview.html",
        {"groups": groups, "labels": STATUS_LABELS, "to_local": _to_local_str, "msg": msg},
    )


@router.get("/articles/{article_id}", response_class=HTMLResponse)
def article_detail(request: Request, article_id: int, msg: str = "") -> HTMLResponse:
    with Session(engine) as s:
        art = s.get(Article, article_id)
    if not art:
        return _redirect("/preview", "Статья не найдена")
    return templates.TemplateResponse(
        request,
        "article.html",
        {"a": art, "publish_local": _to_local_str(art.publish_at), "msg": msg},
    )


@router.post("/articles/{article_id}")
def save_article(
    article_id: int,
    title: str = Form(""),
    annotation: str = Form(""),
    body: str = Form(""),
    publish_at: str = Form(""),
) -> RedirectResponse:
    with Session(engine) as s:
        art = s.get(Article, article_id)
        if not art:
            return _redirect("/preview", "Не найдено")
        art.title = title
        art.annotation = annotation
        art.body = body
        dt = _parse_local(publish_at)
        art.publish_at = dt
        if dt and art.status == "draft":
            art.status = "scheduled"
        s.add(art)
        s.commit()
    return _redirect(f"/articles/{article_id}", "Сохранено")


@router.post("/articles/{article_id}/publish")
def publish_article(article_id: int) -> RedirectResponse:
    from app.publisher import publish

    with Session(engine) as s:
        art = s.get(Article, article_id)
        if not art:
            return _redirect("/preview", "Не найдено")
        result = publish(art)
        art.publish_note = result.get("note", "")
        if result.get("published"):
            art.status = "published"
            art.published_at = datetime.now(timezone.utc)
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


# ── Глобальные настройки LLM ──────────────────────────────────────────
@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = "") -> HTMLResponse:
    with Session(engine) as s:
        config = s.get(AppConfig, 1) or AppConfig(id=1)
    return templates.TemplateResponse(
        request, "settings.html", {"config": config, "msg": msg, "models": DEEPSEEK_MODELS}
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

"""Маршруты WEB-админки: сайты, источники, превью по сайтам, статьи, настройки."""

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, UploadFile
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
    IGAccount,
    IGPost,
    IGSource,
    Site,
    Source,
    TGAccount,
    TGPost,
    TGSource,
)
from app.db.session import engine
from app.llm.client import LLMClient, LLMError
from app.util import normalize_repo

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
            "site_langs": [("cs" if x == "cz" else x) for x in site.languages.split(",")],
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
    github_token: str = Form(""),
    path_pattern: str = Form("{lang}/blog/{slug}"),
    langs: list[str] = Form(default=[]),
    collect_days: list[str] = Form(default=[]),
    collect_time: str = Form("09:00"),
    publish_days: list[str] = Form(default=[]),
    publish_time: str = Form("09:00"),
    collect_limit: int = Form(3),
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
        site.repo = normalize_repo(repo)
        site.branch = branch.strip() or "main"
        if github_token.strip():
            site.github_token = github_token.strip()
        site.path_pattern = path_pattern.strip() or "{lang}/blog/{slug}"
        site.languages = ",".join(c for c in langs if c in allowed_l)
        site.collect_days = ",".join(c for c in collect_days if c in allowed_d)
        site.collect_time = collect_time.strip() or "09:00"
        site.publish_days = ",".join(c for c in publish_days if c in allowed_d)
        site.publish_time = publish_time.strip() or "09:00"
        site.collect_limit = max(1, collect_limit)
        site.publish_per_run = max(1, publish_per_run)
        site.enabled = enabled
        s.add(site)
        s.commit()
    scheduler.reload_jobs()
    return _redirect(f"/sites/{site_id}", "Настройки сайта сохранены")


@router.post("/sites/{site_id}/template")
async def upload_template(
    site_id: int,
    template_file: UploadFile | None = File(default=None),
    template_text: str = Form(""),
) -> RedirectResponse:
    content = ""
    if template_file is not None and template_file.filename:
        raw = await template_file.read()
        content = raw.decode("utf-8", errors="replace")
    elif template_text.strip():
        content = template_text
    else:
        return _redirect(f"/sites/{site_id}", "Не передан файл или текст шаблона")
    with Session(engine) as s:
        site = s.get(Site, site_id)
        if not site:
            return _redirect("/", "Сайт не найден")
        site.template = content
        s.add(site)
        s.commit()
    return _redirect(f"/sites/{site_id}", "Шаблон сохранён")


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


# ── Instagram: аккаунты ───────────────────────────────────────────────
IG_STATUS_LABELS = {
    "draft": "Черновики",
    "scheduled": "В пуле",
    "published": "Опубликовано",
    "failed": "Ошибки",
}


@router.get("/instagram", response_class=HTMLResponse)
def ig_accounts_page(request: Request, msg: str = "", check: int = 0) -> HTMLResponse:
    from app.instagram.updater import installed_version, latest_version

    with Session(engine) as s:
        accounts = s.exec(select(IGAccount).order_by(IGAccount.id)).all()
    ig_ver = {
        "installed": installed_version(),
        # PyPI дёргаем только по запросу (кнопка «Проверить»), чтобы не тормозить страницу
        "latest": latest_version() if check else None,
    }
    return templates.TemplateResponse(
        request, "ig_accounts.html",
        {"accounts": accounts, "msg": msg, "ig_ver": ig_ver},
    )


@router.post("/instagram/update")
def ig_update(version: str = Form("")) -> RedirectResponse:
    from app.instagram.updater import update

    res = update(version.strip())
    if res.get("ok"):
        msg = (f"instagrapi обновлён до {res.get('version')}. "
               "Если публикация уже шла — перезапустите контейнер.")
    else:
        msg = f"Не удалось обновить: {res.get('log', '')[-160:]}"
    return _redirect("/instagram", msg)


@router.post("/instagram")
def ig_add_account(name: str = Form(...)) -> RedirectResponse:
    with Session(engine) as s:
        acc = IGAccount(name=name.strip())
        s.add(acc)
        s.commit()
        s.refresh(acc)
    scheduler.reload_jobs()
    return _redirect(f"/instagram/{acc.id}", "Аккаунт создан — заполните настройки")


@router.get("/instagram/{account_id}", response_class=HTMLResponse)
def ig_account_page(request: Request, account_id: int, msg: str = "") -> HTMLResponse:
    with Session(engine) as s:
        acc = s.get(IGAccount, account_id)
        if not acc:
            return _redirect("/instagram", "Аккаунт не найден")
        sources = s.exec(
            select(IGSource).where(IGSource.account_id == account_id).order_by(IGSource.id)
        ).all()
        posts = s.exec(
            select(IGPost)
            .where(IGPost.account_id == account_id)
            .order_by(IGPost.created_at.desc())
        ).all()
    sections = {"draft": [], "scheduled": [], "published": [], "failed": []}
    for p in posts:
        sections.get(p.status, sections["draft"]).append(p)
    runs = [j for j in scheduler.jobs_info()
            if j["id"].startswith("ig-")
            and j["id"].split("-")[2:3] == [str(account_id)]]
    return templates.TemplateResponse(
        request,
        "ig_account.html",
        {
            "acc": acc,
            "sources": sources,
            "languages": LANGUAGES,
            "sections": sections,
            "labels": IG_STATUS_LABELS,
            "runs": runs,
            "msg": msg,
        },
    )


@router.post("/instagram/{account_id}")
def ig_save_account(
    account_id: int,
    name: str = Form(...),
    username: str = Form(""),
    password: str = Form(""),
    proxy: str = Form(""),
    language: str = Form("ru"),
    collect_time: str = Form("07:00"),
    post_time: str = Form("11:00"),
    story_times: str = Form("13:00,17:00,21:00"),
    collect_limit: int = Form(8),
    story_music: bool = Form(False),
    enabled: bool = Form(False),
) -> RedirectResponse:
    allowed_l = {c for c, _ in LANGUAGES}
    with Session(engine) as s:
        acc = s.get(IGAccount, account_id)
        if not acc:
            return _redirect("/instagram", "Аккаунт не найден")
        acc.story_music = story_music
        acc.name = name.strip()
        acc.username = username.strip()
        if password.strip():
            acc.password = password.strip()
        acc.proxy = proxy.strip()
        acc.language = language if language in allowed_l else "ru"
        acc.collect_time = collect_time.strip() or "07:00"
        acc.post_time = post_time.strip() or "11:00"
        acc.story_times = ",".join(
            t.strip() for t in story_times.split(",") if t.strip()
        ) or "13:00,17:00,21:00"
        acc.collect_limit = max(1, collect_limit)
        acc.enabled = enabled
        s.add(acc)
        s.commit()
    scheduler.reload_jobs()
    return _redirect(f"/instagram/{account_id}", "Настройки аккаунта сохранены")


@router.post("/instagram/{account_id}/delete")
def ig_delete_account(account_id: int) -> RedirectResponse:
    with Session(engine) as s:
        acc = s.get(IGAccount, account_id)
        if acc:
            for src in s.exec(
                select(IGSource).where(IGSource.account_id == account_id)
            ).all():
                s.delete(src)
            for p in s.exec(
                select(IGPost).where(IGPost.account_id == account_id)
            ).all():
                s.delete(p)
            s.delete(acc)
            s.commit()
    scheduler.reload_jobs()
    return _redirect("/instagram", "Аккаунт удалён")


@router.post("/instagram/{account_id}/login")
def ig_login(account_id: int, verification_code: str = Form("")) -> RedirectResponse:
    from app.instagram.service import login_account

    res = login_account(account_id, verification_code.strip())
    if res.get("ok"):
        msg = "Вход выполнен"
    elif res.get("challenge"):
        msg = "Нужен код подтверждения — введите его и повторите вход"
    else:
        msg = f"Не удалось войти: {res.get('note', '')[:120]}"
    return _redirect(f"/instagram/{account_id}", msg)


@router.post("/instagram/{account_id}/collect")
def ig_collect_now(account_id: int) -> RedirectResponse:
    from app.instagram.service import collect_account

    res = collect_account(account_id)
    return _redirect(f"/instagram/{account_id}",
                     f"Собрано в пул: {res.get('created', 0)}")


@router.post("/instagram/{account_id}/publish")
def ig_publish_now(account_id: int, kind: str = Form("post")) -> RedirectResponse:
    from app.instagram.service import run_ig_publish

    as_kind = "story" if kind == "story" else "post"
    res = run_ig_publish(account_id, as_kind, count=1)
    return _redirect(f"/instagram/{account_id}",
                     f"{res.get('note', '')} (опубликовано {res.get('published', 0)})")


# ── Instagram: источники и посты ──────────────────────────────────────
@router.post("/instagram/{account_id}/sources")
def ig_add_source(
    account_id: int,
    name: str = Form(...),
    url: str = Form(...),
    link_url: str = Form(""),
) -> RedirectResponse:
    with Session(engine) as s:
        s.add(IGSource(
            account_id=account_id, name=name.strip(), url=url.strip(),
            link_url=link_url.strip(),
        ))
        s.commit()
    return _redirect(f"/instagram/{account_id}", "Источник добавлен")


@router.post("/ig-sources/{source_id}")
def ig_edit_source(
    source_id: int,
    name: str = Form(...),
    url: str = Form(...),
    link_url: str = Form(""),
) -> RedirectResponse:
    with Session(engine) as s:
        src = s.get(IGSource, source_id)
        account_id = src.account_id if src else 0
        if src:
            src.name = name.strip()
            src.url = url.strip()
            src.link_url = link_url.strip()
            s.add(src)
            s.commit()
    return _redirect(f"/instagram/{account_id}", "Источник обновлён")


@router.post("/ig-sources/{source_id}/delete")
def ig_delete_source(source_id: int) -> RedirectResponse:
    with Session(engine) as s:
        src = s.get(IGSource, source_id)
        account_id = src.account_id if src else 0
        if src:
            s.delete(src)
            s.commit()
    return _redirect(f"/instagram/{account_id}", "Источник удалён")


@router.post("/ig-posts/{post_id}")
def ig_save_post(post_id: int, caption: str = Form("")) -> RedirectResponse:
    with Session(engine) as s:
        post = s.get(IGPost, post_id)
        if not post:
            return _redirect("/instagram", "Не найдено")
        post.caption = caption
        s.add(post)
        s.commit()
        account_id = post.account_id
    return _redirect(f"/instagram/{account_id}", "Подпись сохранена")


@router.post("/ig-posts/{post_id}/publish")
def ig_publish_post(post_id: int, kind: str = Form("post")) -> RedirectResponse:
    from app.instagram.client import IGChallengeRequired, IGClient, IGError
    from app.instagram.service import _persist_session, _send_post

    as_kind = "story" if kind == "story" else "post"
    with Session(engine) as s:
        post = s.get(IGPost, post_id)
        if not post:
            return _redirect("/instagram", "Не найдено")
        account_id = post.account_id
        acc = s.get(IGAccount, account_id)
        try:
            igc = IGClient(acc)
            igc.ensure_login()
        except IGChallengeRequired as exc:
            return _redirect(f"/instagram/{account_id}",
                             f"Нужен код подтверждения: {str(exc)[:100]}")
        except IGError as exc:
            return _redirect(f"/instagram/{account_id}", f"Ошибка входа: {str(exc)[:100]}")
        try:
            pk = _send_post(igc, acc, post, as_kind)
        except IGError as exc:
            post.status = "failed"
            post.publish_note = str(exc)[:300]
            s.add(post)
            s.commit()
            return _redirect(f"/instagram/{account_id}", f"Ошибка: {str(exc)[:100]}")
        post.status = "published"
        post.kind = as_kind
        post.ig_media_pk = pk
        post.published_at = datetime.now(timezone.utc)
        post.publish_note = "опубликовано вручную"
        s.add(post)
        _persist_session(s, acc, igc, "ok")
    return _redirect(f"/instagram/{account_id}", f"Опубликовано ({as_kind})")


@router.post("/ig-posts/{post_id}/delete")
def ig_delete_post(post_id: int) -> RedirectResponse:
    with Session(engine) as s:
        post = s.get(IGPost, post_id)
        account_id = post.account_id if post else 0
        if post:
            s.delete(post)
            s.commit()
    return _redirect(f"/instagram/{account_id}", "Удалено")


# ── Telegram: аккаунты ────────────────────────────────────────────────
@router.get("/telegram", response_class=HTMLResponse)
def tg_accounts_page(request: Request, msg: str = "") -> HTMLResponse:
    with Session(engine) as s:
        accounts = s.exec(select(TGAccount).order_by(TGAccount.id)).all()
    return templates.TemplateResponse(
        request, "tg_accounts.html", {"accounts": accounts, "msg": msg}
    )


@router.post("/telegram")
def tg_add_account(name: str = Form(...)) -> RedirectResponse:
    with Session(engine) as s:
        acc = TGAccount(name=name.strip())
        s.add(acc)
        s.commit()
        s.refresh(acc)
    scheduler.reload_jobs()
    return _redirect(f"/telegram/{acc.id}", "Аккаунт создан — заполните настройки")


@router.get("/telegram/{account_id}", response_class=HTMLResponse)
def tg_account_page(request: Request, account_id: int, msg: str = "") -> HTMLResponse:
    with Session(engine) as s:
        acc = s.get(TGAccount, account_id)
        if not acc:
            return _redirect("/telegram", "Аккаунт не найден")
        sources = s.exec(
            select(TGSource).where(TGSource.account_id == account_id).order_by(TGSource.id)
        ).all()
        posts = s.exec(
            select(TGPost)
            .where(TGPost.account_id == account_id)
            .order_by(TGPost.created_at.desc())
        ).all()
    sections = {"draft": [], "scheduled": [], "published": [], "failed": []}
    for p in posts:
        sections.get(p.status, sections["draft"]).append(p)
    runs = [j for j in scheduler.jobs_info()
            if j["id"].startswith("tg-")
            and j["id"].split("-")[2:3] == [str(account_id)]]
    return templates.TemplateResponse(
        request,
        "tg_account.html",
        {
            "acc": acc,
            "sources": sources,
            "languages": LANGUAGES,
            "sections": sections,
            "labels": IG_STATUS_LABELS,
            "runs": runs,
            "msg": msg,
        },
    )


@router.post("/telegram/{account_id}")
def tg_save_account(
    account_id: int,
    name: str = Form(...),
    bot_token: str = Form(""),
    chat_id: str = Form(""),
    language: str = Form("ru"),
    collect_time: str = Form("07:00"),
    post_times: str = Form("11:00,18:00"),
    collect_limit: int = Form(8),
    enabled: bool = Form(False),
) -> RedirectResponse:
    allowed_l = {c for c, _ in LANGUAGES}
    with Session(engine) as s:
        acc = s.get(TGAccount, account_id)
        if not acc:
            return _redirect("/telegram", "Аккаунт не найден")
        acc.name = name.strip()
        if bot_token.strip():
            acc.bot_token = bot_token.strip()
        acc.chat_id = chat_id.strip()
        acc.language = language if language in allowed_l else "ru"
        acc.collect_time = collect_time.strip() or "07:00"
        acc.post_times = ",".join(
            t.strip() for t in post_times.split(",") if t.strip()
        ) or "11:00,18:00"
        acc.collect_limit = max(1, collect_limit)
        acc.enabled = enabled
        s.add(acc)
        s.commit()
    scheduler.reload_jobs()
    return _redirect(f"/telegram/{account_id}", "Настройки аккаунта сохранены")


@router.post("/telegram/{account_id}/delete")
def tg_delete_account(account_id: int) -> RedirectResponse:
    with Session(engine) as s:
        acc = s.get(TGAccount, account_id)
        if acc:
            for src in s.exec(
                select(TGSource).where(TGSource.account_id == account_id)
            ).all():
                s.delete(src)
            for p in s.exec(
                select(TGPost).where(TGPost.account_id == account_id)
            ).all():
                s.delete(p)
            s.delete(acc)
            s.commit()
    scheduler.reload_jobs()
    return _redirect("/telegram", "Аккаунт удалён")


@router.post("/telegram/{account_id}/verify")
def tg_verify(account_id: int) -> RedirectResponse:
    from app.telegram.service import verify_account

    res = verify_account(account_id)
    msg = res.get("note", "")[:160] if res.get("ok") else f"Ошибка: {res.get('note', '')[:140]}"
    return _redirect(f"/telegram/{account_id}", msg)


@router.post("/telegram/{account_id}/collect")
def tg_collect_now(account_id: int) -> RedirectResponse:
    from app.telegram.service import collect_account

    res = collect_account(account_id)
    return _redirect(f"/telegram/{account_id}", f"Собрано в пул: {res.get('created', 0)}")


@router.post("/telegram/{account_id}/publish")
def tg_publish_now(account_id: int) -> RedirectResponse:
    from app.telegram.service import run_tg_publish

    res = run_tg_publish(account_id, count=1)
    return _redirect(f"/telegram/{account_id}",
                     f"{res.get('note', '')} (опубликовано {res.get('published', 0)})")


# ── Telegram: источники и посты ───────────────────────────────────────
@router.post("/telegram/{account_id}/sources")
def tg_add_source(
    account_id: int,
    name: str = Form(...),
    url: str = Form(...),
    link_url: str = Form(""),
) -> RedirectResponse:
    with Session(engine) as s:
        s.add(TGSource(
            account_id=account_id, name=name.strip(), url=url.strip(),
            link_url=link_url.strip(),
        ))
        s.commit()
    return _redirect(f"/telegram/{account_id}", "Источник добавлен")


@router.post("/tg-sources/{source_id}")
def tg_edit_source(
    source_id: int,
    name: str = Form(...),
    url: str = Form(...),
    link_url: str = Form(""),
) -> RedirectResponse:
    with Session(engine) as s:
        src = s.get(TGSource, source_id)
        account_id = src.account_id if src else 0
        if src:
            src.name = name.strip()
            src.url = url.strip()
            src.link_url = link_url.strip()
            s.add(src)
            s.commit()
    return _redirect(f"/telegram/{account_id}", "Источник обновлён")


@router.post("/tg-sources/{source_id}/delete")
def tg_delete_source(source_id: int) -> RedirectResponse:
    with Session(engine) as s:
        src = s.get(TGSource, source_id)
        account_id = src.account_id if src else 0
        if src:
            s.delete(src)
            s.commit()
    return _redirect(f"/telegram/{account_id}", "Источник удалён")


@router.post("/tg-posts/{post_id}")
def tg_save_post(post_id: int, caption: str = Form("")) -> RedirectResponse:
    with Session(engine) as s:
        post = s.get(TGPost, post_id)
        if not post:
            return _redirect("/telegram", "Не найдено")
        post.caption = caption
        s.add(post)
        s.commit()
        account_id = post.account_id
    return _redirect(f"/telegram/{account_id}", "Подпись сохранена")


@router.post("/tg-posts/{post_id}/publish")
def tg_publish_post(post_id: int) -> RedirectResponse:
    from app.telegram.client import TGClient, TGError

    with Session(engine) as s:
        post = s.get(TGPost, post_id)
        if not post:
            return _redirect("/telegram", "Не найдено")
        account_id = post.account_id
        acc = s.get(TGAccount, account_id)
        try:
            mid = TGClient(acc).send_post(post.caption, post.image_url, post.link_url or "")
        except TGError as exc:
            post.status = "failed"
            post.publish_note = str(exc)[:300]
            s.add(post)
            s.commit()
            return _redirect(f"/telegram/{account_id}", f"Ошибка: {str(exc)[:100]}")
        post.status = "published"
        post.message_id = mid
        post.published_at = datetime.now(timezone.utc)
        post.publish_note = "опубликовано вручную"
        s.add(post)
        s.commit()
    return _redirect(f"/telegram/{account_id}", "Опубликовано в Telegram")


@router.post("/tg-posts/{post_id}/delete")
def tg_delete_post(post_id: int) -> RedirectResponse:
    with Session(engine) as s:
        post = s.get(TGPost, post_id)
        account_id = post.account_id if post else 0
        if post:
            s.delete(post)
            s.commit()
    return _redirect(f"/telegram/{account_id}", "Удалено")


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

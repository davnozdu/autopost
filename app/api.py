"""HTTP API для автоматизации (Bearer-ключ).

Доступен по IP без cookie-сессии — для внешних автоматизаций и проверки.
Включается заданием API_KEY; пустой ключ → API отвечает 401.
Авторизация: заголовок `Authorization: Bearer <API_KEY>`.
"""

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app import scheduler, services
from app.config import get_settings
from app.db.models import Article, Site, Source
from app.db.session import engine
from app.llm.client import LLMClient, LLMError

api_router = APIRouter(prefix="/api")


def require_api_key(authorization: str = Header(default="")) -> None:
    key = get_settings().api_key
    if not key:
        raise HTTPException(status_code=401, detail="API disabled (no API_KEY set)")
    expected = f"Bearer {key}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _site_dict(s: Site) -> dict:
    return {
        "id": s.id, "name": s.name, "enabled": s.enabled, "repo": s.repo,
        "branch": s.branch, "languages": s.languages,
        "collect_days": s.collect_days, "collect_time": s.collect_time,
        "publish_days": s.publish_days, "publish_time": s.publish_time,
        "publish_per_run": s.publish_per_run,
        "has_template": bool((s.template or "").strip()),
        "has_token": bool(s.github_token),
    }


def _article_dict(a: Article) -> dict:
    return {
        "id": a.id, "site_id": a.site_id, "title": a.title, "slug": a.slug,
        "lang": a.lang, "status": a.status, "tag": a.tag,
        "annotation": a.annotation, "image_url": a.image_url,
        "publish_at": a.publish_at.isoformat() if a.publish_at else None,
        "published_at": a.published_at.isoformat() if a.published_at else None,
        "publish_note": a.publish_note,
    }


# ── статус ────────────────────────────────────────────────────────────
@api_router.get("/status", dependencies=[Depends(require_api_key)])
def status() -> dict:
    s = get_settings()
    with Session(engine) as db:
        sites = len(db.exec(select(Site)).all())
        arts = len(db.exec(select(Article)).all())
    return {
        "ok": True, "sites": sites, "articles": arts,
        "llm_provider": s.llm_provider, "llm_model": s.resolved_model(),
        "scheduler_jobs": scheduler.jobs_info(),
    }


# ── сайты ─────────────────────────────────────────────────────────────
class SiteIn(BaseModel):
    name: str


@api_router.get("/sites", dependencies=[Depends(require_api_key)])
def list_sites() -> list[dict]:
    with Session(engine) as db:
        return [_site_dict(s) for s in db.exec(select(Site).order_by(Site.id)).all()]


@api_router.post("/sites", dependencies=[Depends(require_api_key)])
def create_site(body: SiteIn) -> dict:
    with Session(engine) as db:
        site = Site(name=body.name.strip())
        db.add(site)
        db.commit()
        db.refresh(site)
        out = _site_dict(site)
    scheduler.reload_jobs()
    return out


@api_router.get("/sites/{site_id}", dependencies=[Depends(require_api_key)])
def get_site(site_id: int) -> dict:
    with Session(engine) as db:
        site = db.get(Site, site_id)
        if not site:
            raise HTTPException(404, "site not found")
        srcs = db.exec(select(Source).where(Source.site_id == site_id)).all()
        d = _site_dict(site)
        d["sources"] = [{"id": x.id, "name": x.name, "url": x.url, "enabled": x.enabled}
                        for x in srcs]
        return d


class SourceIn(BaseModel):
    name: str
    url: str


@api_router.post("/sites/{site_id}/sources", dependencies=[Depends(require_api_key)])
def add_source(site_id: int, body: SourceIn) -> dict:
    with Session(engine) as db:
        if not db.get(Site, site_id):
            raise HTTPException(404, "site not found")
        src = Source(site_id=site_id, name=body.name.strip(), url=body.url.strip())
        db.add(src)
        db.commit()
        db.refresh(src)
        return {"id": src.id, "name": src.name, "url": src.url}


@api_router.post("/sites/{site_id}/collect", dependencies=[Depends(require_api_key)])
def collect(site_id: int) -> dict:
    return services.collect_and_generate(site_id)


@api_router.post("/sites/{site_id}/publish", dependencies=[Depends(require_api_key)])
def publish_due(site_id: int) -> dict:
    return services.run_publish(site_id)


# ── статьи ────────────────────────────────────────────────────────────
@api_router.get("/articles", dependencies=[Depends(require_api_key)])
def list_articles(site_id: int | None = None, status: str | None = None) -> list[dict]:
    with Session(engine) as db:
        q = select(Article).order_by(Article.created_at.desc())
        if site_id is not None:
            q = q.where(Article.site_id == site_id)
        if status:
            q = q.where(Article.status == status)
        return [_article_dict(a) for a in db.exec(q).all()]


@api_router.get("/articles/{article_id}", dependencies=[Depends(require_api_key)])
def get_article(article_id: int) -> dict:
    with Session(engine) as db:
        a = db.get(Article, article_id)
        if not a:
            raise HTTPException(404, "article not found")
        d = _article_dict(a)
        d["body"] = a.body
        return d


@api_router.post("/articles/{article_id}/publish", dependencies=[Depends(require_api_key)])
def publish_article(article_id: int) -> dict:
    from datetime import datetime, timezone

    from app.publisher import publish

    with Session(engine) as db:
        a = db.get(Article, article_id)
        if not a:
            raise HTTPException(404, "article not found")
        result = publish(a)
        a.publish_note = result.get("note", "")
        if result.get("published"):
            a.status = "published"
            a.published_at = datetime.now(timezone.utc)
        db.add(a)
        db.commit()
    return result


# ── Instagram ─────────────────────────────────────────────────────────
@api_router.get("/ig/version", dependencies=[Depends(require_api_key)])
def ig_version(check: bool = False) -> dict:
    from app.instagram.updater import installed_version, latest_version

    return {
        "installed": installed_version(),
        "latest": latest_version() if check else None,
    }


@api_router.post("/ig/update", dependencies=[Depends(require_api_key)])
def ig_update(version: str = "") -> dict:
    from app.instagram.updater import update

    return update(version.strip())


@api_router.post("/ig/{account_id}/collect", dependencies=[Depends(require_api_key)])
def ig_collect(account_id: int) -> dict:
    from app.instagram.service import collect_account

    return collect_account(account_id)


@api_router.post("/ig/{account_id}/publish", dependencies=[Depends(require_api_key)])
def ig_publish(account_id: int, kind: str = "post") -> dict:
    from app.instagram.service import run_ig_publish

    return run_ig_publish(account_id, "story" if kind == "story" else "post", count=1)


@api_router.post("/ig/{account_id}/check-gif", dependencies=[Depends(require_api_key)])
def ig_check_gif(account_id: int) -> dict:
    from app.instagram.service import check_gif

    return check_gif(account_id)


# ── Telegram ──────────────────────────────────────────────────────────
@api_router.post("/tg/{account_id}/verify", dependencies=[Depends(require_api_key)])
def tg_verify(account_id: int) -> dict:
    from app.telegram.service import verify_account

    return verify_account(account_id)


@api_router.post("/tg/{account_id}/collect", dependencies=[Depends(require_api_key)])
def tg_collect(account_id: int) -> dict:
    from app.telegram.service import collect_account

    return collect_account(account_id)


@api_router.post("/tg/{account_id}/publish", dependencies=[Depends(require_api_key)])
def tg_publish(account_id: int) -> dict:
    from app.telegram.service import run_tg_publish

    return run_tg_publish(account_id, count=1)


# ── X (Twitter) ───────────────────────────────────────────────────────
@api_router.get("/x/version", dependencies=[Depends(require_api_key)])
def x_version(check: bool = False) -> dict:
    from app.x.updater import installed_version, latest_version

    return {"installed": installed_version(), "latest": latest_version() if check else None}


@api_router.post("/x/update", dependencies=[Depends(require_api_key)])
def x_update(version: str = "") -> dict:
    from app.x.updater import update

    return update(version.strip())


@api_router.post("/x/{account_id}/verify", dependencies=[Depends(require_api_key)])
def x_verify(account_id: int) -> dict:
    from app.x.service import verify_account

    return verify_account(account_id)


@api_router.post("/x/{account_id}/collect", dependencies=[Depends(require_api_key)])
def x_collect(account_id: int) -> dict:
    from app.x.service import collect_account

    return collect_account(account_id)


@api_router.post("/x/{account_id}/publish", dependencies=[Depends(require_api_key)])
def x_publish(account_id: int) -> dict:
    from app.x.service import run_x_publish

    return run_x_publish(account_id, count=1)


# ── Дайджесты ─────────────────────────────────────────────────────────
@api_router.post("/digest/{digest_id}/run", dependencies=[Depends(require_api_key)])
def digest_run(digest_id: int) -> dict:
    from app.digest.service import run_digest

    return run_digest(digest_id)


# ── LLM ───────────────────────────────────────────────────────────────
@api_router.post("/llm/test", dependencies=[Depends(require_api_key)])
def llm_test() -> dict:
    try:
        r = LLMClient().chat(system="Odpovídej stručně.", user="Napiš jednu větu.",
                             temperature=0.3)
    except LLMError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "provider": r.provider, "model": r.model, "text": r.text}

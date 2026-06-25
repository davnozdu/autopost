"""Бизнес-логика X (Twitter): сбор материалов в пул (равномерная ротация
источников + LLM-выбор внутри источника) и публикация твитов с картинкой.

Лимит твита — 280 символов; ссылка считается за 23 (X сам сокращает в t.co).
"""

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import httpx
from sqlmodel import Session, select

from app import services
from app.config import get_settings
from app.db.models import AppConfig, XAccount, XPost, XSource
from app.db.session import engine
from app.instagram.service import _rotate, _select_within, _shorten
from app.llm.client import LLMClient, LLMError
from app.llm.prompt import build_x_prompt, parse_ig_parts
from app.scraper.rss import peek_feed
from app.util import clean_image_url
from app.x.client import XClient, XError

X_LIMIT = 280
URL_LEN = 23              # X считает любую ссылку за 23 символа
X_MAX_HASHTAGS = 3
SELECT_PER_SOURCE = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _media_dir() -> Path:
    d = Path(get_settings().data_dir) / "x" / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _compose(body: str, link: str, tags: list[str]) -> str:
    s = (body or "").strip()
    tagline = " ".join("#" + t.lstrip("#") for t in tags if t.strip())
    if tagline:
        s += "\n" + tagline
    if link.strip():
        s += "\n" + link.strip()
    return s


def _eff_len(text: str, link: str) -> int:
    """Длина твита по правилам X: ссылка считается за 23 символа."""
    n = len(text)
    if link.strip() and link.strip() in text:
        n = n - len(link.strip()) + URL_LEN
    return n


def _fit(client: LLMClient, config: AppConfig, language: str, body: str,
         link: str, tags: list[str]) -> str:
    tags = tags[:X_MAX_HASHTAGS]
    cap = _compose(body, link, tags)
    if _eff_len(cap, link) <= X_LIMIT:
        return cap
    overhead = _eff_len(_compose("", link, tags), link)
    body = _shorten(client, config, language, body, max(40, X_LIMIT - overhead - 3))
    cap = _compose(body, link, tags)
    # финальная страховка: подрезать тело, если всё ещё длинно
    while _eff_len(cap, link) > X_LIMIT and len(body) > 10:
        body = body[: max(10, len(body) - 10)].rstrip()
        cap = _compose(body.rstrip(" .,;:") + "…", link, tags)
    return cap


def _download(url: str | None) -> Path | None:
    url = clean_image_url(url)
    if not url:
        return None
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 autopost"})
        r.raise_for_status()
        data = r.content
    except Exception:
        return None
    # привести к JPEG, чтобы X точно принял
    try:
        from PIL import Image

        img = Image.open(BytesIO(data)).convert("RGB")
        out = _media_dir() / "tmp.jpg"
        img.save(out, "JPEG", quality=90)
        return out
    except Exception:
        return None


def collect_account(account_id: int) -> dict:
    """Собрать твиты в пул, чередуя источники по кругу (равномерно)."""
    with Session(engine) as s:
        acc = s.get(XAccount, account_id)
        if not acc:
            return {"created": 0, "error": "no account"}
        sources = s.exec(
            select(XSource).where(
                XSource.account_id == account_id, XSource.enabled == True  # noqa: E712
            )
        ).all()
        if not sources:
            return {"created": 0, "note": "нет источников"}
        config = s.get(AppConfig, 1) or AppConfig(id=1)
        language = acc.language or config.language or "ru"
        client = LLMClient()

        pub_titles = s.exec(
            select(XPost.source_title).where(
                XPost.account_id == account_id, XPost.status == "published"
            )
        ).all()
        pub_norm = {services._norm_title(t) for t in pub_titles}

        have = len(s.exec(
            select(XPost).where(
                XPost.account_id == account_id,
                XPost.status.in_(["draft", "scheduled"]),
            )
        ).all())
        need = max(0, acc.collect_limit - have)
        if need == 0:
            return {"created": 0, "note": "пул уже заполнен"}

        seen: set[str] = set()
        per_source: dict[int, list[dict]] = {}
        for src in sources:
            try:
                data = peek_feed(src.url)
            except Exception:
                continue
            cands = []
            for e in data["entries"]:
                link = e.get("link")
                if not link or link in seen:
                    continue
                seen.add(link)
                if s.exec(select(XPost).where(XPost.source_url == link)).first():
                    continue
                if services._norm_title(e.get("title", "")) in pub_norm:
                    continue
                e["_src_id"] = src.id
                e["_link_url"] = src.link_url.strip()
                cands.append(e)
            if cands:
                per_source[src.id] = _select_within(
                    client, config, language, cands, SELECT_PER_SOURCE
                )

        order = _rotate([x for x in sources if x.id in per_source], acc.last_source_id)
        queue: list[dict] = []
        rnd = 0
        while len(queue) < need and order:
            progressed = False
            for src in order:
                lst = per_source.get(src.id, [])
                if rnd < len(lst):
                    queue.append(lst[rnd])
                    progressed = True
                    if len(queue) >= need:
                        break
            if not progressed:
                break
            rnd += 1

        created = 0
        last_sid = acc.last_source_id
        for e in queue:
            title = e.get("title", "")
            link = e.get("link", "")
            summary = e.get("summary", "")
            link_url = e.get("_link_url", "")
            text, image = services._fetch_full(link, e.get("image"), summary)
            try:
                system, user = build_x_prompt(
                    config, {"title": title, "text": text}, language=language
                )
                res = client.chat(system, user, json_mode=True, temperature=0.8,
                                  model=(config.llm_model or None))
                body, hashtags = parse_ig_parts(res.text, fallback_text=summary)
            except LLMError:
                continue
            caption = _fit(client, config, language, body, link_url, hashtags)
            s.add(XPost(
                account_id=account_id,
                source_id=e.get("_src_id", 0),
                source_url=link,
                source_title=title,
                image_url=clean_image_url(image),
                caption=caption,
                link_url=link_url,
                status="scheduled",
            ))
            s.commit()
            last_sid = e.get("_src_id", last_sid)
            created += 1

        acc.last_source_id = last_sid
        s.add(acc)
        s.commit()
        return {"created": created}


def verify_account(account_id: int) -> dict:
    with Session(engine) as s:
        acc = s.get(XAccount, account_id)
        if not acc:
            return {"ok": False, "note": "no account"}
        try:
            info = XClient(acc).verify()
        except XError as exc:
            acc.verify_status = "error"
            acc.verify_note = str(exc)[:300]
            s.add(acc)
            s.commit()
            return {"ok": False, "note": str(exc)}
        acc.verify_status = "ok"
        acc.verify_note = f"аккаунт @{info['username']}"
        s.add(acc)
        s.commit()
        return {"ok": True, "note": acc.verify_note}


def _send(xc: XClient, post: XPost) -> str:
    """Собрать текст твита (подпись + ссылка) и опубликовать с картинкой."""
    text = post.caption or ""
    if post.link_url and post.link_url not in text:
        text = (text.rstrip() + "\n" + post.link_url).strip()
    img = _download(post.image_url)
    return xc.post(text, img)


def run_x_publish(account_id: int, count: int = 1) -> dict:
    """Опубликовать `count` твитов из пула (FIFO)."""
    with Session(engine) as s:
        acc = s.get(XAccount, account_id)
        if not acc:
            return {"published": 0, "error": "no account"}
        pending = s.exec(
            select(XPost)
            .where(XPost.account_id == account_id, XPost.status == "scheduled")
            .order_by(XPost.created_at)
        ).all()
        if not pending:
            return {"published": 0, "note": "нет материалов в пуле"}
        try:
            xc = XClient(acc)
        except XError as exc:
            return {"published": 0, "note": str(exc)}

        published = 0
        errors: list[str] = []
        for post in pending[:count]:
            try:
                tid = _send(xc, post)
            except XError as exc:
                post.status = "failed"
                post.publish_note = str(exc)[:300]
                errors.append(str(exc))
                s.add(post)
                continue
            post.status = "published"
            post.tweet_id = tid
            post.published_at = _now()
            post.publish_note = "опубликовано"
            s.add(post)
            published += 1
        s.commit()
        note = f"опубликовано {published}"
        if errors:
            note += " | ошибки: " + "; ".join(e[:80] for e in errors[:2])
        return {"published": published, "note": note}

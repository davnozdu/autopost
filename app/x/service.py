"""Бизнес-логика X (Twitter): сбор материалов в пул (равномерная ротация
источников + LLM-выбор внутри источника) и публикация твитов (OAuth 2.0).

Лимит твита — 280 символов; ссылка считается за 23 (X сам сокращает в t.co).
Картинку не прикладываем — X показывает превью по ссылке на статью (og:image).
"""

from datetime import datetime, timezone

from sqlmodel import Session, select

from app import services
from app.db.models import AppConfig, XAccount, XPost, XSource
from app.db.session import engine
from app.instagram.service import _rotate, _select_within, _shorten
from app.llm.client import LLMClient, LLMError
from app.llm.prompt import build_x_prompt, parse_ig_parts
from app.scraper.rss import peek_feed
from app.util import clean_image_url
from app.x.client import XClient, XError

X_LIMIT = 280            # лимит твита; ссылки в основном твите нет (она в ответе)
X_MAX_HASHTAGS = 3
SELECT_PER_SOURCE = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _compose(body: str, tags: list[str]) -> str:
    """Текст основного твита: тело + хэштеги. Ссылку НЕ включаем — она идёт ответом."""
    s = (body or "").strip()
    tagline = " ".join("#" + t.lstrip("#") for t in tags[:X_MAX_HASHTAGS] if t.strip())
    if tagline:
        s += "\n\n" + tagline
    return s


def _fit(client: LLMClient, config: AppConfig, language: str, body: str,
         tags: list[str]) -> str:
    """Подпись основного твита в пределах 280 (ссылки в нём нет — она в ответе)."""
    cap = _compose(body, tags)
    if len(cap) <= X_LIMIT:
        return cap
    overhead = len(_compose("", tags))
    body = _shorten(client, config, language, body, max(40, X_LIMIT - overhead - 3))
    cap = _compose(body, tags)
    if len(cap) > X_LIMIT:
        cap = cap[: X_LIMIT - 1].rstrip() + "…"
    return cap


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

        known_urls = set(s.exec(select(XPost.source_url)).all())  # дедуп одним запросом
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
                if link in known_urls:
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
            caption = _fit(client, config, language, body, hashtags)
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
            XClient(acc).verify()
        except XError as exc:
            acc.verify_status = "error"
            acc.verify_note = str(exc)[:300]
            s.add(acc)
            s.commit()
            return {"ok": False, "note": str(exc)}
        acc.verify_status = "ok"
        acc.verify_note = "cookie валидны — постинг доступен"
        s.add(acc)
        s.commit()
        return {"ok": True, "note": acc.verify_note}


def _send(xc: "XClient", post: XPost) -> str:
    """Основной твит (без ссылки) + ссылка первым комментарием — лучше для охвата."""
    return xc.post(post.caption or "", link=post.link_url or "")


def _published_this_month(account_id: int) -> int:
    now = _now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with Session(engine) as s:
        return len(s.exec(
            select(XPost).where(
                XPost.account_id == account_id,
                XPost.status == "published",
                XPost.published_at >= month_start,
            )
        ).all())


def _pool_size(account_id: int) -> int:
    with Session(engine) as s:
        return len(s.exec(
            select(XPost).where(
                XPost.account_id == account_id, XPost.status == "scheduled"
            )
        ).all())


def run_x_publish(account_id: int, skippable: bool = False, count: int = 1) -> dict:
    """Опубликовать `count` твитов (FIFO) с учётом лимитов free-тарифа X.

    skippable=True (2-й и далее слот дня) → публикуем «через раз», чтобы выходило
    то 1, то 2 твита в день. Жёсткий месячный лимит (`monthly_limit`) не даёт
    превысить 500/мес. Если пул пуст — авто-добор.
    """
    import random

    with Session(engine) as s:
        acc = s.get(XAccount, account_id)
        if not acc:
            return {"published": 0, "error": "no account"}
        limit = acc.monthly_limit

    if skippable and random.random() < 0.5:
        return {"published": 0, "note": "пропуск слота (вариативность 1–2/день)"}
    if _published_this_month(account_id) >= limit:
        return {"published": 0, "note": f"достигнут месячный лимит {limit}"}
    if _pool_size(account_id) == 0:
        collect_account(account_id)

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

"""Бизнес-логика Telegram: сбор материалов в пул (с равномерной ротацией
источников и LLM-выбором внутри источника) и публикация постов в чат.

Аналогично Instagram, но проще: только посты (без сториз), картинку Telegram
берёт по URL, подпись до 1024 символов, ссылка кликабельная (HTML при отправке).
"""

from datetime import datetime, timezone
from pathlib import Path  # noqa: F401 (паритет импортов; не используется напрямую)

from sqlmodel import Session, select

from app import services
from app.config import get_settings
from app.db.models import AppConfig, TGAccount, TGPost, TGSource
from app.db.session import engine
# переиспуем общие хелперы соц-публикации
from app.instagram.service import _rotate, _select_within, _shorten
from app.llm.client import LLMClient, LLMError
from app.llm.prompt import build_tg_prompt, parse_ig_parts
from app.scraper.rss import peek_feed
from app.telegram.client import TG_CAPTION_LIMIT, TGClient, TGError
from app.util import clean_image_url

TG_MAX_HASHTAGS = 8
SELECT_PER_SOURCE = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _compose(body: str, hashtags: list[str]) -> str:
    cap = (body or "").strip()
    tags = [t.lstrip("#") for t in hashtags if t.strip()][:TG_MAX_HASHTAGS]
    if tags:
        cap = cap.rstrip() + "\n\n" + " ".join("#" + t for t in tags)
    return cap


def _fit(client: LLMClient, config: AppConfig, language: str, body: str,
         hashtags: list[str]) -> str:
    """Подпись в пределах лимита Telegram; иначе — саммари. Ссылки в посте нет
    (она идёт первым комментарием), поэтому используем весь лимит."""
    cap = _compose(body, hashtags)
    limit = TG_CAPTION_LIMIT
    if len(cap) <= limit:
        return cap
    overhead = len(_compose("", hashtags))
    body = _shorten(client, config, language, body, max(150, limit - overhead - 10))
    cap = _compose(body, hashtags)
    if len(cap) > limit:
        cap = cap[: limit - 1].rstrip() + "…"
    return cap


def _send(tg: "TGClient", acc: TGAccount, post: TGPost) -> str:
    """Опубликовать пост (без ссылки) + ссылку первым комментарием по шаблону аккаунта."""
    comment_html = ""
    if post.link_url:
        comment_html = TGClient.build_comment(acc.comment_template, post.link_url)
    return tg.send_post(post.caption or "", post.image_url, comment_html=comment_html)


def collect_account(account_id: int) -> dict:
    """Собрать материалы в пул, чередуя источники по кругу (равномерно)."""
    with Session(engine) as s:
        acc = s.get(TGAccount, account_id)
        if not acc:
            return {"created": 0, "error": "no account"}
        sources = s.exec(
            select(TGSource).where(
                TGSource.account_id == account_id, TGSource.enabled == True  # noqa: E712
            )
        ).all()
        if not sources:
            return {"created": 0, "note": "нет источников"}
        config = s.get(AppConfig, 1) or AppConfig(id=1)
        language = acc.language or config.language or "ru"
        client = LLMClient()

        pub_titles = s.exec(
            select(TGPost.source_title).where(
                TGPost.account_id == account_id, TGPost.status == "published"
            )
        ).all()
        pub_norm = {services._norm_title(t) for t in pub_titles}

        have = len(s.exec(
            select(TGPost).where(
                TGPost.account_id == account_id,
                TGPost.status.in_(["draft", "scheduled"]),
            )
        ).all())
        need = max(0, acc.collect_limit - have)
        if need == 0:
            return {"created": 0, "note": "пул уже заполнен"}

        # 1) кандидаты по источникам + LLM-выбор лучших ВНУТРИ источника
        known_urls = set(s.exec(select(TGPost.source_url)).all())  # дедуп одним запросом
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

        # 2) чередование по кругу
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

        # 3) генерация подписи на языке аккаунта
        created = 0
        last_sid = acc.last_source_id
        for e in queue:
            title = e.get("title", "")
            link = e.get("link", "")
            summary = e.get("summary", "")
            link_url = e.get("_link_url", "")
            text, image = services._fetch_full(link, e.get("image"), summary)
            try:
                system, user = build_tg_prompt(
                    config, {"title": title, "text": text}, language=language
                )
                res = client.chat(system, user, json_mode=True, temperature=0.8,
                                  model=(config.llm_model or None))
                body, hashtags = parse_ig_parts(res.text, fallback_text=summary)
            except LLMError:
                continue
            caption = _fit(client, config, language, body, hashtags)
            s.add(TGPost(
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
    """Проверить токен бота и доступ к чату; сохранить статус."""
    with Session(engine) as s:
        acc = s.get(TGAccount, account_id)
        if not acc:
            return {"ok": False, "note": "no account"}
        try:
            info = TGClient(acc).verify()
        except TGError as exc:
            acc.verify_status = "error"
            acc.verify_note = str(exc)[:300]
            s.add(acc)
            s.commit()
            return {"ok": False, "note": str(exc)}
        acc.verify_status = "ok"
        acc.verify_note = f"бот @{info['bot']} → чат «{info['chat']}»"
        s.add(acc)
        s.commit()
        return {"ok": True, "note": acc.verify_note}


def _pool_size(account_id: int) -> int:
    with Session(engine) as s:
        return len(s.exec(
            select(TGPost).where(
                TGPost.account_id == account_id, TGPost.status == "scheduled"
            )
        ).all())


def run_tg_publish(account_id: int, count: int = 1) -> dict:
    """Опубликовать `count` постов из пула (FIFO). Если пул пуст — авто-добор."""
    if _pool_size(account_id) == 0:
        collect_account(account_id)  # часовой режим: держим пул наполненным
    with Session(engine) as s:
        acc = s.get(TGAccount, account_id)
        if not acc:
            return {"published": 0, "error": "no account"}
        pending = s.exec(
            select(TGPost)
            .where(TGPost.account_id == account_id, TGPost.status == "scheduled")
            .order_by(TGPost.created_at)
        ).all()
        if not pending:
            return {"published": 0, "note": "нет материалов в пуле"}
        try:
            tg = TGClient(acc)
        except TGError as exc:
            _notify(f"Telegram «{acc.name}»", str(exc))
            return {"published": 0, "note": str(exc)}

        published = 0
        errors: list[str] = []
        for post in pending[:count]:
            try:
                mid = _send(tg, acc, post)
            except TGError as exc:
                post.status = "failed"
                post.publish_note = str(exc)[:300]
                errors.append(str(exc))
                s.add(post)
                continue
            post.status = "published"
            post.message_id = mid
            post.published_at = _now()
            post.publish_note = "опубликовано"
            s.add(post)
            published += 1
        s.commit()
        note = f"опубликовано {published}"
        if errors:
            note += " | ошибки: " + "; ".join(e[:80] for e in errors[:2])
            _notify(f"Telegram «{acc.name}» публикация", "; ".join(errors[:3]))
        return {"published": published, "note": note}


def _notify(area: str, detail: str) -> None:
    """Тихо отправить ошибку в бот мониторинга (если включён)."""
    try:
        from app.notify import notify_error
        notify_error(area, detail)
    except Exception:
        pass

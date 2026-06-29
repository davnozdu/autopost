"""Бизнес-логика Instagram: сбор материалов в пул, вход в аккаунт,
публикация постов и сториз. Используется планировщиком и кнопками админки.

Все источники обрабатываются одинаково: материал прогоняется через LLM
(summary + хэштеги на языке аккаунта), в подпись добавляется ссылка на сайт
источника. Источники чередуются по кругу (равномерно), а ВНУТРИ источника
лучший материал выбирает LLM.
"""

import re
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from app import services
from app.config import get_settings
from app.db.models import AppConfig, IGAccount, IGPost, IGSource
from app.db.session import engine
from app.instagram import media as ig_media
from app.instagram.client import IGChallengeRequired, IGClient, IGError
from app.llm.client import LLMClient, LLMError
from app.llm.prompt import (
    build_ig_prompt,
    build_select_prompt,
    build_shorten_prompt,
    parse_ig_parts,
    parse_selection,
)
from app.scraper.rss import peek_feed
from app.util import clean_image_url

IG_CAPTION_LIMIT = 2200   # лимит символов подписи поста в Instagram
IG_MAX_HASHTAGS = 30      # лимит хэштегов в посте
SELECT_PER_SOURCE = 5     # сколько лучших оставлять от одного источника при отборе


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _media_dir() -> Path:
    d = Path(get_settings().data_dir) / "ig" / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _compose_caption(body: str, link: str, hashtags: list[str]) -> str:
    """Собрать подпись: текст + ссылка на сайт + хэштеги (не более лимита)."""
    parts = [body.strip()]
    if link.strip():
        parts.append(f"Спасибо проекту {link.strip()}")
    cap = "\n\n".join(p for p in parts if p)
    tags = [t.lstrip("#") for t in hashtags if t.strip()][:IG_MAX_HASHTAGS]
    if tags:
        cap = cap.rstrip() + "\n\n" + " ".join("#" + t for t in tags)
    return cap


def _shorten(client: LLMClient, config: AppConfig, language: str, text: str,
             max_chars: int) -> str:
    """Сократить текст под лимит через LLM; при сбое — обрезать по словам."""
    try:
        system, user = build_shorten_prompt(text, max_chars, language)
        res = client.chat(system, user, json_mode=False, temperature=0.4,
                          model=(config.llm_model or None))
        out = res.text.strip()
        if out and len(out) <= max_chars + 50:
            return out
    except LLMError:
        pass
    cut = text[:max_chars].rsplit(" ", 1)[0].rstrip()
    return (cut + "…") if cut else text[:max_chars]


def _fit_caption(client: LLMClient, config: AppConfig, language: str, body: str,
                 link: str, hashtags: list[str]) -> str:
    """Подпись поста в пределах лимита Instagram; при превышении — саммари тела."""
    cap = _compose_caption(body, link, hashtags)
    if len(cap) <= IG_CAPTION_LIMIT:
        return cap
    # сколько символов занимает обвязка (ссылка + хэштеги) — остаток отдаём телу
    overhead = len(_compose_caption("", link, hashtags))
    target = max(200, IG_CAPTION_LIMIT - overhead - 20)
    body = _shorten(client, config, language, body, target)
    cap = _compose_caption(body, link, hashtags)
    if len(cap) > IG_CAPTION_LIMIT:
        cap = cap[: IG_CAPTION_LIMIT - 1].rstrip() + "…"
    return cap


def _rotate(sources: list[IGSource], last_source_id: int) -> list[IGSource]:
    """Упорядочить источники, начиная со следующего за последним использованным."""
    order = sorted(sources, key=lambda x: x.id)
    ids = [x.id for x in order]
    if last_source_id in ids:
        i = (ids.index(last_source_id) + 1) % len(order)
        order = order[i:] + order[:i]
    return order


def _select_within(client: LLMClient, config: AppConfig, language: str,
                   cands: list[dict], limit: int) -> list[dict]:
    """Из кандидатов ОДНОГО источника выбрать лучшие через LLM (дёшево: title+summary)."""
    if len(cands) <= limit:
        return cands
    items = [{"i": i, "title": e.get("title", ""), "summary": e.get("summary", "")}
             for i, e in enumerate(cands)]
    try:
        system, user = build_select_prompt(items, limit, f"Instagram ({language})")
        res = client.chat(system, user, json_mode=True, temperature=0.2,
                          model=(config.llm_model or None))
        idx = parse_selection(res.text, len(cands), limit)
    except LLMError:
        idx = None
    return [cands[i] for i in idx] if idx else cands[:limit]


def collect_account(account_id: int) -> dict:
    """Собрать материалы в пул, чередуя источники по кругу (равномерно).

    Внутри каждого источника лучший материал выбирает LLM; затем источники
    чередуются (round-robin), начиная со следующего за `last_source_id`, чтобы
    публикации шли из разных источников по очереди.
    """
    with Session(engine) as s:
        acc = s.get(IGAccount, account_id)
        if not acc:
            return {"created": 0, "error": "no account"}
        sources = s.exec(
            select(IGSource).where(
                IGSource.account_id == account_id, IGSource.enabled == True  # noqa: E712
            )
        ).all()
        if not sources:
            return {"created": 0, "note": "нет источников"}
        config = s.get(AppConfig, 1) or AppConfig(id=1)
        language = acc.language or config.language or "ru"
        client = LLMClient()

        # дедуп против уже опубликованного (по нормализованному заголовку)
        pub_titles = s.exec(
            select(IGPost.source_title).where(
                IGPost.account_id == account_id, IGPost.status == "published"
            )
        ).all()
        pub_norm = {services._norm_title(t) for t in pub_titles}

        # сколько ещё нужно добрать до лимита пула
        have = len(s.exec(
            select(IGPost).where(
                IGPost.account_id == account_id,
                IGPost.status.in_(["draft", "scheduled"]),
            )
        ).all())
        need = max(0, acc.collect_limit - have)
        if need == 0:
            return {"created": 0, "note": "пул уже заполнен"}

        # 1) по каждому источнику: кандидаты + LLM-выбор лучших ВНУТРИ источника
        known_urls = set(s.exec(select(IGPost.source_url)).all())  # дедуп одним запросом
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

        # 2) чередование по кругу: по одному из каждого источника за «круг»
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

        # 3) генерация подписи на языке аккаунта + создание постов
        created = 0
        last_sid = acc.last_source_id
        for e in queue:
            title = e.get("title", "")
            link = e.get("link", "")
            summary = e.get("summary", "")
            link_url = e.get("_link_url", "")
            text, image = services._fetch_full(link, e.get("image"), summary)
            try:
                system, user = build_ig_prompt(
                    config, {"title": title, "text": text}, language=language
                )
                res = client.chat(system, user, json_mode=True, temperature=0.8,
                                  model=(config.llm_model or None))
                body, hashtags = parse_ig_parts(res.text, fallback_text=summary)
            except LLMError:
                continue
            caption = _fit_caption(client, config, language, body, link_url, hashtags)
            s.add(IGPost(
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

        # запомнить курсор ротации для следующего сбора
        acc.last_source_id = last_sid
        s.add(acc)
        s.commit()
        return {"created": created}


def _persist_session(s: Session, acc: IGAccount, igc: IGClient,
                     status: str, note: str = "") -> None:
    acc.session_json = igc.session_json() or acc.session_json
    acc.login_status = status
    acc.login_note = note[:300]
    acc.last_login_at = _now()
    s.add(acc)
    s.commit()


def login_account(account_id: int, verification_code: str = "") -> dict:
    """Войти в аккаунт (для кнопки в админке). Сохраняет сессию и статус."""
    with Session(engine) as s:
        acc = s.get(IGAccount, account_id)
        if not acc:
            return {"ok": False, "note": "no account"}
        try:
            igc = IGClient(acc)
            igc.ensure_login(verification_code)
        except IGChallengeRequired as exc:
            acc.login_status = "challenge"
            acc.login_note = str(exc)[:300]
            s.add(acc)
            s.commit()
            return {"ok": False, "challenge": True, "note": str(exc)}
        except IGError as exc:
            acc.login_status = "error"
            acc.login_note = str(exc)[:300]
            s.add(acc)
            s.commit()
            return {"ok": False, "note": str(exc)}
        _persist_session(s, acc, igc, "ok", "Вход выполнен")
        return {"ok": True, "note": "Вход выполнен"}


def _story_overlay(post: IGPost) -> str:
    """Короткий текст для плашки сториз: 1–2 коротких предложения (БЕЗ хэштегов)."""
    cap = post.caption or ""
    body = re.sub(r"#\w+", "", cap)  # убрать хэштеги из тела
    body = re.sub(r"\s+", " ", body).strip()
    title = body or post.source_title or ""
    text, count = "", 0
    for sn in re.split(r"(?<=[.!?])\s+", title):
        if count >= 2 or (text and len(text) + len(sn) > 140):
            break
        text = (text + " " + sn).strip()
        count += 1
    if not text:
        text = title[:140]
    return text[:160]


def _send_post(igc: IGClient, acc: IGAccount, post: IGPost, as_kind: str) -> str:
    """Подготовить медиа и опубликовать (пост или сториз с текстом+музыкой). → pk."""
    if as_kind == "story":
        img = ig_media.prepare(
            post.image_url, _media_dir() / f"{post.id}-story.jpg", "story",
            overlay_title=_story_overlay(post), overlay_link=post.link_url or "",
        )
        if not img:
            raise IGError("нет/битая картинка")
        return igc.upload_story(img, caption=post.source_title or "",
                                link=post.link_url or "", with_music=acc.story_music)
    img = ig_media.prepare(post.image_url, _media_dir() / f"{post.id}-post.jpg", "post")
    if not img:
        raise IGError("нет/битая картинка")
    return igc.upload_photo(img, post.caption or "")


def run_ig_publish(account_id: int, as_kind: str, count: int = 1) -> dict:
    """Опубликовать `count` материалов из пула как пост (feed) или сториз.

    as_kind: "post" | "story". Берём самые ранние запланированные (FIFO),
    каждый материал расходуется один раз (пост ИЛИ сториз).
    """
    with Session(engine) as s:
        acc = s.get(IGAccount, account_id)
        if not acc:
            return {"published": 0, "error": "no account"}
        pending = s.exec(
            select(IGPost)
            .where(IGPost.account_id == account_id, IGPost.status == "scheduled")
            .order_by(IGPost.created_at)
        ).all()
        if not pending:
            return {"published": 0, "note": "нет материалов в пуле"}

        # вход один раз на прогон
        try:
            igc = IGClient(acc)
            igc.ensure_login()
        except IGChallengeRequired as exc:
            _persist_session(s, acc, igc, "challenge", str(exc))
            _notify(f"Instagram «{acc.name}» вход", f"нужен код подтверждения: {exc}")
            return {"published": 0, "note": f"нужен код подтверждения: {exc}"}
        except IGError as exc:
            acc.login_status = "error"
            acc.login_note = str(exc)[:300]
            s.add(acc)
            s.commit()
            _notify(f"Instagram «{acc.name}» вход", str(exc))
            return {"published": 0, "note": str(exc)}
        _persist_session(s, acc, igc, "ok")

        published = 0
        errors: list[str] = []
        for post in pending[:count]:
            try:
                pk = _send_post(igc, acc, post, as_kind)
            except IGError as exc:
                post.status = "failed"
                post.publish_note = str(exc)[:300]
                errors.append(str(exc))
                s.add(post)
                continue
            post.status = "published"
            post.kind = as_kind
            post.ig_media_pk = pk
            post.published_at = _now()
            post.publish_note = "опубликовано"
            if as_kind == "story" and igc.music_note:
                post.publish_note += " | " + igc.music_note
            s.add(post)
            published += 1
        s.commit()
        _persist_session(s, acc, igc, "ok")

        note = f"опубликовано {published} ({as_kind})"
        if errors:
            note += " | ошибки: " + "; ".join(e[:80] for e in errors[:2])
            _notify(f"Instagram «{acc.name}» публикация ({as_kind})",
                    "; ".join(errors[:3]))
        return {"published": published, "note": note}


def _notify(area: str, detail: str) -> None:
    """Тихо отправить ошибку в бот мониторинга (если включён)."""
    try:
        from app.notify import notify_error
        notify_error(area, detail)
    except Exception:
        pass


def publish_post(post_id: int, as_kind: str = "post") -> tuple[bool, str]:
    """Опубликовать один материал из пула по id (пост или сториз). → (ok, заметка)."""
    with Session(engine) as s:
        post = s.get(IGPost, post_id)
        if not post or post.status == "published":
            return False, "не найдено или уже опубликовано"
        acc = s.get(IGAccount, post.account_id)
        if not acc:
            return False, "аккаунт не найден"
        try:
            igc = IGClient(acc)
            igc.ensure_login()
        except IGChallengeRequired as exc:
            return False, f"нужен код подтверждения: {str(exc)[:120]}"
        except IGError as exc:
            return False, f"вход: {str(exc)[:120]}"
        kind = "story" if as_kind == "story" else "post"
        try:
            pk = _send_post(igc, acc, post, kind)
        except IGError as exc:
            post.status = "failed"
            post.publish_note = str(exc)[:300]
            s.add(post)
            s.commit()
            return False, str(exc)[:150]
        post.status = "published"
        post.kind = kind
        post.ig_media_pk = pk
        post.published_at = _now()
        post.publish_note = "опубликовано из бота"
        if kind == "story" and getattr(igc, "music_note", ""):
            post.publish_note += " | " + igc.music_note
        s.add(post)
        _persist_session(s, acc, igc, "ok")
        return True, f"Instagram «{acc.name}»: опубликовано ({kind})"

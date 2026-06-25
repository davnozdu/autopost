"""Бизнес-логика Instagram: сбор материалов в пул, вход в аккаунт,
публикация постов и сториз. Используется планировщиком и кнопками админки.

Две группы источников (IGSource.kind):
  • own — мои сайты: текст уже готов (прогнан LLM на сайте). Берём аннотацию,
          добавляем ссылку на сайт. LLM повторно НЕ вызываем.
  • rss — внешние RSS: материал гоним через LLM (подпись + хэштеги).
"""

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
from app.llm.prompt import build_ig_prompt, build_shorten_prompt, parse_ig_parts
from app.scraper.rss import peek_feed

CANDIDATE_POOL = 40       # сколько кандидатов рассматривать максимум
IG_CAPTION_LIMIT = 2200   # лимит символов подписи поста в Instagram
IG_MAX_HASHTAGS = 30      # лимит хэштегов в посте


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
        parts.append(f"Подробнее: {link.strip()}")
    cap = "\n\n".join(p for p in parts if p)
    tags = [t.lstrip("#") for t in hashtags if t.strip()][:IG_MAX_HASHTAGS]
    if tags:
        cap = cap.rstrip() + "\n\n" + " ".join("#" + t for t in tags)
    return cap


def _shorten(client: LLMClient, config: AppConfig, text: str, max_chars: int) -> str:
    """Сократить текст под лимит через LLM; при сбое — обрезать по словам."""
    try:
        system, user = build_shorten_prompt(text, max_chars, config.language)
        res = client.chat(system, user, json_mode=False, temperature=0.4,
                          model=(config.llm_model or None))
        out = res.text.strip()
        if out and len(out) <= max_chars + 50:
            return out
    except LLMError:
        pass
    cut = text[:max_chars].rsplit(" ", 1)[0].rstrip()
    return (cut + "…") if cut else text[:max_chars]


def _fit_caption(client: LLMClient, config: AppConfig, body: str, link: str,
                 hashtags: list[str]) -> str:
    """Подпись поста в пределах лимита Instagram; при превышении — саммари тела."""
    cap = _compose_caption(body, link, hashtags)
    if len(cap) <= IG_CAPTION_LIMIT:
        return cap
    # сколько символов занимает обвязка (ссылка + хэштеги) — остаток отдаём телу
    overhead = len(_compose_caption("", link, hashtags))
    target = max(200, IG_CAPTION_LIMIT - overhead - 20)
    body = _shorten(client, config, body, target)
    cap = _compose_caption(body, link, hashtags)
    if len(cap) > IG_CAPTION_LIMIT:
        cap = cap[: IG_CAPTION_LIMIT - 1].rstrip() + "…"
    return cap


def collect_account(account_id: int) -> dict:
    """Собрать материалы со всех источников аккаунта в пул черновиков IGPost."""
    with Session(engine) as s:
        acc = s.get(IGAccount, account_id)
        if not acc:
            return {"created": 0, "error": "no account"}
        sources = s.exec(
            select(IGSource).where(
                IGSource.account_id == account_id, IGSource.enabled == True  # noqa: E712
            )
        ).all()
        config = s.get(AppConfig, 1) or AppConfig(id=1)
        client = LLMClient()

        # 1) кандидаты со всех лент, дедуп по ссылке + против уже созданных IGPost
        seen: set[str] = set()
        candidates: list[tuple[IGSource, dict]] = []
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
                if s.exec(select(IGPost).where(IGPost.source_url == link)).first():
                    continue
                candidates.append((src, e))

        # дедуп против опубликованного по нормализованному заголовку
        pub_titles = s.exec(
            select(IGPost.source_title).where(
                IGPost.account_id == account_id, IGPost.status == "published"
            )
        ).all()
        pub_norm = {services._norm_title(t) for t in pub_titles}
        candidates = [
            (src, e) for (src, e) in candidates
            if services._norm_title(e.get("title", "")) not in pub_norm
        ]
        candidates = candidates[:CANDIDATE_POOL]

        # 2) сколько ещё нужно добрать до лимита пула
        have = s.exec(
            select(IGPost).where(
                IGPost.account_id == account_id,
                IGPost.status.in_(["draft", "scheduled"]),
            )
        ).all()
        need = max(0, acc.collect_limit - len(have))
        candidates = candidates[:need]

        created = 0
        for src, e in candidates:
            title = e.get("title", "")
            link = e.get("link", "")
            summary = e.get("summary", "")
            image = e.get("image")
            link_url = src.link_url.strip()
            # любой источник: материал через LLM → summary + хэштеги по содержимому
            text, image = services._fetch_full(link, image, summary)
            try:
                system, user = build_ig_prompt(config, {"title": title, "text": text})
                res = client.chat(system, user, json_mode=True, temperature=0.8,
                                  model=(config.llm_model or None))
                body, hashtags = parse_ig_parts(res.text, fallback_text=summary)
            except LLMError:
                continue
            caption = _fit_caption(client, config, body, link_url, hashtags)
            s.add(IGPost(
                account_id=account_id,
                source_url=link,
                source_title=title,
                image_url=image,
                caption=caption,
                link_url=link_url,
                status="scheduled",
            ))
            s.commit()
            created += 1
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
            return {"published": 0, "note": f"нужен код подтверждения: {exc}"}
        except IGError as exc:
            acc.login_status = "error"
            acc.login_note = str(exc)[:300]
            s.add(acc)
            s.commit()
            return {"published": 0, "note": str(exc)}
        _persist_session(s, acc, igc, "ok")

        published = 0
        errors: list[str] = []
        for post in pending[:count]:
            img_path = ig_media.prepare(
                post.image_url, _media_dir() / f"{post.id}-{as_kind}.jpg", as_kind
            )
            if not img_path:
                post.status = "failed"
                post.publish_note = "нет/битая картинка"
                s.add(post)
                continue
            try:
                if as_kind == "story":
                    pk = igc.upload_story(img_path, caption=post.source_title or "",
                                          link=post.link_url or "")
                else:
                    pk = igc.upload_photo(img_path, post.caption or "")
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
            s.add(post)
            published += 1
        s.commit()
        _persist_session(s, acc, igc, "ok")

        note = f"опубликовано {published} ({as_kind})"
        if errors:
            note += " | ошибки: " + "; ".join(e[:80] for e in errors[:2])
        return {"published": published, "note": note}

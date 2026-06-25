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
from app.llm.prompt import build_ig_prompt, parse_ig_caption
from app.scraper.rss import peek_feed

CANDIDATE_POOL = 40  # сколько кандидатов рассматривать максимум


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _media_dir() -> Path:
    d = Path(get_settings().data_dir) / "ig" / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _own_caption(summary: str, title: str, link: str) -> str:
    """Подпись для перезалива своего материала: текст + ссылка на сайт."""
    text = (summary or title or "").strip()
    if link.strip():
        text = (text + "\n\n" + f"Подробнее: {link.strip()}").strip()
    return text


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
            if src.kind == "own":
                # текст готов — только перезалив + ссылка на сайт
                if not image:
                    _, image = services._fetch_full(link, image, summary)
                caption = _own_caption(summary, title, acc.link_url)
            else:
                # внешний RSS — полная обработка через LLM
                text, image = services._fetch_full(link, image, summary)
                try:
                    system, user = build_ig_prompt(config, {"title": title, "text": text})
                    res = client.chat(system, user, json_mode=True, temperature=0.8,
                                      model=(config.llm_model or None))
                    caption = parse_ig_caption(res.text, fallback_text=summary)
                except LLMError:
                    continue
            s.add(IGPost(
                account_id=account_id,
                source_url=link,
                source_title=title,
                media_kind=src.kind,
                image_url=image,
                caption=caption,
                link_url=acc.link_url,
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

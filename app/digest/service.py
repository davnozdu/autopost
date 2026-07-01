"""Бизнес-логика дайджеста (соцсети).

Поток за один прогон (вечером по расписанию):
  1) собрать ПУЛ новостей из лент дайджеста — без LLM (только RSS);
  2) ранжировать по актуальности через Brave Search — без LLM (если задан ключ
     и тема), иначе по свежести ленты;
  3) ОДИН вызов LLM собирает итоговый пост из топ-N заголовков/аннотаций;
  4) создать пост целевой соцсети и сразу опубликовать.

Расход токенов = ровно один вызов LLM в день на дайджест (на вход — короткие
заголовки+аннотации, не полные тексты).
"""

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from sqlmodel import Session, select

from app.config import get_settings  # noqa: F401  (единообразие импорта)
from app.db.models import (
    AppConfig,
    Digest,
    DigestSource,
    IGAccount,
    IGPost,
    TGAccount,
    TGPost,
    XAccount,
    XPost,
)
from app.db.session import engine
from app.digest import brave
from app.llm.client import LLMClient, LLMError
from app.llm.prompt import build_digest_prompt, parse_ig_parts
from app.scraper.rss import peek_feed
from app.util import clean_image_url

# Лимит символов поста по площадке (caption). TG с фото ограничен 1024 — берём с
# запасом; X — короткий твит.
PLATFORM_MAXCHARS = {"ig": 2000, "tg": 1000, "x": 260}
HASHTAG_LIMIT = 8


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"\w{4,}", (text or "").lower())}


def _parse_ts(value: str):
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError, IndexError):
        return None


def _collect_pool(sources: list[DigestSource]) -> list[dict]:
    """Собрать новости из лент дайджеста (без LLM). Дедуп по ссылке/заголовку."""
    seen: set[str] = set()
    items: list[dict] = []
    for src in sources:
        try:
            data = peek_feed(src.url)
        except Exception:
            continue
        for e in data.get("entries", []):
            link = (e.get("link") or "").strip()
            key = link or (e.get("title") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            items.append({
                "title": e.get("title", ""),
                "summary": e.get("summary", ""),
                "link": link,
                "image": clean_image_url(e.get("image")),
                "_ts": _parse_ts(e.get("published", "")),
            })
    return items


def _rank(items: list[dict], brave_blobs: list[str], limit: int) -> list[dict]:
    """Отранжировать пул: релевантность тренду Brave (вес) + свежесть.

    brave_blobs пуст → ранжируем только по свежести (дата публикации, иначе
    порядок в ленте — обычно новые первыми).
    """
    brave_words: set[str] = set()
    for blob in brave_blobs:
        brave_words |= _tokens(blob)

    ts_list = [it["_ts"] for it in items if it.get("_ts")]
    newest = max(ts_list) if ts_list else None
    oldest = min(ts_list) if ts_list else None
    span = (newest - oldest).total_seconds() if newest and oldest and newest > oldest else 0
    n = len(items)

    scored = []
    for i, it in enumerate(items):
        rel = len(_tokens(it.get("title", "") + " " + it.get("summary", "")) & brave_words)
        if it.get("_ts") and span:
            rec = (it["_ts"] - oldest).total_seconds() / span
        else:
            rec = (n - i) / n if n else 0
        # релевантность Brave доминирует, свежесть — вторичный ключ
        scored.append((rel * 10 + rec, i, it))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [it for _, _, it in scored[: max(1, limit)]]


def _compose(caption: str, hashtags: list[str], limit: int) -> str:
    """Подпись = текст + хэштеги, в пределах лимита площадки."""
    cap = (caption or "").strip()
    tags = " ".join("#" + t.lstrip("#") for t in hashtags[:HASHTAG_LIMIT] if t.strip())
    full = (cap + ("\n\n" + tags if tags else "")).strip()
    if limit and len(full) > limit:
        # сперва пробуем сохранить хэштеги, ужав тело
        if tags and len(tags) + 4 < limit:
            body = cap[: limit - len(tags) - 4].rstrip()
            full = (body + "…\n\n" + tags).strip()
        else:
            full = full[: limit - 1].rstrip() + "…"
    return full


def _account_language(dg: Digest) -> tuple[bool, str]:
    """Проверить наличие целевого аккаунта и вернуть (есть?, язык аккаунта)."""
    model = {"ig": IGAccount, "tg": TGAccount, "x": XAccount}.get(dg.platform)
    if not model:
        return False, ""
    with Session(engine) as s:
        acc = s.get(model, dg.account_id)
    if not acc:
        return False, ""
    return True, getattr(acc, "language", "") or ""


def _publish(dg: Digest, caption: str, image: str | None) -> tuple[bool, str]:
    """Создать пост целевой площадки и сразу опубликовать (через её publish_post)."""
    with Session(engine) as s:
        if dg.platform == "ig":
            post = IGPost(account_id=dg.account_id, source_title=f"Дайджест · {dg.name}",
                          image_url=image, caption=caption, status="scheduled")
        elif dg.platform == "tg":
            post = TGPost(account_id=dg.account_id, source_title=f"Дайджест · {dg.name}",
                          image_url=image, caption=caption, status="scheduled")
        elif dg.platform == "x":
            post = XPost(account_id=dg.account_id, source_title=f"Дайджест · {dg.name}",
                         image_url=image, caption=caption, status="scheduled")
        else:
            return False, "неизвестная площадка"
        s.add(post)
        s.commit()
        s.refresh(post)
        post_id = post.id

    if dg.platform == "ig":
        from app.instagram.service import publish_post
        return publish_post(post_id, "post")
    if dg.platform == "tg":
        from app.telegram.service import publish_post
        return publish_post(post_id)
    from app.x.service import publish_post
    return publish_post(post_id)


def _finish(digest_id: int, note: str) -> None:
    with Session(engine) as s:
        dg = s.get(Digest, digest_id)
        if dg:
            dg.last_run_at = _now()
            dg.last_note = note[:300]
            s.add(dg)
            s.commit()


def run_digest(digest_id: int) -> dict:
    """Собрать и опубликовать итоговый пост-дайджест. Один вызов LLM на прогон."""
    with Session(engine) as s:
        dg = s.get(Digest, digest_id)
        if not dg:
            return {"ok": False, "note": "дайджест не найден"}
        sources = s.exec(
            select(DigestSource).where(
                DigestSource.digest_id == digest_id,
                DigestSource.enabled == True,  # noqa: E712
            )
        ).all()
        config = s.get(AppConfig, 1) or AppConfig(id=1)

    has_acc, acc_lang = _account_language(dg)
    if not has_acc:
        _finish(digest_id, "нет целевого аккаунта")
        return {"ok": False, "note": "нет целевого аккаунта"}
    if not sources:
        _finish(digest_id, "нет источников")
        return {"ok": False, "note": "нет источников"}

    language = (dg.language or acc_lang or config.language or "ru").strip()

    # 1) пул из RSS (без LLM)
    items = _collect_pool(sources)
    if not items:
        _finish(digest_id, "ленты пусты")
        return {"ok": False, "note": "ленты пусты"}

    # 2) ранжирование Brave (или по свежести) — без LLM
    brave_blobs: list[str] = []
    if dg.use_brave and config.brave_api_key.strip() and dg.brave_query.strip():
        brave_blobs = brave.search_titles(
            config.brave_api_key, dg.brave_query,
            freshness=dg.brave_freshness, lang=language,
        )
    top = _rank(items, brave_blobs, dg.collect_limit)

    # 3) ОДИН вызов LLM → итоговый пост
    maxc = PLATFORM_MAXCHARS.get(dg.platform, 1000)
    system, user = build_digest_prompt(top, dg.instructions, language, max_chars=maxc)
    try:
        res = LLMClient().chat(system, user, json_mode=True, temperature=0.7,
                               model=(config.llm_model or None))
        body, hashtags = parse_ig_parts(res.text)
    except LLMError as exc:
        _finish(digest_id, f"LLM: {exc}")
        return {"ok": False, "note": f"LLM: {exc}"}
    if not body.strip():
        _finish(digest_id, "пустой ответ LLM")
        return {"ok": False, "note": "пустой ответ LLM"}

    caption = _compose(body, hashtags, maxc)
    image = next((it.get("image") for it in top if it.get("image")), None)
    if not image and top and top[0].get("link"):
        # ни у одной новости нет картинки в ленте — берём og:image со страницы
        # верхней новости (один заход на страницу, токены не тратятся).
        try:
            from app.scraper.extract import extract_image, fetch_html
            link = top[0]["link"]
            image = clean_image_url(extract_image(fetch_html(link), link))
        except Exception:
            image = None

    # 4) публикация
    ok, note = _publish(dg, caption, image)
    full_note = ("опубликован: " if ok else "ошибка: ") + note
    _finish(digest_id, full_note)
    if not ok:
        try:
            from app.notify import notify_error
            notify_error(f"Дайджест «{dg.name}» ({dg.platform})", note)
        except Exception:
            pass
    return {"ok": ok, "note": note, "items": len(top), "brave": len(brave_blobs)}

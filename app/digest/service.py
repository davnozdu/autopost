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

import html as _html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from sqlmodel import Session, select

from app.config import get_settings  # noqa: F401  (единообразие импорта)
from app.db.models import (
    DEFAULT_MOVIE_INSTRUCTIONS,
    AppConfig,
    Digest,
    DigestSeen,
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
from app.llm.prompt import build_digest_prompt, build_movie_digest_prompt, parse_ig_parts
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
    if not value:
        return None
    # RFC 2822 (Torznab «Wed, 01 Jul 2026 …»)
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError, IndexError):
        pass
    # ISO 8601 (Prowlarr «2026-07-01T10:10:59Z»)
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
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

    language = (dg.language or acc_lang or config.language or "ru").strip()

    # Ветка «Новинки кино»: источники берутся автоматически из Prowlarr (или, как
    # запас, из ручных Torznab-URL). Проверку «нет источников» здесь не делаем.
    if dg.mode == "movies":
        return _run_movies_digest(dg, sources, config, language)

    if not sources:
        _finish(digest_id, "нет источников")
        return {"ok": False, "note": "нет источников"}

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


# ── Movies-дайджест (торренты через Torznab) ──────────────────────────
def _english_title(title: str) -> str:
    """Из «Русское / English» → английскую часть (лучше матчится в OMDb)."""
    if "/" in (title or ""):
        return title.split("/")[-1].strip()
    return (title or "").strip()


def _seen_keys(digest_id: int) -> set[str]:
    """Ключи уже опубликованных позиций дайджеста (для исключения повторов)."""
    with Session(engine) as s:
        rows = s.exec(
            select(DigestSeen.item_key).where(DigestSeen.digest_id == digest_id)
        ).all()
    return set(rows)


def _record_seen(digest_id: int, items: list[dict]) -> None:
    """Запомнить опубликованные позиции + подчистить совсем старые (>180 дней)."""
    from app.digest import release
    with Session(engine) as s:
        for it in items:
            key = release.norm_key(it.get("title", ""), it.get("year", ""),
                                   it.get("season", ""))
            s.add(DigestSeen(digest_id=digest_id, item_key=key,
                             title=(it.get("title") or "")[:200]))
        cutoff = datetime.now(timezone.utc) - timedelta(days=180)
        for old in s.exec(
            select(DigestSeen).where(DigestSeen.digest_id == digest_id,
                                     DigestSeen.created_at < cutoff)
        ).all():
            s.delete(old)
        s.commit()


def _build_movie_caption(items: list[dict], intro: str, hashtags: list[str],
                         max_chars: int = 1024) -> str:
    """Красиво оформленная HTML-подпись: вступление + список фильмов (жирное
    название, год, ⭐рейтинг, тип, жанр, описание) + строка про комментарий.

    Описания НЕ выкидываются целиком при нехватке места (лимит подписи к фото —
    1024), а адаптивно укорачиваются, чтобы влезли короткими."""
    intro = (intro or "").strip()
    footer = "⬇️ Ссылки на скачивание — в первом комментарии"
    tags = " ".join("#" + t.lstrip("#") for t in hashtags[:6] if t.strip())

    heads = []  # блок каждого фильма БЕЗ описания
    for i, it in enumerate(items, 1):
        title = _html.escape(it.get("omdb_title") or it.get("title") or it.get("raw_title", ""))
        year = f" ({it['year']})" if it.get("year") else ""
        meta = []
        if it.get("rating"):
            meta.append(f"⭐ {_html.escape(str(it['rating']))} {it.get('rating_src', '')}".strip())
        if it.get("is_series") or it.get("omdb_type") == "series":
            meta.append("сериал")
        if it.get("genre"):
            meta.append(_html.escape(str(it["genre"])))
        h = f"{i}. <b>{title}</b>{year}"
        if meta:
            h += "\n" + " · ".join(meta)
        heads.append(h)

    fixed = ([_html.escape(intro)] if intro else []) + heads + [footer] + ([tags] if tags else [])
    base_len = len("\n\n".join(fixed))
    room = max_chars - base_len - len(items) * 6      # запас на теги <i></i> и переводы строк
    per = room // max(1, len(items)) if room > 0 else 0

    blocks = [_html.escape(intro)] if intro else []
    for i, it in enumerate(items, 1):
        b = heads[i - 1]
        ov = (it.get("overview") or "").strip()
        if ov and per > 24:
            ov = ov.split(". ")[0].strip().rstrip(".")
            limit = min(200, per - 2)
            if len(ov) > limit:
                ov = ov[:limit].rstrip() + "…"
            b += f"\n<i>{_html.escape(ov)}</i>"
        blocks.append(b)
    blocks.append(footer)
    if tags:
        blocks.append(tags)
    cap = "\n\n".join(blocks)
    return cap[:max_chars] if len(cap) > max_chars else cap


def _movie_magnet_comment(items: list[dict]) -> str:
    """Первый комментарий: по каждому фильму — название, короткое описание и
    magnet-ссылка для скачивания (плейн-текст → Telegram делает magnet тапабельным).

    Если magnet нет — ссылка на страницу трекера. Download-ссылку Prowlarr НЕ
    публикуем (в ней apikey и адрес сервера)."""
    blocks = []
    for it in items:
        icon = "📺" if (it.get("is_series") or it.get("omdb_type") == "series") else "🎬"
        title = it.get("omdb_title") or it.get("title") or it.get("raw_title", "")
        year = f" ({it['year']})" if it.get("year") else ""
        link = it.get("magnet") or it.get("page_url") or ""
        if not link:
            continue
        block = f"{icon} {title}{year}"
        ov = (it.get("overview") or "").strip()
        if ov:
            ov = ov.split(". ")[0].strip().rstrip(".")
            if len(ov) > 220:
                ov = ov[:217].rstrip() + "…"
            block += f"\n{ov}"
        block += f"\n{link}"
        blocks.append(block)
    return "\n\n".join(blocks)


def _run_movies_digest(dg: Digest, sources: list, config: AppConfig, language: str) -> dict:
    """Подборка новинок с торрентов → пост в TG + magnet-ссылки первым комментарием."""
    if dg.platform != "tg":
        _finish(dg.id, "movies-дайджест доступен только для Telegram")
        return {"ok": False, "note": "movies только для Telegram"}

    from app.digest import prowlarr, ratings, torrentfile, torznab

    # 1) источники АВТОМАТИЧЕСКИ из Prowlarr (агрегирует все индексаторы). Запасной
    #    путь — ручные Torznab-URL в источниках, если Prowlarr не задан.
    raw: list[dict] = []
    if config.prowlarr_url.strip() and config.prowlarr_api_key.strip():
        raw = prowlarr.search(config.prowlarr_url, config.prowlarr_api_key,
                              dg.torznab_categories, limit=150)
    if not raw:
        for src in sources:
            raw += torznab.fetch(src.url, dg.torznab_categories, limit=100)
    if not raw:
        _finish(dg.id, "Prowlarr/Torznab: пусто или недоступно (проверьте URL/ключ и категории)")
        return {"ok": False, "note": "Prowlarr/Torznab: пусто/недоступно"}

    # 2) свернуть дубли, оставить «живые» раздачи (magnet добудем ниже)
    from app.digest import release
    items = torznab.dedup_best(raw)
    items = [x for x in items if x.get("seeders", 0) >= max(0, dg.min_seeders)]
    # фильтр по ГОДУ ВЫПУСКА: отсекаем старый каталог, залитый сегодня (год
    # известен и старше порога). Год неизвестен → не трогаем.
    if dg.max_age_years and dg.max_age_years > 0:
        cutoff = datetime.now(timezone.utc).year - dg.max_age_years

        def _recent(x):
            y = x.get("year")
            if not y:
                return True
            try:
                return int(y) >= cutoff
            except (TypeError, ValueError):
                return True
        items = [x for x in items if _recent(x)]
    if not items:
        _finish(dg.id, "нет свежих релизов (проверьте категории/год/сиды)")
        return {"ok": False, "note": "нет подходящих релизов"}

    # 3) ИСКЛЮЧИТЬ уже опубликованное (защита от повторов между днями)
    seen = _seen_keys(dg.id)
    fresh = [x for x in items
             if release.norm_key(x.get("title", ""), x.get("year", ""),
                                  x.get("season", "")) not in seen]
    if not fresh:
        _finish(dg.id, "все свежие релизы уже публиковались — повторов не будет")
        return {"ok": False, "note": "новых релизов нет (все уже были)"}

    # 4) ранжирование. База — СВЕЖЕСТЬ (новые загрузки сверху), сиды — вторичны.
    #    Опц. Brave (1 запрос, без токенов): поднимаем релизы, которые сейчас «на
    #    слуху» (премьеры, о которых пишут), и опускаем старые ре-апы с сегодняшней
    #    датой. Brave НЕ добавляет фильмов — только меняет порядок свежих раздач.
    brave_words: set[str] = set()
    if dg.use_brave and config.brave_api_key.strip() and dg.brave_query.strip():
        for blob in brave.search_titles(config.brave_api_key, dg.brave_query,
                                        freshness=dg.brave_freshness, lang=language):
            brave_words |= _tokens(blob)
    _old = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def _movie_score(x):
        words = _tokens(f"{x.get('title', '')} {_english_title(x.get('title', ''))} "
                        f"{x.get('year', '')}")
        rel = len(words & brave_words)  # 0, если Brave выключен/пусто → чистая свежесть
        return (rel, _parse_ts(x.get("pubdate", "")) or _old, x.get("seeders", 0))

    fresh.sort(key=_movie_score, reverse=True)
    top = fresh[: min(max(dg.collect_limit, 2), 5)]

    # 4) для выбранных: если magnet нет — добыть из .torrent (через download-ссылку
    #    Prowlarr); английское название для OMDb. Только 2–5 штук → дёшево.
    for it in top:
        if not it.get("magnet") and it.get("download_url"):
            nm = it.get("title") or it.get("raw_title")
            it["magnet"] = torrentfile.magnet_from_url(it["download_url"], name=nm) or ""
        it["en_title"] = _english_title(it.get("title", ""))

    # 5) рейтинг + постер (TMDb → OMDb, без токенов)
    for it in top:
        ratings.enrich(it, config.omdb_api_key, config.tmdb_api_key)

    # 6) ОДИН вызов LLM → подпись подборки
    instructions = (dg.instructions or "").strip() or DEFAULT_MOVIE_INSTRUCTIONS
    system, user = build_movie_digest_prompt(top, instructions, language, max_chars=900)
    try:
        res = LLMClient().chat(system, user, json_mode=True, temperature=0.7,
                               model=(config.llm_model or None))
        body, hashtags = parse_ig_parts(res.text)
    except LLMError as exc:
        _finish(dg.id, f"LLM: {exc}")
        return {"ok": False, "note": f"LLM: {exc}"}
    if not body.strip():
        _finish(dg.id, "пустой ответ LLM")
        return {"ok": False, "note": "пустой ответ LLM"}

    caption = _build_movie_caption(top, body, hashtags)
    posters = [it["poster"] for it in top if it.get("poster")]
    comment = _movie_magnet_comment(top)

    # 7) публикация: красивый пост (постеры альбомом) + magnet первым комментарием
    from app.telegram.client import TGClient, TGError
    with Session(engine) as s:
        acc = s.get(TGAccount, dg.account_id)
    if not acc:
        _finish(dg.id, "Telegram-аккаунт не найден")
        return {"ok": False, "note": "нет аккаунта"}
    try:
        client = TGClient(acc)
        if len(posters) >= 2:
            client.send_album(caption, posters, comment, comment_mode="", caption_mode="HTML")
        else:
            client.send_post(caption, posters[0] if posters else None, comment,
                             comment_mode="", caption_mode="HTML")
        rats = sum(1 for it in top if it.get("rating"))
        links = sum(1 for it in top if it.get("magnet") or it.get("page_url"))
        ok, note = True, (f"опубликовано в Telegram ({len(top)} шт.: постеров {len(posters)}, "
                          f"рейтингов {rats}, ссылок в комментарии {links})")
        if not posters and not rats:
            note += " · нет постеров/рейтингов — задайте ключ TMDb в Настройках"
    except TGError as exc:
        ok, note = False, str(exc)[:200]

    if ok:
        _record_seen(dg.id, top)  # запомнить, чтобы завтра не повторять

    _finish(dg.id, ("опубликован: " if ok else "ошибка: ") + note)
    if not ok:
        try:
            from app.notify import notify_error
            notify_error(f"Дайджест «{dg.name}» (movies/tg)", note)
        except Exception:
            pass
    return {"ok": ok, "note": note, "items": len(top)}

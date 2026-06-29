"""Интерактивный управляющий бот в Telegram (long-polling, без публичного URL).

Возможности (только для владельца — chat_id из настроек мониторинга):
  • /status   — здоровье аккаунтов, размеры пулов, ближайшие задачи;
  • /stats    — сегодняшняя сводка по площадкам;
  • /post     — мастер: выбрать площадку кнопками → прислать текст/фото → когда;
  • /ig /tg /x /all <текст> — опубликовать вручную ВНЕ ОЧЕРЕДИ (в обход робота),
    сразу или отложенно (первый токен — время «HH:MM» или «+30m»/«+2h»);
  • /cancel   — отменить мастер.

Управляющий бот ДОЛЖЕН быть отдельным от публикующих (свой токен): long-polling
getUpdates конфликтует с getUpdates публикующего Telegram-бота (комментарии).

Фоновый воркер запускается из lifespan приложения. Состояние мастера — в памяти
(один владелец, один процесс). Отложенные посты хранятся в БД (AdhocPost) и
публикуются задачей планировщика — переживают перезапуск.
"""

import random
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import (
    AdhocPost,
    AppConfig,
    IGAccount,
    TGAccount,
    XAccount,
)
from app.db.session import engine

_API = "https://api.telegram.org/bot{token}/{method}"
_FILE = "https://api.telegram.org/file/bot{token}/{path}"
_TIMEOUT = 35.0
_POLL = 25  # long-poll, сек

_PLATFORMS = {"ig": "Instagram", "tg": "Telegram", "x": "X"}

_thread: threading.Thread | None = None
_stop = threading.Event()
_pending: dict[int, dict] = {}  # chat_id → состояние мастера /post


# ── управление воркером ───────────────────────────────────────────────

def start_worker() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_worker_loop, name="tg-control-bot", daemon=True)
    _thread.start()


def stop_worker() -> None:
    _stop.set()


def _worker_loop() -> None:
    offset = 0
    token = None
    while not _stop.is_set():
        cfg = _cfg()
        if not cfg or not cfg.notify_enabled or not cfg.notify_bot_token:
            time.sleep(5)
            continue
        if cfg.notify_bot_token != token:
            token = cfg.notify_bot_token
            offset = 0  # сменили бота — начать с чистой очереди обновлений
        try:
            updates = _get_updates(token, offset)
        except Exception:
            time.sleep(5)
            continue
        for upd in updates:
            offset = upd.get("update_id", offset) + 1
            try:
                _handle_update(cfg, upd)
            except Exception as exc:  # один сбойный апдейт не должен ронять цикл
                _send(cfg, f"⚠️ Ошибка обработки команды: {exc}")


def _cfg() -> AppConfig | None:
    try:
        with Session(engine) as s:
            return s.get(AppConfig, 1)
    except Exception:
        return None


# ── низкоуровневый Telegram API ───────────────────────────────────────

def _call(token: str, method: str, payload: dict, timeout: float = _TIMEOUT) -> dict:
    r = httpx.post(_API.format(token=token, method=method), json=payload, timeout=timeout)
    return r.json()


def _get_updates(token: str, offset: int) -> list:
    data = _call(token, "getUpdates", {
        "offset": offset, "timeout": _POLL,
        "allowed_updates": ["message", "callback_query"],
    }, timeout=_POLL + 10)
    return data.get("result", []) if data.get("ok") else []


def _send(cfg: AppConfig, text: str, buttons: list | None = None) -> None:
    payload = {"chat_id": cfg.notify_chat_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons is not None:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        _call(cfg.notify_bot_token, "sendMessage", payload)
    except Exception:
        pass


def _answer_callback(token: str, cb_id: str, text: str = "") -> None:
    try:
        _call(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": text}, timeout=15)
    except Exception:
        pass


def _file_url(token: str, file_id: str) -> str:
    """Получить публичный (для бота) URL присланного фото."""
    try:
        data = _call(token, "getFile", {"file_id": file_id}, timeout=15)
        path = data.get("result", {}).get("file_path")
        return _FILE.format(token=token, path=path) if path else ""
    except Exception:
        return ""


def _largest_photo(msg: dict) -> str:
    photos = msg.get("photo") or []
    return photos[-1].get("file_id", "") if photos else ""


# ── маршрутизация обновлений ──────────────────────────────────────────

def _owner(cfg: AppConfig, chat_id) -> bool:
    return str(chat_id) == str(cfg.notify_chat_id).strip()


def _handle_update(cfg: AppConfig, upd: dict) -> None:
    cb = upd.get("callback_query")
    if cb:
        chat = (cb.get("message") or {}).get("chat", {}).get("id")
        if not _owner(cfg, chat):
            return
        _answer_callback(cfg.notify_bot_token, cb.get("id", ""))
        _handle_callback(cfg, chat, cb.get("data", ""))
        return

    msg = upd.get("message")
    if not msg:
        return
    chat = msg.get("chat", {}).get("id")
    if not _owner(cfg, chat):
        return

    text = (msg.get("text") or msg.get("caption") or "").strip()
    photo_id = _largest_photo(msg)
    image_url = _file_url(cfg.notify_bot_token, photo_id) if photo_id else ""

    if text.startswith("/"):
        _handle_command(cfg, chat, text, image_url)
    else:
        _route_message(cfg, chat, text, image_url)


def _route_message(cfg: AppConfig, chat, text: str, image_url: str) -> None:
    """Сообщение без слэша: продолжение мастера или новый приём контента."""
    st = _pending.get(chat)
    step = st.get("step") if st else None
    if step == "content":            # мастер /post ждёт текст/фото
        _continue_wizard(cfg, chat, text, image_url)
    elif step == "await_time":       # приём контента ждёт время
        _ingest_set_time(cfg, chat, text)
    elif step in ("confirm", "pick_platform", "mode"):
        _send(cfg, "Завершите текущий шаг кнопками или /cancel.")
    else:                            # любой другой контент → предложить подготовку
        _start_ingest(cfg, chat, text, image_url)


def _handle_command(cfg: AppConfig, chat, text: str, image_url: str) -> None:
    parts = text.split(maxsplit=1)
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("start", "help"):
        _send(cfg, _help_text())
    elif cmd == "status":
        _send(cfg, _status_text())
    elif cmd in ("stats", "digest"):
        from app.notify import build_daily_digest
        _send(cfg, build_daily_digest())
    elif cmd == "cancel":
        _pending.pop(chat, None)
        _send(cfg, "Отменено.")
    elif cmd == "post":
        _pending[chat] = {"step": "platform"}
        _send(cfg, "Куда опубликовать?", _platform_buttons())
    elif cmd == "queue":
        _send(cfg, "Очередь какой площадки показать?", [
            [{"text": "📷 Instagram", "callback_data": "q:ig"},
             {"text": "✈️ Telegram", "callback_data": "q:tg"},
             {"text": "🐦 X", "callback_data": "q:x"}],
        ])
    elif cmd == "collect":
        _run_collect(cfg)
    elif cmd in ("pause", "resume"):
        _toggle_platform(cfg, cmd, arg.lower().strip())
    elif cmd == "scheduled":
        _list_scheduled(cfg)
    elif cmd in ("ig", "tg", "x", "all"):
        plats = list(_PLATFORMS) if cmd == "all" else [cmd]
        if not arg and not image_url:
            _send(cfg, f"После /{cmd} напишите текст поста. "
                       f"Можно с временем: <code>/{cmd} 18:30 текст</code> или "
                       f"<code>/{cmd} +30m текст</code>.")
            return
        when, body = _split_when(arg)
        _dispatch(cfg, plats, body, image_url, when)
    else:
        _send(cfg, "Не понял команду. /help — список.")


def _handle_callback(cfg: AppConfig, chat, data: str) -> None:
    st = _pending.get(chat)
    if data.startswith("q:"):
        _show_queue(cfg, data[2:])
        return
    if data.startswith("pub:"):
        _, plat, *rest = data.split(":")
        pid = int(rest[0])
        kind = rest[1] if len(rest) > 1 else "post"
        ok, note = _publish_one(plat, pid, kind)
        _send(cfg, ("✅ " if ok else "⚠️ ") + note)
        return
    if data.startswith("del:"):
        _, plat, pid = data.split(":")
        _delete_one(plat, int(pid))
        _send(cfg, "🗑 Удалено из очереди.")
        return
    if data.startswith("ax:"):
        _cancel_scheduled(int(data[3:]))
        _send(cfg, "🗑 Отложенный пост отменён.")
        return
    if data == "prep:no":
        _pending.pop(chat, None)
        _send(cfg, "Ок, не готовлю.")
        return
    if data == "prep:yes" and st and st.get("step") == "confirm":
        _prepare_publication(cfg, chat)
        return
    if data.startswith("ipf:") and st and st.get("step") == "pick_platform":
        plat = data[4:]
        st["platforms"] = list(_PLATFORMS) if plat == "all" else [plat]
        st["step"] = "mode"
        _send(cfg, "Когда/как опубликовать?", _mode_buttons())
        return
    if data.startswith("im:") and st and st.get("step") == "mode":
        _ingest_mode(cfg, chat, data[3:])
        return
    if data.startswith("p:"):
        plat = data[2:]
        plats = list(_PLATFORMS) if plat == "all" else [plat]
        _pending[chat] = {"step": "content", "platforms": plats}
        names = ", ".join(_PLATFORMS[p] for p in plats)
        _send(cfg, f"Площадки: <b>{names}</b>.\nПришлите текст поста (можно фото с подписью).")
    elif data.startswith("w:") and st and st.get("step") == "when":
        when = None
        if data == "w:+1h":
            when = datetime.now(timezone.utc) + timedelta(hours=1)
        elif data == "w:+3h":
            when = datetime.now(timezone.utc) + timedelta(hours=3)
        plats, body, img = st["platforms"], st.get("text", ""), st.get("image_url", "")
        _pending.pop(chat, None)
        _dispatch(cfg, plats, body, img, when)


def _continue_wizard(cfg: AppConfig, chat, text: str, image_url: str) -> None:
    st = _pending.get(chat)
    if not st or st.get("step") != "content":
        return
    if not text and not image_url:
        _send(cfg, "Нужен текст или фото. /cancel — отмена.")
        return
    st.update(step="when", text=text, image_url=image_url)
    _send(cfg, "Когда опубликовать?", [
        [{"text": "Сейчас", "callback_data": "w:now"}],
        [{"text": "Через 1 ч", "callback_data": "w:+1h"},
         {"text": "Через 3 ч", "callback_data": "w:+3h"}],
    ])


# ── публикация ────────────────────────────────────────────────────────

def _dispatch(cfg: AppConfig, platforms: list[str], text: str,
              image_url: str, when: datetime | None) -> None:
    """Опубликовать сразу или поставить отложенно (вне очереди)."""
    if when and when > datetime.now(timezone.utc):
        with Session(engine) as s:
            s.add(AdhocPost(platforms=",".join(platforms), text=text,
                            image_url=image_url or "", publish_at=when, status="pending"))
            s.commit()
        local = when.astimezone(ZoneInfo(get_settings().tz)).strftime("%d.%m %H:%M")
        _send(cfg, f"🕓 Запланировано на <b>{local}</b>: "
                   f"{', '.join(_PLATFORMS[p] for p in platforms)}.")
        return
    _send(cfg, "Публикую…")
    note = _do_publish(platforms, text, image_url)
    _send(cfg, "✅ Готово.\n" + note)


def _do_publish(platforms: list[str], text: str, image_url: str) -> str:
    """Немедленная публикация по площадкам (все включённые аккаунты). → отчёт."""
    lines: list[str] = []
    image_url = (image_url or "").strip() or None
    for plat in platforms:
        try:
            if plat == "tg":
                lines += _publish_tg(text, image_url)
            elif plat == "x":
                lines += _publish_x(text)
            elif plat == "ig":
                lines += _publish_ig(text, image_url)
        except Exception as exc:
            lines.append(f"• {_PLATFORMS.get(plat, plat)}: ошибка — {exc}")
    return "\n".join(lines) if lines else "Нет подключённых аккаунтов."


def _publish_tg(text: str, image_url: str | None) -> list[str]:
    from app.telegram.client import TGClient, TGError
    out = []
    with Session(engine) as s:
        accs = s.exec(select(TGAccount).where(TGAccount.enabled == True)).all()  # noqa: E712
    for acc in accs:
        # бэклинк-указатель в первый комментарий: случайная ссылка из RSS-источников
        link = _random_source_link("tg", acc.id)
        comment = TGClient.build_comment(acc.comment_template, link) if link else ""
        try:
            TGClient(acc).send_post(_fit(text or "", _FIT["tg"]), image_url,
                                    comment_html=comment)
            out.append(f"• Telegram «{acc.name}»: опубликовано")
        except TGError as exc:
            out.append(f"• Telegram «{acc.name}»: ошибка — {exc}")
    return out


def _random_source_link(plat: str, account_id: int) -> str:
    """Случайная «Ссылка на сайт» из включённых RSS-источников аккаунта (бэклинк).

    Едино для всех площадок: сайт берётся из настроек RSS и различается
    (если источников несколько — выбор случайный, как в Telegram).
    """
    from app.db.models import IGSource, TGSource, XSource
    model = {"ig": IGSource, "tg": TGSource, "x": XSource}.get(plat)
    if not model:
        return ""
    with Session(engine) as s:
        srcs = s.exec(
            select(model).where(
                model.account_id == account_id, model.enabled == True)  # noqa: E712
        ).all()
    links = [(sr.link_url or "").strip() for sr in srcs if (sr.link_url or "").strip()]
    return random.choice(links) if links else ""


def _publish_x(text: str) -> list[str]:
    from app.x.client import XClient, XError
    out = []
    with Session(engine) as s:
        accs = s.exec(select(XAccount).where(XAccount.enabled == True)).all()  # noqa: E712
    for acc in accs:
        link = _random_source_link("x", acc.id)  # бэклинк из RSS → ответом под твитом
        try:
            XClient(acc).post(_fit(text or "", _FIT["x"]), link=link)
            out.append(f"• X «{acc.name}»: опубликовано")
        except XError as exc:
            out.append(f"• X «{acc.name}»: ошибка — {exc}")
    return out


def _publish_ig(text: str, image_url: str | None) -> list[str]:
    from app.instagram import media as ig_media
    from app.instagram.client import IGClient, IGError
    out = []
    if not image_url:
        return ["• Instagram: пропущено — нужна картинка (пришлите фото)"]
    with Session(engine) as s:
        accs = s.exec(select(IGAccount).where(IGAccount.enabled == True)).all()  # noqa: E712
    tmp = Path(get_settings().data_dir) / "bot"
    tmp.mkdir(parents=True, exist_ok=True)
    for acc in accs:
        link = _random_source_link("ig", acc.id)  # бэклинк из RSS → в подпись
        body = text or ""
        if link:
            body = (body.rstrip() + f"\n\nСпасибо проекту {link}").strip()
        try:
            igc = IGClient(acc)
            igc.ensure_login()
            img = ig_media.prepare(image_url, tmp / f"adhoc-{acc.id}.jpg", "post")
            if not img:
                out.append(f"• Instagram «{acc.name}»: битая картинка")
                continue
            igc.upload_photo(img, _fit(body, _FIT["ig"]))
            out.append(f"• Instagram «{acc.name}»: опубликовано")
        except IGError as exc:
            out.append(f"• Instagram «{acc.name}»: ошибка — {exc}")
    return out


def publish_due_adhoc() -> None:
    """Опубликовать отложенные ручные посты, у которых наступило время (планировщик)."""
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        due = s.exec(select(AdhocPost).where(AdhocPost.status == "pending")).all()
        due = [p for p in due if p.publish_at and _aware(p.publish_at) <= now]
    if not due:
        return
    cfg = _cfg()
    for post in due:
        note = _do_publish(post.platforms.split(","), post.text, post.image_url)
        with Session(engine) as s:
            row = s.get(AdhocPost, post.id)
            if row:
                row.status = "published"
                row.note = note[:1000]
                s.add(row)
                s.commit()
        if cfg:
            _send(cfg, f"🕓 Отложенный пост опубликован:\n{note}")


# ── очередь, модерация, управление ────────────────────────────────────

_QUEUE_LIMIT = 8  # сколько постов из очереди показывать за раз


def _send_item(cfg: AppConfig, text: str, image_url: str, buttons: list) -> None:
    """Отправить карточку поста: фото с подписью (если есть) или текст, + кнопки."""
    markup = {"inline_keyboard": buttons}
    try:
        if image_url:
            _call(cfg.notify_bot_token, "sendPhoto", {
                "chat_id": cfg.notify_chat_id, "photo": image_url,
                "caption": text[:1024], "parse_mode": "HTML", "reply_markup": markup,
            })
        else:
            _call(cfg.notify_bot_token, "sendMessage", {
                "chat_id": cfg.notify_chat_id, "text": text[:4096],
                "parse_mode": "HTML", "disable_web_page_preview": True,
                "reply_markup": markup,
            })
    except Exception:
        # если фото не приняли (битый URL) — пробуем текстом
        _send(cfg, text, buttons)


def _queue_models():
    from app.db.models import IGPost, TGPost, XPost
    return {"ig": IGPost, "tg": TGPost, "x": XPost}


def _show_queue(cfg: AppConfig, plat: str) -> None:
    model = _queue_models().get(plat)
    if not model:
        return
    with Session(engine) as s:
        rows = s.exec(
            select(model).where(model.status == "scheduled").order_by(model.created_at)
        ).all()
    if not rows:
        _send(cfg, f"{_PLATFORMS[plat]}: очередь пуста.")
        return
    _send(cfg, f"📋 {_PLATFORMS[plat]} — в очереди {len(rows)}"
               + (f", показываю первые {_QUEUE_LIMIT}" if len(rows) > _QUEUE_LIMIT else ""))
    for p in rows[:_QUEUE_LIMIT]:
        title = p.source_title or "(без названия)"
        body = (p.caption or "").strip().replace("\n", " ")
        text = f"<b>{_esc(title)}</b>\n{_esc(body[:350])}"
        _send_item(cfg, text, (p.image_url or "").strip(), _item_buttons(plat, p.id))


def _item_buttons(plat: str, pid: int) -> list:
    if plat == "ig":
        return [
            [{"text": "✅ В ленту", "callback_data": f"pub:ig:{pid}:post"},
             {"text": "📲 Сториз", "callback_data": f"pub:ig:{pid}:story"}],
            [{"text": "🗑 Удалить", "callback_data": f"del:ig:{pid}"}],
        ]
    return [[
        {"text": "✅ Опубликовать", "callback_data": f"pub:{plat}:{pid}"},
        {"text": "🗑 Удалить", "callback_data": f"del:{plat}:{pid}"},
    ]]


def _publish_one(plat: str, pid: int, kind: str = "post") -> tuple[bool, str]:
    if plat == "ig":
        from app.instagram.service import publish_post
        return publish_post(pid, kind)
    if plat == "tg":
        from app.telegram.service import publish_post
        return publish_post(pid)
    if plat == "x":
        from app.x.service import publish_post
        return publish_post(pid)
    return False, "неизвестная площадка"


def _delete_one(plat: str, pid: int) -> None:
    model = _queue_models().get(plat)
    if not model:
        return
    with Session(engine) as s:
        row = s.get(model, pid)
        if row:
            s.delete(row)
            s.commit()


def _run_collect(cfg: AppConfig) -> None:
    """Добрать пулы по всем включённым аккаунтам (ручной запуск сбора)."""
    _send(cfg, "Собираю материалы…")
    from app.instagram import service as ig_s
    from app.telegram import service as tg_s
    from app.x import service as x_s
    lines: list[str] = []
    with Session(engine) as s:
        ig_ids = [a.id for a in s.exec(select(IGAccount).where(IGAccount.enabled == True)).all()]  # noqa: E712
        tg_ids = [a.id for a in s.exec(select(TGAccount).where(TGAccount.enabled == True)).all()]  # noqa: E712
        x_ids = [a.id for a in s.exec(select(XAccount).where(XAccount.enabled == True)).all()]  # noqa: E712
    for aid in ig_ids:
        lines.append(f"• Instagram: +{ig_s.collect_account(aid).get('created', 0)}")
    for aid in tg_ids:
        lines.append(f"• Telegram: +{tg_s.collect_account(aid).get('created', 0)}")
    for aid in x_ids:
        lines.append(f"• X: +{x_s.collect_account(aid).get('created', 0)}")
    _send(cfg, "Готово.\n" + ("\n".join(lines) if lines else "Нет включённых аккаунтов."))


def _toggle_platform(cfg: AppConfig, action: str, plat: str) -> None:
    models = {"ig": IGAccount, "tg": TGAccount, "x": XAccount}
    model = models.get(plat)
    if not model:
        _send(cfg, "Укажите площадку: <code>/pause tg</code> или <code>/resume ig</code> "
                   "(ig / tg / x).")
        return
    on = action == "resume"
    n = 0
    with Session(engine) as s:
        for a in s.exec(select(model)).all():
            a.enabled = on
            s.add(a)
            n += 1
        s.commit()
    try:
        from app import scheduler
        scheduler.reload_jobs()
    except Exception:
        pass
    verb = "возобновлён" if on else "поставлен на паузу"
    _send(cfg, f"{_PLATFORMS[plat]}: автопилот {verb} ({n} акк.).")


def _list_scheduled(cfg: AppConfig) -> None:
    with Session(engine) as s:
        rows = s.exec(
            select(AdhocPost).where(AdhocPost.status == "pending").order_by(AdhocPost.publish_at)
        ).all()
    if not rows:
        _send(cfg, "Отложенных ручных постов нет.")
        return
    tz = ZoneInfo(get_settings().tz)
    _send(cfg, f"🕓 Отложенных постов: {len(rows)}")
    for p in rows:
        when = _aware(p.publish_at).astimezone(tz).strftime("%d.%m %H:%M") if p.publish_at else "—"
        names = ", ".join(_PLATFORMS.get(x, x) for x in p.platforms.split(","))
        text = f"<b>{when}</b> · {names}\n{_esc((p.text or '')[:300])}"
        _send_item(cfg, text, (p.image_url or "").strip(),
                   [[{"text": "🗑 Отменить", "callback_data": f"ax:{p.id}"}]])


def _cancel_scheduled(pid: int) -> None:
    with Session(engine) as s:
        row = s.get(AdhocPost, pid)
        if row and row.status == "pending":
            s.delete(row)
            s.commit()


# ── приём контента: репост / текст / ссылка → подготовка через LLM ─────

import re as _re

_URL_RE = _re.compile(r"https?://\S+")
_FIT = {"ig": 2200, "tg": 1024, "x": 280}


def _start_ingest(cfg: AppConfig, chat, text: str, image_url: str) -> None:
    """Запомнить присланный материал и предложить подготовить публикацию."""
    if not text and not image_url:
        return
    m = _URL_RE.search(text or "")
    link = m.group(0) if m else ""
    _pending[chat] = {"step": "confirm",
                      "src": {"text": text or "", "image_url": image_url or "", "link": link}}
    if link:
        what = f"ссылку: {link}"
    elif image_url:
        what = "фото с текстом" if text else "фото"
    else:
        what = "текст статьи"
    _send(cfg, f"Получил {what}.\nГотовить публикацию?", [
        [{"text": "✅ Да", "callback_data": "prep:yes"},
         {"text": "✖️ Нет", "callback_data": "prep:no"}],
    ])


def _prepare_publication(cfg: AppConfig, chat) -> None:
    """Извлечь контент (из ссылки при необходимости) и сгенерировать пост через LLM."""
    st = _pending.get(chat)
    if not st:
        return
    src = st["src"]
    _send(cfg, "Готовлю публикацию через LLM…")

    from app import services
    from app.db.models import AppConfig as _AC
    from app.llm.client import LLMClient, LLMError
    from app.llm.prompt import build_tg_prompt, parse_ig_parts
    from app.util import clean_image_url

    title, text, image = "", src["text"], src["image_url"]
    if src["link"]:
        # из ссылки извлекаем статью и картинку (og:image), если фото не прислали
        ftext, fimg = services._fetch_full(src["link"], image or None, src["text"])
        text = ftext or src["text"]
        image = image or (fimg or "")
    if not (text or "").strip():
        _pending.pop(chat, None)
        _send(cfg, "⚠️ Пустой материал — нечего готовить.")
        return

    with Session(engine) as s:
        config = s.get(_AC, 1) or _AC(id=1)
    try:
        system, user = build_tg_prompt(config, {"title": title, "text": text})
        res = LLMClient().chat(system, user, json_mode=True, temperature=0.8,
                               model=(config.llm_model or None))
        body, tags = parse_ig_parts(res.text, fallback_text=text)
    except LLMError as exc:
        _pending.pop(chat, None)
        _send(cfg, f"⚠️ LLM не ответил: {exc}")
        return

    caption = body.strip()
    tagline = " ".join("#" + t for t in tags[:8] if t)
    if tagline:
        caption = caption.rstrip() + "\n\n" + tagline
    image = clean_image_url(image) or ""

    st["prepared"] = {"text": caption, "image_url": image}
    st["step"] = "pick_platform"
    _send_item(cfg, f"<b>Готово к публикации:</b>\n{_esc(caption[:900])}", image, [])
    _send(cfg, "Куда опубликовать?", _platform_buttons("ipf"))


def _mode_buttons() -> list:
    return [
        [{"text": "📤 Опубликовать сейчас", "callback_data": "im:now"}],
        [{"text": "🗂 Поставить в очередь", "callback_data": "im:queue"}],
        [{"text": "⏭ Следующий слот", "callback_data": "im:next"},
         {"text": "⏰ В указанное время", "callback_data": "im:time"}],
    ]


def _ingest_mode(cfg: AppConfig, chat, mode: str) -> None:
    st = _pending.get(chat)
    if not st or "prepared" not in st:
        return
    plats = st["platforms"]
    text = st["prepared"]["text"]
    image = st["prepared"]["image_url"]

    if mode == "now":
        _pending.pop(chat, None)
        _send(cfg, "Публикую…")
        _send(cfg, "✅ Готово.\n" + _do_publish(plats, text, image))
    elif mode == "queue":
        _pending.pop(chat, None)
        note = _enqueue_native(plats, text, image)
        _send(cfg, "🗂 В очереди.\n" + note)
    elif mode == "next":
        _pending.pop(chat, None)
        _send(cfg, "⏭ " + _schedule_next_slot(plats, text, image))
    elif mode == "time":
        st["step"] = "await_time"
        _send(cfg, "Во сколько опубликовать? Пришлите время: <code>18:30</code> "
                   "или <code>+30m</code> / <code>+2h</code>.")


def _ingest_set_time(cfg: AppConfig, chat, text: str) -> None:
    st = _pending.get(chat)
    if not st or "prepared" not in st:
        return
    when, _ = _split_when((text or "").strip() + " .")  # хвост, чтобы корректно отделить токен
    if not when:
        _send(cfg, "Не понял время. Пример: <code>18:30</code> или <code>+45m</code>.")
        return
    plats = st["platforms"]
    prep = st["prepared"]
    _pending.pop(chat, None)
    _save_adhoc(plats, prep["text"], prep["image_url"], when)
    local = when.astimezone(ZoneInfo(get_settings().tz)).strftime("%d.%m %H:%M")
    _send(cfg, f"🕓 Запланировано на <b>{local}</b>: {', '.join(_PLATFORMS[p] for p in plats)}.")


def _save_adhoc(platforms: list[str], text: str, image_url: str, when: datetime) -> None:
    with Session(engine) as s:
        s.add(AdhocPost(platforms=",".join(platforms), text=text,
                        image_url=image_url or "", publish_at=when, status="pending"))
        s.commit()


def _schedule_next_slot(platforms: list[str], text: str, image_url: str) -> str:
    """Запланировать на ближайший плановый слот публикации каждой площадки."""
    out = []
    for plat in platforms:
        when = _next_run_for(plat)
        if not when:
            when = datetime.now(timezone.utc) + timedelta(hours=1)
        _save_adhoc([plat], text, image_url, when)
        local = when.astimezone(ZoneInfo(get_settings().tz)).strftime("%d.%m %H:%M")
        out.append(f"{_PLATFORMS[plat]} → {local}")
    return "Следующий слот:\n" + "\n".join("• " + o for o in out)


def _next_run_for(plat: str) -> datetime | None:
    """Время ближайшей плановой публикации для площадки (из задач планировщика)."""
    from app import scheduler
    prefix = f"{plat}-post"
    times = []
    for j in scheduler.jobs_info():
        if j.get("id", "").startswith(prefix) and j.get("next_run"):
            times.append(datetime.fromisoformat(j["next_run"]))
    if not times:
        return None
    return min(times).astimezone(timezone.utc)


def _enqueue_native(platforms: list[str], text: str, image_url: str) -> str:
    """Положить готовый пост в штатный пул площадок (status=scheduled) — как у робота."""
    from app.db.models import IGPost, TGPost, XPost
    models = {"ig": (IGPost, IGAccount), "tg": (TGPost, TGAccount), "x": (XPost, XAccount)}
    out = []
    with Session(engine) as s:
        for plat in platforms:
            model, acc_model = models[plat]
            acc = s.exec(select(acc_model).where(acc_model.enabled == True)).first()  # noqa: E712
            if not acc:
                out.append(f"• {_PLATFORMS[plat]}: нет включённого аккаунта")
                continue
            # бэклинк из RSS-источников. TG: робот вставит в комментарий по link_url;
            # X: ответом по link_url; IG (фид): впишем прямо в подпись.
            link_url = _random_source_link(plat, acc.id)
            caption = text or ""
            if plat == "ig" and link_url:
                caption = (caption.rstrip() + f"\n\nСпасибо проекту {link_url}").strip()
            s.add(model(account_id=acc.id, source_title="ручной пост",
                        caption=_fit(caption, _FIT[plat]), image_url=image_url or None,
                        link_url=link_url, status="scheduled"))
            out.append(f"• {_PLATFORMS[plat]} «{acc.name}»: добавлено в пул")
        s.commit()
    return "\n".join(out)


# ── вспомогательное ───────────────────────────────────────────────────

def _fit(text: str, limit: int) -> str:
    """Жёстко уложить текст в лимит площадки (по границе слова)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rsplit(" ", 1)[0].rstrip()
    return (cut or text[:limit - 1]) + "…"


def _esc(s: str) -> str:
    from html import escape
    return escape(s or "")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _split_when(arg: str) -> tuple[datetime | None, str]:
    """Отделить ведущий токен времени от текста. → (когда|None, текст)."""
    if not arg:
        return None, ""
    head, _, rest = arg.partition(" ")
    tz = ZoneInfo(get_settings().tz)
    now_local = datetime.now(tz)
    # +30m / +2h
    if head.startswith("+") and len(head) > 2 and head[-1] in "mhм":
        try:
            n = int(head[1:-1])
            delta = timedelta(minutes=n) if head[-1] in "mм" else timedelta(hours=n)
            return (now_local + delta).astimezone(timezone.utc), rest.strip()
        except ValueError:
            return None, arg
    # HH:MM
    if ":" in head:
        try:
            hh, mm = head.split(":")
            cand = now_local.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if cand <= now_local:
                cand += timedelta(days=1)
            return cand.astimezone(timezone.utc), rest.strip()
        except ValueError:
            return None, arg
    return None, arg


def _platform_buttons(prefix: str = "p") -> list:
    return [
        [{"text": "📷 Instagram", "callback_data": f"{prefix}:ig"},
         {"text": "✈️ Telegram", "callback_data": f"{prefix}:tg"}],
        [{"text": "🐦 X", "callback_data": f"{prefix}:x"},
         {"text": "🌐 Все площадки", "callback_data": f"{prefix}:all"}],
    ]


def _help_text() -> str:
    return (
        "🤖 <b>autopost — управление</b>\n\n"
        "/status — состояние аккаунтов и очередей\n"
        "/stats — сводка за сутки\n"
        "/queue — посмотреть очередь и опубликовать понравившееся\n"
        "/collect — добрать пулы сейчас\n"
        "/scheduled — отложенные ручные посты\n"
        "/pause /resume <i>ig|tg|x</i> — пауза/возобновление автопилота\n\n"
        "<b>Ручная публикация вне очереди:</b>\n"
        "/post — мастер с кнопками (площадка → текст/фото → когда)\n"
        "/ig /tg /x /all <i>текст</i> — в площадку (или во все)\n"
        "С временем: <code>/tg 18:30 текст</code> или <code>/all +30m текст</code>\n"
        "Фото — пришлите картинку с подписью-командой.\n\n"
        "<b>Приём контента:</b> просто перешлите боту репост, текст статьи "
        "или ссылку — он спросит «Готовить публикацию?», прогонит через LLM "
        "и предложит площадки и способ публикации.\n\n"
        "/cancel — отменить мастер"
    )


def _status_text() -> str:
    from app.db.models import IGPost, TGPost, XPost
    with Session(engine) as s:
        ig_accs = s.exec(select(IGAccount)).all()
        tg_accs = s.exec(select(TGAccount)).all()
        x_accs = s.exec(select(XAccount)).all()

        def _pool(model, aid):
            return len(s.exec(select(model).where(
                model.account_id == aid, model.status == "scheduled")).all())

        lines = ["📟 <b>Состояние</b>", ""]
        lines.append("📷 <b>Instagram</b>")
        for a in ig_accs:
            st = a.login_status or "—"
            flag = "🟢" if (a.enabled and st == "ok") else ("⚪️" if not a.enabled else "🔴")
            lines.append(f"{flag} {a.name}: вход {st}, в пуле {_pool(IGPost, a.id)}")
        lines.append("\n✈️ <b>Telegram</b>")
        for a in tg_accs:
            st = a.verify_status or "—"
            flag = "🟢" if (a.enabled and st == "ok") else ("⚪️" if not a.enabled else "🔴")
            lines.append(f"{flag} {a.name}: бот {st}, в пуле {_pool(TGPost, a.id)}")
        lines.append("\n🐦 <b>X</b>")
        for a in x_accs:
            st = a.verify_status or "—"
            flag = "🟢" if (a.enabled and st == "ok") else ("⚪️" if not a.enabled else "🔴")
            lines.append(f"{flag} {a.name}: cookie {st}, в пуле {_pool(XPost, a.id)}")

    from app import scheduler
    runs = sorted(
        (j for j in scheduler.jobs_info() if j.get("next_run")),
        key=lambda j: j["next_run"],
    )[:6]
    if runs:
        tz = ZoneInfo(get_settings().tz)
        lines.append("\n⏰ <b>Ближайшие задачи</b>")
        for j in runs:
            t = datetime.fromisoformat(j["next_run"]).astimezone(tz).strftime("%d.%m %H:%M")
            lines.append(f"• {t} — {j['id']}")
    return "\n".join(lines)

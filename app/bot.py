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
    elif chat in _pending:
        _continue_wizard(cfg, chat, text, image_url)
    # прочие сообщения игнорируем


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
        try:
            TGClient(acc).send_post(text or "", image_url, comment_html="")
            out.append(f"• Telegram «{acc.name}»: опубликовано")
        except TGError as exc:
            out.append(f"• Telegram «{acc.name}»: ошибка — {exc}")
    return out


def _publish_x(text: str) -> list[str]:
    from app.x.client import XClient, XError
    out = []
    with Session(engine) as s:
        accs = s.exec(select(XAccount).where(XAccount.enabled == True)).all()  # noqa: E712
    for acc in accs:
        try:
            XClient(acc).post(text or "", link="")
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
        try:
            igc = IGClient(acc)
            igc.ensure_login()
            img = ig_media.prepare(image_url, tmp / f"adhoc-{acc.id}.jpg", "post")
            if not img:
                out.append(f"• Instagram «{acc.name}»: битая картинка")
                continue
            igc.upload_photo(img, text or "")
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


# ── вспомогательное ───────────────────────────────────────────────────

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


def _platform_buttons() -> list:
    return [
        [{"text": "📷 Instagram", "callback_data": "p:ig"},
         {"text": "✈️ Telegram", "callback_data": "p:tg"}],
        [{"text": "🐦 X", "callback_data": "p:x"},
         {"text": "🌐 Все площадки", "callback_data": "p:all"}],
    ]


def _help_text() -> str:
    return (
        "🤖 <b>autopost — управление</b>\n\n"
        "/status — состояние аккаунтов и очередей\n"
        "/stats — сводка за сутки\n"
        "/post — опубликовать вручную (мастер с кнопками)\n\n"
        "<b>Быстрая публикация вне очереди:</b>\n"
        "/ig /tg /x /all <i>текст</i> — в площадку (или во все)\n"
        "С временем: <code>/tg 18:30 текст</code> или <code>/all +30m текст</code>\n"
        "Фото — пришлите картинку с подписью-командой.\n\n"
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

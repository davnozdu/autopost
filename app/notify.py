"""Telegram-бот мониторинга: ошибки в реальном времени + ежедневная сводка.

Отдельный от публикующих ботов канал связи: шлёт в личный чат админа
(или в служебный канал) уведомления об ошибках публикации/входа и раз в день —
сводку по площадкам (сколько опубликовано, что в очереди, где проблемы).

Настройки хранятся в AppConfig (редактируются в админке), не в env.
Все функции «тихие»: сбой отправки никогда не роняет основной поток.
"""

from datetime import datetime, timedelta, timezone
from html import escape

import httpx
from sqlmodel import Session, select

from app.db.models import (
    AppConfig,
    Article,
    IGAccount,
    IGPost,
    TGAccount,
    TGPost,
    XAccount,
    XPost,
)
from app.db.session import engine

_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 15.0
_THROTTLE_SECONDS = 600  # одинаковое сообщение об ошибке не чаще раза в 10 минут
_last_error_sent: dict[str, datetime] = {}


def _cfg() -> AppConfig | None:
    try:
        with Session(engine) as s:
            return s.get(AppConfig, 1)
    except Exception:
        return None


def _post(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """Низкоуровневая отправка сообщения ботом. Возвращает (ok, ошибка)."""
    if not token or not chat_id:
        return False, "не задан токен или chat_id"
    try:
        resp = httpx.post(
            _API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return False, f"сеть: {exc}"
    if resp.status_code >= 400:
        return False, f"{resp.status_code}: {resp.text[:200]}"
    return True, ""


def send(text: str) -> bool:
    """Отправить произвольное сообщение, если мониторинг включён и настроен."""
    cfg = _cfg()
    if not cfg or not cfg.notify_enabled:
        return False
    ok, _ = _post(cfg.notify_bot_token, cfg.notify_chat_id, text)
    return ok


def notify_error(area: str, detail: str) -> None:
    """Сообщить об ошибке в реальном времени (с защитой от спама одинаковыми)."""
    cfg = _cfg()
    if not cfg or not cfg.notify_enabled or not cfg.notify_errors:
        return
    detail = (detail or "").strip()
    key = f"{area}|{detail[:200]}"
    now = datetime.now(timezone.utc)
    last = _last_error_sent.get(key)
    if last and (now - last).total_seconds() < _THROTTLE_SECONDS:
        return  # недавно уже слали такое же — не дублируем
    _last_error_sent[key] = now
    text = (
        f"⚠️ <b>Ошибка · {escape(area)}</b>\n"
        f"{escape(detail[:1000]) or 'без описания'}"
    )
    _post(cfg.notify_bot_token, cfg.notify_chat_id, text)


def send_test() -> dict:
    """Тестовое сообщение (кнопка в админке). Возвращает {ok, note}."""
    cfg = _cfg()
    if not cfg:
        return {"ok": False, "note": "нет конфигурации"}
    ok, err = _post(
        cfg.notify_bot_token, cfg.notify_chat_id,
        "✅ <b>autopost</b>\nБот мониторинга подключён. Сюда будут приходить ошибки и ежедневная сводка.",
    )
    return {"ok": ok, "note": "сообщение отправлено" if ok else err}


# ── Статистика и ежедневная сводка ────────────────────────────────────

def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def stats_24h() -> dict:
    """Сводные счётчики за последние сутки + текущие очереди и проблемы."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    def _recent(rows, pred=lambda r: True) -> int:
        n = 0
        for r in rows:
            pa = _aware(r.published_at)
            if pa and pa >= cutoff and pred(r):
                n += 1
        return n

    with Session(engine) as s:
        # сайты
        articles = s.exec(select(Article)).all()
        art_pub = _recent([a for a in articles if a.status == "published"])
        art_queue = len([a for a in articles if a.status in ("scheduled", "draft")])

        ig_posts = s.exec(select(IGPost)).all()
        tg_posts = s.exec(select(TGPost)).all()
        x_posts = s.exec(select(XPost)).all()

        ig_accs = s.exec(select(IGAccount)).all()
        tg_accs = s.exec(select(TGAccount)).all()
        x_accs = s.exec(select(XAccount)).all()

    def _pool(rows) -> int:
        return len([r for r in rows if r.status == "scheduled"])

    return {
        "site": {"published": art_pub, "queue": art_queue},
        "ig": {
            "posts": _recent(ig_posts, lambda r: r.kind != "story"),
            "stories": _recent(ig_posts, lambda r: r.kind == "story"),
            "failed": len([r for r in ig_posts if r.status == "failed"]),
            "queue": _pool(ig_posts),
        },
        "tg": {
            "published": _recent(tg_posts),
            "failed": len([r for r in tg_posts if r.status == "failed"]),
            "queue": _pool(tg_posts),
        },
        "x": {
            "published": _recent(x_posts),
            "failed": len([r for r in x_posts if r.status == "failed"]),
            "queue": _pool(x_posts),
        },
        "issues": _account_issues(ig_accs, tg_accs, x_accs),
    }


def _account_issues(ig_accs, tg_accs, x_accs) -> list[str]:
    issues: list[str] = []
    for a in ig_accs:
        if a.enabled and a.login_status and a.login_status != "ok":
            issues.append(f"Instagram «{a.name}»: вход — {a.login_status}")
    for a in tg_accs:
        if a.enabled and a.verify_status and a.verify_status != "ok":
            issues.append(f"Telegram «{a.name}»: бот — {a.verify_status}")
    for a in x_accs:
        if a.enabled and a.verify_status and a.verify_status != "ok":
            issues.append(f"X «{a.name}»: cookie — {a.verify_status}")
    return issues


def build_daily_digest() -> str:
    st = stats_24h()
    lines = ["📊 <b>autopost — сводка за сутки</b>", ""]
    lines.append(
        f"🌐 Сайты: опубликовано <b>{st['site']['published']}</b>, "
        f"в очереди {st['site']['queue']}"
    )
    ig = st["ig"]
    lines.append(
        f"📷 Instagram: постов <b>{ig['posts']}</b>, сториз <b>{ig['stories']}</b>"
        f"{_fail(ig['failed'])}, в пуле {ig['queue']}"
    )
    tg = st["tg"]
    lines.append(
        f"✈️ Telegram: постов <b>{tg['published']}</b>"
        f"{_fail(tg['failed'])}, в пуле {tg['queue']}"
    )
    x = st["x"]
    lines.append(
        f"🐦 X: твитов <b>{x['published']}</b>"
        f"{_fail(x['failed'])}, в пуле {x['queue']}"
    )
    if st["issues"]:
        lines += ["", "⚠️ <b>Требуют внимания:</b>"]
        lines += [f"• {escape(i)}" for i in st["issues"]]
    return "\n".join(lines)


def _fail(n: int) -> str:
    return f", ошибок {n}" if n else ""


def send_daily_digest() -> dict:
    """Сформировать и отправить ежедневную сводку (вызывается планировщиком/кнопкой)."""
    cfg = _cfg()
    if not cfg or not cfg.notify_enabled or not cfg.notify_daily:
        return {"ok": False, "note": "сводка выключена"}
    ok, err = _post(cfg.notify_bot_token, cfg.notify_chat_id, build_daily_digest())
    return {"ok": ok, "note": "сводка отправлена" if ok else err}

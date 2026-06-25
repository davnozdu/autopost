"""Планировщик автопилота на APScheduler.

На каждый включённый сайт регистрируются две задачи:
  • сбор+генерация — по дням/времени сбора;
  • публикация — по дням/времени публикации.
Таймзона берётся из настроек (env TZ). Перерегистрация — при изменении сайтов.
"""

from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session, select

from app import services
from app.config import get_settings
from app.db.models import IGAccount, Site, TGAccount, XAccount
from app.db.session import engine
from app.instagram import service as ig_service
from app.telegram import service as tg_service
from app.x import service as x_service

_scheduler: BackgroundScheduler | None = None

IG_JITTER = 300  # случайный сдвиг публикаций Instagram, ±5 минут


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().tz)


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone=_tz())
    _scheduler.start()
    reload_jobs()


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def reload_jobs() -> None:
    """Пересобрать задачи из текущих настроек сайтов."""
    if _scheduler is None:
        return
    _scheduler.remove_all_jobs()
    with Session(engine) as s:
        sites = s.exec(select(Site).where(Site.enabled == True)).all()  # noqa: E712
        ig_accounts = s.exec(
            select(IGAccount).where(IGAccount.enabled == True)  # noqa: E712
        ).all()
        tg_accounts = s.exec(
            select(TGAccount).where(TGAccount.enabled == True)  # noqa: E712
        ).all()
        x_accounts = s.exec(
            select(XAccount).where(XAccount.enabled == True)  # noqa: E712
        ).all()
    tz = _tz()
    for site in sites:
        ch, cm = services._parse_hhmm(site.collect_time)
        ph, pm = services._parse_hhmm(site.publish_time)
        try:
            if site.collect_days.strip():
                _scheduler.add_job(
                    services.collect_and_generate,
                    CronTrigger(day_of_week=site.collect_days, hour=ch, minute=cm, timezone=tz),
                    args=[site.id], id=f"collect-{site.id}", replace_existing=True,
                )
            if site.publish_days.strip():
                _scheduler.add_job(
                    services.run_publish,
                    CronTrigger(day_of_week=site.publish_days, hour=ph, minute=pm, timezone=tz),
                    args=[site.id], id=f"publish-{site.id}", replace_existing=True,
                )
        except Exception:
            continue

    # Instagram-аккаунты: ежедневный сбор + 1 пост/день + сториз по временам.
    # jitter = случайный сдвиг времени публикации (естественность, меньше «механики»).
    for acc in ig_accounts:
        try:
            cch, ccm = services._parse_hhmm(acc.collect_time)
            _scheduler.add_job(
                ig_service.collect_account,
                CronTrigger(hour=cch, minute=ccm, timezone=tz),
                args=[acc.id], id=f"ig-collect-{acc.id}", replace_existing=True,
            )
            pph, ppm = services._parse_hhmm(acc.post_time)
            _scheduler.add_job(
                ig_service.run_ig_publish,
                CronTrigger(hour=pph, minute=ppm, timezone=tz, jitter=IG_JITTER),
                args=[acc.id, "post"], id=f"ig-post-{acc.id}", replace_existing=True,
            )
            for i, t in enumerate(x for x in acc.story_times.split(",") if x.strip()):
                sh, sm = services._parse_hhmm(t.strip())
                _scheduler.add_job(
                    ig_service.run_ig_publish,
                    CronTrigger(hour=sh, minute=sm, timezone=tz, jitter=IG_JITTER),
                    args=[acc.id, "story"], id=f"ig-story-{acc.id}-{i}",
                    replace_existing=True,
                )
        except Exception:
            continue

    # Telegram-аккаунты: сбор + посты. Режим «каждый час» (Telegram без лимита) или
    # по списку времён; во всех случаях случайный сдвиг ±jitter_min минут.
    for acc in tg_accounts:
        try:
            jit = max(0, acc.jitter_min) * 60
            cch, ccm = services._parse_hhmm(acc.collect_time)
            _scheduler.add_job(
                tg_service.collect_account,
                CronTrigger(hour=cch, minute=ccm, timezone=tz),
                args=[acc.id], id=f"tg-collect-{acc.id}", replace_existing=True,
            )
            if acc.post_every_hour:
                _scheduler.add_job(
                    tg_service.run_tg_publish,
                    CronTrigger(minute=0, timezone=tz, jitter=jit),
                    args=[acc.id], id=f"tg-post-{acc.id}-hourly", replace_existing=True,
                )
            else:
                for i, t in enumerate(x for x in acc.post_times.split(",") if x.strip()):
                    ph, pm = services._parse_hhmm(t.strip())
                    _scheduler.add_job(
                        tg_service.run_tg_publish,
                        CronTrigger(hour=ph, minute=pm, timezone=tz, jitter=jit),
                        args=[acc.id], id=f"tg-post-{acc.id}-{i}", replace_existing=True,
                    )
        except Exception:
            continue

    # X (Twitter): сбор + твиты по временам с джиттером. Второй и далее слоты —
    # «через раз» (skippable), чтобы выходило то 1, то 2 в день; плюс месячный лимит.
    for acc in x_accounts:
        try:
            jit = max(0, acc.jitter_min) * 60
            cch, ccm = services._parse_hhmm(acc.collect_time)
            _scheduler.add_job(
                x_service.collect_account,
                CronTrigger(hour=cch, minute=ccm, timezone=tz),
                args=[acc.id], id=f"x-collect-{acc.id}", replace_existing=True,
            )
            for i, t in enumerate(x for x in acc.post_times.split(",") if x.strip()):
                ph, pm = services._parse_hhmm(t.strip())
                _scheduler.add_job(
                    x_service.run_x_publish,
                    CronTrigger(hour=ph, minute=pm, timezone=tz, jitter=jit),
                    args=[acc.id, i > 0], id=f"x-post-{acc.id}-{i}", replace_existing=True,
                )
        except Exception:
            continue


def jobs_info() -> list[dict]:
    """Сводка ближайших запусков — для отображения в админке."""
    if _scheduler is None:
        return []
    out = []
    for job in _scheduler.get_jobs():
        out.append(
            {
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            }
        )
    return out

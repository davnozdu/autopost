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
from app.db.models import Site
from app.db.session import engine

_scheduler: BackgroundScheduler | None = None


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

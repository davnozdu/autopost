"""Подключение к SQLite и инициализация схемы."""

from pathlib import Path

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

_settings = get_settings()
DB_PATH = Path(_settings.data_dir) / "autopost.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)


def _column_default_sql(column) -> str:
    """Безопасный DEFAULT для ALTER TABLE ADD COLUMN на существующих строках."""
    try:
        pytype = column.type.python_type
    except (NotImplementedError, AttributeError):
        pytype = str
    if pytype is str:
        return "''"
    if pytype is bool or pytype is int:
        return "0"
    if pytype is float:
        return "0"
    return "NULL"


def _migrate_add_missing_columns() -> None:
    """Лёгкая авто-миграция: дописать недостающие колонки в существующие таблицы.

    SQLModel.create_all создаёт только отсутствующие таблицы, но не добавляет
    новые поля в уже существующие. Здесь сравниваем модель с фактической схемой
    и добавляем недостающие колонки (ALTER TABLE ADD COLUMN). Данные сохраняются.
    """
    insp = inspect(engine)
    with engine.begin() as conn:
        for table_name, table in SQLModel.metadata.tables.items():
            if not insp.has_table(table_name):
                continue  # новую таблицу создаст create_all
            existing = {c["name"] for c in insp.get_columns(table_name)}
            model_cols = {c.name for c in table.columns}
            # добавить недостающие колонки
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                default = _column_default_sql(column)
                conn.execute(
                    text(
                        f'ALTER TABLE "{table_name}" '
                        f'ADD COLUMN "{column.name}" {col_type} DEFAULT {default}'
                    )
                )
            # удалить устаревшие колонки-сироты (которых нет в модели): они могут
            # иметь NOT NULL без значения и ломать INSERT (напр. legacy feed_name).
            for col_name in existing - model_cols:
                try:
                    conn.execute(
                        text(f'ALTER TABLE "{table_name}" DROP COLUMN "{col_name}"')
                    )
                except Exception:
                    pass


# Прежняя инструкция по умолчанию — заменяем на новую, если пользователь её не менял.
_OLD_DEFAULT_INSTRUCTIONS = (
    "Přepiš novinku jako originální SEO článek, neopisuj doslova. "
    "Zachovej fakta a uveď odkaz na zdroj."
)


def _migrate_feeds_to_sites(s: Session) -> None:
    """Перенос legacy-источников Feed в модель Site→Source (однократно)."""
    from sqlmodel import select

    from app.db import models

    if s.exec(select(models.Source)).first() is not None:
        return  # уже мигрировано
    feeds = s.exec(select(models.Feed)).all()
    if not feeds:
        return
    # группируем по сайту назначения
    by_site: dict[str, list] = {}
    for f in feeds:
        by_site.setdefault(f.dest_site or "Мой сайт", []).append(f)
    for site_name, group in by_site.items():
        site = models.Site(name=site_name, languages=group[0].languages or "")
        s.add(site)
        s.commit()
        s.refresh(site)
        for f in group:
            s.add(models.Source(site_id=site.id, name=f.name, url=f.url, enabled=f.enabled))
        s.commit()


def _purge_llm_cache() -> None:
    """Удалить просроченные записи кэша LLM (TTL из настроек)."""
    try:
        from datetime import datetime, timedelta, timezone

        from sqlmodel import select

        from app.db import models

        ttl = _settings.llm_cache_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl)
        with Session(engine) as s:
            old = s.exec(
                select(models.LLMCache).where(models.LLMCache.created_at < cutoff)
            ).all()
            for row in old:
                s.delete(row)
            s.commit()
    except Exception:
        pass


def init_db() -> None:
    # импорт моделей нужен, чтобы они зарегистрировались в metadata
    from app.db import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _migrate_add_missing_columns()
    _purge_llm_cache()
    with Session(engine) as s:
        config = s.get(models.AppConfig, 1)
        if config is None:
            s.add(models.AppConfig(id=1))
            s.commit()
        elif config.llm_instructions.strip() == _OLD_DEFAULT_INSTRUCTIONS:
            config.llm_instructions = models.DEFAULT_LLM_INSTRUCTIONS
            s.add(config)
            s.commit()
        _migrate_feeds_to_sites(s)


def analysis_dir() -> Path:
    d = Path(_settings.data_dir) / "analysis"
    d.mkdir(parents=True, exist_ok=True)
    return d

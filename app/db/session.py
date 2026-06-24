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


# Прежняя инструкция по умолчанию — заменяем на новую, если пользователь её не менял.
_OLD_DEFAULT_INSTRUCTIONS = (
    "Přepiš novinku jako originální SEO článek, neopisuj doslova. "
    "Zachovej fakta a uveď odkaz na zdroj."
)


def init_db() -> None:
    # импорт моделей нужен, чтобы они зарегистрировались в metadata
    from app.db import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _migrate_add_missing_columns()
    with Session(engine) as s:
        config = s.get(models.AppConfig, 1)
        if config is None:
            s.add(models.AppConfig(id=1))
            s.commit()
        elif config.llm_instructions.strip() == _OLD_DEFAULT_INSTRUCTIONS:
            config.llm_instructions = models.DEFAULT_LLM_INSTRUCTIONS
            s.add(config)
            s.commit()


def analysis_dir() -> Path:
    d = Path(_settings.data_dir) / "analysis"
    d.mkdir(parents=True, exist_ok=True)
    return d

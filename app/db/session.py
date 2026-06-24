"""Подключение к SQLite и инициализация схемы."""

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

_settings = get_settings()
DB_PATH = Path(_settings.data_dir) / "autopost.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    # импорт моделей нужен, чтобы они зарегистрировались в metadata
    from app.db import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        if s.get(models.AppConfig, 1) is None:
            s.add(models.AppConfig(id=1))
            s.commit()


def analysis_dir() -> Path:
    d = Path(_settings.data_dir) / "analysis"
    d.mkdir(parents=True, exist_ok=True)
    return d

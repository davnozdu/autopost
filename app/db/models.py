"""Модели БД (SQLModel)."""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Feed(SQLModel, table=True):
    """RSS-источник."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    url: str
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)


class AppConfig(SQLModel, table=True):
    """Глобальные настройки обработки (одна строка, id=1)."""

    id: int = Field(default=1, primary_key=True)
    language: str = "cs"
    chars_per_news: int = 1500
    images_from_source_only: bool = True
    llm_instructions: str = (
        "Přepiš novinku jako originální SEO článek, neopisuj doslova. "
        "Zachovej fakta a uveď odkaz na zdroj."
    )
    # Выбранная модель (переопределяет дефолт провайдера). Пусто → дефолт.
    llm_model: str = ""
    # Пароль админки. Пусто → ещё не задан (первый вход).
    password_hash: str = ""
    password_salt: str = ""


class Article(SQLModel, table=True):
    """Подготовленная новость (результат LLM) и её статус."""

    id: int | None = Field(default=None, primary_key=True)
    feed_name: str = ""
    source_title: str = ""
    source_url: str = ""
    source_path: str = Field(default="", index=True)  # папка новости (если из «Собрать»)
    image_url: str | None = None
    title: str = ""
    annotation: str = ""  # краткая аннотация для превью
    body: str = ""
    status: str = "prepared"  # prepared | approved | rejected | published
    publish_note: str = ""
    created_at: datetime = Field(default_factory=_now)
    approved_at: datetime | None = None

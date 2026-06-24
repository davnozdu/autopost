"""Модели БД (SQLModel)."""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Инструкция по умолчанию: SEO-статья, написанная «как живой человек», без следов
# ИИ и без упоминания источника. Редактируется в админке (Настройки).
DEFAULT_LLM_INSTRUCTIONS = (
    "Напиши полностью оригинальную статью по предоставленному материалу. "
    "Не копируй и не пересказывай дословно — переосмысли и подай по-своему, "
    "сохранив факты, цифры и суть.\n\n"
    "Стиль — как пишет живой человек, опытный журналист, а не нейросеть:\n"
    "- естественный, живой язык; чередуй короткие и длинные предложения, ритм неровный;\n"
    "- без канцелярита, клише и воды («в современном мире», «играет важную роль», "
    "«не секрет, что», «стоит отметить»);\n"
    "- без пафоса и рекламных превосходных степеней без фактов;\n"
    "- избегай шаблонных перечислений из трёх, симметричных конструкций «не X, а Y», "
    "обилия тире;\n"
    "- конкретика вместо общих слов: детали, примеры, числа из материала;\n"
    "- допустимы лёгкая ирония, риторический вопрос, личный тон — в меру.\n\n"
    "SEO:\n"
    "- цепляющий, но честный заголовок с ключевой темой (без кликбейта);\n"
    "- логичная структура с подзаголовками H2/H3, короткие абзацы;\n"
    "- естественно вплетай ключевые слова, без переспама;\n"
    "- в первом абзаце — суть и ответ на запрос читателя.\n\n"
    "Важно:\n"
    "- НЕ упоминай источник, не указывай название издания и не вставляй ссылки на источник;\n"
    "- не пиши, что текст основан на новости или сгенерирован;\n"
    "- статья подаётся как полностью самостоятельная и авторская."
)

# Языки перевода для заливки (код → метка). Используется в настройках источника.
LANGUAGES = [("ru", "RU"), ("en", "EN"), ("cz", "CZ"), ("de", "DE")]


class Feed(SQLModel, table=True):
    """RSS-источник."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    url: str
    enabled: bool = True
    # Сайт назначения для публикации.
    dest_site: str = ""
    # Языки перевода через запятую, напр. "ru,en,cz".
    languages: str = ""
    created_at: datetime = Field(default_factory=_now)


class AppConfig(SQLModel, table=True):
    """Глобальные настройки обработки (одна строка, id=1)."""

    id: int = Field(default=1, primary_key=True)
    language: str = "cs"
    chars_per_news: int = 1500
    images_from_source_only: bool = True
    llm_instructions: str = DEFAULT_LLM_INSTRUCTIONS
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
    # Снимок настроек источника на момент обработки (для публикации/переводов).
    dest_site: str = ""
    languages: str = ""
    status: str = "prepared"  # prepared | approved | rejected | published
    publish_note: str = ""
    created_at: datetime = Field(default_factory=_now)
    approved_at: datetime | None = None

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

# Языки перевода для заливки (код → метка). Используется в настройках сайта.
LANGUAGES = [("ru", "RU"), ("en", "EN"), ("cs", "CS"), ("de", "DE"), ("ua", "UA")]

# Дни недели для расписания (код APScheduler → метка).
WEEKDAYS = [
    ("mon", "Пн"), ("tue", "Вт"), ("wed", "Ср"), ("thu", "Чт"),
    ("fri", "Пт"), ("sat", "Сб"), ("sun", "Вс"),
]


class Site(SQLModel, table=True):
    """Сайт назначения: параметры публикации, языки, расписание."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    # Публикация
    repo: str = ""           # owner/name
    branch: str = "main"
    github_token: str = ""   # PAT с правом contents:write на репозиторий сайта
    # Шаблон статьи (Jinja2) — ЗАГРУЖАЕТСЯ через админку. По нему рендерится статья.
    template: str = ""
    # Базовый путь публикации; {lang}/{slug} подставляются. Пусто → "{lang}/blog/{slug}".
    path_pattern: str = "{lang}/blog/{slug}"
    # Языки перевода через запятую, напр. "ru,en,cz"
    languages: str = ""
    # Расписание: дни через запятую (mon,fri) + время HH:MM
    collect_days: str = "mon,fri"
    collect_time: str = "09:00"
    publish_days: str = "wed,sun"
    publish_time: str = "09:00"
    publish_per_run: int = 3  # сколько публиковать за один публикационный день
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)


class Source(SQLModel, table=True):
    """RSS-источник, привязанный к сайту."""

    id: int | None = Field(default=None, primary_key=True)
    site_id: int = Field(index=True)
    name: str
    url: str
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)


class Feed(SQLModel, table=True):
    """Legacy: плоский RSS-источник (до модели Сайт→Источники). Только для миграции."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    url: str
    enabled: bool = True
    dest_site: str = ""
    languages: str = ""
    created_at: datetime = Field(default_factory=_now)


class AppConfig(SQLModel, table=True):
    """Глобальные настройки обработки (одна строка, id=1)."""

    id: int = Field(default=1, primary_key=True)
    language: str = "cs"
    chars_per_news: int = 1500
    images_from_source_only: bool = True
    llm_instructions: str = DEFAULT_LLM_INSTRUCTIONS
    llm_model: str = ""
    password_hash: str = ""
    password_salt: str = ""


class Article(SQLModel, table=True):
    """Подготовленная статья: текст, расписание и статус."""

    id: int | None = Field(default=None, primary_key=True)
    site_id: int = Field(default=0, index=True)
    site_name: str = ""
    source_title: str = ""
    source_url: str = Field(default="", index=True)
    source_path: str = ""
    image_url: str | None = None
    title: str = ""
    slug: str = ""
    annotation: str = ""
    meta_description: str = ""
    keywords: str = ""   # через запятую
    tag: str = ""
    body: str = ""       # body_html от LLM
    lang: str = "cs"     # язык статьи (на каком написана)
    languages: str = ""  # снимок языков сайта на момент обработки
    status: str = "draft"  # draft | scheduled | published | failed
    publish_at: datetime | None = None
    published_at: datetime | None = None
    publish_note: str = ""
    created_at: datetime = Field(default_factory=_now)

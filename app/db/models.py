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
    collect_limit: int = 3    # сколько статей готовить за один сбор (после отбора)
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


class LLMCache(SQLModel, table=True):
    """Кэш ответов LLM (ключ = хэш запроса). Сбрасывается по TTL (см. config)."""

    key: str = Field(primary_key=True)
    response: str
    created_at: datetime = Field(default_factory=_now)


# ── Instagram (соцсети) ───────────────────────────────────────────────
# Любой RSS-источник обрабатывается одинаково: текст материала прогоняется
# через LLM (summary + хэштеги по содержимому), в подпись добавляется ссылка
# на сайт этого источника (IGSource.link_url).


class IGAccount(SQLModel, table=True):
    """Аккаунт Instagram для публикации (instagrapi) + расписание."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    username: str = ""
    password: str = ""           # нужен для входа/перелогина (хранится как github_token)
    session_json: str = ""       # сохранённая сессия instagrapi (чтобы не входить заново)
    proxy: str = ""              # опциональный прокси (рекомендуется для стабильности)
    language: str = "ru"         # язык публикации: на нём LLM готовит подпись
    # Расписание (ежедневно): 1 пост в день + сториз по списку времён.
    collect_time: str = "07:00"  # когда готовить пул постов
    post_time: str = "11:00"     # когда публиковать 1 пост в ленту
    story_times: str = "13:00,17:00,21:00"  # времена публикации сториз (2–3 в день)
    collect_limit: int = 8       # сколько материалов готовить за один сбор (пул)
    last_source_id: int = 0      # курсор ротации источников (для равномерного чередования)
    story_music: bool = True     # добавлять музыку из библиотеки Instagram к сториз
    enabled: bool = True
    # Состояние входа (для админки): "", ok, challenge, error
    login_status: str = ""
    login_note: str = ""
    last_login_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)


class IGSource(SQLModel, table=True):
    """RSS-источник для Instagram-аккаунта."""

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(index=True)
    name: str
    url: str
    link_url: str = ""           # ссылка на сайт ЭТОГО источника: в подпись + стикер сториз
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)


class IGPost(SQLModel, table=True):
    """Подготовленный материал для Instagram (пост в ленту или сториз)."""

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(default=0, index=True)
    source_id: int = 0           # из какого источника (для ротации/статистики)
    source_url: str = Field(default="", index=True)
    source_title: str = ""
    kind: str = ""               # post | story — назначается при публикации
    image_url: str | None = None
    caption: str = ""            # готовая подпись (с хэштегами/ссылкой)
    link_url: str = ""           # ссылка на сайт (для стикера сториз / текста поста)
    status: str = "scheduled"    # draft | scheduled | published | failed
    publish_at: datetime | None = None
    published_at: datetime | None = None
    publish_note: str = ""
    ig_media_pk: str = ""        # id опубликованного медиа в Instagram
    created_at: datetime = Field(default_factory=_now)


# ── Telegram (соцсети) ────────────────────────────────────────────────
# По аналогии с Instagram: TGAccount → TGSource → TGPost. Публикация через
# официальный Bot API (токен + chat_id группы/канала), без сессий/2FA.
# Сториз нет — только посты (фото+подпись или текст). Ссылка кликабельная (HTML).


class TGAccount(SQLModel, table=True):
    """Telegram-бот + чат назначения (группа/канал) и расписание."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    bot_token: str = ""          # токен бота от @BotFather (хранится как github_token)
    chat_id: str = ""            # @username канала или числовой id группы (бот должен быть в ней)
    language: str = "ru"         # язык публикации: на нём LLM готовит подпись
    collect_time: str = "07:00"  # когда готовить пул постов
    post_times: str = "11:00,18:00"  # времена публикации постов (сториз в TG нет)
    collect_limit: int = 8       # размер пула за сбор
    last_source_id: int = 0      # курсор ротации источников
    enabled: bool = True
    verify_status: str = ""      # "", ok, error — результат проверки бота
    verify_note: str = ""
    created_at: datetime = Field(default_factory=_now)


class TGSource(SQLModel, table=True):
    """RSS-источник для Telegram-аккаунта."""

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(index=True)
    name: str
    url: str
    link_url: str = ""           # ссылка на сайт источника: добавляется в подпись (кликабельно)
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)


class TGPost(SQLModel, table=True):
    """Подготовленный материал для Telegram (пост в чат)."""

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(default=0, index=True)
    source_id: int = 0
    source_url: str = Field(default="", index=True)
    source_title: str = ""
    image_url: str | None = None
    caption: str = ""            # готовая подпись (текст + хэштеги, без HTML-ссылки)
    link_url: str = ""           # ссылка на сайт (кликабельной делается при отправке)
    status: str = "scheduled"    # draft | scheduled | published | failed
    publish_at: datetime | None = None
    published_at: datetime | None = None
    publish_note: str = ""
    message_id: str = ""         # id отправленного сообщения
    created_at: datetime = Field(default_factory=_now)


# ── X / Twitter (соцсети) ─────────────────────────────────────────────
# По аналогии: XAccount → XSource → XPost. Публикация через API X (OAuth 1.0a:
# загрузка медиа v1.1 + создание твита v2). Лимит твита 280 символов
# (ссылка считается за 23). Только посты.


class XAccount(SQLModel, table=True):
    """Аккаунт X (Twitter): ключи OAuth 1.0a и расписание."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    api_key: str = ""              # Consumer Key (API Key)
    api_secret: str = ""           # Consumer Secret (API Key Secret)
    access_token: str = ""         # Access Token аккаунта
    access_secret: str = ""        # Access Token Secret
    language: str = "ru"           # язык публикации
    collect_time: str = "07:00"
    post_times: str = "11:00,18:00"  # времена публикации твитов
    collect_limit: int = 8
    last_source_id: int = 0
    enabled: bool = True
    verify_status: str = ""        # "", ok, error
    verify_note: str = ""
    created_at: datetime = Field(default_factory=_now)


class XSource(SQLModel, table=True):
    """RSS-источник для X-аккаунта."""

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(index=True)
    name: str
    url: str
    link_url: str = ""             # ссылка на сайт источника (добавляется в твит)
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)


class XPost(SQLModel, table=True):
    """Подготовленный твит."""

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(default=0, index=True)
    source_id: int = 0
    source_url: str = Field(default="", index=True)
    source_title: str = ""
    image_url: str | None = None
    caption: str = ""              # текст твита (без ссылки; ссылка добавляется при отправке)
    link_url: str = ""
    status: str = "scheduled"
    publish_at: datetime | None = None
    published_at: datetime | None = None
    publish_note: str = ""
    tweet_id: str = ""
    created_at: datetime = Field(default_factory=_now)


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

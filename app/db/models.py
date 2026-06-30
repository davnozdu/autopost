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
    # Telegram-бот мониторинга: ошибки в реальном времени + ежедневная сводка.
    notify_enabled: bool = False
    notify_bot_token: str = ""
    notify_chat_id: str = ""          # личный чат админа (или @канал)
    notify_errors: bool = True        # слать ошибки публикации/входа сразу
    notify_daily: bool = True         # ежедневная сводка по площадкам
    notify_daily_time: str = "09:00"  # время сводки (по TZ приложения)
    # Giphy: анимированные стикеры по теме на сториз (бесплатный ключ api.giphy.com).
    giphy_api_key: str = ""
    # Brave Search API: ранжирование новостей дайджеста по актуальности (бесплатный
    # ключ на api.search.brave.com). LLM при этом не тратится — ранжирует Brave.
    brave_api_key: str = ""
    # Резервный LLM-канал: если основной провайдер (DeepSeek) недоступен — временно
    # переключаемся на этот (по умолч. OpenAI/ChatGPT по API). С авто-восстановлением:
    # по истечении кулдауна снова пробуется основной (см. app/llm/client.py).
    llm_fallback_enabled: bool = False
    llm_fallback_provider: str = "openai"   # пресет из app/llm/providers.py
    llm_fallback_base_url: str = ""          # пусто → base_url пресета
    llm_fallback_key: str = ""               # ключ резервного провайдера (секрет)
    llm_fallback_model: str = ""             # пусто → default_model пресета


class LLMCache(SQLModel, table=True):
    """Кэш ответов LLM (ключ = хэш запроса). Сбрасывается по TTL (см. config)."""

    key: str = Field(primary_key=True)
    response: str
    created_at: datetime = Field(default_factory=_now)


class AdhocPost(SQLModel, table=True):
    """Ручной пост из Telegram-бота — публикуется вне очереди (в обход робота).

    platforms — какие площадки (csv: ig,tg,x). publish_at в прошлом/None → сразу;
    в будущем → отложенно (планировщик публикует, когда наступит время).
    """

    id: int | None = Field(default=None, primary_key=True)
    platforms: str = ""              # csv: ig,tg,x
    text: str = ""
    image_url: str = ""              # ссылка на фото (Telegram file URL), опц.
    publish_at: datetime | None = None
    status: str = "pending"          # pending | published | failed
    note: str = ""
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
    story_gif: bool = True       # добавлять анимированный GIF-стикер по теме (Giphy)
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
    comment_template: str = "Спасибо проекту {link}"  # первый комментарий; {link} → ссылка
    language: str = "ru"         # язык публикации: на нём LLM готовит подпись
    collect_time: str = "07:00"  # когда готовить пул постов
    post_times: str = "11:00,18:00"  # времена публикации постов (сториз в TG нет)
    post_every_hour: bool = False  # режим: постить каждый час (Telegram без лимита)
    jitter_min: int = 10         # случайный сдвиг времени ±N минут (естественность)
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
    """Аккаунт X (Twitter): публикация через twikit по cookie аккаунта (без платного API)."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    auth_token: str = ""           # cookie auth_token из браузера (залогинен в аккаунт)
    ct0: str = ""                  # cookie ct0 (CSRF) из браузера
    twid: str = ""                 # cookie twid (id пользователя) — нужен для ПУБЛИКАЦИИ
    language: str = "ru"           # язык публикации
    collect_time: str = "07:00"
    post_times: str = "11:00,18:00"  # времена публикации твитов (2-й слот — через раз)
    jitter_min: int = 10           # случайный сдвиг времени ±N минут
    monthly_limit: int = 450       # предел твитов в календарный месяц (free X = 500)
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


# ── Дайджест (соцсети) ────────────────────────────────────────────────
# Отдельная логика от пер-статейной публикации: вечером собирается ПУЛ новостей
# из лент дайджеста, Brave Search ранжирует их по актуальности (без токенов),
# затем ОДИН вызов LLM собирает итоговый пост и он сразу публикуется в соцсеть.

# Инструкция по умолчанию: что должен выдать LLM. Полностью редактируется в админке.
DEFAULT_DIGEST_INSTRUCTIONS = (
    "Собери из присланных новостей единый итоговый пост-дайджест дня для соцсети.\n"
    "Структура:\n"
    "• «🔥 Главное» — 2–3 самые важные новости, по 1–2 живых предложения;\n"
    "• «📌 Коротко» — 4–6 второстепенных пунктов одной строкой каждый.\n"
    "Пиши живо и по-человечески, без воды и канцелярита. НЕ выдумывай факты — "
    "опирайся только на присланные заголовки и аннотации.\n\n"
    "Если тема про кино и ТВ — вместо новостей сделай подборку «Что посмотреть "
    "вечером»: 3–5 фильмов/передач с короткой подачей и рейтингом (если он есть "
    "в данных)."
)

# Тарифы свежести Brave (код → метка).
BRAVE_FRESHNESS = [("pd", "За сутки"), ("pw", "За неделю"), ("pm", "За месяц")]


class Digest(SQLModel, table=True):
    """Вечерний дайджест для соцсети: пул новостей → Brave-ранжирование → 1 пост."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    platform: str = "tg"          # ig | tg | x — целевая соцсеть
    account_id: int = 0           # id соответствующего соц-аккаунта (creds/публикация)
    language: str = ""            # язык поста; пусто → берём язык аккаунта
    publish_time: str = "18:00"   # время вечерней сборки И публикации (ежедневно)
    instructions: str = DEFAULT_DIGEST_INSTRUCTIONS  # редактируемая инструкция LLM
    brave_query: str = ""         # тема для Brave-ранжирования (напр. «новости Прага»)
    brave_freshness: str = "pd"   # pd | pw | pm — окно свежести Brave
    use_brave: bool = True        # ранжировать через Brave (иначе только по свежести)
    collect_limit: int = 12       # сколько новостей подаём в LLM (это и есть расход токенов)
    jitter_min: int = 0           # случайный сдвиг времени публикации ±N минут
    enabled: bool = True
    last_run_at: datetime | None = None
    last_note: str = ""
    created_at: datetime = Field(default_factory=_now)


class DigestSource(SQLModel, table=True):
    """RSS-источник, привязанный к дайджесту (свой список лент у каждого дайджеста)."""

    id: int | None = Field(default=None, primary_key=True)
    digest_id: int = Field(index=True)
    name: str
    url: str
    enabled: bool = True
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

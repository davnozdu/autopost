"""Настройки приложения (из переменных окружения / .env)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.llm.providers import get_preset


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM ---
    # Провайдер по умолчанию. Поддерживаются и hermes, и deepseek.
    llm_provider: str = "deepseek"
    # base_url переопределяет дефолт пресета (обязателен для hermes/local).
    llm_base_url: str | None = None
    llm_key: str = ""
    # Имя модели; пусто → берётся default_model пресета.
    llm_model: str | None = None
    llm_timeout_seconds: float = 120.0

    # --- Прочее ---
    # Подпись cookie-сессий. Должен быть стабильным между перезапусками,
    # иначе все входы сбрасываются. Пароль админки задаётся при первом входе.
    secret_key: str = "dev-secret-change-me"
    data_dir: str = "data"
    # Таймзона планировщика (env TZ). От неё зависят дни/время сбора и публикации.
    tz: str = "Europe/Prague"
    # Ключ для HTTP API (/api/*). Пусто → API выключен (401). Bearer-токен.
    api_key: str = ""
    # Кэш ответов LLM (экономия токенов). TTL в днях.
    llm_cache: bool = True
    llm_cache_days: int = 30

    def resolved_base_url(self) -> str:
        preset = get_preset(self.llm_provider)
        base = self.llm_base_url or preset.base_url
        if not base:
            raise ValueError(
                f"Для провайдера '{self.llm_provider}' нужно задать LLM_BASE_URL"
            )
        return base.rstrip("/")

    def resolved_model(self) -> str:
        return self.llm_model or get_preset(self.llm_provider).default_model

    def supports_json_mode(self) -> bool:
        return get_preset(self.llm_provider).supports_json_mode


@lru_cache
def get_settings() -> Settings:
    return Settings()

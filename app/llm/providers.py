"""Пресеты LLM-провайдеров.

Все провайдеры обращаются по OpenAI-совместимому интерфейсу
``POST {base_url}/chat/completions``. Hermes и DeepSeek — равноправные
варианты; провайдер выбирается настройкой и может переопределяться на сайт.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
    """Дефолты провайдера. base_url/model можно переопределить настройками."""

    name: str
    base_url: str | None  # None → должен быть задан пользователем (напр. свой Hermes)
    default_model: str
    supports_json_mode: bool  # поддержка response_format={"type": "json_object"}


PRESETS: dict[str, ProviderPreset] = {
    # Hermes — ваш развёрнутый эндпоинт. base_url задаётся пользователем.
    "hermes": ProviderPreset(
        name="hermes",
        base_url=None,
        default_model="hermes",
        supports_json_mode=False,
    ),
    # DeepSeek — облачный API, по умолчанию основной провайдер.
    "deepseek": ProviderPreset(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        supports_json_mode=True,
    ),
    "openai": ProviderPreset(
        name="openai",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        supports_json_mode=True,
    ),
    # Anthropic через OpenAI-совместимый слой.
    "claude": ProviderPreset(
        name="claude",
        base_url="https://api.anthropic.com/v1",
        default_model="claude-sonnet-4-6",
        supports_json_mode=False,
    ),
    # Локальная модель (Ollama / llama.cpp / vLLM) с OpenAI-совместимым API.
    "local": ProviderPreset(
        name="local",
        base_url="http://localhost:11434/v1",
        default_model="llama3.1",
        supports_json_mode=False,
    ),
}


def get_preset(provider: str) -> ProviderPreset:
    key = provider.strip().lower()
    if key not in PRESETS:
        raise ValueError(
            f"Неизвестный провайдер '{provider}'. Доступны: {', '.join(PRESETS)}"
        )
    return PRESETS[key]

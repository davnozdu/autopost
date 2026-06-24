"""Провайдер-независимый LLM-клиент (синхронный).

Единая точка вызова модели по OpenAI-совместимому API. Подходит и для
Hermes, и для DeepSeek, и для остальных пресетов — различается лишь
base_url / ключ / модель и поддержка JSON-режима.
"""

from dataclasses import dataclass

import httpx

from app.config import Settings, get_settings
from app.llm.providers import get_preset


@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    raw: dict


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def chat(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.7,
    ) -> LLMResult:
        provider = self.settings.llm_provider
        base_url = self.settings.resolved_base_url()
        model = self.settings.resolved_model()

        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        # JSON-режим включаем только если провайдер его поддерживает (напр. DeepSeek).
        if json_mode and get_preset(provider).supports_json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self.settings.llm_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_key}"

        url = f"{base_url}/chat/completions"
        try:
            resp = httpx.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.settings.llm_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"Сетевая ошибка при вызове {provider}: {exc}") from exc

        if resp.status_code >= 400:
            raise LLMError(f"{provider} вернул {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Неожиданный формат ответа {provider}: {data}") from exc

        return LLMResult(text=text, model=model, provider=provider, raw=data)

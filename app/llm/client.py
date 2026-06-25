"""Провайдер-независимый LLM-клиент (синхронный).

Единая точка вызова модели по OpenAI-совместимому API. Подходит и для
Hermes, и для DeepSeek, и для остальных пресетов — различается лишь
base_url / ключ / модель и поддержка JSON-режима.
"""

import hashlib
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
        model: str | None = None,
    ) -> LLMResult:
        provider = self.settings.llm_provider
        base_url = self.settings.resolved_base_url()
        model = model or self.settings.resolved_model()

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

        # Кэш: одинаковый запрос → не тратим токены повторно (TTL из настроек).
        cache_key = None
        if self.settings.llm_cache:
            cache_key = _cache_key(provider, base_url, model, temperature, json_mode, system, user)
            cached = _cache_get(cache_key, self.settings.llm_cache_days)
            if cached is not None:
                return LLMResult(text=cached, model=model, provider=provider, raw={"cached": True})

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

        if cache_key is not None:
            _cache_put(cache_key, text)
        return LLMResult(text=text, model=model, provider=provider, raw=data)


def _cache_key(provider, base_url, model, temperature, json_mode, system, user) -> str:
    raw = f"{provider}|{base_url}|{model}|{temperature}|{json_mode}|{system}|{user}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str, ttl_days: int) -> str | None:
    try:
        from datetime import datetime, timedelta, timezone

        from sqlmodel import Session

        from app.db.models import LLMCache
        from app.db.session import engine

        with Session(engine) as s:
            row = s.get(LLMCache, key)
            if not row:
                return None
            created = row.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - created > timedelta(days=ttl_days):
                s.delete(row)
                s.commit()
                return None
            return row.response
    except Exception:
        return None


def _cache_put(key: str, text: str) -> None:
    try:
        from datetime import datetime, timezone

        from sqlmodel import Session

        from app.db.models import LLMCache
        from app.db.session import engine

        with Session(engine) as s:
            row = s.get(LLMCache, key)
            if row:
                row.response = text
                row.created_at = datetime.now(timezone.utc)
            else:
                row = LLMCache(key=key, response=text)
            s.add(row)
            s.commit()
    except Exception:
        pass

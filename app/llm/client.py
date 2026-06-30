"""Провайдер-независимый LLM-клиент (синхронный) с резервным каналом.

Единая точка вызова модели по OpenAI-совместимому API. Основной провайдер —
из настроек окружения (по умолч. DeepSeek). Если он недоступен, клиент временно
переключается на РЕЗЕРВНЫЙ провайдер (по умолч. OpenAI/ChatGPT по API), заданный
в админке (AppConfig). Реализован «предохранитель» с авто-восстановлением:
после сбоя основного на короткий кулдаун запросы идут сразу в резерв, а по
истечении кулдауна основной пробуется снова (восстановление). Резерв опционален —
без ключа всё работает как раньше.
"""

import hashlib
import threading
import time
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


# ── Предохранитель основного провайдера (общий на процесс) ─────────────
# Пока основной «в кулдауне», запросы идут сразу в резерв; по истечении —
# основной пробуется снова (зонд). Так реализуется авто-восстановление.
FAIL_COOLDOWN_SECONDS = 300  # 5 минут «не дёргать» упавший основной
_breaker_lock = threading.Lock()
_breaker = {"until": 0.0, "provider": "", "note": ""}


def _in_cooldown() -> bool:
    with _breaker_lock:
        return _breaker["until"] > time.monotonic()


def _arm_cooldown(provider: str, exc: Exception, has_fallback: bool) -> None:
    """Открыть предохранитель. Уведомить ОДИН раз при переходе в «упал»."""
    with _breaker_lock:
        was_open = _breaker["until"] > time.monotonic()
        _breaker["until"] = time.monotonic() + FAIL_COOLDOWN_SECONDS
        _breaker["provider"] = provider
        _breaker["note"] = str(exc)[:200]
    if not was_open:
        where = "переключаюсь на резерв" if has_fallback else "резерв не настроен"
        _notify(f"LLM «{provider}» недоступен — {where}", str(exc)[:300])


def _clear_cooldown(notify_recovery: bool) -> None:
    """Закрыть предохранитель (основной снова отвечает)."""
    with _breaker_lock:
        was_open = _breaker["until"] > time.monotonic()
        _breaker["until"] = 0.0
        provider = _breaker["provider"]
    if was_open and notify_recovery:
        _notify(f"LLM «{provider or 'основной'}» восстановлен", "основной провайдер снова отвечает")


def failover_status() -> dict:
    """Состояние предохранителя для отображения в админке."""
    with _breaker_lock:
        remaining = max(0.0, _breaker["until"] - time.monotonic())
        return {
            "primary_down": remaining > 0,
            "seconds_left": int(remaining),
            "provider": _breaker["provider"],
            "note": _breaker["note"],
        }


def _notify(area: str, detail: str) -> None:
    """Тихо отправить событие в бот мониторинга (если включён)."""
    try:
        from app.notify import notify_error
        notify_error(area, detail)
    except Exception:
        pass


def _load_fallback() -> dict | None:
    """Конфиг резервного провайдера из AppConfig (или None, если выключен/нет ключа)."""
    try:
        from sqlmodel import Session

        from app.db.models import AppConfig
        from app.db.session import engine

        with Session(engine) as s:
            cfg = s.get(AppConfig, 1)
        if not cfg or not getattr(cfg, "llm_fallback_enabled", False):
            return None
        key = (cfg.llm_fallback_key or "").strip()
        if not key:
            return None
        provider = (cfg.llm_fallback_provider or "openai").strip().lower()
        try:
            preset = get_preset(provider)
        except ValueError:
            provider, preset = "openai", get_preset("openai")
        base = (cfg.llm_fallback_base_url or "").strip() or preset.base_url
        if not base:
            return None
        return {
            "provider": provider,
            "base_url": base.rstrip("/"),
            "key": key,
            "model": (cfg.llm_fallback_model or "").strip() or preset.default_model,
            "json_supported": preset.supports_json_mode,
        }
    except Exception:
        return None


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
        settings = self.settings
        provider = settings.llm_provider
        base_url = settings.resolved_base_url()
        model = model or settings.resolved_model()
        json_supported = get_preset(provider).supports_json_mode

        # Кэш: одинаковый запрос → не тратим токены повторно (ключ — по основному
        # провайдеру; ответ резерва тоже кэшируется под этим ключом).
        cache_key = None
        if settings.llm_cache:
            cache_key = _cache_key(provider, base_url, model, temperature, json_mode, system, user)
            cached = _cache_get(cache_key, settings.llm_cache_days)
            if cached is not None:
                return LLMResult(text=cached, model=model, provider=provider, raw={"cached": True})

        # 1) Обычный режим (основной не в кулдауне): сначала основной.
        if not _in_cooldown():
            try:
                text = self._request(provider, base_url, model, settings.llm_key,
                                     system, user, temperature, json_mode and json_supported)
                _clear_cooldown(notify_recovery=False)
                return self._result(text, model, provider, cache_key, raw={})
            except LLMError as exc:
                fb = _load_fallback()
                _arm_cooldown(provider, exc, fb is not None)
                if not fb:
                    raise
                try:
                    return self._fallback_call(fb, system, user, temperature, json_mode, cache_key)
                except LLMError:
                    raise exc  # резерв тоже упал → отдаём исходную ошибку основного

        # 2) Основной «в кулдауне»: сначала резерв, основной — как зонд восстановления.
        fb = _load_fallback()
        if fb:
            try:
                return self._fallback_call(fb, system, user, temperature, json_mode, cache_key)
            except LLMError:
                pass  # резерв не ответил — пробуем основной (вдруг уже поднялся)
        try:
            text = self._request(provider, base_url, model, settings.llm_key,
                                 system, user, temperature, json_mode and json_supported)
            _clear_cooldown(notify_recovery=True)
            return self._result(text, model, provider, cache_key, raw={})
        except LLMError as exc:
            _arm_cooldown(provider, exc, fb is not None)
            raise

    def _fallback_call(self, fb: dict, system: str, user: str, temperature: float,
                       json_mode: bool, cache_key: str | None) -> LLMResult:
        text = self._request(fb["provider"], fb["base_url"], fb["model"], fb["key"],
                            system, user, temperature, json_mode and fb["json_supported"])
        return self._result(text, fb["model"], fb["provider"], cache_key, raw={"fallback": True})

    def _result(self, text: str, model: str, provider: str, cache_key: str | None,
                raw: dict) -> LLMResult:
        if cache_key is not None:
            _cache_put(cache_key, text)
        return LLMResult(text=text, model=model, provider=provider, raw=raw)

    def _request(self, provider: str, base_url: str, model: str, key: str,
                 system: str, user: str, temperature: float, want_json: bool) -> str:
        """Один HTTP-вызов chat/completions к указанному провайдеру. → текст ответа."""
        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if want_json:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        url = f"{base_url}/chat/completions"
        try:
            resp = httpx.post(
                url, json=payload, headers=headers,
                timeout=self.settings.llm_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"Сетевая ошибка при вызове {provider}: {exc}") from exc

        if resp.status_code >= 400:
            raise LLMError(f"{provider} вернул {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Неожиданный формат ответа {provider}: {data}") from exc


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

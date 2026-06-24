"""FastAPI-приложение autopost.

Пайплайн (минимальная версия):
  Источники (RSS) → Собрать → папки анализа → Обработать (LLM) → Превью → Одобрить.

Служебные эндпоинты:
  GET  /health        — healthcheck
  GET  /api/llm/info  — текущий провайдер/модель/base_url (без ключа)
  POST /api/llm/test  — пробный вызов LLM (Hermes или DeepSeek)
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db.session import init_db
from app.llm.client import LLMClient, LLMError
from app.web.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="autopost", version="0.2.0", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/llm/info")
def llm_info() -> dict:
    s = get_settings()
    return {
        "provider": s.llm_provider,
        "model": s.resolved_model(),
        "base_url": s.resolved_base_url(),
        "json_mode": s.supports_json_mode(),
        "key_configured": bool(s.llm_key),
    }


@app.post("/api/llm/test")
def llm_test() -> dict:
    """Пробный вызов модели — проверка, что Hermes/DeepSeek отвечает."""
    try:
        result = LLMClient().chat(
            system="Odpovídej stručně česky.",
            user="Napiš jednu větu na téma stavba a rekonstrukce.",
            temperature=0.3,
        )
    except LLMError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "provider": result.provider,
        "model": result.model,
        "text": result.text,
    }

"""FastAPI-приложение autopost.

Пайплайн (минимальная версия):
  Источники (RSS) → Собрать → папки анализа → Обработать (LLM) → Превью → Одобрить.

Доступ к админке защищён паролем, который задаётся при первом входе (/setup).

Служебные эндпоинты:
  GET  /health        — healthcheck (открыт, для Docker)
  GET  /api/llm/info  — текущий провайдер/модель/base_url (защищён)
  POST /api/llm/test  — пробный вызов LLM (защищён)
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from app import scheduler
from app.auth import RedirectException, auth_router, require_auth
from app.config import get_settings
from app.db.session import init_db
from app.llm.client import LLMClient, LLMError
from app.web.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="autopost", version="0.3.0", lifespan=lifespan)

# Сессии для входа (подписанная cookie). SECRET_KEY должен быть стабильным,
# иначе все сессии инвалидируются при перезапуске.
app.add_middleware(SessionMiddleware, secret_key=get_settings().secret_key)


@app.exception_handler(RedirectException)
async def _redirect_handler(request: Request, exc: RedirectException):
    return RedirectResponse(exc.url, status_code=303)


# Маршруты входа/установки пароля — без защиты.
app.include_router(auth_router)
# Админка — только после входа.
app.include_router(router, dependencies=[Depends(require_auth)])


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/llm/info", dependencies=[Depends(require_auth)])
def llm_info() -> dict:
    s = get_settings()
    return {
        "provider": s.llm_provider,
        "model": s.resolved_model(),
        "base_url": s.resolved_base_url(),
        "json_mode": s.supports_json_mode(),
        "key_configured": bool(s.llm_key),
    }


@app.post("/api/llm/test", dependencies=[Depends(require_auth)])
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

"""FastAPI-приложение autopost.

Пайплайн: Сайты → Источники (RSS) → сбор/генерация (LLM) → превью →
публикация (рендер по шаблону сайта + push в GitHub → FTP).

Доступ:
  • WEB-админка — пароль (cookie-сессия), задаётся при первом входе (/setup).
  • HTTP API /api/* — Bearer-ключ (API_KEY) для автоматизаций по IP.
  • GET /health — открыт (для Docker healthcheck).
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from app import scheduler
from app.api import api_router
from app.auth import RedirectException, auth_router, require_auth
from app.config import get_settings
from app.db.session import init_db
from app.web.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="autopost", version="0.4.0", lifespan=lifespan)

# Сессии для входа (подписанная cookie). SECRET_KEY должен быть стабильным.
app.add_middleware(SessionMiddleware, secret_key=get_settings().secret_key)


@app.exception_handler(RedirectException)
async def _redirect_handler(request: Request, exc: RedirectException):
    return RedirectResponse(exc.url, status_code=303)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# HTTP API (Bearer-ключ) — для автоматизаций.
app.include_router(api_router)
# Маршруты входа/установки пароля — без защиты.
app.include_router(auth_router)
# WEB-админка — только после входа (cookie).
app.include_router(router, dependencies=[Depends(require_auth)])

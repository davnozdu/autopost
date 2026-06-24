"""FastAPI-приложение: каркас (Фаза 0).

Сейчас доступно:
- GET /            — заглушка админки со статусом конфигурации LLM
- GET /health      — healthcheck
- GET /api/llm/info — текущий провайдер/модель/base_url (без ключа)
- POST /api/llm/test — пробный вызов LLM (Hermes или DeepSeek) коротким промптом

Последующие фазы добавят: источники, scraper, очередь статей, Telegram, публикацию.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.config import get_settings
from app.llm.client import LLMClient, LLMError

app = FastAPI(title="autopost", version="0.1.0")

templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "web" / "templates")
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/llm/info")
async def llm_info() -> dict:
    s = get_settings()
    return {
        "provider": s.llm_provider,
        "model": s.resolved_model(),
        "base_url": s.resolved_base_url(),
        "json_mode": s.supports_json_mode(),
        "key_configured": bool(s.llm_key),
    }


@app.post("/api/llm/test")
async def llm_test() -> dict:
    """Пробный вызов модели — проверка, что Hermes/DeepSeek отвечает."""
    client = LLMClient()
    try:
        result = await client.chat(
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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    s = get_settings()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "provider": s.llm_provider,
            "model": s.resolved_model(),
            "key_configured": bool(s.llm_key),
        },
    )

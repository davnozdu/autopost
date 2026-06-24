"""Аутентификация админки.

Пароль задаётся при первом входе (страница /setup), хранится в БД в виде
PBKDF2-хэша с солью. Дальше доступ к админке — только после входа (/login),
сессия в подписанной cookie.
"""

import hashlib
import hmac
import os
from pathlib import Path

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session
from starlette.requests import Request

from app.db.models import AppConfig
from app.db.session import engine

ITERATIONS = 200_000
MIN_LEN = 6

templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "web" / "templates")
)
auth_router = APIRouter()


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, ITERATIONS
    ).hex()


def _get_config(s: Session) -> AppConfig:
    config = s.get(AppConfig, 1)
    if config is None:
        config = AppConfig(id=1)
        s.add(config)
        s.commit()
        s.refresh(config)
    return config


def is_password_set() -> bool:
    with Session(engine) as s:
        config = s.get(AppConfig, 1)
        return bool(config and config.password_hash)


def set_password(password: str) -> None:
    salt = os.urandom(16)
    with Session(engine) as s:
        config = _get_config(s)
        config.password_salt = salt.hex()
        config.password_hash = _hash(password, salt)
        s.add(config)
        s.commit()


def verify_password(password: str) -> bool:
    with Session(engine) as s:
        config = s.get(AppConfig, 1)
        if not config or not config.password_hash:
            return False
        salt = bytes.fromhex(config.password_salt)
        return hmac.compare_digest(_hash(password, salt), config.password_hash)


class RedirectException(Exception):
    """Поднимается зависимостью require_auth для перенаправления на вход."""

    def __init__(self, url: str):
        self.url = url


def require_auth(request: Request) -> None:
    if not is_password_set():
        raise RedirectException("/setup")
    if not request.session.get("authed"):
        raise RedirectException("/login")


# ── Маршруты входа/установки пароля ───────────────────────────────────
@auth_router.get("/setup", response_class=HTMLResponse)
def setup_get(request: Request):
    if is_password_set():
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {})


@auth_router.post("/setup")
def setup_post(
    request: Request,
    password: str = Form(...),
    confirm: str = Form(...),
):
    if is_password_set():
        return RedirectResponse("/login", status_code=303)
    if len(password) < MIN_LEN or password != confirm:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": f"Пароли должны совпадать и быть не короче {MIN_LEN} символов."},
        )
    set_password(password)
    request.session["authed"] = True
    return RedirectResponse("/", status_code=303)


@auth_router.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if not is_password_set():
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@auth_router.post("/login")
def login_post(request: Request, password: str = Form(...)):
    if not is_password_set():
        return RedirectResponse("/setup", status_code=303)
    if not verify_password(password):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Неверный пароль."}
        )
    request.session["authed"] = True
    return RedirectResponse("/", status_code=303)


@auth_router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

"""Клиент Telegram Bot API: проверка бота и отправка поста в чат.

Никаких сессий/2FA — только токен бота и chat_id. Картинку Telegram скачивает
сам по URL (resize не нужен); если фото не отдаётся — шлём текстом.
"""

import html

import httpx

from app.util import clean_image_url

API = "https://api.telegram.org/bot{token}/{method}"
TG_CAPTION_LIMIT = 1024   # лимит подписи к фото
TG_TEXT_LIMIT = 4096      # лимит текстового сообщения


class TGError(Exception):
    """Ошибка Telegram Bot API."""


class TGClient:
    def __init__(self, account):
        self.token = (account.bot_token or "").strip()
        self.chat_id = (account.chat_id or "").strip()
        if not self.token or not self.chat_id:
            raise TGError("Не заданы токен бота или chat_id")

    def _call(self, method: str, payload: dict | None = None) -> dict:
        url = API.format(token=self.token, method=method)
        try:
            r = httpx.post(url, json=payload or {}, timeout=30)
            data = r.json()
        except Exception as exc:
            raise TGError(f"Сеть/ответ Telegram: {exc}")
        if not data.get("ok"):
            raise TGError(data.get("description") or "Telegram API error")
        return data.get("result", {})

    def verify(self) -> dict:
        """Проверить токен (getMe) и доступ к чату (getChat)."""
        me = self._call("getMe")
        chat = self._call("getChat", {"chat_id": self.chat_id})
        return {
            "bot": me.get("username", ""),
            "chat": chat.get("title") or chat.get("username") or str(self.chat_id),
        }

    @staticmethod
    def build_html(caption: str, link: str) -> str:
        """Подпись для parse_mode=HTML: экранируем текст, ссылку делаем кликабельной."""
        body = html.escape(caption or "")
        if link.strip():
            href = html.escape(link.strip(), quote=True)
            label = html.escape(link.strip().split("//", 1)[-1].rstrip("/"))
            body = body.rstrip() + f'\n\nПодробнее: <a href="{href}">{label}</a>'
        return body

    def send_post(self, caption: str, image_url: str | None, link: str = "") -> str:
        """Отправить пост: фото+подпись, при сбое фото — текстом. Возвращает message_id."""
        text = self.build_html(caption, link)
        img = clean_image_url(image_url)
        if img:
            try:
                res = self._call("sendPhoto", {
                    "chat_id": self.chat_id, "photo": img,
                    "caption": text[:TG_CAPTION_LIMIT], "parse_mode": "HTML",
                })
                return str(res.get("message_id", ""))
            except TGError:
                pass  # картинка не принялась — отправим текстом ниже
        res = self._call("sendMessage", {
            "chat_id": self.chat_id, "text": text[:TG_TEXT_LIMIT],
            "parse_mode": "HTML", "disable_web_page_preview": False,
        })
        return str(res.get("message_id", ""))

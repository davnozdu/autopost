"""Клиент X (Twitter) на twikit — публикация через внутренний веб-API по cookie
аккаунта (auth_token + ct0), без платного API X.

twikit — асинхронная библиотека; оборачиваем вызовы в asyncio.run (приложение
синхронное). Каждый вызов создаёт свежий twikit.Client, ставит cookie и делает
одну операцию — без переиспользования между разными event loop.

twikit подгружается лениво (и из тома обновлений, см. app.x.updater).
"""

import asyncio


class XError(Exception):
    """Ошибка проверки/публикации в X."""


class XClient:
    def __init__(self, account):
        self.auth_token = (account.auth_token or "").strip()
        self.ct0 = (account.ct0 or "").strip()
        if not self.auth_token or not self.ct0:
            raise XError("Заполните cookie auth_token и ct0")

    def _new_client(self):
        from app.x.updater import ensure_on_path

        ensure_on_path()  # использовать обновлённый twikit из тома, если есть
        from twikit import Client

        c = Client("en-US")
        c.set_cookies({"auth_token": self.auth_token, "ct0": self.ct0})
        return c

    def verify(self) -> dict:
        """Проверить cookie: вернуть @screen_name аккаунта."""
        async def _run():
            c = self._new_client()
            u = await c.user()
            return {"username": getattr(u, "screen_name", "") or getattr(u, "name", "")}

        try:
            return asyncio.run(_run())
        except Exception as exc:
            raise XError(f"Проверка не удалась (cookie неверны/протухли?): {exc}")

    def post(self, text: str) -> str:
        """Опубликовать твит. Возвращает id."""
        async def _run():
            c = self._new_client()
            t = await c.create_tweet(text=text)
            return str(getattr(t, "id", "") or "")

        try:
            return asyncio.run(_run())
        except Exception as exc:
            raise XError(f"Ошибка публикации твита: {exc}")

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

        from app.x._patch import apply_patch

        apply_patch()  # фикс x-client-transaction-id (KEY_BYTE) для twikit 2.3.3
        c = Client("en-US")
        c.set_cookies({"auth_token": self.auth_token, "ct0": self.ct0})
        return c

    def verify(self) -> dict:
        """Проверить, что cookie валидны (аутентифицированный запрос).

        Используем поиск (не требует cookie `twid`/user_id, в отличие от user()),
        чтобы подтвердить, что auth_token+ct0 рабочие — этого же достаточно для постинга.
        """
        async def _run():
            c = self._new_client()
            await c.search_tweet("news", "Top", count=1)
            return {"ok": True}

        try:
            asyncio.run(_run())
            return {"ok": True}
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

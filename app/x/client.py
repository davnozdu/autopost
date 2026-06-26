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
        self.twid = (account.twid or "").strip()
        if not self.auth_token or not self.ct0:
            raise XError("Заполните cookie auth_token и ct0")

    def _new_client(self):
        from app.x.updater import ensure_on_path

        ensure_on_path()  # использовать обновлённый twikit из тома, если есть
        from twikit import Client

        from app.x._patch import apply_patch

        apply_patch()  # фикс x-client-transaction-id (KEY_BYTE) для twikit 2.3.3
        c = Client("en-US")
        cookies = {"auth_token": self.auth_token, "ct0": self.ct0}
        if self.twid:
            cookies["twid"] = self.twid  # нужен для публикации (иначе 344 Permissions)
        c.set_cookies(cookies)
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

    @staticmethod
    async def _create(c, text: str, reply_to: str | None = None) -> str:
        """Создать твит/ответ через нижний GraphQL-слой; вернуть rest_id.

        В обход высокоуровневого create_tweet — его User-парсер падает с KeyError
        на изменившейся структуре ответа X, хотя твит уже создан.
        Сигнатура gql.create_tweet (twikit 2.3.x): is_note_tweet, text,
        media_entities, poll_uri, reply_to, attachment_url, community_id,
        share_with_followers, richtext_options, edit_tweet_id, limit_mode.
        """
        response, _ = await c.gql.create_tweet(
            False, text, [], None, reply_to, None, None, False, None, None, None
        )
        if isinstance(response, dict) and response.get("errors"):
            raise XError(str(response["errors"][0] if response["errors"] else response))
        try:
            return str(
                response["data"]["create_tweet"]["tweet_results"]["result"].get("rest_id", "")
                or ""
            )
        except Exception:
            return ""  # твит создан, но id не достали — не критично

    def post(self, text: str, link: str = "") -> str:
        """Опубликовать основной твит (без ссылки) и ссылку — первым комментарием.

        Так основной твит не теряет охват из-за внешней ссылки, а переход на сайт
        даёт ответ под ним. Возвращает id основного твита.
        """
        if not self.twid:
            raise XError("Для публикации нужен cookie twid (id аккаунта)")

        async def _run():
            c = self._new_client()
            main_id = await self._create(c, text)
            if link.strip() and main_id:
                try:
                    await self._create(c, f"Спасибо проекту {link.strip()}", reply_to=main_id)
                except Exception:
                    pass  # основной твит уже опубликован; ответ со ссылкой не критичен
            return main_id

        try:
            return asyncio.run(_run())
        except XError:
            raise
        except Exception as exc:
            raise XError(f"Ошибка публикации твита: {exc}")

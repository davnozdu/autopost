"""Клиент X (Twitter) на tweepy: проверка ключей и публикация твита с картинкой.

OAuth 1.0a (4 ключа): загрузка медиа идёт через API v1.1 (media_upload),
создание твита — через API v2 (create_tweet). tweepy импортируется лениво.
"""

from pathlib import Path


class XError(Exception):
    """Ошибка публикации/проверки в X."""


class XClient:
    def __init__(self, account):
        try:
            import tweepy
        except Exception as exc:  # pragma: no cover
            raise XError(f"tweepy не установлен: {exc}")
        keys = [account.api_key, account.api_secret,
                account.access_token, account.access_secret]
        if not all((k or "").strip() for k in keys):
            raise XError("Заполните все 4 ключа X (API Key/Secret, Access Token/Secret)")
        self._tweepy = tweepy
        # v2 — создание твита
        self.client = tweepy.Client(
            consumer_key=account.api_key.strip(),
            consumer_secret=account.api_secret.strip(),
            access_token=account.access_token.strip(),
            access_token_secret=account.access_secret.strip(),
        )
        # v1.1 — загрузка медиа
        auth = tweepy.OAuth1UserHandler(
            account.api_key.strip(), account.api_secret.strip(),
            account.access_token.strip(), account.access_secret.strip(),
        )
        self.api = tweepy.API(auth)

    def verify(self) -> dict:
        """Проверить ключи: вернуть @username аккаунта."""
        try:
            me = self.client.get_me()
        except Exception as exc:
            raise XError(f"Ключи не подошли: {exc}")
        data = getattr(me, "data", None)
        username = getattr(data, "username", "") if data else ""
        if not username:
            raise XError("Не удалось получить аккаунт по ключам")
        return {"username": username}

    def post(self, text: str, image_path: Path | None = None) -> str:
        """Опубликовать твит (с картинкой при наличии). Возвращает id твита."""
        media_ids = None
        if image_path:
            try:
                media = self.api.media_upload(filename=str(image_path))
                media_ids = [media.media_id]
            except Exception:
                media_ids = None  # без картинки, но твит выложим
        try:
            resp = self.client.create_tweet(text=text, media_ids=media_ids)
        except Exception as exc:
            raise XError(f"Ошибка публикации твита: {exc}")
        return str((resp.data or {}).get("id", ""))

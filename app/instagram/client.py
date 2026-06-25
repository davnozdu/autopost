"""Обёртка над instagrapi: вход с сохранением сессии и публикация фото/сториз.

instagrapi импортируется лениво — приложение работает и без установленного пакета
(например, в окружении без соцсетей). Сессия сохраняется в JSON (IGAccount.session_json),
чтобы не входить по паролю при каждом запуске и реже ловить challenge.
"""

import json
from pathlib import Path


class IGError(Exception):
    """Любая ошибка публикации/входа в Instagram."""


class IGChallengeRequired(IGError):
    """Требуется код подтверждения (2FA / проверка) — ввести в админке."""


class IGClient:
    def __init__(self, account):
        try:
            from app.instagram.updater import ensure_on_path

            ensure_on_path()  # использовать обновлённую версию из тома, если есть
            from instagrapi import Client
        except Exception as exc:  # pragma: no cover - зависит от окружения
            raise IGError(f"instagrapi не установлен: {exc}")
        self.account = account
        self.cl = Client()
        self.cl.delay_range = [1, 3]  # человеческие задержки между запросами
        if account.session_json:
            try:
                self.cl.set_settings(json.loads(account.session_json))
            except Exception:
                pass
        if account.proxy.strip():
            try:
                self.cl.set_proxy(account.proxy.strip())
            except Exception:
                pass

    def session_json(self) -> str:
        """Текущая сессия для сохранения в БД."""
        try:
            return json.dumps(self.cl.get_settings())
        except Exception:
            return ""

    def ensure_login(self, verification_code: str = "") -> None:
        """Войти, переиспользуя сессию. Поднимает IGChallengeRequired при 2FA."""
        from instagrapi.exceptions import (
            BadPassword,
            ChallengeRequired,
            PleaseWaitFewMinutes,
            TwoFactorRequired,
        )

        acc = self.account
        if not acc.username or not acc.password:
            raise IGError("Не заданы логин/пароль аккаунта")
        try:
            # Если в сессии есть валидные куки — login() их переиспользует.
            self.cl.login(acc.username, acc.password,
                          verification_code=verification_code.strip())
        except TwoFactorRequired as exc:
            raise IGChallengeRequired(
                f"Нужен код двухфакторной аутентификации: {exc}"
            )
        except ChallengeRequired as exc:
            raise IGChallengeRequired(
                f"Instagram запросил проверку (подтвердите вход в приложении/почте): {exc}"
            )
        except BadPassword as exc:
            raise IGError(f"Неверный логин или пароль: {exc}")
        except PleaseWaitFewMinutes as exc:
            raise IGError(f"Instagram просит подождать (лимит запросов): {exc}")
        except Exception as exc:
            raise IGError(f"Ошибка входа: {exc}")

    def upload_photo(self, path: Path, caption: str) -> str:
        """Опубликовать фото в ленту. Возвращает media pk."""
        try:
            media = self.cl.photo_upload(Path(path), caption)
        except Exception as exc:
            raise IGError(f"Ошибка публикации поста: {exc}")
        return str(getattr(media, "pk", "") or "")

    def upload_story(self, path: Path, caption: str = "", link: str = "") -> str:
        """Опубликовать сториз (с кликабельным стикером-ссылкой при наличии link)."""
        links = []
        if link.strip():
            try:
                from instagrapi.types import StoryLink

                links = [StoryLink(webUri=link.strip())]
            except Exception:
                links = []
        try:
            story = self.cl.photo_upload_to_story(
                Path(path), caption=caption, links=links
            )
        except Exception as exc:
            raise IGError(f"Ошибка публикации сториз: {exc}")
        return str(getattr(story, "pk", "") or "")

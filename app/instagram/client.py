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
        self.music_note = ""  # причина, по которой музыка не добавилась (для диагностики)
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

    def _pick_track(self):
        """Случайный трек из библиотеки Instagram (лицензированный) — для сториз.

        Рандомизируем и порядок запросов, и выбор трека из выдачи, чтобы музыка
        не повторялась от сторис к сторис.
        """
        import random

        queries = ["trending", "pop", "vibe", "hits", "music",
                   "chill", "summer", "beats", "mood", "energy"]
        random.shuffle(queries)
        for q in queries:
            try:
                tracks = self.cl.search_music(q)
            except Exception:
                tracks = None
            if tracks:
                return random.choice(tracks[:15])
        return None

    def _story_links(self, link: str) -> list:
        """Кликабельный стикер-ссылка над нарисованной «пилюлей» (низ белой плашки)."""
        if not link.strip():
            return []
        try:
            from instagrapi.types import StoryLink
            try:
                return [StoryLink(webUri=link.strip(),
                                  x=0.32, y=0.95, width=0.55, height=0.06)]
            except Exception:
                return [StoryLink(webUri=link.strip())]
        except Exception:
            return []

    def _story_gif_stickers(self, gif_ids: list[str] | None) -> list:
        """Анимированный GIF-стикер Giphy ТОЛЬКО в зоне фото (верхние ~66%),

        чтобы он не перекрывал текстовую плашку снизу (она с y≈0.70). Позиция и
        размер слегка рандомны → стикер каждый раз смотрится по-разному. Берём один
        случайный id (поэтому гифки «разные»).
        """
        if not gif_ids:
            return []
        try:
            import random

            from instagrapi.types import StorySticker
        except Exception:
            return []
        gid = str(random.choice(gif_ids))
        w = round(random.uniform(0.26, 0.38), 3)
        h = w  # стикеры Giphy ~квадратные
        # держим стикер целиком в кадре и в зоне фото: центр по y такой, чтобы
        # нижний край (y + h/2) не заходил на плашку (≈0.66 — верх плашки)
        x = round(random.uniform(0.22, 0.78), 3)
        y_max = 0.66 - h / 2
        y = round(random.uniform(0.18, max(0.20, y_max)), 3)
        try:
            return [StorySticker(type="gif", id=gid, x=x, y=y, width=w, height=h,
                                 extra={"str_id": gid})]
        except Exception:
            return []

    def upload_story(self, path: Path, caption: str = "", link: str = "",
                     with_music: bool = False,
                     gif_ids: list[str] | None = None) -> str:
        """Опубликовать сториз: ссылка-стикер + (опц.) музыка и GIF-стикер по теме.

        Публикуем по «планам» от богатого к простому: если музыка или GIF-стикер
        не принимаются Instagram'ом, откатываемся к более простому варианту, но
        сториз всё равно выходит (и ссылка сохраняется).
        """
        links = self._story_links(link)
        stickers = self._story_gif_stickers(gif_ids)
        self.music_note = ""

        # планы: (режим, доп. стикеры?) — сверху самый «нарядный»
        plans: list[tuple[str, bool]] = []
        if with_music:
            plans.append(("music", bool(stickers)))
            if stickers:
                plans.append(("music", False))
        plans.append(("plain", bool(stickers)))
        if stickers:
            plans.append(("plain", False))  # последний оплот: только ссылка

        last_exc: Exception | None = None
        for mode, use_stickers in plans:
            kw = {"links": links}
            if use_stickers:
                kw["stickers"] = stickers
            try:
                if mode == "music":
                    track = self._pick_track()
                    if track is None:
                        self.music_note = "музыка: трек не найден (search_music пуст)"
                        continue
                    story = self.cl.photo_upload_to_story_with_music(
                        Path(path), caption=caption, track=track, duration=15.0, **kw
                    )
                else:
                    story = self.cl.photo_upload_to_story(
                        Path(path), caption=caption, **kw
                    )
                return str(getattr(story, "pk", "") or "")
            except Exception as exc:
                last_exc = exc
                if mode == "music":
                    self.music_note = (
                        f"музыка не удалась: {type(exc).__name__}: {str(exc)[:140]}"
                    )
        raise IGError(f"Ошибка публикации сториз: {last_exc}")

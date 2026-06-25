"""Клиент X (Twitter) на OAuth 2.0 (по офиц. докам docs.x.com).

Постинг от имени пользователя через user access token (scope tweet.write).
Долгоживущий доступ — через refresh_token (scope offline.access): перед работой
обновляем короткоживущий access token (живёт ~2 часа). Refresh-токен может
ротироваться, поэтому свежий ВСЕГДА возвращаем для сохранения.

Публикация — прямой вызов POST /2/tweets (без сторонних либ).
"""

import httpx

TOKEN_URL = "https://api.x.com/2/oauth2/token"
API = "https://api.x.com/2"


class XError(Exception):
    """Ошибка авторизации/публикации в X."""


class XClient:
    def __init__(self, account):
        self.client_id = (account.client_id or "").strip()
        self.client_secret = (account.client_secret or "").strip()
        self.refresh_token = (account.refresh_token or "").strip()
        if not self.client_id or not self.refresh_token:
            raise XError("Заполните Client ID и Refresh Token (OAuth 2.0)")
        self.access_token = ""
        self.new_refresh_token = self.refresh_token
        self.scope = ""

    def authorize(self) -> str:
        """Обновить access token. Возвращает (возможно новый) refresh_token для сохранения."""
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        # confidential client → client_id:client_secret в Basic-авторизации
        auth = (self.client_id, self.client_secret) if self.client_secret else None
        try:
            r = httpx.post(TOKEN_URL, data=data, headers=headers, auth=auth, timeout=30)
            j = r.json()
        except Exception as exc:
            raise XError(f"Сеть/ответ X при обновлении токена: {exc}")
        if r.status_code != 200 or "access_token" not in j:
            msg = j.get("error_description") or j.get("error") or j
            raise XError(f"Не удалось обновить токен ({r.status_code}): {msg}")
        self.access_token = j["access_token"]
        self.new_refresh_token = j.get("refresh_token") or self.refresh_token
        self.scope = j.get("scope", "")
        return self.new_refresh_token

    def _ensure(self) -> None:
        if not self.access_token:
            self.authorize()

    def verify(self) -> dict:
        """Проверить токены: вернуть @username и выданные scope."""
        self._ensure()
        try:
            r = httpx.get(f"{API}/users/me",
                          headers={"Authorization": f"Bearer {self.access_token}"},
                          timeout=30)
            j = r.json()
        except Exception as exc:
            raise XError(f"Сеть/ответ X: {exc}")
        if r.status_code != 200 or "data" not in j:
            raise XError(f"Не удалось получить аккаунт ({r.status_code}): {j}")
        return {"username": j["data"].get("username", ""), "scope": self.scope}

    def post(self, text: str) -> str:
        """Опубликовать твит (POST /2/tweets). Возвращает id твита."""
        self._ensure()
        try:
            r = httpx.post(f"{API}/tweets",
                           headers={"Authorization": f"Bearer {self.access_token}",
                                    "Content-Type": "application/json"},
                           json={"text": text}, timeout=30)
            j = r.json()
        except Exception as exc:
            raise XError(f"Сеть/ответ X: {exc}")
        if r.status_code not in (200, 201) or "data" not in j:
            detail = j.get("detail") or j.get("title") or j
            raise XError(f"Ошибка публикации твита ({r.status_code}): {detail}")
        return str(j["data"].get("id", ""))

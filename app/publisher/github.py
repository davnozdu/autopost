"""Запись файлов в репозиторий GitHub через Contents API (без локального клона)."""

import base64

import httpx

API = "https://api.github.com"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def put_file(
    repo: str, branch: str, path: str, content: bytes, token: str, message: str
) -> dict:
    """Создать/обновить файл по пути path в repo (owner/name) на ветке branch."""
    url = f"{API}/repos/{repo}/contents/{path}"
    headers = _headers(token)

    # узнать sha, если файл уже существует (нужно для обновления)
    sha = None
    r = httpx.get(url, headers=headers, params={"ref": branch}, timeout=30.0)
    if r.status_code == 200:
        sha = r.json().get("sha")

    payload = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    r = httpx.put(url, headers=headers, json=payload, timeout=30.0)
    r.raise_for_status()
    return r.json()

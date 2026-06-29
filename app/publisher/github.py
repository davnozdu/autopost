"""Запись файлов в репозиторий GitHub через REST API (без локального клона).

`commit_files` пишет НЕСКОЛЬКО файлов ОДНИМ коммитом (Git Data API: blobs →
tree → commit → один update ref) — это одно событие push, значит один запуск
деплоя сайта. `put_file` (Contents API) оставлен для совместимости, но он
делает по коммиту на файл и его лучше не использовать в пакетной публикации.
"""

import base64

import httpx

API = "https://api.github.com"
_T = 30.0


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


def commit_files(
    repo: str, branch: str, files: list[tuple[str, bytes]], token: str, message: str
) -> dict:
    """Записать несколько файлов ОДНИМ коммитом (один push → один деплой).

    files: список (path, content_bytes). Берём текущий HEAD ветки, на его дереве
    собираем новое (с нашими файлами), делаем один коммит и один раз двигаем ref.
    """
    h = _headers(token)
    base = f"{API}/repos/{repo}"

    r = httpx.get(f"{base}/git/ref/heads/{branch}", headers=h, timeout=_T)
    r.raise_for_status()
    head_sha = r.json()["object"]["sha"]

    r = httpx.get(f"{base}/git/commits/{head_sha}", headers=h, timeout=_T)
    r.raise_for_status()
    base_tree = r.json()["tree"]["sha"]

    tree = []
    for path, content in files:
        rb = httpx.post(f"{base}/git/blobs", headers=h, timeout=_T, json={
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        })
        rb.raise_for_status()
        tree.append({"path": path, "mode": "100644", "type": "blob",
                     "sha": rb.json()["sha"]})

    rt = httpx.post(f"{base}/git/trees", headers=h, timeout=_T,
                    json={"base_tree": base_tree, "tree": tree})
    rt.raise_for_status()
    new_tree = rt.json()["sha"]

    rc = httpx.post(f"{base}/git/commits", headers=h, timeout=_T,
                    json={"message": message, "tree": new_tree, "parents": [head_sha]})
    rc.raise_for_status()
    new_commit = rc.json()["sha"]

    ru = httpx.patch(f"{base}/git/refs/heads/{branch}", headers=h, timeout=_T,
                     json={"sha": new_commit})
    ru.raise_for_status()
    return ru.json()

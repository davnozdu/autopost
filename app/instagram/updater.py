"""Обновление instagrapi без пересборки образа.

instagrapi часто выпускает фиксы (Instagram меняет приватный API). Чтобы не
пересобирать Docker-образ каждый раз, обновлённую версию ставим в монтируемый
том `DATA_DIR/pylibs` и подкладываем его в начало sys.path — она перекрывает
версию, вшитую в образ. Поскольку DATA_DIR монтируется наружу, обновление
сохраняется и при пересоздании контейнера.

Ставим с `--no-deps`: обновляется только код instagrapi, а его зависимости
(pydantic, pycryptodomex, requests) берутся из образа — иначе можно затянуть
несовместимую версию pydantic и сломать FastAPI/SQLModel.
"""

import subprocess
import sys
from importlib import invalidate_caches, metadata
from pathlib import Path

import httpx

from app.config import get_settings

PACKAGE = "instagrapi"


def pylibs_dir() -> Path:
    return Path(get_settings().data_dir) / "pylibs"


def ensure_on_path() -> None:
    """Подложить том с обновлённым instagrapi в начало sys.path."""
    d = pylibs_dir()
    if d.is_dir():
        p = str(d)
        if p not in sys.path:
            sys.path.insert(0, p)
            invalidate_caches()


def installed_version() -> str | None:
    """Активная версия instagrapi (с учётом тома) или None, если не установлен."""
    ensure_on_path()
    try:
        return metadata.version(PACKAGE)
    except Exception:
        return None


def latest_version() -> str | None:
    """Последняя версия на PyPI (или None при отсутствии сети)."""
    try:
        r = httpx.get(f"https://pypi.org/pypi/{PACKAGE}/json", timeout=15)
        r.raise_for_status()
        return r.json()["info"]["version"]
    except Exception:
        return None


def _purge_modules() -> None:
    for name in list(sys.modules):
        if name == PACKAGE or name.startswith(PACKAGE + "."):
            del sys.modules[name]


def update(version: str = "") -> dict:
    """Установить/обновить instagrapi в том. version пусто → последняя.

    Возвращает {ok, version, log}. После успеха выгружает закэшированные модули
    instagrapi, поэтому следующая публикация подхватит новую версию без
    перезапуска (если же instagrapi уже использовался — лучше перезапустить
    контейнер).
    """
    d = pylibs_dir()
    d.mkdir(parents=True, exist_ok=True)
    spec = f"{PACKAGE}=={version}" if version.strip() else PACKAGE
    cmd = [
        sys.executable, "-m", "pip", "install", "--upgrade", "--no-deps",
        "--target", str(d), spec,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as exc:
        return {"ok": False, "version": installed_version(), "log": str(exc)}
    ok = proc.returncode == 0
    log = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-2000:]
    if ok:
        _purge_modules()
        ensure_on_path()
        invalidate_caches()
    return {"ok": ok, "version": installed_version(), "log": log}

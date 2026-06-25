"""Обновление twikit без пересборки образа (как у instagrapi).

twikit ломается, когда X меняет внутренние эндпоинты, и часто выпускает фиксы.
Обновлённую версию ставим в тот же том `DATA_DIR/pylibs` и подкладываем в sys.path.
Ставим с `--no-deps`: обновляется только код twikit, его зависимости берутся из образа.
"""

import subprocess
import sys
from importlib import invalidate_caches, metadata

import httpx

# Том и подключение к sys.path — общие с instagrapi-апдейтером.
from app.instagram.updater import ensure_on_path, pylibs_dir

PACKAGE = "twikit"


def installed_version() -> str | None:
    ensure_on_path()
    try:
        return metadata.version(PACKAGE)
    except Exception:
        return None


def latest_version() -> str | None:
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

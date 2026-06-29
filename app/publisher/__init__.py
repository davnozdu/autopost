"""Публикация статьи: рендер по шаблону сайта + push в GitHub, на язык генерации
и на каждый выбранный язык сайта (через перевод DeepSeek). Общий slug, hreflang.

GitHub после push сам заливает по FTP. Листинг/RSS строит PHP на сайте.
"""

import json
from concurrent.futures import ThreadPoolExecutor

from sqlmodel import Session

from app.db.models import AppConfig, Article, Site
from app.db.session import engine
from app.llm.client import LLMClient, LLMError
from app.llm.prompt import build_translate_prompt, parse_translation
from app.publisher.github import commit_files
from app.publisher.render import render_page
from app.util import lang_name, lang_segment, normalize_repo


def _target_langs(article: Article, site: Site) -> list[str]:
    """Сегменты языков для публикации: язык генерации первым, затем языки сайта."""
    out = [lang_segment(article.lang)]
    for code in (site.languages or "").split(","):
        seg = lang_segment(code.strip())
        if code.strip() and seg not in out:
            out.append(seg)
    return out


def render_article(article: Article) -> dict:
    """Подготовить файлы статьи (рендер + переводы) БЕЗ записи в репозиторий.

    Возвращает: {ok, files:[(path,bytes)], urls, errors, title, repo, branch, token}.
    Сетевую запись (коммит) делает вызывающий — чтобы можно было собрать файлы
    нескольких статей в ОДИН push.
    """
    with Session(engine) as s:
        site = s.get(Site, article.site_id)
        config = s.get(AppConfig, 1) or AppConfig(id=1)

    if not site:
        return {"ok": False, "errors": ["Сайт статьи не найден."]}
    if not (site.template or "").strip():
        return {"ok": False, "errors": ["Не задан шаблон сайта."]}
    if not site.repo or not site.github_token:
        return {"ok": False, "errors": ["Не настроен репозиторий или GitHub-токен сайта."]}

    repo = normalize_repo(site.repo)
    primary = lang_segment(article.lang)
    base_content = {
        "title": article.title,
        "annotation": article.annotation,
        "meta_description": article.meta_description,
        "tag": article.tag,
        "keywords": article.keywords,
        "body_html": article.body,
    }
    targets = _target_langs(article, site)
    client = LLMClient()
    errors = []

    # переводы для не-основных языков — параллельно (это самая долгая часть)
    def _translate(seg: str):
        try:
            system, user = build_translate_prompt(base_content, lang_name(seg))
            res = client.chat(system, user, json_mode=True, temperature=0.3,
                              model=(config.llm_model or None))
            return seg, parse_translation(res.text, base_content), None
        except LLMError as exc:
            return seg, None, f"{seg}: перевод не удался ({str(exc)[:80]})"

    content_by_seg = {primary: base_content}
    others = [seg for seg in targets if seg != primary]
    if others:
        with ThreadPoolExecutor(max_workers=min(4, len(others))) as pool:
            for seg, content, err in pool.map(_translate, others):
                if err:
                    errors.append(err)
                else:
                    content_by_seg[seg] = content

    files: list[tuple[str, bytes]] = []
    urls = []
    for seg in targets:
        content = content_by_seg.get(seg)
        if content is None:
            continue
        try:
            html, meta, path_base = render_page(
                site, lang=seg, slug=article.slug, content=content,
                image_url=article.image_url, publish_at=None,  # дата = момент публикации
                alternates=targets,
            )
            files.append((f"{path_base}/index.html", html.encode("utf-8")))
            files.append((f"{path_base}/meta.json",
                          json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")))
            urls.append(meta["url"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{seg}: {str(exc)[:80]}")

    return {
        "ok": bool(files), "files": files, "urls": urls, "errors": errors,
        "title": article.title, "repo": repo, "branch": site.branch,
        "token": site.github_token,
    }


def publish(article: Article) -> dict:
    """Опубликовать одну статью — все её файлы ОДНИМ коммитом (один push)."""
    info = render_article(article)
    if not info.get("ok"):
        return {"published": False,
                "note": "Публикация не удалась: " + "; ".join(info.get("errors", []))}
    try:
        commit_files(info["repo"], info["branch"], info["files"], info["token"],
                     f"blog: {info['title']}")
    except Exception as exc:  # noqa: BLE001
        return {"published": False, "note": f"push не удался: {str(exc)[:140]}"}
    note = "Опубликовано: " + ", ".join(info["urls"])
    if info["errors"]:
        note += " | ошибки: " + "; ".join(info["errors"])
    return {"published": True, "url": info["urls"][0], "note": note}


def publish_many(articles: list[Article]) -> list[dict]:
    """Опубликовать несколько статей ОДНИМ push (все файлы — в один коммит).

    Возвращает список результатов, выровненный по порядку `articles`. Подходит
    для статей ОДНОГО сайта (общий repo/branch/token); см. run_publish.
    """
    results: list[dict | None] = [None] * len(articles)
    all_files: list[tuple[str, bytes]] = []
    rendered: list[tuple[int, list, list]] = []  # (idx, urls, errors)
    repo = branch = token = None
    titles: list[str] = []

    for i, art in enumerate(articles):
        info = render_article(art)
        if not info.get("ok"):
            results[i] = {"published": False,
                          "note": "не удалось: " + "; ".join(info.get("errors", []))}
            continue
        repo, branch, token = info["repo"], info["branch"], info["token"]
        all_files.extend(info["files"])
        rendered.append((i, info["urls"], info["errors"]))
        titles.append(info["title"])

    if not all_files:
        return [r or {"published": False, "note": "нет файлов"} for r in results]

    msg = f"blog: {len(titles)} ст. — " + ", ".join(titles[:3])
    if len(titles) > 3:
        msg += " …"
    try:
        commit_files(repo, branch, all_files, token, msg)
    except Exception as exc:  # noqa: BLE001
        note = f"push не удался: {str(exc)[:140]}"
        for i, _, _ in rendered:
            results[i] = {"published": False, "note": note}
        return [r or {"published": False, "note": "нет файлов"} for r in results]

    for i, urls, errs in rendered:
        note = "Опубликовано: " + ", ".join(urls)
        if errs:
            note += " | ошибки: " + "; ".join(errs)
        results[i] = {"published": True, "url": urls[0], "note": note}
    return [r or {"published": False, "note": "нет файлов"} for r in results]

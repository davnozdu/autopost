"""Публикация одобренной статьи: рендер по шаблону сайта + push в GitHub.

GitHub после push сам заливает по FTP на хостинг. Листинг/RSS строит PHP на сайте,
поэтому генератору достаточно записать index.html + meta.json статьи.
"""

import json

from sqlmodel import Session

from app.db.models import Article, Site
from app.db.session import engine
from app.publisher.github import put_file
from app.publisher.render import render_article


def publish(article: Article) -> dict:
    with Session(engine) as s:
        site = s.get(Site, article.site_id)

    if not site:
        return {"published": False, "note": "Сайт статьи не найден."}
    if not (site.template or "").strip():
        return {"published": False,
                "note": "Не задан шаблон сайта — загрузите его в настройках сайта."}
    if not site.repo or not site.github_token:
        return {"published": False,
                "note": "Не настроен репозиторий или GitHub-токен сайта."}

    try:
        html, meta, path_base = render_article(article, site)
        meta_json = json.dumps(meta, ensure_ascii=False, indent=2)
        put_file(site.repo, site.branch, f"{path_base}/index.html",
                 html.encode("utf-8"), site.github_token,
                 f"blog: {article.title}")
        put_file(site.repo, site.branch, f"{path_base}/meta.json",
                 meta_json.encode("utf-8"), site.github_token,
                 f"blog meta: {meta['slug']}")
    except Exception as exc:  # noqa: BLE001
        return {"published": False, "note": f"Ошибка публикации: {str(exc)[:200]}"}

    return {"published": True, "url": meta["url"],
            "note": f"Опубликовано: {meta['url']}"}

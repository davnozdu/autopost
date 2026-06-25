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
from app.publisher.github import put_file
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


def publish(article: Article) -> dict:
    with Session(engine) as s:
        site = s.get(Site, article.site_id)
        config = s.get(AppConfig, 1) or AppConfig(id=1)

    if not site:
        return {"published": False, "note": "Сайт статьи не найден."}
    if not (site.template or "").strip():
        return {"published": False,
                "note": "Не задан шаблон сайта — загрузите его в настройках сайта."}
    if not site.repo or not site.github_token:
        return {"published": False,
                "note": "Не настроен репозиторий или GitHub-токен сайта."}

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

    # 1) переводы для не-основных языков — параллельно (это самая долгая часть)
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

    # 2) рендер + запись в репозиторий (последовательно: один branch → без конфликтов)
    published_urls = []
    for seg in targets:
        content = content_by_seg.get(seg)
        if content is None:
            continue
        try:
            html, meta, path_base = render_page(
                site, lang=seg, slug=article.slug, content=content,
                image_url=article.image_url, publish_at=article.publish_at,
                alternates=targets,
            )
            put_file(repo, site.branch, f"{path_base}/index.html",
                     html.encode("utf-8"), site.github_token, f"blog [{seg}]: {meta['title']}")
            put_file(repo, site.branch, f"{path_base}/meta.json",
                     json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
                     site.github_token, f"blog meta [{seg}]: {article.slug}")
            published_urls.append(meta["url"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{seg}: {str(exc)[:80]}")

    if not published_urls:
        return {"published": False, "note": "Публикация не удалась: " + "; ".join(errors)}
    note = "Опубликовано: " + ", ".join(published_urls)
    if errors:
        note += " | ошибки: " + "; ".join(errors)
    return {"published": True, "url": published_urls[0], "note": note}

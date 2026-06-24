"""Загрузка страницы статьи и извлечение чистого текста + картинки."""

from urllib.parse import urljoin

import httpx
import trafilatura
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (compatible; autopost/0.1; +https://github.com/)"}


def fetch_html(url: str) -> str:
    resp = httpx.get(url, headers=UA, timeout=20.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def extract_text(html: str) -> str:
    text = trafilatura.extract(html, include_comments=False, include_tables=False)
    return text or ""


def extract_image(html: str, base_url: str) -> str | None:
    """Картинка строго со страницы источника: og:image → первый <img>."""
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        return urljoin(base_url, og["content"])
    img = soup.find("img")
    if img and img.get("src"):
        return urljoin(base_url, img["src"])
    return None

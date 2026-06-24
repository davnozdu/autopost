# Docker + Hermes — автопостинг статей с утверждением через Telegram

## Идея

Docker-контейнер по расписанию собирает новости с чешских сайтов по теме строительства/ремонта, парсит их и отправляет в Hermes через REST API. Hermes на основе новостей пишет SEO-статью, отправляет её в Telegram на утверждение, ждёт ответа и после подтверждения публикует: сохраняет в репозиторий GitHub (→ автозаливка на хостинг) + кросс-постинг в Instagram и X/Twitter.

---

## Архитектура

```
┌────────────────────────────────────────────────────────────┐
│                   DOCKER КОНТЕЙНЕР (scraper)                │
│                                                             │
│  Cron: каждые 3 дня                                         │
│                                                             │
│  1. RSS-парсинг чешских сайтов                               │
│     (estav.cz, tzb-info.cz, stavebnictvi3000.cz и т.д.)     │
│                                                             │
│  2. Извлечение чистого текста новостей                       │
│                                                             │
│  3. POST → http://hermes:8642/v1/chat/completions            │
│     (сырые новости + промпт на написание статьи)             │
│                                                             │
└────────────────────────┬───────────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────────┐
│                    HERMES (локально или сервер)              │
│                                                             │
│  • API Server :8642                                         │
│  • Telegram Gateway (подключён)                              │
│  • X/Twitter (через xurl)                                   │
│  • Python instagrapi (Instagram)                              │
│                                                             │
│  Шаг 1: LLM пишет SEO-статью (чешский, 1000-1500 слов)      │
│  Шаг 2: Отправляет в Telegram:                               │
│         "📝 Новая статья готова:                             │
│          [заголовок]                                         │
│          [краткое содержание]                                 │
│          Напиши 'да' для публикации"                         │
│                                                             │
│  Шаг 3: Ждёт ответа в Telegram                               │
│                                                             │
│  Шаг 4: При 'да':                                           │
│         ├── .md → git push → GitHub → FTP → хостинг         │
│         ├── instagrapi → Instagram                          │
│         └── xurl post → X/Twitter                           │
│                                                             │
│  Шаг 5: Telegram: "✅ Опубликовано!"                        │
│                                                             │
└────────────────────────────────────────────────────────────┘
```

---

## Компоненты

### 1. Docker-контейнер (scraper)

- Язык: Python
- Зависимости: `feedparser`, `requests`, `beautifulsoup4`, `lxml`
- Источники: RSS-ленты чешских сайтов по строительству
- Частота: раз в 3 дня (через cron или sleep-loop внутри контейнера)
- Действие: собирает новости → отправляет POST в Hermes API
- Хранит last-run маркер, чтобы не дублировать новости

### 2. Hermes (на хосте или в соседнем контейнере)

Необходимые компоненты Hermes:

- **API Server** — приём POST-запросов от Docker (вкл через `API_SERVER_ENABLED=true`)
- **Telegram Gateway** — отправка статьи и ожидание подтверждения
- **xurl** — кросс-постинг в X/Twitter
- **instagrapi** (Python-библиотека) — постинг в Instagram
- **Git** — push статьи в репозиторий

### 3. Telegram (канал связи)

- Hermes отправляет текст статьи
- Пользователь отвечает "да", "нет" или даёт правки
- Hermes обрабатывает ответ и публикует / отклоняет

---

## Рабочий процесс (user flow)

1. Docker собирает новости → POST в Hermes
2. Hermes через LLM пишет статью
3. Пользователь получает в Telegram:
   - Заголовок статьи
   - Краткое содержание
   - Ссылку для просмотра (опционально)
4. Пользователь пишет "да" → публикация
5. Пользователь пишет "нет" → отмена
6. Пользователь пишет правки → Hermes переписывает

---

## Dockerfile (черновик)

```dockerfile
FROM python:3.12-slim

RUN pip install feedparser requests beautifulsoup4 lxml

COPY scraper.py /app/scraper.py
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
```

## scraper.py (черновик логики)

```python
import feedparser
import requests
import os
import json

HERMES_URL = os.getenv("HERMES_URL", "http://localhost:8642")
HERMES_KEY = os.getenv("HERMES_KEY", "")
SITE_THEME = os.getenv("SITE_THEME", "stavba a rekonstrukce")

RSS_FEEDS = [
    "https://www.estav.cz/rss/clanky",
    "https://www.tzb-info.cz/rss.xml",
    # добавить другие фиды
]

def fetch_news():
    news = []
    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:3]:
            news.append({
                "title": entry.title,
                "link": entry.link,
                "summary": getattr(entry, "summary", "")[:500]
            })
    return news

def send_to_hermes(news):
    news_text = "\n".join([
        f"- {n['title']}: {n['link']}" for n in news
    ])

    prompt = f"""Téma webu: {SITE_THEME}

Čerstvé novinky:
{news_text}

Úkol:
1. Napiš SEO článek v češtině (1000-1500 slov)
2. Ulož do ~/repo/articles/
3. Pošli mi do Telegramu na schválení
4. Počkej na mou odpověď před publikováním
"""

    requests.post(
        f"{HERMES_URL}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {HERMES_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": prompt}]
        }
    )

if __name__ == "__main__":
    news = fetch_news()
    if news:
        send_to_hermes(news)
```

## docker-compose.yml (черновик)

```yaml
version: '3.8'
services:
  scraper:
    build: .
    environment:
      - HERMES_URL=http://hermes:8642
      - HERMES_KEY=${HERMES_API_KEY}
      - SITE_THEME=stavba a rekonstrukce
    depends_on:
      - hermes

  hermes:
    image: nousresearch/hermes-agent:latest
    volumes:
      - ./hermes_home:/root/.hermes
      - ./repo:/root/repo
    environment:
      - API_SERVER_ENABLED=true
      - API_SERVER_KEY=${HERMES_API_KEY}
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
    ports:
      - "8642:8642"
```

---

## Что нужно для запуска

- [ ] Установленный Hermes + Telegram Gateway
- [ ] Telegram Bot Token
- [ ] Twitter/X API ключи (для xurl)
- [ ] Instagram аккаунт (для instagrapi)
- [ ] GitHub репозиторий сайта + SSH-ключ
- [ ] Docker (если Hermes тоже в контейнере) или Python на хосте
- [ ] Список RSS-фидов по теме
- [ ] Файл .env с API ключами

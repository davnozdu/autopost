# autopost

Docker-сервис автопостинга статей: собирает новости с заданных источников,
отдаёт их LLM (**Hermes** или **DeepSeek** — на выбор) для написания
SEO-статьи, отправляет на согласование (WEB-админка + Telegram), а после
подтверждения публикует — push в GitHub-репозиторий сайта (далее GitHub
сам заливает по FTP) + кросс-постинг в соцсети.

> Полная архитектура и план по фазам: [`ARCHITECTURE.md`](ARCHITECTURE.md).
> Исходная идея: [`idea.md`](idea.md).

## Статус

**Фаза 0 — каркас.** Готово:
- FastAPI-приложение (заглушка админки + healthcheck);
- провайдер-независимый LLM-клиент с пресетами `hermes`, `deepseek`,
  `openai`, `claude`, `local`;
- Dockerfile, docker-compose, CI для сборки образа в GHCR.

Дальше по `ARCHITECTURE.md`: источники → scraper → генерация → согласование
(Telegram) → публикация на сайт → соцсети.

## Запуск (локально)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # впишите LLM_PROVIDER / LLM_BASE_URL / LLM_KEY
uvicorn app.main:app --reload --port 8080
```

Откройте http://localhost:8080 — статус LLM и кнопка пробного вызова.

## Выбор провайдера

Меняется в `.env` без правки кода:

| LLM_PROVIDER | base_url по умолчанию | модель по умолчанию |
|--------------|-----------------------|---------------------|
| `deepseek`   | `https://api.deepseek.com/v1` | `deepseek-chat` |
| `hermes`     | — (задайте `LLM_BASE_URL`) | `hermes` |
| `openai`     | `https://api.openai.com/v1` | `gpt-4o-mini` |
| `claude`     | `https://api.anthropic.com/v1` | `claude-sonnet-4-6` |
| `local`      | `http://localhost:11434/v1` | `llama3.1` |

## Docker

```bash
docker compose up --build
```

Образ также собирается и публикуется в GHCR через GitHub Actions при push в `main`.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Шрифты с кириллицей — для наложения текста на сторис (PIL).
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# moviepy — для музыки в сторис Instagram. Ставим БЕЗ зависимостей: иначе тянется
# Pillow<12 и ломает instagrapi (нужен Pillow>=12.2). Рантайм-зависимости ставим
# отдельно; ffmpeg приходит со своим бинарём в imageio-ffmpeg (apt не нужен).
RUN pip install --no-cache-dir --no-deps moviepy==2.2.1 \
    && pip install --no-cache-dir numpy imageio imageio-ffmpeg proglog decorator python-dotenv

COPY app ./app
COPY templates_sites ./templates_sites

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV TMP_ROOT=/data/tmp

RUN useradd --create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /data \
    && chown -R bot:bot /app /data

USER bot

CMD ["python", "bot.py"]

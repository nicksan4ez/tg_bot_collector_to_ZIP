FROM python:3.11-slim

<<<<<<< HEAD
# Рабочая директория
WORKDIR /app

# Установим зависимости (если нужны системные - добавьте сюда)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Скопируем и установим python-зависимости
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Копируем проект
COPY . /app

# Каталог для временных файлов внутри контейнера
ENV TMP_ROOT=/data/tmp

# Создадим конечную папку и пользователя для безопасности
RUN mkdir -p /data && groupadd -r bot && useradd -r -g bot bot \
    && chown -R bot:bot /app /data

USER bot

# Запуск
CMD ["python", "bot.py"]

=======
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

CMD ["python", "main.py"]
>>>>>>> bba2b88fb528a473ace533b0d53555aa4b2525a4

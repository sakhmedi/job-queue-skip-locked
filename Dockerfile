FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Сначала только зависимости — этот слой кэшируется, пока не меняется pyproject.toml.
# Dev-зависимости (pytest, ruff) ставим в тот же образ ради простоты:
# `docker compose run --rm api pytest` работает без отдельного образа.
COPY pyproject.toml ./
RUN pip install ".[dev]"

COPY . .

# Не запускаем приложение от root.
RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8000

# Миграции применяются при каждом старте api (Alembic идемпотентен),
# `exec` — чтобы uvicorn стал PID 1 и получал SIGTERM напрямую.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]

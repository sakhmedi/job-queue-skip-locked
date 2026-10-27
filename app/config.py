"""Настройки приложения из переменных окружения.

pydantic-settings читает env-переменные без учёта регистра:
`WORKER_TOKEN` → `settings.worker_token`. Значения валидируются при старте,
поэтому опечатка вроде `MAX_ATTEMPTS=abc` сразу уронит api с понятной ошибкой.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/jobs"

    # Общий секрет воркеров. Без значения по умолчанию: забыть его — ошибка конфигурации.
    worker_token: str = Field(min_length=8)

    # Задание в `running` дольше этого времени считается зависшим.
    stale_after_seconds: int = Field(default=600, gt=0)
    # Сколько раз задание можно выдать воркеру, прежде чем reaper пометит его failed.
    max_attempts: int = Field(default=3, ge=1)
    reaper_interval_seconds: float = Field(default=30, gt=0)
    reaper_enabled: bool = True

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Один объект настроек на процесс (аналог sync.Once в Go)."""
    return Settings()

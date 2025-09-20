from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet

from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    """Raised when environment configuration is invalid."""


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise ConfigError(f"Environment variable {name} is required")
    return value


def _parse_allowed_user_ids(raw: str) -> FrozenSet[int]:
    user_ids: set[int] = set()
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            user_ids.add(int(token))
        except ValueError as exc:
            raise ConfigError(f"Invalid Telegram user id: {token}") from exc
    if not user_ids:
        raise ConfigError("ALLOWED_USER_IDS must contain at least one user id")
    return frozenset(user_ids)


@dataclass(slots=True)
class Settings:
    bot_token: str
    allowed_user_ids: FrozenSet[int]
    archive_name: str
    temp_root: Path
    archive_build_delay: float
    download_timeout: float
    archive_size_limit_bytes: int
    log_file: Path
    log_level: str

    @classmethod
    def load(cls) -> "Settings":
        bot_token = _require_env("BOT_TOKEN")
        allowed_user_ids = _parse_allowed_user_ids(_require_env("ALLOWED_USER_IDS"))
        archive_name = _require_env("ARCHIVE_NAME")
        temp_root = Path(os.getenv("TEMP_ROOT", "./temp")).expanduser().resolve()
        archive_build_delay = float(os.getenv("ARCHIVE_BUILD_DELAY", "3"))
        download_timeout = float(os.getenv("DOWNLOAD_TIMEOUT", "120"))
        archive_size_limit_mb = float(os.getenv("ARCHIVE_SIZE_LIMIT_MB", "48"))
        if archive_size_limit_mb <= 0:
            raise ConfigError("ARCHIVE_SIZE_LIMIT_MB must be greater than zero")
        archive_size_limit_bytes = int(archive_size_limit_mb * 1024 * 1024)
        log_file = Path(os.getenv("LOG_FILE", "logs/bot.log")).expanduser().resolve()
        log_level = os.getenv("LOG_LEVEL", "INFO").upper()

        if not archive_name.lower().endswith(".zip"):
            raise ConfigError("ARCHIVE_NAME must end with .zip")

        temp_root.mkdir(parents=True, exist_ok=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        return cls(
            bot_token=bot_token,
            allowed_user_ids=allowed_user_ids,
            archive_name=archive_name,
            temp_root=temp_root,
            archive_build_delay=archive_build_delay,
            download_timeout=download_timeout,
            archive_size_limit_bytes=archive_size_limit_bytes,
            log_file=log_file,
            log_level=log_level,
        )


settings = Settings.load()

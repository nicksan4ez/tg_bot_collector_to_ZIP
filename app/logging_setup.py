from __future__ import annotations

import logging
import time
from logging import Logger

from .settings import Settings


class RateLimitFilter(logging.Filter):
    """Допускает лог не чаще, чем раз в min_interval секунд."""

    def __init__(self, min_interval: float) -> None:
        super().__init__()
        self.min_interval = min_interval
        self._last_emit = 0.0

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        now = time.monotonic()
        if now - self._last_emit < self.min_interval:
            return False
        self._last_emit = now
        return True


def setup_logging(settings: Settings) -> Logger:
    fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level)

    # Clear existing handlers to avoid duplicate logs when reloading
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    formatter = logging.Formatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(settings.log_level)

    file_handler = logging.FileHandler(settings.log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(settings.log_level)

    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)

    # Ограничиваем шумные запросы httpx до одного сообщения в минуту
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.addFilter(RateLimitFilter(60.0))

    root_logger.debug("Logging has been configured")
    return root_logger

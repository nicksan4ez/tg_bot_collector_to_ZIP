from __future__ import annotations

import logging
from logging import Logger

from .settings import Settings


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

    root_logger.debug("Logging has been configured")
    return root_logger

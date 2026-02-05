from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import Any, Mapping

from common.file_utils import get_project_base_directory

_LOGGER_NAME = "multirag.askdata_perf"
_DEFAULT_LOG_FILENAME = "askdata_perf.log"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


def _format_meta(meta: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, dict):
            value = f"dict({len(value)})"
        elif isinstance(value, (list, tuple, set)):
            value = f"{type(value).__name__}({len(value)})"
        parts.append(f"{key}={value}")
    return " ".join(parts)


def get_perf_logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger

    log_level = os.getenv("ASKDATA_PERF_LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, log_level, logging.INFO))
    logger.propagate = False

    log_path = os.getenv("ASKDATA_PERF_LOG_PATH")
    if not log_path:
        log_path = get_project_base_directory("logs", _DEFAULT_LOG_FILENAME)

    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    handler = RotatingFileHandler(log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


@dataclass
class PerfSpan:
    stage: str
    meta: dict[str, Any] = field(default_factory=dict)
    start: float = field(default_factory=time.perf_counter)
    logger: logging.Logger = field(default_factory=get_perf_logger)
    _ended: bool = field(default=False, init=False)

    def end(self, status: str = "ok", **extra: Any) -> None:
        if self._ended:
            return
        self._ended = True
        elapsed_ms = (time.perf_counter() - self.start) * 1000
        meta = {**self.meta, **extra}
        meta_str = _format_meta(meta)
        message = f"stage={self.stage} status={status} elapsed_ms={elapsed_ms:.2f}"
        if meta_str:
            message = f"{message} {meta_str}"

        if status == "error":
            self.logger.error(message)
        elif status == "warning":
            self.logger.warning(message)
        else:
            self.logger.info(message)

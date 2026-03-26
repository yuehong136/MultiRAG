import logging
import os
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler

from common.file_utils import get_project_base_directory

# 请求级别的 ask_id 上下文，各端点入口设置，日志自动注入
askdata_ask_id: ContextVar[str] = ContextVar("askdata_ask_id", default="-")


class _AskdataContextFilter(logging.Filter):
    def filter(self, record):
        record.ask_id = askdata_ask_id.get("-")
        return True


_logger: logging.Logger | None = None


def get_askdata_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    logger = logging.getLogger("askdata")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    log_path = os.path.join(get_project_base_directory(), "logs", "askdata.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(ask_id)s] [%(module)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    handler.addFilter(_AskdataContextFilter())
    logger.addHandler(handler)

    _logger = logger
    return logger

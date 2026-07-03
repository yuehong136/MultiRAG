import logging
import os
import os.path
from logging.handlers import RotatingFileHandler

from common.file_utils import get_project_base_directory

initialized_root_logger = False
pkg_levels: dict[str, str] = {}  # 提为模块级，以支持运行时动态修改


def init_root_logger(logfile_basename: str, log_format: str = "%(asctime)-15s %(levelname)-8s %(process)d %(message)s"):
    global initialized_root_logger, pkg_levels
    if initialized_root_logger:
        return
    initialized_root_logger = True

    logger = logging.getLogger()
    logger.handlers.clear()
    log_path = os.path.abspath(os.path.join(get_project_base_directory(), "logs", f"{logfile_basename}.log"))

    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    formatter = logging.Formatter(log_format)

    handler1 = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5)
    handler1.setFormatter(formatter)
    logger.addHandler(handler1)

    handler2 = logging.StreamHandler()
    handler2.setFormatter(formatter)
    logger.addHandler(handler2)

    logging.captureWarnings(True)

    LOG_LEVELS = os.environ.get("LOG_LEVELS", "")
    for pkg_name_level in LOG_LEVELS.split(","):
        terms = pkg_name_level.split("=")
        if len(terms) != 2:
            continue
        pkg_name, pkg_level_raw = terms[0], terms[1]
        pkg_name = pkg_name.strip()
        level_no = logging.getLevelName(pkg_level_raw.strip().upper())
        if not isinstance(level_no, int):
            level_no = logging.INFO
        pkg_levels[pkg_name] = logging.getLevelName(level_no)

    for pkg_name in ["sqlalchemy", "pdfminer"]:
        if pkg_name not in pkg_levels:
            pkg_levels[pkg_name] = logging.getLevelName(logging.WARNING)
    if "root" not in pkg_levels:
        pkg_levels["root"] = logging.getLevelName(logging.INFO)

    for pkg_name, pkg_level in pkg_levels.items():
        pkg_logger = logging.getLogger(pkg_name)
        pkg_logger.setLevel(pkg_level)

    msg = f"{logfile_basename} log path: {log_path}, log levels: {pkg_levels}"
    logger.info(msg)


def set_log_level(pkg_name: str, level: str) -> bool:
    """运行时设置指定包的日志级别，成功返回 True。"""
    global pkg_levels
    level_value = logging.getLevelName(level.strip().upper())
    if not isinstance(level_value, int):
        return False
    pkg_levels[pkg_name] = logging.getLevelName(level_value)
    pkg_logger = logging.getLogger(pkg_name)
    pkg_logger.setLevel(level_value)
    return True


def get_log_levels() -> dict:
    """获取所有包的当前日志级别。"""
    global pkg_levels
    return dict(pkg_levels)


def log_exception(e, *args):
    logging.exception(e)
    for a in args:
        try:
            text = a.text
        except Exception:
            text = None
        if text is not None:
            logging.error(text)
            raise Exception(text)
        logging.error(str(a))
    raise e

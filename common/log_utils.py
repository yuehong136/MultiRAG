import json
import logging
import os
import os.path
import re
import traceback
from logging.handlers import RotatingFileHandler
from typing import Any

from common.file_utils import get_project_base_directory
from common.log_ctx import ContextInjectFilter

initialized_root_logger = False
pkg_levels: dict[str, str] = {}  # 提为模块级，以支持运行时动态修改

# ---------------------------------------------------------------------------
# 脱敏（方案 §8-3 硬验收）：曾在 logs/ 实测发现 330 处密钥被原文打印。
# filter 挂根 logger 的全部 handler、无条件启用（与 JSON/plain 格式无关）。
# ---------------------------------------------------------------------------

_MASK_KEEP = 4  # 掩码保留前缀长度

# k=v / k: v / "k": "v" 形态；键名**包含**密钥词干即掩码其值（宁可过掩不可漏掩：
# 覆盖 api_key/secret_key/aes_key_hex/access_token 等任意组合命名）
_KV_PATTERN = re.compile(
    r"""(?P<prefix>['"]?[A-Za-z0-9_\-]*(?:key|token|password|passwd|pwd|secret|authorization)[A-Za-z0-9_\-]*['"]?\s*[:=]\s*['"]?)(?P<value>[^'",;\s}\]]+)""",
    re.IGNORECASE,
)
# Bearer/Basic 凭据
_BEARER_PATTERN = re.compile(r"(?P<prefix>\b(?:Bearer|Basic)\s+)(?P<value>[A-Za-z0-9\-._~+/=]{8,})")
# 常见裸值形态：sk- 前缀 key（OpenAI/DashScope 等）
_BARE_KEY_PATTERN = re.compile(r"(?P<prefix>\bsk-)(?P<value>[A-Za-z0-9\-_]{12,})")

# 快速预筛：消息不含任何敏感线索时跳过正则，控日志热路径开销
_HINT_PATTERN = re.compile(r"key|token|password|passwd|pwd|secret|authorization|bearer|basic\s|sk-", re.IGNORECASE)


def _mask_value(value: str) -> str:
    return value[:_MASK_KEEP] + "***" if len(value) > _MASK_KEEP else "***"


def sanitize_message(message: str) -> str:
    """对密钥形态内容统一掩码（保留前 4 字符便于比对排查）。

    Bearer/Basic 必须先于 KV 处理：否则 ``Authorization: Bearer xxx`` 会被
    KV 规则把 "Bearer" 当作值掩掉，真正的凭据反而漏网。
    """
    if not _HINT_PATTERN.search(message):
        return message
    message = _BEARER_PATTERN.sub(lambda m: m.group("prefix") + _mask_value(m.group("value")), message)
    message = _KV_PATTERN.sub(lambda m: m.group("prefix") + _mask_value(m.group("value")), message)
    message = _BARE_KEY_PATTERN.sub(lambda m: m.group("prefix") + _mask_value(m.group("value")), message)
    return message


class SecretMaskingFilter(logging.Filter):
    """在格式化之前把合成后的消息脱敏（record.msg 替换、args 清空）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # 格式化坏参数时不拦截，交给 logging 自身报错
            return True
        sanitized = sanitize_message(message)
        if sanitized != message or record.args:
            record.msg = sanitized
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    """结构化 JSON 行日志：固定字段 + 日志上下文（request_id/tenant_id/doc_id）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "process": record.process,
            "message": record.getMessage(),
        }
        for field in ("request_id", "tenant_id", "doc_id"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc_info"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return json.dumps(payload, ensure_ascii=False, default=str)


def _resolve_log_format() -> str:
    """observability.log_format（json|plain，默认 json）。

    init_root_logger 在部分入口先于 bootstrap 执行，此处 best-effort 读配置：
    配置坏了退回默认值，让真正的 fail-fast 留给 bootstrap（那时日志已可用）。
    """
    try:
        from common.app_config import get_app_config

        value = str(get_app_config().observability.log_format).lower()
        return value if value in ("json", "plain") else "json"
    except Exception:
        return "json"


def init_root_logger(logfile_basename: str, log_format: str = "%(asctime)-15s %(levelname)-8s %(process)d %(message)s"):
    global initialized_root_logger, pkg_levels
    if initialized_root_logger:
        return
    initialized_root_logger = True

    logger = logging.getLogger()
    logger.handlers.clear()
    log_path = os.path.abspath(os.path.join(get_project_base_directory(), "logs", f"{logfile_basename}.log"))

    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    formatter: logging.Formatter = JsonFormatter() if _resolve_log_format() == "json" else logging.Formatter(log_format)
    # 上下文注入 + 脱敏对两种格式一律生效（脱敏是硬要求，见模块头注释）
    context_filter = ContextInjectFilter()
    masking_filter = SecretMaskingFilter()

    handler1 = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5)
    handler1.setFormatter(formatter)
    handler1.addFilter(context_filter)
    handler1.addFilter(masking_filter)
    logger.addHandler(handler1)

    handler2 = logging.StreamHandler()
    handler2.setFormatter(formatter)
    handler2.addFilter(context_filter)
    handler2.addFilter(masking_filter)
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

"""日志文件编码回归。

未指定 encoding 的 FileHandler 在 Windows 上按 locale 编码写入（中文系统为 GBK），
检索内容里的 ¥、零宽空格、emoji 等字符会抛 UnicodeEncodeError：整条日志丢失，
控制台刷出 ``--- Logging error ---`` traceback。文件 handler 必须固定 UTF-8。
"""

import contextlib
import logging
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from api.service.askdata_service.util import askdata_logger
from common import log_utils

# 半角人民币符号（线上触发字符，来自微信文章「最低赞赏 ¥0」）、零宽空格、emoji
NON_GBK_TEXT = "最低赞赏 ¥0​🙂"


@contextlib.contextmanager
def _isolated_loggers(*names: str) -> Iterator[None]:
    """记录 logger 的 handler/filter/level/propagate，结束时关闭新增 handler 并原样恢复。

    在用例函数体内使用：pytest 每个阶段各挂一个捕获 handler，在 call 阶段内保存才能如实还原。
    """
    loggers = [logging.getLogger(name or None) for name in names]
    saved = [(lg, lg.handlers[:], lg.filters[:], lg.level, lg.propagate) for lg in loggers]
    try:
        yield
    finally:
        for lg, handlers, filters, level, propagate in saved:
            for handler in lg.handlers:
                if handler not in handlers:
                    handler.close()
            lg.handlers[:] = handlers
            lg.filters[:] = filters
            lg.setLevel(level)
            lg.propagate = propagate


def test_probe_text_is_not_gbk_encodable() -> None:
    with pytest.raises(UnicodeEncodeError):
        NON_GBK_TEXT.encode("gbk")


def test_root_logger_file_keeps_non_gbk_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(log_utils, "initialized_root_logger", False)
    monkeypatch.setattr(log_utils, "pkg_levels", {})
    monkeypatch.setattr(log_utils, "get_project_base_directory", lambda: str(tmp_path))
    monkeypatch.setattr(logging, "captureWarnings", lambda capture: None)
    monkeypatch.delenv("LOG_LEVELS", raising=False)

    with _isolated_loggers("", "sqlalchemy", "pdfminer"):
        log_utils.init_root_logger("encoding_probe")
        file_handlers = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
        assert len(file_handlers) == 1
        assert file_handlers[0].encoding == "utf-8"

        logging.getLogger().info("[HISTORY STREAMLY] %s", NON_GBK_TEXT)

    content = (tmp_path / "logs" / "encoding_probe.log").read_text(encoding="utf-8")
    assert NON_GBK_TEXT in content


def test_askdata_shared_log_keeps_non_gbk_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(askdata_logger, "_logger", None)
    monkeypatch.setattr(askdata_logger, "get_project_base_directory", lambda: str(tmp_path))
    monkeypatch.setattr(askdata_logger, "_start_retention_thread", lambda base_dir: None)
    # 每问文件 handler 已自带 UTF-8，本用例只验共享索引文件
    monkeypatch.setattr(askdata_logger, "PerQuestionRoutingHandler", lambda base_dir: logging.NullHandler())

    with _isolated_loggers("askdata"):
        # 导入 app 时 askdata 模块已在加载期初始化过该 logger；清空后只看本次新建的 handler，结束时原样恢复
        existing = logging.getLogger("askdata")
        existing.handlers.clear()
        existing.filters.clear()
        logger = askdata_logger.get_askdata_logger()
        shared_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(shared_handlers) == 1
        assert shared_handlers[0].encoding == "utf-8"

        logger.info("stage done: %s", NON_GBK_TEXT)

    content = (tmp_path / "logs" / "askdata.log").read_text(encoding="utf-8")
    assert NON_GBK_TEXT in content

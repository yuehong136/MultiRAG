"""智能问数「自助分析」专用日志系统。

两层输出（同一个 `askdata` logger，业务模块无需改动）：
  1) 共享索引 `logs/askdata.log`：INFO 级、滚动 10MB×5，仅阶段/状态/耗时流水，看全局时间线用；
  2) 每问全量 `logs/askdata/<YYYY-MM-DD>/<HHMMSS>_<问题>_<ask_id6>.log`：DEBUG 级，
     一次问数全链路明细各自独立成文件，出问题直接打开该文件即可追踪。

关联键是 `ask_id`（各端点入口经 ContextVar 注入，跨「语义层→get-sql→流式分析」三个 HTTP 稳定一致），
故同一问题的多段请求/重试都会追加进同一个文件。
"""

import logging
import os
import re
import shutil
import threading
import time
import traceback
from collections import OrderedDict
from contextvars import ContextVar
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from common.file_utils import get_project_base_directory

# ============ 可调常量（集中一处） ============
RETENTION_DAYS = 30  # 每问日志按天目录保留天数，过期整目录删除
OPEN_FILES_LRU = 32  # 路由 handler 同时保持打开的文件句柄上限（防 fd 耗尽）
REGISTRY_MAX = 4096  # ask_id→路径 注册表上限（防长进程内存无界增长）
QUESTION_MAXLEN = 40  # 文件名中问题截断长度（字符；UTF-8 下给时间前缀/ask_id 后缀留 255 字节余量）
_SWEEP_INTERVAL_SEC = 24 * 60 * 60

# 请求级上下文：各端点入口 set/reset，日志经 _AskdataContextFilter 自动注入。
askdata_ask_id: ContextVar[str] = ContextVar("askdata_ask_id", default="-")
askdata_query: ContextVar[str] = ContextVar("askdata_query", default="")


def _askdata_base_dir() -> str:
    """每问日志根目录 logs/askdata（与共享文件 logs/askdata.log 同级、互不混淆）。"""
    return os.path.join(get_project_base_directory(), "logs", "askdata")


class _AskdataContextFilter(logging.Filter):
    """把请求级 ask_id / user_query 注入每条记录，供共享格式化与每问路由共同使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.ask_id = askdata_ask_id.get("-")
        record.user_query = askdata_query.get("")
        return True


# 文件名净化：路径分隔符、Windows 保留符、引号、各类空白/控制符统统替换掉
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t\x00-\x1f]+')
_WHITESPACE = re.compile(r"\s+")


def _resolve_question_filename(question: str, ask_id: str) -> str:
    """组每问日志文件名：`HHMMSS_<问题净化截断>_<ask_id后6位>.log`。

    时间取「首次落盘时刻」（命名定死后由注册表复用，跨端点不会重算）；
    ask_id 后 6 位既保证不同问绝不撞名、又能反查回 ask_id。
    """
    hhmmss = datetime.now().strftime("%H%M%S")
    q = (question or "").strip()
    q = _ILLEGAL_CHARS.sub("_", q)
    q = _WHITESPACE.sub("_", q).strip("_")
    if not q:
        q = "noquery"
    if len(q) > QUESTION_MAXLEN:
        q = q[:QUESTION_MAXLEN]
    suffix = (ask_id or "")[-6:] or "noaskid"
    return f"{hhmmss}_{q}_{suffix}.log"


class PerQuestionRoutingHandler(logging.Handler):
    """单 handler 内按 `record.ask_id` 把每条记录路由到「每问一文件」。

    为何不是「给 logger 加多个 FileHandler」：logger 会把一条记录广播给它的所有 handler，
    并发多问时 A 的日志会被写进 B 的文件（串台），且动态增删 handler 在 async 下有竞态。
    因此这里用单个 handler、在 emit 内按 record.ask_id 自行分流。

    线程安全：logging 在 handler 自带锁内串行调用 emit，故 _registry / _open_files 的改动
    无需额外加锁；任何 IO 异常都走 self.handleError(record)，绝不抛回业务线程。
    """

    def __init__(self, base_dir: str):
        super().__init__(level=logging.DEBUG)
        self._base_dir = base_dir
        # ask_id -> 绝对路径（命名定死，跨端点追加同一文件）；OrderedDict 作 LRU
        self._registry = OrderedDict()
        # path -> 打开的文件对象；OrderedDict 作句柄 LRU
        self._open_files = OrderedDict()

    # ---- 文件句柄管理（LRU） ----
    def _open(self, path: str, header: str | None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        is_new = (not os.path.exists(path)) or os.path.getsize(path) == 0
        stream = open(path, "a", encoding="utf-8")
        if is_new and header:
            stream.write(header)
            stream.flush()
        self._open_files[path] = stream
        self._open_files.move_to_end(path)
        while len(self._open_files) > OPEN_FILES_LRU:
            _, old = self._open_files.popitem(last=False)
            try:
                old.close()
            except Exception:
                pass
        return stream

    def _get_stream(self, path: str, header: str | None):
        stream = self._open_files.get(path)
        if stream is not None and not stream.closed:
            self._open_files.move_to_end(path)
            return stream
        return self._open(path, header)

    # ---- 路由：ask_id → (路径, 首次落盘的文件头) ----
    def _path_for(self, record: logging.LogRecord) -> tuple[str, str | None]:
        ask_id = getattr(record, "ask_id", "-") or "-"
        if ask_id == "-":
            # 无法关联到具体问题 → 进按天兜底文件，不建每问文件
            misc = os.path.join(self._base_dir, "_misc", datetime.now().strftime("%Y-%m-%d") + ".log")
            return misc, None

        path = self._registry.get(ask_id)
        if path:
            self._registry.move_to_end(ask_id)
            return path, None

        # 首次见到该 ask_id：用问题文本组路径并缓存，时间戳即「首次落盘时刻」
        question = getattr(record, "user_query", "") or ""
        date_dir = os.path.join(self._base_dir, datetime.now().strftime("%Y-%m-%d"))
        path = os.path.join(date_dir, _resolve_question_filename(question, ask_id))
        self._registry[ask_id] = path
        self._registry.move_to_end(ask_id)
        while len(self._registry) > REGISTRY_MAX:
            self._registry.popitem(last=False)

        header = "=" * 60 + "\n" + f"问题: {question}\n" + f"ask_id: {ask_id}\n" + f"创建时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n" + "=" * 60 + "\n"
        return path, header

    def emit(self, record: logging.LogRecord) -> None:
        try:
            path, header = self._path_for(record)
            stream = self._get_stream(path, header)
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)


# ============ 留存清扫 ============
def _parse_date_dir(name: str) -> datetime | None:
    try:
        return datetime.strptime(name, "%Y-%m-%d")
    except ValueError:
        return None


def _sweep_once(base_dir: str, retention_days: int) -> None:
    """删除 base_dir 下日期早于 retention_days 的每问目录；_misc 下按 <date>.log 文件逐个删。"""
    if not os.path.isdir(base_dir):
        return
    cutoff = datetime.now() - timedelta(days=retention_days)
    for name in os.listdir(base_dir):
        full = os.path.join(base_dir, name)
        if name == "_misc":
            if os.path.isdir(full):
                for fname in os.listdir(full):
                    d = _parse_date_dir(fname[:-4] if fname.endswith(".log") else fname)
                    if d and d < cutoff:
                        try:
                            os.remove(os.path.join(full, fname))
                        except OSError:
                            pass
            continue
        if os.path.isdir(full):
            d = _parse_date_dir(name)
            if d and d < cutoff:
                shutil.rmtree(full, ignore_errors=True)


def _start_retention_thread(base_dir: str) -> None:
    def _loop():
        while True:
            try:
                _sweep_once(base_dir, RETENTION_DAYS)
            except Exception:
                pass
            time.sleep(_SWEEP_INTERVAL_SEC)

    threading.Thread(target=_loop, name="askdata-log-retention", daemon=True).start()


# ============ logger 单例 ============
_logger: logging.Logger | None = None


def get_askdata_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    logger = logging.getLogger("askdata")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    base_dir = _askdata_base_dir()
    os.makedirs(base_dir, exist_ok=True)

    # 在 logger 层注入上下文，确保两个 handler 格式化前 ask_id/user_query 都已就绪
    logger.addFilter(_AskdataContextFilter())

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(ask_id)s] [%(module)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 共享索引：INFO 级、滚动。重活都是 DEBUG，不会进这里，保持「瘦」。
    shared_path = os.path.join(get_project_base_directory(), "logs", "askdata.log")
    os.makedirs(os.path.dirname(shared_path), exist_ok=True)
    shared_handler = RotatingFileHandler(shared_path, maxBytes=10 * 1024 * 1024, backupCount=5)
    shared_handler.setLevel(logging.INFO)
    shared_handler.setFormatter(formatter)
    logger.addHandler(shared_handler)

    # 每问全量：DEBUG 级，按 ask_id 路由。
    per_question_handler = PerQuestionRoutingHandler(base_dir)
    per_question_handler.setLevel(logging.DEBUG)
    per_question_handler.setFormatter(formatter)
    logger.addHandler(per_question_handler)

    _start_retention_thread(base_dir)

    _logger = logger
    return logger


def log_incident(stage: str, exc: BaseException, **fields) -> None:
    """失败事故快照：在出错端点的 except 内调用，往该问文件（及共享）写一段醒目分隔块。

    ask_id / 用户问句从 ContextVar 取（此时 finally 尚未 reset，仍有效），
    额外字段（如 sql、sql_components、chart_type）由调用方按现场可得透传。
    以 ERROR 记录，故每问文件与共享索引都会留痕；traceback 取当前 except 上下文。
    """
    logger = get_askdata_logger()
    lines = [
        "",
        "=" * 60,
        "ASKDATA 事故快照",
        f"阶段/端点: {stage}",
        f"ask_id: {askdata_ask_id.get('-')}",
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"用户问句: {askdata_query.get('')}",
    ]
    for key, value in fields.items():
        lines.append(f"{key}: {value!r}")
    lines.append(f"异常: {exc!r}")
    lines.append("traceback:")
    lines.append(traceback.format_exc())
    lines.append("=" * 60)
    logger.error("\n".join(lines))

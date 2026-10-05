"""Bounded retries for explicitly replay-safe Infinity metadata operations."""

import logging
import os
import random
import time
from collections.abc import Callable

from infinity.common import InfinityException


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
        if minimum <= value <= maximum:
            return value
    except ValueError:
        pass
    logging.getLogger(__name__).warning("Invalid %s; using default %d (allowed %d..%d)", name, default, minimum, maximum)
    return default


_META_RETRY_MAX = _int_env("INFINITY_META_RETRY_MAX", 5, 1, 10)
_META_RETRY_BASE_DELAY_MS = _int_env("INFINITY_META_RETRY_BASE_DELAY_MS", 50, 0, 1000)
_META_RETRY_DELAY_CAP_SECONDS = 1.5


def _is_meta_contention_error(exc: Exception) -> bool:
    # SDK 0.7.0-dev5 exposes error_code/error_msg (keyword construction can
    # leave args empty). Legacy callers may carry (code, message) in args[0].
    code = getattr(exc, "error_code", None)
    args = exc.args
    if len(args) == 1 and isinstance(args[0], tuple):
        args = args[0]
    if code is None and args and isinstance(args[0], (int, str)):
        candidate = args[0]
        if isinstance(candidate, int) or candidate.isdecimal():
            code = candidate
    message = f"{getattr(exc, 'error_msg', '')} {exc}".lower()
    # 9003 is kRocksDBError, not a dedicated contention code: corruption and
    # IO errors use it too. An explicit different code must never fall back.
    if code is not None:
        return code in (9003, "9003") and "resource busy" in message
    return "rocksdb" in message and "resource busy" in message


def _retry_on_meta_contention[T](
    op_name: str,
    operation: Callable[[], T],
    *,
    logger: logging.Logger | None = None,
    max_attempts: int = _META_RETRY_MAX,
    base_delay_ms: int = _META_RETRY_BASE_DELAY_MS,
) -> T:
    """Retry only caller-verified Ignore CREATE/DROP calls, never data writes.

    Bounds apply before executing the operation. Defaults allow five attempts
    and at most 1.125s total sleep; the largest configuration sleeps <=13.5s.
    SDK/network call time is outside this backoff budget.
    """
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be an integer in 1..10")
    if type(base_delay_ms) is not int or not 0 <= base_delay_ms <= 1000:
        raise ValueError("base_delay_ms must be an integer in 0..1000")
    log = logger or logging.getLogger(__name__)
    for attempt in range(max_attempts):
        try:
            result = operation()
            # drop_table(Ignore) returns a response even on failure in dev5.
            code = getattr(result, "error_code", 0)
            if code != 0:
                raise InfinityException(code, getattr(result, "error_msg", ""))
            return result
        except Exception as exc:
            if not _is_meta_contention_error(exc):
                raise
            if attempt + 1 == max_attempts:
                log.warning("INFINITY metadata %s exhausted %d attempts: %s", op_name, max_attempts, exc)
                raise
            delay = min(base_delay_ms / 1000 * 2**attempt, _META_RETRY_DELAY_CAP_SECONDS / 1.5)
            delay *= random.uniform(1.0, 1.5)
            log.info("INFINITY metadata %s contention (%d/%d), retrying in %.3fs", op_name, attempt + 1, max_attempts, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")

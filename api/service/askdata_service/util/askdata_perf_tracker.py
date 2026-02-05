from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from api.service.askdata_service.util.perf_logger import PerfSpan


@dataclass
class _PerfEntry:
    span: PerfSpan
    created_at: float
    ttl: int

    @property
    def is_expired(self) -> bool:
        return time.time() > self.created_at + self.ttl


class AskdataPerfTracker:
    def __init__(self, ttl: int = 3600, cleanup_interval: int = 600):
        self._cache: dict[str, _PerfEntry] = {}
        self._lock = threading.RLock()
        self._ttl = ttl
        self._cleanup_interval = cleanup_interval
        self._cleanup_thread = threading.Thread(target=self._cleanup_expired, daemon=True)
        self._cleanup_thread.start()

    def start(self, ask_id: str, meta: dict[str, Any] | None = None, stage: str = "askdata.total") -> bool:
        if not ask_id or not ask_id.strip():
            return False

        with self._lock:
            if ask_id in self._cache:
                return False
            span = PerfSpan(stage, meta=meta or {})
            self._cache[ask_id] = _PerfEntry(span=span, created_at=time.time(), ttl=self._ttl)
        return True

    def end(self, ask_id: str, status: str = "ok", **extra: Any) -> bool:
        if not ask_id or not ask_id.strip():
            return False

        with self._lock:
            entry = self._cache.pop(ask_id, None)

        if not entry:
            return False

        entry.span.end(status=status, **extra)
        return True

    def _cleanup_expired(self) -> None:
        while True:
            try:
                time.sleep(self._cleanup_interval)
                self._clear_expired()
            except Exception:
                continue

    def _clear_expired(self) -> None:
        with self._lock:
            expired_keys = [key for key, entry in self._cache.items() if entry.is_expired]
            for key in expired_keys:
                del self._cache[key]


askdata_perf_tracker = AskdataPerfTracker()

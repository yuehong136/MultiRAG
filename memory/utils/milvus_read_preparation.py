"""One physical pass of lazy public-SDK preparation, never cached rows."""

from collections.abc import Callable
from threading import Lock
from typing import Any

SourceRows = list[dict[str, Any]]
SourceQuery = Callable[[list[int]], SourceRows]


class MilvusReadPreparation:
    """Discard preparation after a pass; errors never become absence or fallback."""

    def __init__(self, prepare: Callable[[], SourceQuery | None]) -> None:
        self._prepare = prepare
        self._query: SourceQuery | None = None
        self._closed = False
        self._failed = False
        self._lock = Lock()

    def query(self, message_ids: list[int]) -> SourceRows:
        if not 1 <= len(message_ids) <= 100 or any(type(identity) is not int or not 0 < identity <= 2**63 - 1 for identity in message_ids):
            raise ValueError("Source message IDs must be a bounded batch of positive signed 64-bit integers")
        identities = list(dict.fromkeys(message_ids))
        with self._lock:
            if self._closed:
                raise RuntimeError("Milvus read preparation is closed")
            if self._failed:
                raise RuntimeError("Milvus read preparation previously failed")
            try:
                if self._query is None:
                    query = self._prepare()
                    if query is None:
                        # Collection absence is not a negative cache. A later
                        # explicit read may observe a newly created collection.
                        return []
                    self._query = query
                return self._query(identities)
            except BaseException:
                self._failed = True
                self._query = None
                raise

    def close(self) -> None:
        """Drain local use and discard references; never unload a shared collection."""
        with self._lock:
            self._closed = True
            self._query = None

"""
LLM 响应性能缓存
基于 prompt 的 MD5 哈希作为 key，进程内缓存 LLM 响应结果，
相同 prompt 再次请求时直接返回缓存结果，跳过 LLM 调用。
"""

import hashlib
import time
import threading
import logging
from typing import Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PerfCacheEntry:
    """缓存条目"""
    value: Any
    namespace: str
    created_at: float
    last_accessed_at: float
    ttl: int

    @property
    def is_expired(self) -> bool:
        return time.time() > self.created_at + self.ttl


@dataclass
class NamespaceStats:
    """命名空间统计"""
    hits: int = 0
    misses: int = 0


class PerfCache:
    """
    LLM 响应性能缓存

    特性：
    - key = "{namespace}:{md5(prompt)}"
    - 默认 TTL 300秒（5分钟）
    - LRU 淘汰（max_size=500）
    - 按 namespace 统计 hit/miss
    - 线程安全
    - 后台定期清理过期条目
    """

    def __init__(self, default_ttl: int = 300, max_size: int = 500, cleanup_interval: int = 120):
        self._cache: dict[str, PerfCacheEntry] = {}
        self._lock = threading.RLock()
        self._default_ttl = default_ttl
        self._max_size = max_size
        self._cleanup_interval = cleanup_interval
        self._ns_stats: dict[str, NamespaceStats] = {}

        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

        logger.info(f"PerfCache initialized: ttl={default_ttl}s, max_size={max_size}")

    @staticmethod
    def _make_key(prompt: str, namespace: str) -> str:
        md5 = hashlib.md5(prompt.encode("utf-8")).hexdigest()
        return f"{namespace}:{md5}"

    def _get_ns_stats(self, namespace: str) -> NamespaceStats:
        if namespace not in self._ns_stats:
            self._ns_stats[namespace] = NamespaceStats()
        return self._ns_stats[namespace]

    def get(self, prompt: str, namespace: str = "default") -> Optional[Any]:
        key = self._make_key(prompt, namespace)
        with self._lock:
            entry = self._cache.get(key)
            stats = self._get_ns_stats(namespace)

            if entry is None:
                stats.misses += 1
                logger.debug(f"PerfCache MISS [{namespace}] key={key[:32]}...")
                return None

            if entry.is_expired:
                del self._cache[key]
                stats.misses += 1
                logger.debug(f"PerfCache EXPIRED [{namespace}] key={key[:32]}...")
                return None

            entry.last_accessed_at = time.time()
            stats.hits += 1
            logger.info(f"PerfCache HIT [{namespace}] (hits={stats.hits})")
            return entry.value

    def set(self, prompt: str, value: Any, namespace: str = "default", ttl: int | None = None) -> None:
        key = self._make_key(prompt, namespace)
        actual_ttl = ttl or self._default_ttl
        now = time.time()

        entry = PerfCacheEntry(
            value=value,
            namespace=namespace,
            created_at=now,
            last_accessed_at=now,
            ttl=actual_ttl,
        )

        with self._lock:
            if len(self._cache) >= self._max_size and key not in self._cache:
                self._evict_lru()
            self._cache[key] = entry
            logger.debug(f"PerfCache SET [{namespace}] key={key[:32]}... ttl={actual_ttl}s size={len(self._cache)}")

    def clear_all(self) -> int:
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            if count > 0:
                logger.info(f"PerfCache cleared all {count} entries")
            return count

    def clear_namespace(self, namespace: str) -> int:
        with self._lock:
            keys_to_remove = [k for k, v in self._cache.items() if v.namespace == namespace]
            for k in keys_to_remove:
                del self._cache[k]
            if keys_to_remove:
                logger.info(f"PerfCache cleared {len(keys_to_remove)} entries in namespace [{namespace}]")
            return len(keys_to_remove)

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            ns_info = {}
            for ns, stats in self._ns_stats.items():
                total = stats.hits + stats.misses
                ns_info[ns] = {
                    "hits": stats.hits,
                    "misses": stats.misses,
                    "hit_rate": f"{stats.hits / total * 100:.1f}%" if total > 0 else "N/A",
                }
            return {
                "total_entries": len(self._cache),
                "max_size": self._max_size,
                "default_ttl": self._default_ttl,
                "namespaces": ns_info,
            }

    def _evict_lru(self) -> None:
        if not self._cache:
            return
        lru_key = min(self._cache, key=lambda k: self._cache[k].last_accessed_at)
        del self._cache[lru_key]
        logger.debug(f"PerfCache evicted LRU entry: {lru_key[:32]}...")

    def _cleanup_expired(self) -> int:
        with self._lock:
            expired = [k for k, v in self._cache.items() if v.is_expired]
            for k in expired:
                del self._cache[k]
        if expired:
            logger.info(f"PerfCache cleanup: removed {len(expired)} expired entries")
        return len(expired)

    def _cleanup_loop(self) -> None:
        while True:
            try:
                time.sleep(self._cleanup_interval)
                self._cleanup_expired()
            except Exception as e:
                logger.error(f"PerfCache cleanup error: {e}")


# 全局单例
perf_cache = PerfCache()

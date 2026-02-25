"""
语义层缓存管理器
用于临时存储敏感的语义层数据，避免通过前端传输
"""

import copy
import json
import time
import threading
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """缓存条目"""
    data: Dict[str, Any]
    created_at: float
    ttl: int

    @property
    def is_expired(self) -> bool:
        """检查是否过期"""
        return time.time() > self.created_at + self.ttl


class SemanticLayerCache:
    """
    语义层缓存管理器

    特性：
    - 线程安全的内存存储
    - 自动过期清理
    - 内存使用监控
    - 安全的数据访问
    """

    def __init__(self, default_ttl: int = 600, max_size: int = 1000, cleanup_interval: int = 300):
        """
        初始化缓存管理器

        Args:
            default_ttl: 默认过期时间（秒），默认10分钟
            max_size: 最大缓存条目数
            cleanup_interval: 清理过期数据的间隔（秒），默认5分钟
        """
        self._cache: Dict[str, CacheEntry] = {}
        self._lock = threading.RLock()
        self._default_ttl = default_ttl
        self._max_size = max_size
        self._cleanup_interval = cleanup_interval

        # 启动后台清理线程
        self._cleanup_thread = threading.Thread(target=self._cleanup_expired, daemon=True)
        self._cleanup_thread.start()

        logger.info(f"SemanticLayerCache initialized: ttl={default_ttl}s, max_size={max_size}")

    def store(self, ask_id: str, data: Dict[str, Any], ttl: Optional[int] = None) -> bool:
        """
        存储语义层数据

        Args:
            ask_id: 请求ID，作为缓存键
            data: 要缓存的敏感数据
            ttl: 自定义过期时间，不指定则使用默认值

        Returns:
            bool: 是否存储成功
        """
        if not ask_id or not ask_id.strip():
            logger.warning("无效的ask_id，无法存储缓存数据")
            return False

        if not isinstance(data, dict):
            logger.warning(f"数据类型错误，期望dict，实际{type(data)}")
            return False

        actual_ttl = ttl or self._default_ttl
        entry = CacheEntry(
            data=copy.deepcopy(data),  # 深拷贝避免外部修改影响缓存
            created_at=time.time(),
            ttl=actual_ttl
        )

        with self._lock:
            # 检查缓存大小，如果超限则清理最旧的数据
            if len(self._cache) >= self._max_size:
                self._evict_oldest()

            self._cache[ask_id.strip()] = entry
            logger.debug(f"成功存储语义层数据: ask_id={ask_id}, ttl={actual_ttl}s, size={len(self._cache)}")

        return True

    def get(self, ask_id: str) -> Optional[Dict[str, Any]]:
        """
        获取语义层数据

        Args:
            ask_id: 请求ID

        Returns:
            Dict[str, Any] | None: 缓存的数据，如果不存在或已过期则返回None
        """
        if not ask_id or not ask_id.strip():
            logger.warning("无效的ask_id，无法获取缓存数据")
            return None

        with self._lock:
            entry = self._cache.get(ask_id.strip())

            if entry is None:
                logger.debug(f"缓存未命中: ask_id={ask_id}")
                return None

            if entry.is_expired:
                logger.debug(f"缓存已过期: ask_id={ask_id}")
                del self._cache[ask_id.strip()]
                return None

            logger.debug(f"缓存命中: ask_id={ask_id}")
            return copy.deepcopy(entry.data)  # 返回深拷贝避免外部修改影响缓存

    def remove(self, ask_id: str) -> bool:
        """
        手动移除缓存条目

        Args:
            ask_id: 请求ID

        Returns:
            bool: 是否移除成功
        """
        if not ask_id or not ask_id.strip():
            return False

        with self._lock:
            if ask_id.strip() in self._cache:
                del self._cache[ask_id.strip()]
                logger.debug(f"手动移除缓存: ask_id={ask_id}")
                return True
            return False

    def get_stats(self) -> Dict[str, Any]:
        """
        获取缓存统计信息

        Returns:
            Dict[str, Any]: 统计信息
        """
        with self._lock:
            current_time = time.time()
            expired_count = sum(1 for entry in self._cache.values() if entry.is_expired)

            return {
                "total_entries": len(self._cache),
                "expired_entries": expired_count,
                "valid_entries": len(self._cache) - expired_count,
                "max_size": self._max_size,
                "default_ttl": self._default_ttl,
                "memory_usage_estimate": self._estimate_memory_usage()
            }

    def clear_expired(self) -> int:
        """
        清理过期的缓存条目

        Returns:
            int: 清理的条目数
        """
        cleared_count = 0
        current_time = time.time()

        with self._lock:
            expired_keys = [
                key for key, entry in self._cache.items()
                if entry.is_expired
            ]

            for key in expired_keys:
                del self._cache[key]
                cleared_count += 1

        if cleared_count > 0:
            logger.info(f"清理了 {cleared_count} 个过期缓存条目")

        return cleared_count

    def clear_all(self) -> int:
        """
        清理所有缓存条目

        Returns:
            int: 清理的条目数
        """
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            logger.info(f"清理了所有 {count} 个缓存条目")
            return count

    def _evict_oldest(self) -> None:
        """内部方法：移除最旧的缓存条目"""
        if not self._cache:
            return

        oldest_key = min(
            self._cache.keys(),
            key=lambda k: self._cache[k].created_at
        )
        del self._cache[oldest_key]
        logger.debug(f"移除最旧的缓存条目: {oldest_key}")

    def _cleanup_expired(self) -> None:
        """后台线程：定期清理过期数据"""
        while True:
            try:
                time.sleep(self._cleanup_interval)
                self.clear_expired()
            except Exception as e:
                logger.error(f"清理过期缓存时发生异常: {e}")

    def _estimate_memory_usage(self) -> str:
        """估算内存使用量"""
        try:
            total_size = 0
            for entry in self._cache.values():
                # 简单估算：使用JSON序列化后的字符串长度
                total_size += len(json.dumps(entry.data, ensure_ascii=False))

            if total_size < 1024:
                return f"{total_size} bytes"
            elif total_size < 1024 * 1024:
                return f"{total_size / 1024:.2f} KB"
            else:
                return f"{total_size / (1024 * 1024):.2f} MB"
        except Exception:
            return "unknown"


# 全局单例缓存实例
semantic_layer_cache = SemanticLayerCache()


def get_cache() -> SemanticLayerCache:
    """获取语义层缓存实例"""
    return semantic_layer_cache
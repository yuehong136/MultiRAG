import time
from threading import Lock

from api.service.askdata_service.util.askdata_logger import get_askdata_logger

logger = get_askdata_logger()


class StopRequestManager:
    """
    停止请求管理器
    用于管理用户发起的停止请求，支持基于ask_id的请求追踪和停止
    """

    def __init__(self, expire_seconds: int = 300):  # 5分钟过期
        """
        初始化停止请求管理器

        Args:
            expire_seconds: 停止记录过期时间（秒），默认300秒（5分钟）
        """
        self._stopped_requests: dict[str, float] = {}  # ask_id -> timestamp
        self._lock = Lock()  # 使用线程锁而不是asyncio.Lock，因为可能在同步和异步环境中使用
        self._expire_seconds = expire_seconds
        self._last_cleanup = time.time()
        self._cleanup_interval = 60  # 每60秒清理一次过期记录

    def stop_request(self, ask_id: str) -> bool:
        """
        标记指定ask_id的请求为停止状态

        Args:
            ask_id: 要停止的请求ID

        Returns:
            bool: 操作是否成功
        """
        if not ask_id or not ask_id.strip():
            logger.warning("尝试停止请求，但ask_id为空")
            return False

        try:
            with self._lock:
                current_time = time.time()
                self._stopped_requests[ask_id.strip()] = current_time

                # 定期清理过期记录
                if current_time - self._last_cleanup > self._cleanup_interval:
                    self._cleanup_expired_unsafe()
                    self._last_cleanup = current_time

                logger.info(f"请求 {ask_id} 已标记为停止")
                return True

        except Exception as e:
            logger.error(f"停止请求 {ask_id} 时发生错误: {e!s}")
            return False

    def is_stopped(self, ask_id: str) -> bool:
        """
        检查指定ask_id的请求是否已被停止

        Args:
            ask_id: 要检查的请求ID

        Returns:
            bool: 请求是否已被停止
        """
        if not ask_id or not ask_id.strip():
            return False

        try:
            with self._lock:
                ask_id = ask_id.strip()

                # 如果记录不存在，返回False
                if ask_id not in self._stopped_requests:
                    return False

                # 检查记录是否过期
                current_time = time.time()
                stop_time = self._stopped_requests[ask_id]

                if current_time - stop_time > self._expire_seconds:
                    # 记录已过期，删除并返回False
                    del self._stopped_requests[ask_id]
                    return False

                return True

        except Exception as e:
            logger.error(f"检查请求 {ask_id} 停止状态时发生错误: {e!s}")
            return False

    def remove_stop_request(self, ask_id: str) -> bool:
        """
        移除指定ask_id的停止记录

        Args:
            ask_id: 要移除的请求ID

        Returns:
            bool: 操作是否成功
        """
        if not ask_id or not ask_id.strip():
            return False

        try:
            with self._lock:
                ask_id = ask_id.strip()
                if ask_id in self._stopped_requests:
                    del self._stopped_requests[ask_id]
                    return True
                return False

        except Exception as e:
            logger.error(f"移除停止记录 {ask_id} 时发生错误: {e!s}")
            return False

    def cleanup_expired(self) -> int:
        """
        清理所有过期的停止记录

        Returns:
            int: 清理的记录数量
        """
        try:
            with self._lock:
                return self._cleanup_expired_unsafe()

        except Exception as e:
            logger.error(f"清理过期停止记录时发生错误: {e!s}")
            return 0

    def _cleanup_expired_unsafe(self) -> int:
        """
        清理过期记录的内部方法（非线程安全，需要在锁内调用）

        Returns:
            int: 清理的记录数量
        """
        current_time = time.time()
        expired_keys = []

        for ask_id, stop_time in self._stopped_requests.items():
            if current_time - stop_time > self._expire_seconds:
                expired_keys.append(ask_id)

        for key in expired_keys:
            del self._stopped_requests[key]

        if expired_keys:
            logger.info(f"清理了 {len(expired_keys)} 条过期的停止记录")

        return len(expired_keys)

    def get_active_stop_requests(self) -> set[str]:
        """
        获取当前所有活跃的停止请求ID

        Returns:
            Set[str]: 活跃的请求ID集合
        """
        try:
            with self._lock:
                current_time = time.time()
                active_requests = set()

                for ask_id, stop_time in self._stopped_requests.items():
                    if current_time - stop_time <= self._expire_seconds:
                        active_requests.add(ask_id)

                return active_requests

        except Exception as e:
            logger.error(f"获取活跃停止请求时发生错误: {e!s}")
            return set()

    def get_stats(self) -> dict[str, int]:
        """
        获取管理器统计信息

        Returns:
            Dict[str, int]: 包含统计信息的字典
        """
        try:
            with self._lock:
                current_time = time.time()
                total_records = len(self._stopped_requests)
                expired_count = 0

                for stop_time in self._stopped_requests.values():
                    if current_time - stop_time > self._expire_seconds:
                        expired_count += 1

                return {
                    "total_records": total_records,
                    "active_records": total_records - expired_count,
                    "expired_records": expired_count,
                    "expire_seconds": self._expire_seconds
                }

        except Exception as e:
            logger.error(f"获取统计信息时发生错误: {e!s}")
            return {
                "total_records": 0,
                "active_records": 0,
                "expired_records": 0,
                "expire_seconds": self._expire_seconds
            }


# 创建全局实例
stop_request_manager = StopRequestManager()

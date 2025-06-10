from typing import Dict, List, Callable, Any
import asyncio
from datetime import datetime
import json
import numpy as np


class NumpyEncoder(json.JSONEncoder):
    """
    自定义JSON编码器，用于处理NumPy类型的序列化
    """

    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


class EventManager:
    """
    事件管理器，用于管理事件的发布和订阅
    """

    def __init__(self):
        # 存储事件ID和对应的订阅者列表
        self._subscribers: Dict[str, List[Callable]] = {}
        # 队列锁，防止并发问题
        self._lock = asyncio.Lock()

    async def subscribe(self, event_id: str) -> asyncio.Queue:
        """
        订阅指定事件ID的事件

        Args:
            event_id: 事件ID

        Returns:
            asyncio.Queue: 用于接收事件数据的队列
        """
        queue = asyncio.Queue()

        async with self._lock:
            if event_id not in self._subscribers:
                self._subscribers[event_id] = []
            self._subscribers[event_id].append(queue.put)

        return queue

    async def unsubscribe(self, event_id: str, callback: Callable):
        """
        取消订阅指定事件ID的事件

        Args:
            event_id: 事件ID
            callback: 订阅时注册的回调函数
        """
        async with self._lock:
            if event_id in self._subscribers and callback in self._subscribers[event_id]:
                self._subscribers[event_id].remove(callback)
                # 如果没有订阅者了，删除该事件ID
                if not self._subscribers[event_id]:
                    del self._subscribers[event_id]

    async def publish(self, event_id: str, data: Any, event_type: str = "data"):
        """
        向指定事件ID发布数据

        Args:
            event_id: 事件ID
            data: 要发布的数据
            event_type: 事件类型，默认为"data"
        """
        if not event_id:
            return

        # 构建统一的事件数据格式
        event_data = {
            "event_id": event_id,
            "event_type": event_type,
            "data": data,
            "timestamp": datetime.utcnow().isoformat()
        }

        # 使用自定义编码器处理NumPy类型
        serialized_data = json.dumps(event_data, cls=NumpyEncoder)

        async with self._lock:
            if event_id in self._subscribers:
                # 使用gather并发发送给所有订阅者
                await asyncio.gather(
                    *[callback(serialized_data) for callback in self._subscribers[event_id]],
                    return_exceptions=True
                )

    def get_active_events(self) -> List[str]:
        """
        获取当前所有活跃的事件ID

        Returns:
            List[str]: 活跃事件ID列表
        """
        return list(self._subscribers.keys())


# 创建全局实例，在整个应用中共享
event_manager = EventManager()

from datetime import datetime
import asyncio
from typing import Dict, Any, List, Optional


class WorkflowStateManager:
    """
    工作流状态管理器
    负责保存和分发工作流状态信息
    """

    def __init__(self):
        # 保存每个工作流的状态订阅者: workflow_id -> list of queues
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        # 保存每个工作流的最新状态: workflow_id -> state
        self._latest_states: Dict[str, Dict[str, Any]] = {}
        # 保存每个工作流的所有历史状态: workflow_id -> list of states
        self._history_states: Dict[str, List[Dict[str, Any]]] = {}
        # 锁，防止并发问题
        self._lock = asyncio.Lock()

    async def subscribe(self, workflow_id: str) -> asyncio.Queue:
        """
        订阅工作流状态更新

        Args:
            workflow_id: 工作流ID

        Returns:
            asyncio.Queue: 用于接收状态更新的队列
        """
        queue = asyncio.Queue()

        async with self._lock:
            if workflow_id not in self._subscribers:
                self._subscribers[workflow_id] = []
            self._subscribers[workflow_id].append(queue)

            # 如果已有该工作流的历史状态，按顺序发送所有历史状态
            if workflow_id in self._history_states and self._history_states[workflow_id]:
                for state in self._history_states[workflow_id]:
                    await queue.put(state)
            # 如果没有历史状态但有最新状态，发送最新状态
            elif workflow_id in self._latest_states:
                await queue.put(self._latest_states[workflow_id])

        return queue

    async def unsubscribe(self, workflow_id: str, queue: asyncio.Queue) -> None:
        """
        取消订阅工作流状态更新

        Args:
            workflow_id: 工作流ID
            queue: 之前订阅时返回的队列
        """
        async with self._lock:
            if workflow_id in self._subscribers and queue in self._subscribers[workflow_id]:
                self._subscribers[workflow_id].remove(queue)

                # 如果没有订阅者了，清理订阅者列表
                if not self._subscribers[workflow_id]:
                    del self._subscribers[workflow_id]

                    # 注意：不再删除最新状态和历史状态
                    # 这样可以确保后来的订阅者也能收到完整状态历史

    def _current_timestamp(self):
        """获取当前时间戳"""
        return datetime.now().isoformat()

    async def publish_state(self, workflow_id: str, state: Dict[str, Any]) -> None:
        """
        发布工作流状态更新

        Args:
            workflow_id: 工作流ID
            state: 工作流状态信息
        """
        # 添加时间戳
        state["timestamp"] = self._current_timestamp()

        async with self._lock:
            # 保存最新状态
            self._latest_states[workflow_id] = state.copy()

            # 将状态添加到历史记录中
            if workflow_id not in self._history_states:
                self._history_states[workflow_id] = []
            self._history_states[workflow_id].append(state.copy())

            # 如果有订阅者，发送状态更新
            if workflow_id in self._subscribers:
                for queue in self._subscribers[workflow_id]:
                    await queue.put(state)

    async def publish_node_state(self,
                                 workflow_id: str,
                                 node_id: str,
                                 node_title: str,
                                 status: str,
                                 started_at: Optional[float] = None,
                                 execution_time: Optional[float] = None,
                                 error_message: Optional[str] = None) -> None:
        """
        发布节点状态更新

        Args:
            workflow_id: 工作流ID
            node_id: 节点ID
            node_title: 节点标题
            status: 节点状态（'waiting', 'executing', 'completed', 'failed'）
            started_at: 节点开始执行时间戳
            execution_time: 当前执行时间（秒）
            error_message: 错误信息，如果有的话
        """
        state = {
            "workflow_id": workflow_id,
            "current_node": {
                "id": node_id,
                "title": node_title,
                "status": status,
            }
        }

        if started_at is not None:
            state["current_node"]["started_at"] = datetime.fromtimestamp(started_at).isoformat()

        if execution_time is not None:
            state["current_node"]["execution_time"] = execution_time

        if error_message:
            state["current_node"]["error"] = error_message

        await self.publish_state(workflow_id, state)

    async def publish_workflow_completed(self, workflow_id: str) -> None:
        """
        发布工作流完成状态

        Args:
            workflow_id: 工作流ID
        """
        state = {
            "workflow_id": workflow_id,
            "status": "completed",
            "timestamp": self._current_timestamp()
        }

        async with self._lock:
            # 保存状态
            self._latest_states[workflow_id] = state.copy()

            # 添加到历史状态
            if workflow_id not in self._history_states:
                self._history_states[workflow_id] = []
            self._history_states[workflow_id].append(state.copy())

            # 发送状态更新
            if workflow_id in self._subscribers:
                for queue in self._subscribers[workflow_id]:
                    await queue.put(state)
                    # 发送结束信号
                    await queue.put(None)

    async def cleanup_old_workflows(self, max_age_hours: int = 24):
        """
        清理旧的工作流状态数据

        Args:
            max_age_hours: 最大保留时间（小时）
        """
        now = datetime.now()
        max_age = now - datetime.timedelta(hours=max_age_hours)

        async with self._lock:
            workflows_to_remove = []

            # 找出超过保留时间的工作流
            for workflow_id, states in self._history_states.items():
                if not states:
                    continue

                # 检查最后一个状态的时间戳
                try:
                    last_state = states[-1]
                    timestamp_str = last_state.get("timestamp")
                    if not timestamp_str:
                        continue

                    timestamp = datetime.fromisoformat(timestamp_str)
                    if timestamp < max_age:
                        workflows_to_remove.append(workflow_id)
                except (ValueError, TypeError):
                    continue

            # 删除旧工作流的状态数据
            for workflow_id in workflows_to_remove:
                if workflow_id in self._history_states:
                    del self._history_states[workflow_id]
                if workflow_id in self._latest_states:
                    del self._latest_states[workflow_id]


# 创建全局单例
workflow_state_manager = WorkflowStateManager()

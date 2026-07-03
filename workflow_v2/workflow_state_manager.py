import asyncio
import time
from datetime import datetime
from typing import Any


class WorkflowStateManager:
    """
    工作流状态管理器
    负责保存和分发工作流状态信息，并包含自动清理机制
    """

    def __init__(self,
                 max_inactive_time=180,  # 默认保留180s非活动工作流
                 max_history_states=50,  # 每个工作流默认最多保留50条历史状态
                 cleanup_interval=300):  # 默认每5分钟清理一次
        # 保存每个工作流的状态订阅者: workflow_id -> list of queues
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        # 保存每个工作流的最新状态: workflow_id -> state
        self._latest_states: dict[str, dict[str, Any]] = {}
        # 保存每个工作流的所有历史状态: workflow_id -> list of states
        self._history_states: dict[str, list[dict[str, Any]]] = {}
        # 工作流最后活跃时间: workflow_id -> timestamp
        self._last_active: dict[str, float] = {}
        # 锁，防止并发问题
        self._lock = asyncio.Lock()

        # 配置参数
        self.max_inactive_time = max_inactive_time  # 秒
        self.max_history_states = max_history_states
        self.cleanup_interval = cleanup_interval  # 秒

        # 清理任务的引用 - 在start方法中创建
        self._cleanup_task = None
        self._started = False

    async def start(self):
        """启动状态管理器"""
        if not self._started:
            self._cleanup_task = asyncio.create_task(self._auto_cleanup())
            self._started = True

    async def _auto_cleanup(self):
        """自动清理过期的工作流状态"""
        try:
            while True:
                await asyncio.sleep(self.cleanup_interval)
                await self.cleanup_expired_workflows()
        except asyncio.CancelledError:
            # 任务被取消时正常退出
            pass

    async def cleanup_expired_workflows(self):
        """清理过期的工作流状态"""
        now = time.time()
        expired_workflows = []

        async with self._lock:
            # 找出过期的工作流
            for workflow_id, last_active in self._last_active.items():
                if now - last_active > self.max_inactive_time:
                    # 确保该工作流没有活跃订阅者
                    if workflow_id not in self._subscribers or not self._subscribers[workflow_id]:
                        expired_workflows.append(workflow_id)

            # 清理过期的工作流数据
            for workflow_id in expired_workflows:
                if workflow_id in self._latest_states:
                    del self._latest_states[workflow_id]
                if workflow_id in self._history_states:
                    del self._history_states[workflow_id]
                del self._last_active[workflow_id]

            # 限制历史状态数量
            for workflow_id, states in self._history_states.items():
                if len(states) > self.max_history_states:
                    # 只保留最新的max_history_states条记录
                    self._history_states[workflow_id] = states[-self.max_history_states:]

    async def clear_workflow_state(self, workflow_id: str) -> None:
        """
        清除指定工作流的所有状态记录

        Args:
            workflow_id: 需要清除状态的工作流ID
        """
        async with self._lock:
            # 清除最新状态
            if workflow_id in self._latest_states:
                del self._latest_states[workflow_id]

            # 清除历史状态
            if workflow_id in self._history_states:
                del self._history_states[workflow_id]

            # 更新最后活跃时间 (保留，便于后续管理)
            self._last_active[workflow_id] = time.time()

            # 注意：不清除订阅者列表，因为可能有正在等待的订阅者

    async def subscribe(self, workflow_id: str) -> asyncio.Queue:
        """
        订阅工作流状态更新

        Args:
            workflow_id: 工作流ID

        Returns:
            asyncio.Queue: 用于接收状态更新的队列
        """
        # 确保状态管理器已启动
        if not self._started:
            await self.start()

        queue = asyncio.Queue()

        async with self._lock:
            # 更新最后活跃时间
            self._last_active[workflow_id] = time.time()

            if workflow_id not in self._subscribers:
                self._subscribers[workflow_id] = []
            self._subscribers[workflow_id].append(queue)

            # 如果已有该工作流的历史状态，按顺序发送所有历史状态
            if self._history_states.get(workflow_id):
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

                    # 更新最后活跃时间，以便后续清理
                    self._last_active[workflow_id] = time.time()

    def _current_timestamp(self):
        """获取当前时间戳"""
        return datetime.now().isoformat()

    async def publish_state(self, workflow_id: str, state: dict[str, Any]) -> None:
        """
        发布工作流状态更新

        Args:
            workflow_id: 工作流ID
            state: 工作流状态信息
        """
        # 确保状态管理器已启动
        if not self._started:
            await self.start()

        # 添加时间戳
        state["timestamp"] = self._current_timestamp()

        async with self._lock:
            # 更新最后活跃时间
            self._last_active[workflow_id] = time.time()

            # 保存最新状态
            self._latest_states[workflow_id] = state.copy()

            # 将状态添加到历史记录中
            if workflow_id not in self._history_states:
                self._history_states[workflow_id] = []

            # 添加新状态，如果超过限制则移除最旧的状态
            self._history_states[workflow_id].append(state.copy())
            if len(self._history_states[workflow_id]) > self.max_history_states:
                self._history_states[workflow_id].pop(0)

            # 如果有订阅者，发送状态更新
            if workflow_id in self._subscribers:
                for queue in self._subscribers[workflow_id]:
                    await queue.put(state)

    async def publish_node_state(self,
                                 workflow_id: str,
                                 node_id: str,
                                 node_title: str,
                                 status: str,
                                 started_at: float | None = None,
                                 execution_time: float | None = None,
                                 error_message: str | None = None) -> None:
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
        # 确保状态管理器已启动
        if not self._started:
            await self.start()

        state = {
            "workflow_id": workflow_id,
            "status": "completed",
            "timestamp": self._current_timestamp()
        }

        async with self._lock:
            # 更新最后活跃时间
            self._last_active[workflow_id] = time.time()

            # 保存状态
            self._latest_states[workflow_id] = state.copy()

            # 添加到历史状态
            if workflow_id not in self._history_states:
                self._history_states[workflow_id] = []

            self._history_states[workflow_id].append(state.copy())
            if len(self._history_states[workflow_id]) > self.max_history_states:
                self._history_states[workflow_id].pop(0)

            # 发送状态更新
            if workflow_id in self._subscribers:
                for queue in self._subscribers[workflow_id]:
                    await queue.put(state)
                    # 发送结束信号
                    await queue.put(None)

    async def shutdown(self):
        """关闭状态管理器，清理资源"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
            self._started = False


# 创建全局单例
workflow_state_manager = WorkflowStateManager()

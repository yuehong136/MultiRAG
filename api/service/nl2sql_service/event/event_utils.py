import logging
from contextlib import asynccontextmanager
from typing import Any

from api.service.nl2sql_service.event.event_manager import event_manager

# 配置日志
logger = logging.getLogger(__name__)


async def send_event(event_id: str, data: Any, event_type: str = "data") -> bool:
    """
    向指定事件ID发送数据

    Args:
        event_id: 事件ID
        data: 要发送的数据
        event_type: 事件类型，默认为"data"

    Returns:
        bool: 发送是否成功
    """
    try:
        await event_manager.publish(event_id, data, event_type)
        return True
    except Exception as e:
        logger.error(f"发送事件失败: {e!s}")
        return False


async def send_step_event(event_id: str, step: int, total_steps: int, message: str, details: dict | None = None) -> bool:
    """
    发送步骤进度事件

    Args:
        event_id: 事件ID
        step: 当前步骤
        total_steps: 总步骤数
        message: 步骤消息
        details: 步骤详情

    Returns:
        bool: 发送是否成功
    """
    percentage = int((step / total_steps) * 100) if total_steps > 0 else 0

    step_data = {"status": "processing", "step": step, "total_steps": total_steps, "percentage": percentage, "message": message}

    if details:
        step_data["details"] = details

    return await send_event(event_id, step_data, "progress")


async def send_status_event(event_id: str, status: str, message: str = "", details: dict | None = None) -> bool:
    """
    发送状态事件

    Args:
        event_id: 事件ID
        status: 状态（started, processing, completed, error等）
        message: 状态消息
        details: 状态详情

    Returns:
        bool: 发送是否成功
    """
    status_data = {"status": status, "message": message}

    if details:
        status_data["details"] = details

    return await send_event(event_id, status_data, "status")


async def send_result_event(event_id: str, results: dict[str, Any]) -> bool:
    """
    发送结果事件

    Args:
        event_id: 事件ID
        results: 结果数据

    Returns:
        bool: 发送是否成功
    """
    result_data = {"status": "success", "results": results}

    return await send_event(event_id, result_data, "result")


async def send_error_event(event_id: str, error_message: str, error_code: str = None) -> bool:
    """
    发送错误事件

    Args:
        event_id: 事件ID
        error_message: 错误消息
        error_code: 错误代码

    Returns:
        bool: 发送是否成功
    """
    error_data = {"status": "error", "error": error_message}

    if error_code:
        error_data["error_code"] = error_code

    return await send_event(event_id, error_data, "error")


@asynccontextmanager
async def process_tracker(event_id: str, total_steps: int = 100):
    """
    一个上下文管理器，用于追踪处理过程的进度

    用法:
    ```python
    async with process_tracker("request-123", total_steps=5) as tracker:
        # 执行第一步
        await tracker.update(1, "开始处理数据")

        # 执行第二步
        await tracker.update(2, "分析查询意图")

        # 最后一步
        await tracker.update(5, "生成报表")
    ```

    Args:
        event_id: 事件ID
        total_steps: 总步骤数

    Yields:
        ProcessTracker: 进度追踪器对象
    """

    class ProcessTracker:
        def __init__(self, event_id: str, total_steps: int):
            self.event_id = event_id
            self.total_steps = total_steps
            self.current_step = 0

        async def update(self, step: int, message: str = "", details: dict | None = None):
            """
            更新进度

            Args:
                step: 当前步骤数（1到total_steps之间）
                message: 进度消息
                details: 可选的详细信息
            """
            if step < 0:
                step = 0
            if step > self.total_steps:
                step = self.total_steps

            self.current_step = step
            await send_step_event(self.event_id, step, self.total_steps, message, details)

    # 初始化进度
    tracker = ProcessTracker(event_id, total_steps)
    await send_status_event(event_id, "started", f"开始处理，共{total_steps}个步骤", {"total_steps": total_steps})

    try:
        # 提供进度追踪器给调用代码
        yield tracker

        # 如果没有到达100%，自动完成进度
        if tracker.current_step < total_steps:
            await tracker.update(total_steps, "已完成")

        # 发送完成消息
        await send_status_event(event_id, "completed", "处理已完成")
    except Exception as e:
        # 记录错误
        logger.exception(f"处理过程出错: {e!s}")

        # 发送错误消息
        await send_error_event(event_id, str(e))

        # 重新抛出异常，让调用者处理
        raise

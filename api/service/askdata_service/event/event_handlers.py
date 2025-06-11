import asyncio
import json
import logging
from datetime import datetime

from fastapi import Request, Response, HTTPException
from fastapi.responses import StreamingResponse
from typing import AsyncGenerator

from api.service.askdata_service.event.event_manager import event_manager

logger = logging.getLogger(__name__)


async def event_generator(request: Request, event_id: str) -> AsyncGenerator[bytes, None]:
    """
    事件生成器，用于生成SSE事件流。
    在接收到 'stream_end' 事件后会自动关闭连接。

    Args:
        request: FastAPI请求对象
        event_id: 事件ID

    Yields:
        bytes: SSE消息格式的字节流
    """
    if not event_id:
        raise HTTPException(status_code=400, detail="Event ID is required")

    # 订阅事件队列
    queue = await event_manager.subscribe(event_id)

    # 发送初始连接成功消息
    await event_manager.publish(
        event_id=event_id,
        data={"status": "connected"},
        event_type="connection"
    )

    try:
        while True:
            if await request.is_disconnected():
                logger.warning(f"Client disconnected for event_id: {event_id}")
                break

            try:
                # 等待新数据，带超时
                data = await asyncio.wait_for(queue.get(), timeout=1.0)

                # 1. 总是先将消息发送给客户端
                message = f"data: {data}\n\n"
                yield message.encode('utf-8')

                # 2. 检查这是否是结束信号
                try:
                    event_data = json.loads(data)
                    if event_data.get("event_type") == "stream_end":
                        logger.info(f"Stream for event_id {event_id} ended. Closing server-side connection.")
                        break  # 退出循环，关闭连接
                except (json.JSONDecodeError, AttributeError):
                    # 如果数据不是有效的JSON或没有event_type，忽略即可
                    pass

            except asyncio.TimeoutError:
                # 超时后发送心跳包，保持连接活跃
                yield b": heartbeat\n\n"
            except Exception as e:
                # 发生错误，发送错误消息并中断连接
                logger.error(f"Error in event generator for {event_id}: {e}")
                error_data = {
                    "event_id": event_id,
                    "event_type": "error",
                    "data": {"error": str(e)},
                    "timestamp": datetime.utcnow().isoformat()
                }
                error_message = f"data: {json.dumps(error_data)}\n\n"
                yield error_message.encode('utf-8')
                break
    finally:
        # 确保在函数结束时取消订阅，防止内存泄漏
        logger.info(f"Unsubscribing and cleaning up for event_id: {event_id}")
        # 注意：这里的 queue.put 是 callback
        await event_manager.unsubscribe(event_id, queue.put)


def create_sse_response(request: Request, event_id: str) -> StreamingResponse:
    """
    创建SSE响应

    Args:
        request: FastAPI请求对象
        event_id: 事件ID

    Returns:
        StreamingResponse: SSE流响应
    """
    response = StreamingResponse(
        event_generator(request, event_id),
        media_type="text/event-stream"
    )

    # 设置SSE相关的响应头
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Connection"] = "keep-alive"
    response.headers["X-Accel-Buffering"] = "no"  # 禁用Nginx缓冲

    return response

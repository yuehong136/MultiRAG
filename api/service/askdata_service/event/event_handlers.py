import asyncio
import json
from datetime import datetime

from fastapi import Request, Response, HTTPException
from fastapi.responses import StreamingResponse
from typing import AsyncGenerator

from api.service.askdata_service.event.event_manager import event_manager


async def event_generator(request: Request, event_id: str) -> AsyncGenerator[bytes, None]:
    """
    事件生成器，用于生成SSE事件流

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

    # 发送初始连接成功消息（使用统一的格式）
    await event_manager.publish(
        event_id=event_id,
        data={"status": "connected"},
        event_type="connection"
    )

    try:
        # 持续监听队列，直到客户端断开连接
        while True:
            # 检查客户端是否已经断开连接
            if await request.is_disconnected():
                break

            try:
                # 等待新数据，带超时
                data = await asyncio.wait_for(queue.get(), timeout=1.0)
                # 直接将数据作为SSE消息发送
                message = f"data: {data}\n\n"
                yield message.encode('utf-8')
            except asyncio.TimeoutError:
                # 超时后发送心跳包，保持连接活跃
                yield b": heartbeat\n\n"
            except Exception as e:
                # 发生错误，发送错误消息并中断连接
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
        await event_manager.unsubscribe(event_id, queue.put)

        # 可选：发送断开连接消息
        try:
            await event_manager.publish(
                event_id=event_id,
                data={"status": "disconnected"},
                event_type="connection"
            )
        except:
            # 忽略断开连接时的错误
            pass


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

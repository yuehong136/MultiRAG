# services/async_llm_service.py - 最终完整版
import asyncio
import logging
import threading
import queue
import time
from typing import Any
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from api.db.services.llm_service import LLMBundle
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from api.service.askdata_service.event.event_manager import event_manager
from api.service.askdata_service.util.askdata_logger import get_askdata_logger, askdata_ask_id, askdata_query
from common.constants import LLMType


logger = get_askdata_logger()


class AsyncLLMService:
    """异步LLM服务 - 简化版本，最稳定"""

    def __init__(self, db: Session):
        self.db = db
        self.executor = ThreadPoolExecutor(max_workers=4)

    async def send_event(self, event_id: str, data: dict[str, Any], event_type: str = "message"):
        """发送事件的辅助方法"""
        try:
            await event_manager.publish(
                event_id=event_id,
                data=data,
                event_type=event_type
            )
        except Exception as e:
            logger.error(f"Failed to send event {event_type}: {e}")

    async def chat_stream_async(
            self,
            event_id: str,
            tenant_id: str,
            system: str = "",
            history: list = None,
            gen_conf: dict = None,
            llm_name: str | None = None
    ) -> None:
        if history is None:
            history = []
        if gen_conf is None:
            gen_conf = {"temperature": 0.7, "max_tokens": 2000}

        try:
            await self.send_event(
                event_id,
                {"message": "开始生成回复", "status": "started"},
                "chat_start"
            )

            logger.info(f"🤖 创建LLM实例 - event_id: {event_id}, llm_name: {llm_name}")
            model_config = get_model_config_by_type_and_name(self.db, tenant_id, LLMType.CHAT.value, llm_name)
            llm_bundle = LLMBundle(
                db=self.db,
                tenant_id=tenant_id,
                model_config=model_config,
            )

            await self.send_event(
                event_id,
                {
                    "message": "模型加载完成，开始流式生成",
                    "model_name": llm_name or "default",
                    "tenant_id": tenant_id
                },
                "model_ready"
            )

            await self._simple_streaming(
                event_id=event_id,
                llm_bundle=llm_bundle,
                system=system,
                history=history,
                gen_conf=gen_conf
            )

        except Exception as e:
            logger.exception(f"Async chat stream error - event_id: {event_id}")
            await self.send_event(
                event_id,
                {
                    "message": f"聊天失败: {str(e)}",
                    "error": str(e),
                    "status": "error"
                },
                "chat_error"
            )
            # SSE 生成器只在收到 stream_end 时关闭连接，错误后必须收尾，否则连接挂到30分钟超时
            await self.send_event(
                event_id,
                {"message": "Stream finished, closing connection."},
                "stream_end"
            )

    async def _simple_streaming(
            self,
            event_id: str,
            llm_bundle: LLMBundle,
            system: str,
            history: list,
            gen_conf: dict
    ) -> None:
        """简化的流式处理，使用标准库队列"""

        chunk_queue = queue.Queue()
        error_occurred = threading.Event()

        # ContextVar 不会自动传进 threading.Thread；先捕获、再在 worker 内 set 回，
        # 否则工作线程里的日志（工作线程完成/异常）会落到 _misc 兜底而非该问文件
        _ctx_ask_id = askdata_ask_id.get("-")
        _ctx_query = askdata_query.get("")

        def llm_worker():
            """LLM工作线程"""
            askdata_ask_id.set(_ctx_ask_id)
            askdata_query.set(_ctx_query)
            try:
                start_time = time.time()

                for i, chunk in enumerate(llm_bundle.chat_streamly(system, history, gen_conf)):
                    chunk_queue.put(('chunk', chunk))
                    if error_occurred.is_set():
                        logger.warning(f"检测到错误信号，停止生成 - event_id: {event_id}")
                        break

                chunk_queue.put(('done', None))
                total_time = time.time() - start_time
                logger.info(f"LLM工作线程完成 - {total_time:.2f}s - event_id: {event_id}")

            except Exception as e:
                logger.error(f"LLM工作线程异常 - event_id: {event_id}: {e}")
                chunk_queue.put(('error', str(e)))
                error_occurred.set()

        worker_thread = threading.Thread(target=llm_worker, daemon=True)
        worker_thread.start()

        try:
            full_content = ""
            last_content = ""
            chunk_count = 0
            first_chunk_time = None

            while True:
                try:
                    chunk_type, chunk_data = chunk_queue.get(timeout=0.1)

                    if chunk_type == 'done':
                        # 流式段是历史事故最集中处：记一次最终内容规模便于复盘；
                        # chunk 有但正文为空 = 推理模型「只思考无正文」（前端已兜底），单独告警一眼定性
                        logger.info(
                            f"分析流完成 - content_len={len(full_content)}, "
                            f"chunk_count={chunk_count} - event_id: {event_id}"
                        )
                        if chunk_count > 0 and len(full_content) == 0:
                            logger.warning(
                                f"分析流只思考无正文 - chunk_count={chunk_count} 但最终正文为空"
                                f"（疑似推理模型以 </think> 收尾）- event_id: {event_id}"
                            )
                        await self.send_event(
                            event_id,
                            {
                                "message": "生成完成",
                                "tokens_used": 0,
                                "chunk_count": chunk_count,
                                "status": "completed"
                            },
                            "chat_complete"
                        )
                        await self.send_event(
                            event_id,
                            {
                                "content": full_content,
                                "tokens_used": 0,
                                "final": True
                            },
                            "chat_result"
                        )

                        await self.send_event(
                            event_id,
                            {"message": "Stream finished, closing connection."},
                            "stream_end"
                        )

                        break

                    elif chunk_type == 'error':
                        logger.error(f"处理错误 - event_id: {event_id}: {chunk_data}")
                        await self.send_event(
                            event_id,
                            {"message": f"LLM生成错误: {chunk_data}", "status": "error"},
                            "chat_error"
                        )
                        await self.send_event(
                            event_id,
                            {"message": "Stream finished, closing connection."},
                            "stream_end"
                        )
                        return

                    elif chunk_type == 'chunk':
                        if first_chunk_time is None:
                            first_chunk_time = time.time()

                        current_content = str(chunk_data)

                        delta_content = current_content[len(last_content):]
                        last_content = current_content

                        full_content = current_content
                        chunk_count += 1

                        await self.send_event(
                            event_id,
                            {
                                "content": delta_content,
                                "chunk_index": chunk_count,
                                "is_final": False
                            },
                            "chat_content"
                        )

                except queue.Empty:
                    await asyncio.sleep(0.01)
                    if not worker_thread.is_alive() and chunk_queue.empty():
                        logger.warning(f"工作线程意外结束 - event_id: {event_id}")
                        await self.send_event(
                            event_id,
                            {"message": "LLM工作线程意外结束", "status": "error"},
                            "chat_error"
                        )
                        await self.send_event(
                            event_id,
                            {"message": "Stream finished, closing connection."},
                            "stream_end"
                        )
                        break

        except Exception as e:
            logger.exception(f"流式处理主循环异常 - event_id: {event_id}")
            error_occurred.set()
            await self.send_event(
                event_id,
                {"message": f"流式处理错误: {str(e)}", "status": "error"},
                "chat_error"
            )
            await self.send_event(
                event_id,
                {"message": "Stream finished, closing connection."},
                "stream_end"
            )
        finally:
            error_occurred.set()
            if worker_thread.is_alive():
                worker_thread.join(timeout=2.0)
                if worker_thread.is_alive():
                    logger.warning(f"工作线程未及时结束 - event_id: {event_id}")

    def close(self):
        """关闭服务，清理资源"""
        self.executor.shutdown(wait=True)


def get_async_llm_service(db: Session) -> AsyncLLMService:
    """获取异步LLM服务实例的依赖注入函数"""
    return AsyncLLMService(db)

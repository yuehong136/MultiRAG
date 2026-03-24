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
from common.constants import LLMType


logger = logging.getLogger(__name__)


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
            logger.debug(f"✅ Event sent: {event_type} - {event_id}")
        except Exception as e:
            logger.error(f"❌ Failed to send event {event_type}: {e}")

    async def chat_stream_async(
            self,
            event_id: str,
            tenant_id: str,
            system: str = "",
            history: list = None,
            gen_conf: dict = None,
            llm_name: str | None = None
    ) -> None:
        """
        异步流式聊天主函数 - 简化版本

        Args:
            event_id: 事件ID，用于前端监听
            tenant_id: 租户ID
            system: 系统提示词
            history: 对话历史
            gen_conf: 生成配置
            llm_name: 模型名称
        """
        if history is None:
            history = []
        if gen_conf is None:
            gen_conf = {"temperature": 0.7, "max_tokens": 2000}

        try:
            # 1. 发送开始事件
            await self.send_event(
                event_id,
                {"message": "开始生成回复", "status": "started"},
                "chat_start"
            )

            # 2. 创建LLM实例
            logger.info(f"🤖 创建LLM实例 - event_id: {event_id}, llm_name: {llm_name}")
            model_config = get_model_config_by_type_and_name(self.db, tenant_id, LLMType.CHAT.value, llm_name)
            llm_bundle = LLMBundle(
                db=self.db,
                tenant_id=tenant_id,
                model_config=model_config,
            )

            # 3. 发送模型就绪事件
            await self.send_event(
                event_id,
                {
                    "message": "模型加载完成，开始流式生成",
                    "model_name": llm_name or "default",
                    "tenant_id": tenant_id
                },
                "model_ready"
            )

            # 4. 执行简化的流式生成
            await self._simple_streaming(
                event_id=event_id,
                llm_bundle=llm_bundle,
                system=system,
                history=history,
                gen_conf=gen_conf
            )

        except Exception as e:
            logger.exception(f"💥 Async chat stream error - event_id: {event_id}: {e}")
            await self.send_event(
                event_id,
                {
                    "message": f"聊天失败: {str(e)}",
                    "error": str(e),
                    "status": "error"
                },
                "chat_error"
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

        def llm_worker():
            """LLM工作线程 - 简化版本"""
            try:
                logger.info(f"🚀 LLM工作线程启动 - event_id: {event_id}")
                start_time = time.time()

                for i, chunk in enumerate(llm_bundle.chat_streamly(system, history, gen_conf)):
                    elapsed = time.time() - start_time
                    logger.debug(f"📦 LLM产生chunk {i + 1} - {elapsed:.2f}s - {type(chunk)} - event_id: {event_id}")
                    chunk_queue.put(('chunk', chunk))
                    if error_occurred.is_set():
                        logger.warning(f"⚠️ 检测到错误信号，停止生成 - event_id: {event_id}")
                        break

                chunk_queue.put(('done', None))
                total_time = time.time() - start_time
                logger.info(f"✅ LLM工作线程完成 - {total_time:.2f}s - event_id: {event_id}")

            except Exception as e:
                logger.error(f"❌ LLM工作线程异常 - event_id: {event_id}: {e}")
                chunk_queue.put(('error', str(e)))
                error_occurred.set()

        worker_thread = threading.Thread(target=llm_worker, daemon=True)
        worker_thread.start()

        try:
            full_content = ""
            last_content = ""
            chunk_count = 0
            first_chunk_time = None

            logger.info(f"⏳ 开始等待LLM响应 - event_id: {event_id}")

            while True:
                try:
                    chunk_type, chunk_data = chunk_queue.get(timeout=0.1)

                    if chunk_type == 'done':
                        logger.info(f"🏁 流处理在工作线程中完成，现在发送最终事件 - event_id: {event_id}")
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

                        # --- [修改] 开始 ---
                        # 发送一个专门的结束事件，以通知服务器关闭连接
                        await self.send_event(
                            event_id,
                            {"message": "Stream finished, closing connection."},
                            "stream_end"
                        )
                        # --- [修改] 结束 ---

                        break

                    elif chunk_type == 'error':
                        logger.error(f"💥 处理错误 - event_id: {event_id}: {chunk_data}")
                        await self.send_event(
                            event_id,
                            {"message": f"LLM生成错误: {chunk_data}", "status": "error"},
                            "chat_error"
                        )
                        return

                    elif chunk_type == 'chunk':
                        if first_chunk_time is None:
                            first_chunk_time = time.time()
                            logger.info(f"⚡ 首个chunk到达 - event_id: {event_id}")

                        # 直接处理文本内容
                        current_content = str(chunk_data)

                        delta_content = current_content[len(last_content):]
                        last_content = current_content

                        full_content = current_content
                        chunk_count += 1

                        logger.debug(
                            f"📝 处理文本chunk {chunk_count} - delta_len:{len(delta_content)} - event_id: {event_id}")

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
                        logger.warning(f"⚠️ 工作线程意外结束 - event_id: {event_id}")
                        await self.send_event(
                            event_id,
                            {"message": "LLM工作线程意外结束", "status": "error"},
                            "chat_error"
                        )
                        break

        except Exception as e:
            logger.exception(f"💥 流式处理主循环异常 - event_id: {event_id}: {e}")
            error_occurred.set()
            await self.send_event(
                event_id,
                {"message": f"流式处理错误: {str(e)}", "status": "error"},
                "chat_error"
            )
        finally:
            error_occurred.set()
            if worker_thread.is_alive():
                logger.info(f"🔄 等待工作线程结束 - event_id: {event_id}")
                worker_thread.join(timeout=2.0)
                if worker_thread.is_alive():
                    logger.warning(f"⚠️ 工作线程未及时结束 - event_id: {event_id}")

            logger.info(f"🧹 流式处理清理完成 - event_id: {event_id}")

    def close(self):
        """关闭服务，清理资源"""
        logger.info("🔄 关闭AsyncLLMService")
        self.executor.shutdown(wait=True)


def get_async_llm_service(db: Session) -> AsyncLLMService:
    """获取异步LLM服务实例的依赖注入函数"""
    return AsyncLLMService(db)

import time
import logging
from typing import TypeVar, Coroutine, Any

logger = logging.getLogger(__name__)

T = TypeVar('T')

async def time_task(coro: Coroutine[Any, Any, T], name: str, log_level: str = "INFO") -> T:
    """
    一个异步辅助函数，用于执行一个协程任务并打印其执行时间。

    :param coro: 需要被执行和计时的协程对象。
    :param name: 任务的描述性名称，用于日志输出。
    :param log_level: 日志级别，默认为INFO，可选DEBUG、WARNING等。
    :return: 原始协程的返回结果。

    Usage:
        result = await time_task(
            some_async_function(param1, param2),
            name="获取用户数据"
        )
    """
    start_time = time.perf_counter()
    try:
        result = await coro
        end_time = time.perf_counter()
        elapsed_time = end_time - start_time

        # 根据指定的日志级别记录
        log_message = f"任务 '{name}' 执行耗时: {elapsed_time:.4f} 秒"
        if log_level == "DEBUG":
            logger.debug(log_message)
        elif log_level == "WARNING":
            logger.warning(log_message)
        else:
            logger.info(log_message)

        return result
    except Exception as e:
        end_time = time.perf_counter()
        elapsed_time = end_time - start_time
        logger.error(f"任务 '{name}' 执行失败，耗时: {elapsed_time:.4f} 秒，错误: {str(e)}")
        raise
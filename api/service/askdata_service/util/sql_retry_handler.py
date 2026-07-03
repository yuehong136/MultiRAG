# sql_retry_handler.py
from collections.abc import Callable
from typing import Any

from api.service.askdata_service.util.askdata_logger import get_askdata_logger

logger = get_askdata_logger()


class SQLRetryHandler:
    """SQL执行重试处理器"""

    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries

    async def execute_with_retry(self, execute_func: Callable, fix_func: Callable, sql: str, dataset_id: int, fix_params: dict[str, Any]) -> dict[str, Any]:
        """
        执行SQL，失败时自动修复并重试

        Args:
            execute_func: 执行SQL的函数 (query_data_with_params)
            fix_func: 修复SQL的函数 (service.fix_sql_query_with_components)
            sql: 初始SQL
            dataset_id: 数据集ID
            fix_params: 修复函数需要的参数

        Returns:
            包含执行结果和元信息的字典
        """
        current_sql = sql
        current_used_models = fix_params.get("used_models")
        execution_history = []

        for attempt in range(self.max_retries + 1):
            logger.info(f"执行SQL尝试 {attempt + 1}/{self.max_retries + 1}")

            # 执行SQL
            result = await execute_func(current_sql, dataset_id, [])

            # 记录执行历史
            execution_history.append(
                {"attempt": attempt + 1, "sql": current_sql, "success": result.get("status") != "error", "error": result.get("message") if result.get("status") == "error" else None}
            )

            # 执行成功，返回结果
            if result.get("status") != "error":
                logger.info(f"SQL执行成功，尝试次数: {attempt + 1}")
                return {
                    "status": "success",
                    "data": result["data"],
                    "final_sql": current_sql,
                    "used_models": current_used_models,
                    "was_fixed": attempt > 0,
                    "retry_times": attempt,
                    "execution_history": execution_history,
                }

            # 如果是最后一次尝试，不再修复
            if attempt >= self.max_retries:
                logger.error(f"SQL执行失败，已达最大重试次数 {self.max_retries}")
                return {
                    "status": "error",
                    "message": result["message"],
                    "final_sql": current_sql,
                    "used_models": current_used_models,
                    "was_fixed": attempt > 0,
                    "retry_times": attempt,
                    "execution_history": execution_history,
                }

            # 尝试修复SQL
            logger.info(f"第 {attempt + 1} 次执行失败，错误: {result['message']}")
            logger.info("开始修复SQL...")

            fix_result = await fix_func(
                user_query=fix_params["user_query"], original_sql=current_sql, error_message=result["message"], semantic_layer=fix_params["semantic_layer"], llm_name=fix_params["llm_name"]
            )

            if not fix_result or not fix_result.get("sql"):
                logger.error(f"第 {attempt + 1} 次SQL修复失败")
                return {
                    "status": "error",
                    "message": f"SQL修复失败: {result['message']}",
                    "final_sql": current_sql,
                    "used_models": current_used_models,
                    "was_fixed": attempt > 0,
                    "retry_times": attempt,
                    "execution_history": execution_history,
                }

            # 更新SQL和相关组件
            current_sql = fix_result["sql"]
            current_used_models = fix_result.get("usedModels", current_used_models)
            logger.info(f"修复成功，新SQL: {current_sql}")

        # 理论上不会到这里
        return {"status": "error", "message": "未知错误", "final_sql": current_sql, "retry_times": self.max_retries}

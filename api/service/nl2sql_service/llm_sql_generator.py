import asyncio
import re
import logging
from typing import Any, Optional, Tuple

from sqlalchemy.orm import Session

from api.db import LLMType
from api.db.services.llm_service import LLMBundle

logger = logging.getLogger(__name__)


class LLMSQLGenerator:
    """使用大语言模型(LLM)生成SQL查询的服务类"""

    # 编译正则表达式以提高性能
    SQL_PATTERN = re.compile(r'```sql\s*([\s\S]*?)```')

    def __init__(self, db: Session, user_id: Any):
        """
        初始化LLM SQL生成器

        参数:
            db: 数据库会话
            user_id: 用户ID
        """
        self.db = db
        self.user_id = user_id

    def _extract_sql_from_response(self, response: str) -> Tuple[Optional[str], bool]:
        """
        从LLM响应中提取SQL查询。

        参数:
            response: LLM返回的原始响应文本

        返回:
            (extracted_sql, success_flag): 提取的SQL和成功标志的元组
        """
        # 尝试从代码块中提取SQL
        sql_match = self.SQL_PATTERN.search(response)

        if sql_match:
            sql_str = sql_match.group(1).strip()
            return sql_str, True
        else:
            logger.warning("Failed to extract SQL from LLM response")
            return response, False

    async def generate_sql(self, prompt: str, llm_name: str) -> str:
        """
        使用LLM生成SQL查询。

        参数:
            prompt: 用于生成SQL的提示词
            llm_name: 用于生成的LLM模型名称

        返回:
            生成的SQL查询字符串
        """
        try:
            # 初始化LLM模型
            llm_model_instance = LLMBundle(self.db, self.user_id, LLMType.CHAT, llm_name=llm_name)

            # 创建包含提示词的对话历史
            history = [{"role": "user", "content": prompt}]

            # LLM配置
            gen_conf = {
                "temperature": 0.3,  # 降低温度以获得更确定的SQL结果
                "top_p": 0.9,
                "max_tokens": 2048
            }

            # 调用LLM处理提示词
            response = await asyncio.to_thread(
                llm_model_instance.chat,
                system="You are a SQL expert. Generate concise, correct SQL code based on the requirements.",
                history=history,
                gen_conf=gen_conf
            )

            # 提取SQL
            sql, success = self._extract_sql_from_response(response)

            if not success:
                logger.warning("Could not extract SQL properly from LLM response, returning raw response")

            return sql

        except Exception as e:
            logger.error(f"Error in LLM SQL generation: {e}", exc_info=True)
            return f"Error generating SQL with LLM: {str(e)}"  # 发生错误时，返回错误信息
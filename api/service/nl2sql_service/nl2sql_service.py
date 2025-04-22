import asyncio
import json
import os
import re
import logging
from typing import Any, List, Optional, Dict, Union, Tuple

from fastapi.params import Depends
from sqlalchemy.orm import Session

from api.apps import manager
from api.db import LLMType
from api.db.db_models import get_db
from api.db.services.llm_service import LLMBundle
from api.utils.prompt_template_util import PromptTemplateUtil

logger = logging.getLogger(__name__)


class NL2SQLService:
    """服务类，用于处理自然语言到SQL的转换和查询重写。"""

    # 编译正则表达式以提高性能
    JSON_PATTERN = re.compile(r'```json\s*([\s\S]*?)```')

    def __init__(self, db: Session, user: Any):
        self.db = db
        self.user = user
        self.prompt_dir = os.path.join(os.path.dirname(__file__), "prompt")

    def _extract_json_from_response(self, response: str) -> Tuple[Optional[Dict], bool]:
        """
        从响应中提取JSON数据。

        参数:
            response: LLM返回的原始响应文本

        返回:
            (parsed_data, success_flag): 提取的数据和成功标志的元组
        """
        # 首先尝试从代码块中提取JSON
        json_match = self.JSON_PATTERN.search(response)

        if json_match:
            json_str = json_match.group(1).strip()
            try:
                return json.loads(json_str), True
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSON from code block: {e}")

        # 如果没有找到代码块或解析失败，尝试解析整个响应
        try:
            return json.loads(response), True
        except json.JSONDecodeError:
            logger.warning("Failed to parse response as JSON")
            return None, False

    def _extract_rewritten_queries(self, data: Optional[Dict], response: str) -> List[str]:
        """
        从解析后的数据中提取重写的查询列表。

        参数:
            data: 解析后的JSON数据
            response: 原始响应（用于回退）

        返回:
            重写的查询列表
        """
        if not data:
            return [response]

        if isinstance(data, list):
            return data

        if isinstance(data, dict) and "rewritten_queries" in data:
            if isinstance(data["rewritten_queries"], list):
                return data["rewritten_queries"]

        # 回退方案：返回整个响应作为单个查询
        logger.info("Response format not as expected, returning original")
        return [response]

    async def rewrite_query(self, query_text: str, llm_name: str) -> List[str]:
        """
        使用LLM重写自然语言查询，生成多个变体。

        参数:
            query_text: 原始自然语言查询文本
            llm_name: 用于重写的LLM模型名称

        返回:
            重写后的查询变体列表
        """
        try:
            # 初始化LLM模型
            llm_model_instance = LLMBundle(self.db, self.user.id, LLMType.CHAT, llm_name=llm_name)

            # 从文件加载提示词模板
            template_path = os.path.join(self.prompt_dir, "user_query_rewrite_template.txt")
            prompt_template = PromptTemplateUtil.load_template_from_file(template_path)

            # 用查询文本填充模板
            prompt = PromptTemplateUtil.fill_template(
                prompt_template,
                {"query_text": query_text}
            )

            # 创建包含我们提示词的对话历史
            history = [{"role": "user", "content": prompt}]

            # LLM配置
            gen_conf = {
                "temperature": 1,
                "top_p": 0.7,
                "max_tokens": 4096
            }

            # 调用LLM处理我们的提示词
            response = await asyncio.to_thread(
                llm_model_instance.chat,
                system="",
                history=history,
                gen_conf=gen_conf
            )

            # 提取和处理响应
            parsed_data, success = self._extract_json_from_response(response)
            return self._extract_rewritten_queries(parsed_data, response)

        except Exception as e:
            logger.error(f"Error in rewrite_query: {e}", exc_info=True)
            return [query_text]  # 发生错误时，返回原始查询


def get_nl2sql_service(db: Session = Depends(get_db), user=Depends(manager)) -> NL2SQLService:
    """通过依赖注入获取NL2SQLService实例"""
    return NL2SQLService(db, user)

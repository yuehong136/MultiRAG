import json
import logging
import os
import re
from typing import Any

from sqlalchemy.orm import Session

from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from api.db.services.llm_service import LLMBundle
from api.utils.prompt_template_util import PromptTemplateUtil
from common.constants import LLMType
from common.misc_utils import thread_pool_exec

logger = logging.getLogger(__name__)


class QueryRewriter:
    """负责将自然语言查询重写为多种变体的服务类"""

    # 编译正则表达式以提高性能
    JSON_PATTERN = re.compile(r'```json\s*([\s\S]*?)```')

    def __init__(self, db: Session, user_id: Any, prompt_dir: str = None):
        """
        初始化查询重写器

        参数:
            db: 数据库会话
            user_id: 用户ID
            prompt_dir: 提示词模板目录路径，如果为None则使用默认路径
        """
        self.db = db
        self.user_id = user_id

        # 如果未提供提示词目录，使用默认路径
        if prompt_dir is None:
            self.prompt_dir = os.path.join(os.path.dirname(__file__), "prompt")
        else:
            self.prompt_dir = prompt_dir

    def _extract_json_from_response(self, response: str) -> tuple[dict | None, bool]:
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

    def _extract_rewritten_queries(self, data: dict | None, response: str) -> list[str]:
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

    async def rewrite_query(self, query_text: str, llm_name: str) -> list[str]:
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
            model_config = get_model_config_by_type_and_name(self.db, self.user_id, LLMType.CHAT.value, llm_name)
            llm_model_instance = LLMBundle(self.db, self.user_id, model_config)

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
            response = await thread_pool_exec(
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

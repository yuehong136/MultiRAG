import json
import logging
import os
import re
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from api.db.services.llm_service import LLMBundle
from api.utils.prompt_template_util import PromptTemplateUtil
from common.constants import LLMType
from common.misc_utils import thread_pool_exec

logger = logging.getLogger(__name__)


class LLMSQLTemplating:
    """负责生成SQL模板的服务类"""

    # 编译正则表达式以提高性能
    JSON_PATTERN = re.compile(r"```json\s*([\s\S]*?)```")

    def __init__(self, db: Session, user_id: Any, prompt_dir: str = None):
        """
        初始化SQL模板生成器

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

    def _extract_json_from_response(self, response: str) -> tuple[dict[str, Any] | None, bool]:
        """
        从响应中提取JSON数据

        参数:
            response: LLM返回的原始响应文本

        返回:
            (extracted_json, success_flag): 提取的JSON数据和成功标志的元组
        """
        # 尝试从JSON代码块中提取
        json_match = self.JSON_PATTERN.search(response)

        if json_match:
            json_str = json_match.group(1).strip()
            try:
                parsed_data = json.loads(json_str)
                return parsed_data, True
            except json.JSONDecodeError:
                logger.warning("从代码块解析JSON失败")

        # 尝试从整个响应中解析JSON
        try:
            # 移除可能的markdown标记
            cleaned_response = re.sub(r"```(\w*\n|\n)", "", response)
            cleaned_response = re.sub(r"```$", "", cleaned_response)
            parsed_data = json.loads(cleaned_response.strip())
            return parsed_data, True
        except json.JSONDecodeError:
            logger.warning("从整个响应解析JSON失败")

        logger.warning("无法从响应中提取JSON数据")
        return None, False

    async def generate_sql_template(self, original_question: str, sql: str, semantic_layer: dict[str, Any], llm_name: str) -> dict[str, Any] | None:
        """
        生成SQL模板

        参数:
            query_text: 原始自然语言查询文本
            sql: 原始SQL查询
            semantic_layer: 语义层信息
            llm_name: 用于生成的LLM模型名称

        返回:
            JSON格式的SQL模板数据，如果生成失败则返回None
        """
        try:
            # 初始化LLM模型
            model_config = get_model_config_by_type_and_name(self.db, self.user_id, LLMType.CHAT.value, llm_name)
            llm_model_instance = LLMBundle(self.db, self.user_id, model_config)

            # 从文件加载提示词模板
            template_path = os.path.join(self.prompt_dir, "sql_templating_template.txt")
            prompt_template = PromptTemplateUtil.load_template_from_file(template_path)

            # 将semantic_layer转换为字符串形式
            semantic_layer_str = json.dumps(semantic_layer, ensure_ascii=False, indent=2)

            # 用参数填充模板
            prompt = PromptTemplateUtil.fill_template(prompt_template, {"original_question": original_question, "sql": sql, "semantic_layer": semantic_layer_str, "current_date": date.today()})

            # 创建包含我们提示词的对话历史
            history = [{"role": "user", "content": prompt}]

            # LLM配置
            gen_conf = {"temperature": 0.3, "top_p": 0.9, "max_tokens": 4096}

            # 调用LLM处理我们的提示词
            response = await thread_pool_exec(llm_model_instance.chat, system="", history=history, gen_conf=gen_conf)

            # 提取和处理响应
            extracted_data, success = self._extract_json_from_response(response)

            if success and extracted_data:
                return extracted_data
            else:
                logger.warning("无法从LLM响应中提取有效的SQL模板数据")
                return None

        except Exception as e:
            logger.error(f"生成SQL模板时出错: {e}", exc_info=True)
            return None

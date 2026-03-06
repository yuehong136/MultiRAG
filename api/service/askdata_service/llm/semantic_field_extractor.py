import json
import os
import re
import logging
from typing import Any

from sqlalchemy.orm import Session

from api.db.services.llm_service import LLMBundle
from api.db.db_models import db_connection
from api.utils.prompt_template_util import PromptTemplateUtil
from common.constants import LLMType
from common.misc_utils import thread_pool_exec

logger = logging.getLogger(__name__)


class SemanticFieldExtractor:
    """负责从语义层中提取用户查询所需的维度和指标的服务类"""

    # 编译正则表达式以提高性能
    JSON_PATTERN = re.compile(r'```json\s*([\s\S]*?)```')

    def __init__(self, db: Session, user_id: Any, prompt_dir: str = None):
        """
        初始化语义字段提取器

        参数:
            db: 数据库会话
            user_id: 用户ID
            prompt_dir: 提示词模板目录路径，如果为None则使用默认路径
        """
        self.db = db
        self.user_id = user_id

        # 如果未提供提示词目录，使用默认路径
        if prompt_dir is None:
            # 根据你提供的路径结构设置默认路径
            current_dir = os.path.dirname(__file__)
            # 向上一级目录找到prompt文件夹
            self.prompt_dir = os.path.join(os.path.dirname(current_dir), "prompt")
        else:
            self.prompt_dir = prompt_dir

    def _extract_json_from_response(self, response: str) -> tuple[list[dict] | None, bool]:
        """
        从响应中提取JSON数组数据。

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
                data = json.loads(json_str)
                # 验证返回的是数组
                if isinstance(data, list):
                    return data, True
                else:
                    logger.warning(f"Expected JSON array but got: {type(data)}")
                    return [], False
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSON from code block: {e}")

        # 如果没有找到代码块或解析失败，尝试解析整个响应
        try:
            data = json.loads(response)
            if isinstance(data, list):
                return data, True
            else:
                logger.warning(f"Expected JSON array but got: {type(data)}")
                return [], False
        except json.JSONDecodeError:
            logger.warning("Failed to parse response as JSON")
            return [], False

    def _validate_and_clean_fields(self, fields: list[dict]) -> list[dict]:
        """
        验证和清理提取的字段，确保格式正确

        参数:
            fields: 提取的字段列表

        返回:
            清理和验证后的字段列表
        """
        cleaned_fields = []

        for field in fields:
            if not isinstance(field, dict):
                logger.warning(f"Skipping non-dict field: {field}")
                continue

            # 检查是否为维度
            if "dimension_id" in field and "dimension_name" in field:
                cleaned_field = {
                    "dimension_id": field.get("dimension_id"),
                    "dimension_name": field.get("dimension_name")
                }
                cleaned_fields.append(cleaned_field)
            # 检查是否为指标
            elif "metric_id" in field and "metric_name" in field:
                cleaned_field = {
                    "metric_id": field.get("metric_id"),
                    "metric_name": field.get("metric_name")
                }
                cleaned_fields.append(cleaned_field)
            else:
                logger.warning(f"Field doesn't match expected structure: {field}")

        return cleaned_fields

    def _separate_dimensions_and_metrics(self, fields: list[dict]) -> dict[str, list[dict]]:
        """
        将字段分离为维度和指标

        参数:
            fields: 混合的字段列表

        返回:
            包含维度和指标分离列表的字典
        """
        dimensions = []
        metrics = []

        for field in fields:
            if "dimension_id" in field:
                dimensions.append(field)
            elif "metric_id" in field:
                metrics.append(field)

        return {
            "dimensions": dimensions,
            "metrics": metrics
        }

    async def extract_semantic_fields(
            self,
            user_query: str,
            dataset_info: list[dict],
            llm_name: str
    ) -> list[dict]:
        """
        从语义层中提取用户查询所需的维度和指标

        参数:
            user_query: 用户的自然语言查询
            dataset_info: 语义层信息（JSON格式）
            llm_name: 用于提取的LLM模型名称

        返回:
            包含识别出的维度和指标的列表
        """
        try:
            # 从文件加载提示词模板
            template_path = os.path.join(self.prompt_dir, "semantic_field_extract.txt")
            prompt_template = PromptTemplateUtil.load_template_from_file(template_path)

            # 准备模板参数
            template_values = {
                "user_query": user_query,
                "dataset_info": json.dumps(dataset_info, ensure_ascii=False, indent=2)
            }

            # 填充模板
            prompt = PromptTemplateUtil.fill_template(prompt_template, template_values)

            # 检查性能缓存
            from api.service.askdata_service.cache import perf_cache
            cached = perf_cache.get(prompt, namespace="field_extraction")
            if cached is not None:
                logger.info("PerfCache命中，跳过LLM调用 [field_extraction]")
                return cached

            # 创建包含我们提示词的对话历史
            history = [{"role": "user", "content": prompt}]

            # LLM配置 - 对于提取任务使用较低的temperature
            gen_conf = {
                "temperature": 0.0,  # 使用0以获得更确定性的结果
                "top_p": 0.9,
                "max_tokens": 4096  # 给予足够的token以处理大量字段
            }

            # 定义在独立线程中执行的函数，使用独立的数据库会话
            def _chat_in_thread():
                # 在线程中创建独立的数据库会话和LLM实例
                # 这样可以避免多线程共享数据库会话导致的事务状态冲突
                with db_connection() as thread_db:
                    thread_llm_instance = LLMBundle(thread_db, self.user_id, LLMType.CHAT, llm_name=llm_name)
                    return thread_llm_instance.chat(
                        system="",
                        history=history,
                        gen_conf=gen_conf
                    )

            # 调用LLM处理我们的提示词
            response = await thread_pool_exec(_chat_in_thread)

            # 提取和处理响应
            extracted_fields, success = self._extract_json_from_response(response)

            if success:
                # 验证和清理字段
                cleaned_fields = self._validate_and_clean_fields(extracted_fields)
                # 缓存成功的结果
                perf_cache.set(prompt, cleaned_fields, namespace="field_extraction")
                return cleaned_fields
            else:
                logger.error("Failed to extract valid JSON from LLM response")
                return []

        except Exception as e:
            logger.error(f"Error in extract_semantic_fields: {e}", exc_info=True)
            return []
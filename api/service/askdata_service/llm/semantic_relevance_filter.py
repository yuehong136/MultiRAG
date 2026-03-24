import json
import os
import re
import logging
from typing import Any

from sqlalchemy.orm import Session

from api.db.services.llm_service import LLMBundle
from api.db.db_models import db_connection
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from api.utils.prompt_template_util import PromptTemplateUtil
from common.misc_utils import thread_pool_exec
from common.constants import LLMType

logger = logging.getLogger(__name__)


class SemanticRelevanceFilter:
    """负责从语义层中过滤掉与用户查询明显不相关的维度和指标的服务类"""

    # 编译正则表达式以提高性能
    JSON_PATTERN = re.compile(r'```json\s*([\s\S]*?)```')

    def __init__(self, db: Session, user_id: Any, prompt_dir: str = None):
        """
        初始化语义相关性过滤器

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

    def _extract_json_from_response(self, response: str) -> dict[str, list[str]]:
        """
        从LLM响应中提取排除列表的JSON数据

        参数:
            response: LLM返回的原始响应文本

        返回:
            包含排除列表的字典
        """
        # 首先尝试从代码块中提取JSON
        json_match = self.JSON_PATTERN.search(response)

        if json_match:
            json_str = json_match.group(1).strip()
            try:
                data = json.loads(json_str)
                return data
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSON from code block: {e}")

        # 如果没有找到代码块或解析失败，尝试解析整个响应
        try:
            data = json.loads(response)
            return data
        except json.JSONDecodeError:
            logger.warning("Failed to parse response as JSON, returning empty exclude lists")
            return {"excludeDim": [], "excludeMetric": []}

    def _validate_exclude_format(self, exclude_data: dict) -> dict[str, list[str]]:
        """
        验证和清理排除列表的格式

        参数:
            exclude_data: 从LLM响应中提取的数据

        返回:
            验证后的排除列表字典
        """
        # 确保返回正确的格式
        validated = {
            "excludeDim": [],
            "excludeMetric": []
        }

        # 验证数据类型
        if not isinstance(exclude_data, dict):
            logger.warning(f"Expected dict but got: {type(exclude_data)}")
            return validated

        # 提取并验证excludeDim
        if "excludeDim" in exclude_data:
            dim_list = exclude_data.get("excludeDim", [])
            if isinstance(dim_list, list):
                # 确保列表中的所有元素都是字符串
                validated["excludeDim"] = [
                    str(dim_id) for dim_id in dim_list
                    if dim_id is not None
                ]
            else:
                logger.warning(f"excludeDim should be a list, got: {type(dim_list)}")

        # 提取并验证excludeMetric
        if "excludeMetric" in exclude_data:
            metric_list = exclude_data.get("excludeMetric", [])
            if isinstance(metric_list, list):
                # 确保列表中的所有元素都是字符串
                validated["excludeMetric"] = [
                    str(metric_id) for metric_id in metric_list
                    if metric_id is not None
                ]
            else:
                logger.warning(f"excludeMetric should be a list, got: {type(metric_list)}")

        # 记录过滤结果
        if validated["excludeDim"] or validated["excludeMetric"]:
            logger.info(f"Filtering out {len(validated['excludeDim'])} dimensions and "
                        f"{len(validated['excludeMetric'])} metrics")

        return validated

    def apply_exclusion_filter(
            self,
            dataset_info: list[dict],
            exclude_data: dict[str, list[str]]
    ) -> list[dict]:
        """
        应用排除列表，从数据集中移除不相关的字段

        参数:
            dataset_info: 原始语义层信息
            exclude_data: 包含排除列表的字典

        返回:
            过滤后的语义层信息
        """
        exclude_dim_ids = set(exclude_data.get("excludeDim", []))
        exclude_metric_ids = set(exclude_data.get("excludeMetric", []))

        filtered_data = []
        excluded_count = {"dimensions": 0, "metrics": 0}

        for field in dataset_info:
            should_include = True

            # 检查是否为维度
            if "dimension_id" in field or "dimensionId" in field:
                dim_id = field.get("dimension_id") or field.get("dimensionId")
                if dim_id in exclude_dim_ids:
                    should_include = False
                    excluded_count["dimensions"] += 1
                    logger.debug(
                        f"Excluding dimension: {field.get('dimension_name') or field.get('dimensionName', dim_id)}")

            # 检查是否为指标
            elif "metric_id" in field or "metricId" in field:
                metric_id = field.get("metric_id") or field.get("metricId")
                if metric_id in exclude_metric_ids:
                    should_include = False
                    excluded_count["metrics"] += 1
                    logger.debug(f"Excluding metric: {field.get('metric_name') or field.get('metricName', metric_id)}")

            if should_include:
                filtered_data.append(field)

        logger.info(f"Excluded {excluded_count['dimensions']} dimensions and {excluded_count['metrics']} metrics. "
                    f"Remaining fields: {len(filtered_data)}")

        return filtered_data

    async def filter_irrelevant_fields(
            self,
            user_query: str,
            dataset_info: list[dict],
            model_relations: list[dict],
            llm_name: str
    ) -> dict[str, list[str]]:
        """
        从语义层中过滤掉与用户查询明显不相关的维度和指标

        参数:
            user_query: 用户的自然语言查询
            dataset_info: 已初步筛选的语义层信息（JSON格式）
            llm_name: 用于过滤的LLM模型名称

        返回:
            包含需要排除的维度ID和指标ID的字典
            格式: {"excludeDim": [...], "excludeMetric": [...]}
        """
        try:
            # 从文件加载提示词模板
            template_path = os.path.join(self.prompt_dir, "semantic_layer_relevance_filter_prompt.txt")

            # 检查模板文件是否存在
            if not os.path.exists(template_path):
                logger.error(f"Prompt template not found at: {template_path}")
                return {"excludeDim": [], "excludeMetric": []}

            prompt_template = PromptTemplateUtil.load_template_from_file(template_path)

            # 准备模板参数
            template_values = {
                "user_query": user_query,
                "semantic_layer": json.dumps(dataset_info, ensure_ascii=False, indent=2),
                "model_relations": json.dumps(model_relations, ensure_ascii=False, indent=2)
            }

            # 填充模板
            prompt = PromptTemplateUtil.fill_template(prompt_template, template_values)

            # 检查性能缓存
            from api.service.askdata_service.cache import perf_cache
            cached = perf_cache.get(prompt, namespace="relevance_filter")
            if cached is not None:
                logger.info("PerfCache命中，跳过LLM调用 [relevance_filter]")
                return cached

            # 创建包含提示词的对话历史
            history = [{"role": "user", "content": prompt}]

            # LLM配置 - 对于精确筛选任务使用低temperature
            gen_conf = {
                "temperature": 0.0,  # 确保结果的确定性
                "top_p": 0.9,
                "max_tokens": 2048  # 输出较简单，不需要太多token
            }

            # 定义在独立线程中执行的函数，使用独立的数据库会话
            def _chat_in_thread():
                # 在线程中创建独立的数据库会话和LLM实例
                # 这样可以避免多线程共享数据库会话导致的事务状态冲突
                with db_connection() as thread_db:
                    model_config = get_model_config_by_type_and_name(thread_db, self.user_id, LLMType.CHAT.value, llm_name)
                    thread_llm_instance = LLMBundle(thread_db, self.user_id, model_config)
                    return thread_llm_instance.chat(
                        system="",
                        history=history,
                        gen_conf=gen_conf
                    )

            # 调用LLM处理提示词
            response = await thread_pool_exec(_chat_in_thread)

            # 提取和处理响应
            exclude_data = self._extract_json_from_response(response)

            # 验证和清理返回的数据
            validated_data = self._validate_exclude_format(exclude_data)

            # 缓存验证后的结果
            perf_cache.set(prompt, validated_data, namespace="relevance_filter")

            return validated_data

        except Exception as e:
            logger.error(f"Error in filter_irrelevant_fields: {e}", exc_info=True)
            # 出错时返回空的排除列表，保守处理
            return {"excludeDim": [], "excludeMetric": []}

    async def filter_and_apply(
            self,
            user_query: str,
            dataset_info: list[dict],
            llm_name: str
    ) -> list[dict]:
        """
        一步完成过滤和应用，返回过滤后的语义层信息

        参数:
            user_query: 用户的自然语言查询
            dataset_info: 已初步筛选的语义层信息（JSON格式）
            llm_name: 用于过滤的LLM模型名称

        返回:
            过滤后的语义层信息列表
        """
        # 获取排除列表
        exclude_data = await self.filter_irrelevant_fields(user_query, dataset_info, llm_name)

        # 应用过滤
        filtered_data = self.apply_exclusion_filter(dataset_info, exclude_data)

        return filtered_data

    def get_filter_statistics(
            self,
            original_data: list[dict],
            filtered_data: list[dict]
    ) -> dict[str, Any]:
        """
        获取过滤统计信息

        参数:
            original_data: 原始数据
            filtered_data: 过滤后的数据

        返回:
            包含统计信息的字典
        """
        original_dims = sum(1 for field in original_data
                            if "dimension_id" in field or "dimensionId" in field)
        original_metrics = sum(1 for field in original_data
                               if "metric_id" in field or "metricId" in field)

        filtered_dims = sum(1 for field in filtered_data
                            if "dimension_id" in field or "dimensionId" in field)
        filtered_metrics = sum(1 for field in filtered_data
                               if "metric_id" in field or "metricId" in field)

        return {
            "original": {
                "total": len(original_data),
                "dimensions": original_dims,
                "metrics": original_metrics
            },
            "filtered": {
                "total": len(filtered_data),
                "dimensions": filtered_dims,
                "metrics": filtered_metrics
            },
            "excluded": {
                "total": len(original_data) - len(filtered_data),
                "dimensions": original_dims - filtered_dims,
                "metrics": original_metrics - filtered_metrics
            },
            "exclusion_rate": {
                "total": (len(original_data) - len(filtered_data)) / len(original_data) * 100 if original_data else 0,
                "dimensions": (original_dims - filtered_dims) / original_dims * 100 if original_dims else 0,
                "metrics": (original_metrics - filtered_metrics) / original_metrics * 100 if original_metrics else 0
            }
        }

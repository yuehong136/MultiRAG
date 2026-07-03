import json
import os
import re
from typing import Any

from sqlalchemy.orm import Session

from api.db.db_models import db_connection
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from api.db.services.llm_service import LLMBundle
from api.service.askdata_service.util.askdata_logger import get_askdata_logger
from api.utils.prompt_template_util import PromptTemplateUtil
from common.constants import LLMType
from common.misc_utils import thread_pool_exec

logger = get_askdata_logger()


class QueryIntentAnalyzer:
    """负责分析用户查询意图并推荐合适图表类型的服务类"""

    # 编译正则表达式以提高性能
    JSON_PATTERN = re.compile(r"```json\s*([\s\S]*?)```")

    def __init__(self, db: Session, user_id: Any, prompt_dir: str = None):
        """
        初始化查询意图分析器

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

    def _extract_chart_recommendation(self, data: dict | None, response: str, supported_charts: list[str]) -> tuple[str, str | None]:
        """
        从解析后的数据中提取推荐的图表类型和推荐理由。

        参数:
            data: 解析后的JSON数据
            response: 原始响应（用于回退）
            supported_charts: 支持的图表类型列表

        返回:
            (推荐的图表类型, 推荐理由): 图表类型和理由的元组，如果解析失败则返回默认值
        """
        if data and isinstance(data, dict):
            recommended_chart = data.get("recommended_chart")
            recommendation_reason = data.get("recommendation_reason")

            # 验证推荐的图表是否在支持列表中
            if recommended_chart and recommended_chart in supported_charts:
                return recommended_chart, recommendation_reason
            else:
                if recommended_chart:
                    logger.warning(f"Recommended chart '{recommended_chart}' not in supported charts list")

        # 回退方案：返回第一个支持的图表类型作为默认值
        if supported_charts:
            logger.info("Using fallback chart type")
            return supported_charts[0], "使用默认图表类型"
        else:
            logger.error("No supported charts available")
            return "指标卡", "没有可用的图表类型，使用基础指标卡"

    def _extract_recommended_chart(self, data: dict | None, response: str, supported_charts: list[str]) -> str:
        """
        从解析后的数据中提取推荐的图表类型（保持向后兼容性）。

        参数:
            data: 解析后的JSON数据
            response: 原始响应（用于回退）
            supported_charts: 支持的图表类型列表

        返回:
            推荐的图表类型，如果解析失败则返回默认值
        """
        chart_type, _ = self._extract_chart_recommendation(data, response, supported_charts)
        return chart_type

    async def recommend_chart_with_reason(self, user_question: str, supported_charts_list: list[str], semantic_layer_info: dict, llm_name: str) -> tuple[str, str | None]:
        """
        分析用户查询意图并推荐合适的图表类型，同时返回推荐理由。

        参数:
            user_question: 用户的自然语言问题
            supported_charts_list: 系统支持的图表类型列表
            semantic_layer_info: 语义层信息字典
            llm_name: 用于分析的LLM模型名称

        返回:
            (推荐的图表类型, 推荐理由): 图表类型和理由的元组
        """
        try:
            # 从文件加载提示词模板
            template_path = os.path.join(self.prompt_dir, "query_intent.txt")
            prompt_template = PromptTemplateUtil.load_template_from_file(template_path)

            # 准备模板参数
            template_values = {
                "USER_QUESTION": user_question,
                "SUPPORTED_CHARTS_LIST": json.dumps(supported_charts_list, ensure_ascii=False),
                "SEMANTIC_LAYER_INFO": json.dumps(semantic_layer_info, ensure_ascii=False, indent=2),
            }

            # 填充模板
            prompt = PromptTemplateUtil.fill_template(prompt_template, template_values)

            # 创建包含我们提示词的对话历史
            history = [{"role": "user", "content": prompt}]

            # LLM配置
            gen_conf = {"temperature": 0.1, "top_p": 0.8, "max_tokens": 1024}

            # 定义在独立线程中执行的函数，使用独立的数据库会话
            def _chat_in_thread():
                # 在线程中创建独立的数据库会话和LLM实例
                # 这样可以避免多线程共享数据库会话导致的事务状态冲突
                with db_connection() as thread_db:
                    model_config = get_model_config_by_type_and_name(thread_db, self.user_id, LLMType.CHAT.value, llm_name)
                    thread_llm_instance = LLMBundle(thread_db, self.user_id, model_config)
                    return thread_llm_instance.chat(system="", history=history, gen_conf=gen_conf)

            # 调用LLM处理我们的提示词
            response = await thread_pool_exec(_chat_in_thread)

            # 提取和处理响应
            parsed_data, success = self._extract_json_from_response(response)
            chart, reason = self._extract_chart_recommendation(parsed_data, response, supported_charts_list)
            return chart, reason

        except Exception as e:
            logger.error(f"Error in recommend_chart_with_reason: {e}", exc_info=True)
            # 发生错误时，返回默认图表类型和错误信息
            default_chart = supported_charts_list[0] if supported_charts_list else "指标卡"
            return default_chart, f"分析过程中发生错误: {e!s}"

    async def recommend_chart_without_semantic(self, user_question: str, supported_charts_list: list[str], llm_name: str) -> tuple[str, str | None]:
        """
        不依赖语义层信息，仅基于用户问题推荐合适的图表类型。

        参数:
            user_question: 用户的自然语言问题
            supported_charts_list: 系统支持的图表类型列表
            llm_name: 用于分析的LLM模型名称

        返回:
            (推荐的图表类型, 推荐理由): 图表类型和理由的元组
        """
        try:
            # 从文件加载提示词模板（使用新的不依赖语义层的模板）
            template_path = os.path.join(self.prompt_dir, "query_intent_without_semantic.txt")
            prompt_template = PromptTemplateUtil.load_template_from_file(template_path)

            # 准备模板参数
            template_values = {"USER_QUESTION": user_question, "SUPPORTED_CHARTS_LIST": json.dumps(supported_charts_list, ensure_ascii=False)}

            # 填充模板
            prompt = PromptTemplateUtil.fill_template(prompt_template, template_values)

            # 检查性能缓存
            from api.service.askdata_service.cache import perf_cache

            cached = perf_cache.get(prompt, namespace="chart_recommendation")
            if cached is not None:
                logger.info("PerfCache命中，跳过LLM调用 [chart_recommendation]")
                return cached

            # 创建包含我们提示词的对话历史
            history = [{"role": "user", "content": prompt}]

            # LLM配置
            gen_conf = {"temperature": 0.1, "top_p": 0.8, "max_tokens": 1024}

            # 定义在独立线程中执行的函数，使用独立的数据库会话
            def _chat_in_thread():
                # 在线程中创建独立的数据库会话和LLM实例
                # 这样可以避免多线程共享数据库会话导致的事务状态冲突
                with db_connection() as thread_db:
                    model_config = get_model_config_by_type_and_name(thread_db, self.user_id, LLMType.CHAT.value, llm_name)
                    thread_llm_instance = LLMBundle(thread_db, self.user_id, model_config)
                    return thread_llm_instance.chat(system="", history=history, gen_conf=gen_conf)

            # 调用LLM处理我们的提示词
            response = await thread_pool_exec(_chat_in_thread)

            # 提取和处理响应
            parsed_data, success = self._extract_json_from_response(response)
            chart, reason = self._extract_chart_recommendation(parsed_data, response, supported_charts_list)

            # 缓存成功的结果
            perf_cache.set(prompt, (chart, reason), namespace="chart_recommendation")

            return chart, reason

        except Exception as e:
            logger.error(f"Error in recommend_chart_without_semantic: {e}", exc_info=True)
            # 发生错误时，返回默认图表类型和错误信息
            default_chart = supported_charts_list[0] if supported_charts_list else "指标卡"
            return default_chart, f"分析过程中发生错误: {e!s}"

    async def analyze_query_intent_with_details(self, user_question: str, supported_charts_list: list[str], semantic_layer_info: dict, llm_name: str) -> dict[str, Any]:
        """
        分析用户查询意图并返回详细结果，包括推荐的图表类型、推荐理由和原始响应。

        参数:
            user_question: 用户的自然语言问题
            supported_charts_list: 系统支持的图表类型列表
            semantic_layer_info: 语义层信息字典
            llm_name: 用于分析的LLM模型名称

        返回:
            包含推荐图表类型、推荐理由、原始响应和解析状态的字典
        """
        try:
            # 从文件加载提示词模板
            template_path = os.path.join(self.prompt_dir, "query_intent.txt")
            prompt_template = PromptTemplateUtil.load_template_from_file(template_path)

            # 准备模板参数
            template_values = {
                "USER_QUESTION": user_question,
                "SUPPORTED_CHARTS_LIST": json.dumps(supported_charts_list, ensure_ascii=False),
                "SEMANTIC_LAYER_INFO": json.dumps(semantic_layer_info, ensure_ascii=False, indent=2),
            }

            # 填充模板
            prompt = PromptTemplateUtil.fill_template(prompt_template, template_values)

            # 创建包含我们提示词的对话历史
            history = [{"role": "user", "content": prompt}]

            # LLM配置
            gen_conf = {"temperature": 0.1, "top_p": 0.8, "max_tokens": 1024}

            # 定义在独立线程中执行的函数，使用独立的数据库会话
            def _chat_in_thread():
                # 在线程中创建独立的数据库会话和LLM实例
                # 这样可以避免多线程共享数据库会话导致的事务状态冲突
                with db_connection() as thread_db:
                    model_config = get_model_config_by_type_and_name(thread_db, self.user_id, LLMType.CHAT.value, llm_name)
                    thread_llm_instance = LLMBundle(thread_db, self.user_id, model_config)
                    return thread_llm_instance.chat(system="", history=history, gen_conf=gen_conf)

            # 调用LLM处理我们的提示词
            response = await thread_pool_exec(_chat_in_thread)

            # 提取和处理响应
            parsed_data, success = self._extract_json_from_response(response)
            recommended_chart, recommendation_reason = self._extract_chart_recommendation(parsed_data, response, supported_charts_list)

            return {
                "recommended_chart": recommended_chart,
                "recommendation_reason": recommendation_reason,
                "raw_response": response,
                "parsed_data": parsed_data,
                "parse_success": success,
                "prompt_used": prompt,
            }

        except Exception as e:
            logger.error(f"Error in analyze_query_intent_with_details: {e}", exc_info=True)
            # 发生错误时，返回默认结果
            default_chart = supported_charts_list[0] if supported_charts_list else "指标卡"
            return {"recommended_chart": default_chart, "recommendation_reason": f"分析过程中发生错误: {e!s}", "raw_response": "", "parsed_data": None, "parse_success": False, "error": str(e)}

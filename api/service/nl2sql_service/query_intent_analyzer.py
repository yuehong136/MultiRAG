import asyncio
import json
import os
import re
import logging
from typing import Any, List, Optional, Dict, Tuple, Set
from enum import Enum

from sqlalchemy.orm import Session

from api.db import LLMType
from api.db.services.llm_service import LLMBundle
from api.utils.prompt_template_util import PromptTemplateUtil

logger = logging.getLogger(__name__)


class QueryIntentType(Enum):
    """查询意图类型枚举"""
    AGGREGATION = "Aggregation Query"  # 获取汇总统计信息 (如: 多少, 总数, 平均, 统计)
    LIST_DETAIL = "List/Detail Query"  # 检索具体记录或字段 (如: 查询, 列出, 哪些, 详情)
    RANKING_ORDERING = "Ranking/Ordering Query"  # 获取排序后数据或排名 (如: 最..., 前N, 按...排序)
    EXISTENCE_BOOLEAN = "Existence/Boolean Query"  # 确认是否存在 (如: 有没有, 是否存在)
    COMPARISON = "Comparison Query"  # 对比不同组或条件的数据 (如: 比较, 比...多/少)
    TEMPORAL_TREND = "Temporal Trend/Periodic Query"  # 分析数据随时间变化 (如: 趋势, 按月/年统计)
    METADATA_DEFINITION = "Metadata/Definition Query"  # 询问表结构、字段含义等 (如: 有哪些字段, ...是什么意思)
    AMBIGUOUS = "Ambiguous Query"  # 问题表达不清或信息不足

    @classmethod
    def from_string(cls, intent_str: str) -> Optional['QueryIntentType']:
        """
        从字符串转换为枚举值，处理可能的格式差异

        参数:
            intent_str: 意图字符串

        返回:
            匹配的枚举值，如果没有匹配则返回None
        """
        # 标准化字符串（移除多余空格、引号等）
        normalized = intent_str.strip().strip('"\'').strip()

        # 尝试直接匹配枚举值
        for intent_type in cls:
            if normalized == intent_type.value:
                return intent_type

        # 处理部分匹配情况
        lowercase_normalized = normalized.lower()
        for intent_type in cls:
            if intent_type.value.lower() in lowercase_normalized:
                return intent_type

        return None


class QueryIntentAnalyzer:
    """负责分析和识别用户自然语言查询意图的服务类"""

    # 编译正则表达式以提高性能
    JSON_PATTERN = re.compile(r'```json\s*([\s\S]*?)```')
    LIST_PATTERN = re.compile(r'\[(.*?)]')  # 方括号在字符类外不需要转义
    QUOTED_ITEM_PATTERN = re.compile(r'"([^"]*)"')

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

    def _extract_data_from_response(self, response: str) -> Tuple[Optional[List[str]], bool]:
        """
        从响应中提取意图列表数据

        参数:
            response: LLM返回的原始响应文本

        返回:
            (extracted_intents, success_flag): 提取的意图列表和成功标志的元组
        """
        # 尝试从JSON代码块中提取
        json_match = self.JSON_PATTERN.search(response)

        if json_match:
            json_str = json_match.group(1).strip()
            try:
                parsed_data = json.loads(json_str)
                if isinstance(parsed_data, list):
                    return parsed_data, True
                elif isinstance(parsed_data, dict) and ("intents" in parsed_data or "query_intents" in parsed_data):
                    return parsed_data.get("intents") or parsed_data.get("query_intents"), True
                return None, False
            except json.JSONDecodeError:
                logger.warning("从代码块解析JSON失败")

        # 尝试从列表格式中提取，如 ["List/Detail Query", "Temporal Trend/Periodic Query"]
        list_match = self.LIST_PATTERN.search(response)
        if list_match:
            items_str = list_match.group(1)
            # 提取引号中的项目
            items = self.QUOTED_ITEM_PATTERN.findall(items_str)
            if items:
                return [item.strip() for item in items], True

            # 如果没有引号，则按逗号分隔
            items = [item.strip() for item in items_str.split(',') if item.strip()]
            if items:
                return items, True

        # 尝试从逗号分隔的字符串中提取，如 "List/Detail Query", "Temporal Trend/Periodic Query"
        if "," in response:
            items = []
            for item in response.split(','):
                # 提取引号中的内容或直接使用清理后的文本
                quoted_items = self.QUOTED_ITEM_PATTERN.findall(item)
                if quoted_items:
                    items.extend([qi.strip() for qi in quoted_items])
                else:
                    cleaned_item = item.strip().strip('"\'')
                    if cleaned_item:
                        items.append(cleaned_item)

            if items:
                return items, True

        # 如果上述方法都失败了，尝试按行分割
        lines = [line.strip() for line in response.split('\n')]
        cleaned_lines = []
        for line in lines:
            if line and not line.startswith('```') and not line.endswith('```'):
                # 移除可能的列表标记、引号等
                cleaned_line = re.sub(r'^[*\-\d.\s]+', '', line).strip()
                cleaned_line = cleaned_line.strip('"\'').strip()
                if cleaned_line:
                    cleaned_lines.append(cleaned_line)

        if cleaned_lines:
            return cleaned_lines, True

        logger.warning("无法从响应中提取意图列表")
        return None, False

    def _extract_intents(self, data: Optional[List[str]], response: str) -> List[QueryIntentType]:
        """
        从解析后的数据中提取查询意图列表

        参数:
            data: 从响应中提取的意图字符串列表
            response: 原始响应（用于回退）

        返回:
            查询意图类型列表
        """
        intent_strings = []

        # 使用提取的数据
        if data and isinstance(data, list):
            intent_strings = data

        # 如果从数据中提取失败，尝试从文本中直接提取
        if not intent_strings:
            # 使用已有的提取功能
            extracted_data, _ = self._extract_data_from_response(response)
            if extracted_data:
                intent_strings = extracted_data

        # 如果仍然没有提取到意图，记录警告并返回默认值
        if not intent_strings:
            logger.warning("无法从响应中提取意图，返回默认值'Ambiguous Query'")
            return [QueryIntentType.AMBIGUOUS]

        # 将字符串转换为枚举类型
        intents = []
        for intent_str in intent_strings:
            intent_type = QueryIntentType.from_string(intent_str)
            if intent_type:
                intents.append(intent_type)

        # 如果没有匹配任何有效意图，返回默认值
        if not intents:
            logger.warning(f"未识别到有效意图类型，原始意图字符串: {intent_strings}")
            return [QueryIntentType.AMBIGUOUS]

        return intents

    async def analyze_query_intent(self, query_text: str, llm_name: str) -> List[QueryIntentType]:
        """
        使用LLM分析自然语言查询意图

        参数:
            query_text: 原始自然语言查询文本
            llm_name: 用于分析的LLM模型名称

        返回:
            查询意图类型列表
        """
        try:
            # 初始化LLM模型
            llm_model_instance = LLMBundle(self.db, self.user_id, LLMType.CHAT, llm_name=llm_name)

            # 从文件加载提示词模板
            template_path = os.path.join(self.prompt_dir, "user_query_intent_template.txt")
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
                "temperature": 0.2,  # 使用较低的温度以确保更一致的输出
                "top_p": 0.9,
                "max_tokens": 1024
            }

            # 调用LLM处理我们的提示词
            response = await asyncio.to_thread(
                llm_model_instance.chat,
                system="",
                history=history,
                gen_conf=gen_conf
            )

            # 提取和处理响应
            extracted_data, success = self._extract_data_from_response(response)
            return self._extract_intents(extracted_data, response)

        except Exception as e:
            logger.error(f"分析查询意图时出错: {e}", exc_info=True)
            return [QueryIntentType.AMBIGUOUS]  # 发生错误时，返回模糊查询类型

    async def get_query_intents(self, query_text: str, llm_name: str) -> Set[str]:
        """
        获取查询的意图标签集合（便捷方法）

        参数:
            query_text: 原始自然语言查询文本
            llm_name: 用于分析的LLM模型名称

        返回:
            意图类型值的集合
        """
        intents = await self.analyze_query_intent(query_text, llm_name)
        return {intent.value for intent in intents}

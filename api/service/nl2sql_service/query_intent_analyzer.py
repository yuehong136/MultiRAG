import json
import logging
import os
import re
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from api.db.services.llm_service import LLMBundle
from api.utils.prompt_template_util import PromptTemplateUtil
from common.constants import LLMType
from common.misc_utils import thread_pool_exec

logger = logging.getLogger(__name__)


class QueryIntentType(Enum):
    """查询意图类型枚举（细化版）"""
    # 详情查询系列
    SPECIFIC_ENTITY = "Specific Entity/Record Lookup"  # 查找特定实体/记录的详细信息
    CONDITIONAL_LIST = "Conditional List Retrieval"  # 获取满足特定条件的一组记录列表
    SIMPLE_LIST = "Simple List Retrieval"  # 获取某个表的所有或部分字段的列表

    # 统计/聚合查询系列
    COUNTING = "Counting"  # 计算满足条件的记录数量
    SUMMATION = "Summation"  # 计算某个数值列的总和
    AVERAGE = "Average"  # 计算某个数值列的平均值
    MIN_MAX = "Min/Max"  # 查找最大值或最小值
    GROUPED_AGGREGATION = "Grouped Aggregation"  # 按维度分组后进行聚合计算

    # 比较查询系列
    DIRECT_COMPARISON = "Direct Comparison"  # 比较两个或多个实体/组的某个指标
    THRESHOLD_COMPARISON = "Threshold/Average Comparison"  # 与阈值或平均值比较

    # 排序/排名查询系列
    TOP_BOTTOM_N = "Top N/Bottom N"  # 找出排在前面或后面的N条记录
    SIMPLE_ORDERING = "Simple Ordering"  # 按特定字段排序

    # 趋势/时间序列分析
    TIME_SERIES = "Time Series Analysis"  # 分析某个指标随时间的变化情况

    # 存在性/布尔查询
    EXISTENCE = "Existence/Boolean Query"  # 确认某个条件是否满足

    # 关联/复杂查询
    RELATIONAL = "Relational/Complex Query"  # 涉及多表关联或复杂逻辑

    # 元数据查询
    METADATA = "Metadata/Definition Query"  # 询问表结构、字段含义等

    # 模糊查询（默认回退类型）
    AMBIGUOUS = "Ambiguous Query"  # 问题表达不清或信息不足

    @classmethod
    def get_chinese_description(cls, intent_type: 'QueryIntentType') -> str:
        """
        获取查询意图类型的中文描述

        参数:
            intent_type: 查询意图类型枚举值

        返回:
            查询意图类型的中文描述
        """
        descriptions = {
            # 详情查询系列
            cls.SPECIFIC_ENTITY: "特定实体查询 - 查找特定记录的详细信息，如'张三的订单'、'产品A001的详情'",
            cls.CONDITIONAL_LIST: "条件列表查询 - 获取满足特定条件的记录列表，如'所有价格高于100元的商品'",
            cls.SIMPLE_LIST: "简单列表查询 - 获取数据表的记录列表，如'显示所有员工名单'",

            # 统计/聚合查询系列
            cls.COUNTING: "计数查询 - 计算满足条件的记录数量，如'有多少个活跃用户'",
            cls.SUMMATION: "求和查询 - 计算某个数值列的总和，如'本月总销售额是多少'",
            cls.AVERAGE: "平均值查询 - 计算某个数值列的平均值，如'平均订单价格是多少'",
            cls.MIN_MAX: "最值查询 - 查找最大值或最小值，如'哪个产品价格最高'",
            cls.GROUPED_AGGREGATION: "分组统计查询 - 按维度分组后进行聚合计算，如'每个部门的员工人数'",

            # 比较查询系列
            cls.DIRECT_COMPARISON: "直接比较查询 - 比较两个或多个实体/组的指标，如'A产品和B产品的销量哪个高'",
            cls.THRESHOLD_COMPARISON: "阈值比较查询 - 与阈值或平均值比较，如'哪些产品的利润率高于20%'",

            # 排序/排名查询系列
            cls.TOP_BOTTOM_N: "TopN/BottomN查询 - 找出排在前面或后面的N条记录，如'销量最高的10个产品'",
            cls.SIMPLE_ORDERING: "简单排序查询 - 按特定字段排序，如'按价格从低到高显示所有商品'",

            # 趋势/时间序列分析
            cls.TIME_SERIES: "时间序列分析 - 分析指标随时间的变化情况，如'过去一年月度销售额变化趋势'",

            # 存在性/布尔查询
            cls.EXISTENCE: "存在性查询 - 确认条件是否满足，如'张三有没有下过单'",

            # 关联/复杂查询
            cls.RELATIONAL: "关联复杂查询 - 涉及多表关联或复杂逻辑，如'购买了A产品也购买了B产品的客户'",

            # 元数据查询
            cls.METADATA: "元数据查询 - 询问表结构、字段含义等，如'用户表有哪些字段'",

            # 模糊查询
            cls.AMBIGUOUS: "模糊查询 - 问题表达不清或信息不足，如'那个数据怎么样了'"
        }

        return descriptions.get(intent_type, "未知查询类型")

    @classmethod
    def from_string(cls, intent_str: str) -> "QueryIntentType | None":
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

        # 处理一些特殊的映射关系（兼容旧类型或缩写）
        mapping = {
            "aggregation query": cls.GROUPED_AGGREGATION,
            "list/detail query": cls.SIMPLE_LIST,
            "ranking/ordering query": cls.SIMPLE_ORDERING,
            "top n": cls.TOP_BOTTOM_N,
            "temporal trend": cls.TIME_SERIES,
            "trend": cls.TIME_SERIES,
            "comparison query": cls.DIRECT_COMPARISON,
        }

        for key, value in mapping.items():
            if key in lowercase_normalized:
                return value

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

    def _extract_data_from_response(self, response: str) -> tuple[list[str] | None, bool]:
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

    def _extract_intents(self, data: list[str] | None, response: str) -> list[QueryIntentType]:
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

    async def analyze_query_intent(self, query_text: str, llm_name: str) -> list[QueryIntentType]:
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
            model_config = get_model_config_by_type_and_name(self.db, self.user_id, LLMType.CHAT.value, llm_name)
            llm_model_instance = LLMBundle(self.db, self.user_id, model_config)

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
                "temperature": 0.4,
                "top_p": 0.9,
                "max_tokens": 1024
            }

            # 调用LLM处理我们的提示词
            response = await thread_pool_exec(
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

    async def get_query_intents(self, query_text: str, llm_name: str) -> set[str]:
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

    async def get_query_intents_with_descriptions(self, query_text: str, llm_name: str) -> list[dict[str, str]]:
        """
        获取查询的意图标签及其中文描述（便捷方法）

        参数:
            query_text: 原始自然语言查询文本
            llm_name: 用于分析的LLM模型名称

        返回:
            包含意图类型、英文标签和中文描述的字典列表
        """
        intents = await self.analyze_query_intent(query_text, llm_name)
        result = []

        for intent in intents:
            result.append({
                "intent_type": intent.name,  # 枚举名称，如 SPECIFIC_ENTITY
                "intent_label": intent.value,  # 英文标签，如 "Specific Entity/Record Lookup"
                "description": QueryIntentType.get_chinese_description(intent)  # 中文描述
            })

        return result

    def get_all_intent_types_with_descriptions(self) -> list[dict[str, str]]:
        """
        获取所有可能的查询意图类型及其中文描述

        返回:
            包含所有查询意图类型、英文标签和中文描述的字典列表
        """
        result = []

        for intent in QueryIntentType:
            result.append({
                "intent_type": intent.name,  # 枚举名称
                "intent_label": intent.value,  # 英文标签
                "description": QueryIntentType.get_chinese_description(intent)  # 中文描述
            })

        return result

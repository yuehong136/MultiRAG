import asyncio
import json
import os
import re
import logging
from typing import Any, Optional, Dict, Tuple

from sqlalchemy.orm import Session

from api.db import LLMType
from api.db.services.llm_service import LLMBundle
from api.utils.prompt_template_util import PromptTemplateUtil

logger = logging.getLogger(__name__)


class SQLComponentsExtractor:
    """负责解析SQL语句并提取各个组件部分的服务类"""

    # 编译正则表达式以提高性能
    JSON_PATTERN = re.compile(r'```json\s*([\s\S]*?)```')

    def __init__(self, db: Session, user_id: Any, prompt_dir: str = None):
        """
        初始化SQL组件提取器

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

    def _extract_json_from_response(self, response: str) -> Tuple[Optional[Dict], bool]:
        """
        从响应中提取JSON对象数据。

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
                # 验证返回的是字典对象
                if isinstance(data, dict):
                    return data, True
                else:
                    logger.warning(f"Expected JSON object but got: {type(data)}")
                    return {}, False
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSON from code block: {e}")

        # 如果没有找到代码块或解析失败，尝试解析整个响应
        try:
            data = json.loads(response)
            if isinstance(data, dict):
                return data, True
            else:
                logger.warning(f"Expected JSON object but got: {type(data)}")
                return {}, False
        except json.JSONDecodeError:
            logger.warning("Failed to parse response as JSON")
            return {}, False

    def _validate_sql_components(self, components: Dict) -> Dict:
        """
        验证和清理提取的SQL组件，确保格式正确

        参数:
            components: 提取的SQL组件字典

        返回:
            验证和清理后的SQL组件字典
        """
        # 定义必需的SQL组件键
        required_keys = ["select", "from", "where", "groupBy", "orderBy", "having"]

        # 确保所有必需的键都存在，如果不存在则设置为空字符串
        validated_components = {}
        for key in required_keys:
            if key in components:
                # 确保值是字符串类型
                value = components[key]
                if value is None:
                    validated_components[key] = ""
                elif isinstance(value, str):
                    validated_components[key] = value.strip()
                else:
                    validated_components[key] = str(value).strip()
            else:
                validated_components[key] = ""

        # 处理分页信息
        pagination = components.get("pagination", {})
        if isinstance(pagination, dict):
            validated_pagination = {
                "limit": str(pagination.get("limit", "")).strip(),
                "offset": str(pagination.get("offset", "")).strip()
            }
        else:
            # 如果没有pagination或格式不对，创建默认的空分页对象
            validated_pagination = {
                "limit": "",
                "offset": ""
            }

        validated_components["pagination"] = validated_pagination

        # 验证至少有SELECT和FROM子句
        if not validated_components.get("select") or not validated_components.get("from"):
            logger.warning("SQL must have at least SELECT and FROM clauses")

        return validated_components

    def _post_process_components(self, components: Dict) -> Dict:
        """
        对提取的SQL组件进行后处理，确保格式一致性

        参数:
            components: SQL组件字典

        返回:
            后处理的SQL组件字典
        """
        # 对每个组件进行格式化处理（不包括pagination）
        for key, value in components.items():
            if key == "pagination":
                # 分页信息不需要添加关键字前缀
                continue

            if value and isinstance(value, str):
                # 移除多余的空白字符
                value = " ".join(value.split())

                # 确保关键字大写（可选，根据需求调整）
                if key == "select" and not value.upper().startswith("SELECT"):
                    value = "SELECT " + value
                elif key == "from" and not value.upper().startswith("FROM"):
                    value = "FROM " + value
                elif key == "where" and not value.upper().startswith("WHERE"):
                    value = "WHERE " + value
                elif key == "groupBy" and not value.upper().startswith("GROUP BY"):
                    value = "GROUP BY " + value
                elif key == "orderBy" and not value.upper().startswith("ORDER BY"):
                    value = "ORDER BY " + value
                elif key == "having" and not value.upper().startswith("HAVING"):
                    value = "HAVING " + value

                components[key] = value

        return components

    async def extract_sql_components(
            self,
            sql_query: str,
            llm_name: str
    ) -> Dict:
        """
        解析SQL语句并提取各个组件部分

        参数:
            sql_query: 待解析的SQL查询语句
            llm_name: 用于提取的LLM模型名称

        返回:
            包含SQL各个组件的字典，格式如下：
            {
                "select": "SELECT ...",
                "from": "FROM ...",
                "where": "WHERE ...",
                "groupBy": "GROUP BY ...",
                "orderBy": "ORDER BY ...",
                "having": "HAVING ...",
                "pagination": {
                    "limit": "10",
                    "offset": "20"
                }
            }
        """
        try:
            # 初始化LLM模型
            llm_model_instance = LLMBundle(self.db, self.user_id, LLMType.CHAT, llm_name=llm_name)

            # 从文件加载提示词模板
            template_path = os.path.join(self.prompt_dir, "extract_sql_components.txt")
            prompt_template = PromptTemplateUtil.load_template_from_file(template_path)

            # 准备模板参数
            template_values = {
                "sql": sql_query
            }

            # 填充模板
            prompt = PromptTemplateUtil.fill_template(prompt_template, template_values)

            # 创建包含我们提示词的对话历史
            history = [{"role": "user", "content": prompt}]

            # LLM配置 - 对于解析任务使用较低的temperature
            gen_conf = {
                "temperature": 0.0,  # 使用0以获得更确定性的结果
                "top_p": 0.9,
                "max_tokens": 2048  # SQL组件通常不会太长
            }

            # 调用LLM处理我们的提示词
            response = await asyncio.to_thread(
                llm_model_instance.chat,
                system="",
                history=history,
                gen_conf=gen_conf
            )

            # 提取和处理响应
            extracted_components, success = self._extract_json_from_response(response)

            if success:
                # 验证和清理SQL组件
                validated_components = self._validate_sql_components(extracted_components)
                # 后处理组件
                final_components = self._post_process_components(validated_components)
                return final_components
            else:
                logger.error("Failed to extract valid JSON from LLM response")
                # 返回默认的空组件结构
                return self._get_default_components()

        except Exception as e:
            logger.error(f"Error in extract_sql_components: {e}", exc_info=True)
            # 返回默认的空组件结构
            return self._get_default_components()

    def _get_default_components(self) -> Dict:
        """
        返回默认的空组件结构

        返回:
            包含所有必需字段的默认空组件字典
        """
        return {
            "select": "",
            "from": "",
            "where": "",
            "groupBy": "",
            "orderBy": "",
            "having": "",
            "pagination": {
                "limit": "",
                "offset": ""
            }
        }

    async def extract_and_reconstruct(
            self,
            sql_query: str,
            llm_name: str
    ) -> Tuple[Dict, str]:
        """
        提取SQL组件并重构SQL语句

        参数:
            sql_query: 待解析的SQL查询语句
            llm_name: 用于提取的LLM模型名称

        返回:
            (components, reconstructed_sql): SQL组件字典和重构的SQL语句
        """
        # 提取SQL组件
        components = await self.extract_sql_components(sql_query, llm_name)

        # 重构SQL语句
        sql_parts = []

        # 按照SQL语句的标准顺序添加各个部分
        if components.get("select"):
            sql_parts.append(components["select"])
        if components.get("from"):
            sql_parts.append(components["from"])
        if components.get("where"):
            sql_parts.append(components["where"])
        if components.get("groupBy"):
            sql_parts.append(components["groupBy"])
        if components.get("having"):
            sql_parts.append(components["having"])
        if components.get("orderBy"):
            sql_parts.append(components["orderBy"])

        # 处理分页信息
        pagination = components.get("pagination", {})
        if pagination:
            limit = pagination.get("limit", "").strip()
            offset = pagination.get("offset", "").strip()

            # 构建LIMIT子句
            if limit:
                limit_clause = f"LIMIT {limit}"
                if offset:
                    limit_clause += f" OFFSET {offset}"
                sql_parts.append(limit_clause)

        # 使用换行符连接各个部分，使SQL更易读
        reconstructed_sql = "\n".join(sql_parts)

        return components, reconstructed_sql

    def get_pagination_info(self, components: Dict) -> Dict[str, str]:
        """
        从组件中提取分页信息

        参数:
            components: SQL组件字典

        返回:
            包含limit和offset的字典
        """
        pagination = components.get("pagination", {})

        return {
            "limit": pagination.get("limit", ""),
            "offset": pagination.get("offset", "")
        }

    def update_pagination(self, components: Dict, limit: Optional[str] = None,
                          offset: Optional[str] = None) -> Dict:
        """
        更新组件中的分页信息

        参数:
            components: SQL组件字典
            limit: 新的limit值（可选）
            offset: 新的offset值（可选）

        返回:
            更新后的组件字典
        """
        if "pagination" not in components:
            components["pagination"] = {"limit": "", "offset": ""}

        if limit is not None:
            components["pagination"]["limit"] = str(limit)

        if offset is not None:
            components["pagination"]["offset"] = str(offset)

        return components
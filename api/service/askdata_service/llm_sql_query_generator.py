import asyncio
import json
import os
import re
import logging
from datetime import date
from typing import Any, Dict, Optional, Tuple, List

from sqlalchemy.orm import Session

from api.db import LLMType
from api.db.services.llm_service import LLMBundle
from api.db.db_models import db_connection
from api.utils.prompt_template_util import PromptTemplateUtil

logger = logging.getLogger(__name__)


class NLQToInitialSQLGenerator:
    """使用大语言模型生成SQL查询的服务类，基于语义层和用户问题"""

    # JSON模式，匹配JSON代码块
    JSON_PATTERN = re.compile(r'```json\s*([\s\S]*?)```')

    def __init__(self, db: Session, user_id: Any, prompt_dir: str = None, database_type: str = "PostgreSQL"):
        """
        初始化SQL查询生成器
        """
        self.db = db
        self.user_id = user_id
        self.database_type = database_type
        self.prompt_dir = prompt_dir or os.path.join(os.path.dirname(__file__), "prompt")

    def _clean_sql(self, sql: str) -> str:
        """
        清理SQL查询，移除多余的空白和注释
        """
        if not sql:
            return sql
        sql = sql.strip()
        if not sql.endswith(';'):
            sql += ';'
        sql = re.sub(r'\n\s*\n', '\n', sql)
        return sql

    def _extract_llm_response_json(self, response: str) -> Optional[Dict]:
        """
        从LLM响应中稳健地提取JSON数据。
        """
        if not response:
            logger.warning("LLM响应为空")
            return None

        # 1. 尝试从```json代码块中提取
        json_match = self.JSON_PATTERN.search(response)
        if json_match:
            json_str = json_match.group(1).strip()
            try:
                return json.loads(json_str)
            except json.JSONDecodeError as e:
                logger.warning(f"从代码块解析JSON失败: {e}. 响应: {json_str[:200]}")
                # 失败后继续尝试其他方法

        # 2. 尝试将整个响应作为JSON解析
        try:
            return json.loads(response.strip())
        except json.JSONDecodeError:
            pass

        # 3. 尝试查找以'{'开头, '}'结尾的最大块
        try:
            start = response.find('{')
            end = response.rfind('}')
            if start != -1 and end != -1 and end > start:
                json_str = response[start:end + 1]
                return json.loads(json_str)
        except json.JSONDecodeError:
            logger.warning("无法从LLM响应中提取任何有效的JSON数据")

        return None

    def _parse_and_validate_llm_json(self, json_data: Dict) -> Optional[Dict[str, Any]]:
        """
        验证从LLM获取的JSON数据结构，并填充缺失的键。
        """
        if not isinstance(json_data, dict):
            logger.warning(f"期望得到字典格式的JSON，但实际类型为 {type(json_data)}")
            return None

        sql = json_data.get('sql')
        if not sql or not isinstance(sql, str):
            logger.warning(f"JSON中缺少必须的'sql'字段或其类型不是字符串。")
            return None

        # 定义期望的组件键 - 添加了 having 键
        component_keys = ["select", "from", "where", "groupBy", "having", "orderBy", "limit"]

        # 获取sqlComponents，如果不存在则为空字典
        sql_components = json_data.get('sqlComponents', {})
        if not isinstance(sql_components, dict):
            logger.warning(f"sqlComponents不是一个字典，将视为空处理。")
            sql_components = {}

        # 确保所有期望的组件都存在，不存在则设置为空字符串
        for key in component_keys:
            if key not in sql_components or not isinstance(sql_components[key], str):
                sql_components[key] = ""

        # 构建返回数据，保留所有原始字段
        result = {
            "sql": self._clean_sql(sql),
            "usedModels": json_data.get('usedModels', []) or [],
            "sqlComponents": sql_components
        }

        # 保留其他可能的字段（如 queryComplexity 等）
        for key, value in json_data.items():
            if key not in ["sql", "usedModels", "sqlComponents"]:
                result[key] = value

        return result

    async def generate_sql_query_with_components(self, user_query: str, semantic_layer: Dict[str, Any],
                                                 llm_name: str, recommended_chart: str) -> \
            Optional[Dict[str, Any]]:
        """
        根据用户问题和语义层信息生成SQL查询，包含分解后的组件和使用的模型信息。

        参数:
            user_query: 用户的自然语言查询问题
            semantic_layer: 语义层信息
            llm_name: 用于生成的LLM模型名称

        返回:
            包含sql, usedModels, 和 sqlComponents 的字典, 如果生成失败则返回None
        """
        try:

            prompt_template = None
            if recommended_chart == "明细表":
                prompt_template = PromptTemplateUtil.load_template_from_file(
                    os.path.join(self.prompt_dir, "nlq_to_initial_sql.txt")
                )
            elif recommended_chart == "聚合表":
                prompt_template = PromptTemplateUtil.load_template_from_file(
                    os.path.join(self.prompt_dir, "nlq_to_initial_sql_table-aggr.txt")
                )
            semantic_layer_str = json.dumps(semantic_layer, ensure_ascii=False, indent=2)

            prompt = PromptTemplateUtil.fill_template(
                prompt_template,
                {
                    "user_query": user_query,
                    "semantic_layer": semantic_layer_str,
                    "current_date": date.today().strftime("%Y-%m-%d"),
                    "database_type": self.database_type
                }
            )

            history = [{"role": "user", "content": prompt}]
            gen_conf = {
                "temperature": 0.1,
                "top_p": 0.9,
                "max_tokens": 4096
            }

            # 定义在独立线程中执行的函数，使用独立的数据库会话
            def _chat_in_thread():
                # 在线程中创建独立的数据库会话和LLM实例
                # 这样可以避免多线程共享数据库会话导致的事务状态冲突
                with db_connection() as thread_db:
                    thread_llm_instance = LLMBundle(thread_db, self.user_id, LLMType.CHAT, llm_name=llm_name)
                    return thread_llm_instance.chat(
                        system="你是一个专业的SQL专家。请根据用户需求和语义层信息生成准确的SQL查询JSON对象。",
                        history=history,
                        gen_conf=gen_conf
                    )

            response = await asyncio.to_thread(_chat_in_thread)

            logger.info(f"智能问数-LLM-生成SQL")
            logger.info(f"prompt:{prompt}")
            logger.info(f"gen_conf:{gen_conf}")
            logger.info(f"response:{response}")

            json_response = self._extract_llm_response_json(response)
            if not json_response:
                logger.error(f"无法从LLM的响应中提取JSON。原始响应: {response}")
                return None
            if json_response.get("status") == "failed":
                logger.error(f"生成SQL失败: {json_response.get('errorMessage')}")
                return json_response
            validated_data = self._parse_and_validate_llm_json(json_response)
            if not validated_data:
                logger.error(f"提取的JSON未能通过验证。原始JSON: {json_response}")
                return None

            logger.info("成功生成并验证了SQL及其组件。")
            return validated_data

        except FileNotFoundError as e:
            logger.error(f"加载提示词模板失败: {e}")
            raise e
        except Exception as e:
            logger.error(f"生成SQL查询时出错: {e}", exc_info=True)
            return None

    def validate_sql_syntax(self, sql: str) -> Tuple[bool, str]:
        """
        基本的SQL语法验证
        """
        if not sql or not sql.strip():
            return False, "SQL查询为空"

        sql_upper = sql.upper().strip()

        if not sql_upper.startswith('SELECT'):
            return False, "SQL查询必须以SELECT开头"

        if 'FROM' not in sql_upper:
            return False, f"SQL查询缺少必需的关键字: FROM"

        if sql.count('(') != sql.count(')'):
            return False, "SQL查询中括号不匹配"

        return True, "SQL语法验证通过"

    # async def generate_and_validate_sql_with_components(self, user_query: str, semantic_layer, llm_name: str) -> Tuple[
    #     Optional[Dict[str, Any]], bool, str]:
    #     """
    #     生成并验证SQL（包含组件和模型信息）。
    #     """
    #     result = await self.generate_sql_query_with_components(user_query, semantic_layer, llm_name)
    #
    #     if not result:
    #         return None, False, "SQL查询生成失败或LLM响应格式错误"
    #
    #     sql_query = result.get('sql')
    #     if not sql_query:
    #         return result, False, "生成的SQL查询为空"
    #
    #     is_valid, validation_message = self.validate_sql_syntax(sql_query)
    #     message = "SQL查询生成并验证成功" if is_valid else f"SQL语法验证失败: {validation_message}"
    #
    #     return result, is_valid, message

    async def fix_sql_query_with_components(self, user_query: str, original_sql: str, error_message: str,
                                            semantic_layer: Dict[str, Any], llm_name: str) -> Optional[Dict[str, Any]]:
        """
        修复执行失败的SQL查询

        参数:
            original_sql: 执行失败的原始SQL
            error_message: 数据库返回的错误信息
            semantic_layer: 语义层信息
            llm_name: 用于修复的LLM模型名称

        返回:
            包含修复后sql, usedModels, 和 sqlComponents 的字典, 如果修复失败则返回None
        """
        try:
            prompt_template = PromptTemplateUtil.load_template_from_file(
                os.path.join(self.prompt_dir, "sql_error_fix.txt")
            )

            semantic_layer_str = json.dumps(semantic_layer, ensure_ascii=False, indent=2)

            prompt = PromptTemplateUtil.fill_template(
                prompt_template,
                {
                    "user_query": user_query,
                    "original_sql": original_sql,
                    "error_message": error_message,
                    "semantic_layer": semantic_layer_str,
                    "current_date": date.today().strftime("%Y-%m-%d"),
                    "database_type": self.database_type
                }
            )

            history = [{"role": "user", "content": prompt}]
            gen_conf = {
                "temperature": 0.1,  # 低温度以确保修复的准确性
                "top_p": 0.9,
                "max_tokens": 4096
            }

            # 定义在独立线程中执行的函数，使用独立的数据库会话
            def _chat_in_thread():
                # 在线程中创建独立的数据库会话和LLM实例
                # 这样可以避免多线程共享数据库会话导致的事务状态冲突
                with db_connection() as thread_db:
                    thread_llm_instance = LLMBundle(thread_db, self.user_id, LLMType.CHAT, llm_name=llm_name)
                    return thread_llm_instance.chat(
                        system="你是一个专业的SQL修复专家。请根据错误信息和语义层信息修复SQL查询，输出JSON格式的结果。",
                        history=history,
                        gen_conf=gen_conf
                    )

            response = await asyncio.to_thread(_chat_in_thread)

            logger.info(f"SQL修复-LLM响应")
            logger.info(f"原始SQL: {original_sql}")
            logger.info(f"错误信息: {error_message}")
            logger.info(f"修复响应: {response}")

            json_response = self._extract_llm_response_json(response)
            if not json_response:
                logger.error(f"无法从LLM的响应中提取JSON。原始响应: {response}")
                return None

            validated_data = self._parse_and_validate_llm_json(json_response)
            if not validated_data:
                logger.error(f"提取的JSON未能通过验证。原始JSON: {json_response}")
                return None

            logger.info("成功修复SQL查询。")
            return validated_data

        except Exception as e:
            logger.error(f"修复SQL查询时出错: {e}", exc_info=True)
            return None
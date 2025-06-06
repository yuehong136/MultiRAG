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
from api.utils.prompt_template_util import PromptTemplateUtil

logger = logging.getLogger(__name__)


class NLQToInitialSQLGenerator:
    """使用大语言模型生成SQL查询的服务类，基于语义层和用户问题"""

    # 编译正则表达式以提高性能
    SQL_PATTERN = re.compile(r'```sql\s*([\s\S]*?)```')
    # 备用SQL模式，匹配单独的SQL代码块
    SQL_PATTERN_ALT = re.compile(r'```\s*(SELECT[\s\S]*?)```', re.IGNORECASE)
    # JSON模式，匹配JSON代码块
    JSON_PATTERN = re.compile(r'```json\s*([\s\S]*?)```')

    def __init__(self, db: Session, user_id: Any, prompt_dir: str = None, database_type: str = "PostgreSQL"):
        """
        初始化SQL查询生成器

        参数:
            db: 数据库会话
            user_id: 用户ID
            prompt_dir: 提示词模板目录路径，如果为None则使用默认路径
            database_type: 数据库类型，默认为MySQL
        """
        self.db = db
        self.user_id = user_id
        self.database_type = database_type

        # 如果未提供提示词目录，使用默认路径
        if prompt_dir is None:
            self.prompt_dir = os.path.join(os.path.dirname(__file__), "prompt")
        else:
            self.prompt_dir = prompt_dir

    def _clean_sql(self, sql: str) -> str:
        """
        清理SQL查询，移除多余的空白和注释

        参数:
            sql: 原始SQL查询字符串

        返回:
            清理后的SQL查询字符串
        """
        if not sql:
            return sql

        # 移除首尾空白
        sql = sql.strip()

        # 如果以分号结尾，保留分号
        if not sql.endswith(';'):
            sql += ';'

        # 移除多余的空行
        sql = re.sub(r'\n\s*\n', '\n', sql)

        return sql

    def _extract_json_from_response(self, response: str) -> Tuple[Optional[Dict], bool]:
        """
        从LLM响应中提取JSON数据

        参数:
            response: LLM返回的原始响应文本

        返回:
            (extracted_json, success_flag): 提取的JSON数据和成功标志的元组
        """
        if not response:
            logger.warning("LLM响应为空")
            return None, False

        # 尝试从```json代码块中提取
        json_match = self.JSON_PATTERN.search(response)
        if json_match:
            json_str = json_match.group(1).strip()
            try:
                json_data = json.loads(json_str)
                logger.info("成功从json代码块中提取JSON")
                return json_data, True
            except json.JSONDecodeError as e:
                logger.warning(f"JSON解析失败: {e}")

        # 尝试直接解析整个响应为JSON
        try:
            response_stripped = response.strip()
            # 移除可能的markdown标记
            if response_stripped.startswith('```') and response_stripped.endswith('```'):
                lines = response_stripped.split('\n')
                if len(lines) >= 3:
                    json_str = '\n'.join(lines[1:-1])
                else:
                    json_str = response_stripped
            else:
                json_str = response_stripped

            json_data = json.loads(json_str)
            logger.info("成功直接解析JSON响应")
            return json_data, True
        except json.JSONDecodeError:
            pass

        # 尝试查找看起来像JSON的部分
        # 寻找以{开头，以}结尾的文本块
        json_pattern = re.compile(r'\{[\s\S]*\}')
        matches = json_pattern.findall(response)

        for match in matches:
            try:
                json_data = json.loads(match)
                logger.info("使用正则表达式成功提取JSON")
                return json_data, True
            except json.JSONDecodeError:
                continue

        logger.warning("无法从LLM响应中提取有效的JSON数据")
        return None, False

    def _extract_sql_and_models_from_response(self, response: str) -> Tuple[Optional[str], Optional[List[str]], bool]:
        """
        从LLM响应中提取SQL查询和使用的模型

        参数:
            response: LLM返回的原始响应文本

        返回:
            (extracted_sql, used_models, success_flag): 提取的SQL查询、使用的模型列表和成功标志的元组
        """
        if not response:
            logger.warning("LLM响应为空")
            return None, None, False

        # 首先尝试提取JSON格式的响应
        json_data, json_success = self._extract_json_from_response(response)

        if json_success and json_data:
            # 检查JSON是否包含必要的字段
            if isinstance(json_data, dict) and 'sql' in json_data:
                sql = json_data.get('sql', '').strip()
                used_models = json_data.get('usedModels', [])

                if sql:
                    cleaned_sql = self._clean_sql(sql)
                    logger.info("成功从JSON响应中提取SQL和模型信息")
                    return cleaned_sql, used_models, True

        # 如果JSON提取失败，回退到原来的SQL提取方法（向后兼容）
        sql, sql_success = self._extract_sql_from_response_legacy(response)

        if sql_success and sql:
            logger.info("使用传统方法成功提取SQL，但无模型信息")
            return sql, [], True

        logger.warning("无法从LLM响应中提取SQL查询")
        return None, None, False

    def _extract_sql_from_response_legacy(self, response: str) -> Tuple[Optional[str], bool]:
        """
        使用传统方法从LLM响应中提取SQL查询（向后兼容）

        参数:
            response: LLM返回的原始响应文本

        返回:
            (extracted_sql, success_flag): 提取的SQL查询和成功标志的元组
        """
        if not response:
            logger.warning("LLM响应为空")
            return None, False

        # 优先尝试从```sql代码块中提取
        sql_match = self.SQL_PATTERN.search(response)
        if sql_match:
            sql_str = sql_match.group(1).strip()
            cleaned_sql = self._clean_sql(sql_str)
            logger.info("成功从sql代码块中提取SQL")
            return cleaned_sql, True

        # 尝试从普通代码块中提取以SELECT开头的SQL
        sql_match_alt = self.SQL_PATTERN_ALT.search(response)
        if sql_match_alt:
            sql_str = sql_match_alt.group(1).strip()
            cleaned_sql = self._clean_sql(sql_str)
            logger.info("成功从普通代码块中提取SQL")
            return cleaned_sql, True

        # 检查响应是否直接就是SQL语句（去除首尾空白后）
        response_stripped = response.strip()
        if re.match(r'^\s*SELECT\s+', response_stripped, re.IGNORECASE):
            # 如果整个响应就是以SELECT开头的SQL语句
            cleaned_sql = self._clean_sql(response_stripped)
            logger.info("响应直接是SQL语句，成功提取")
            return cleaned_sql, True

        # 尝试逐行查找SQL语句
        lines = response.split('\n')
        sql_lines = []
        sql_started = False

        for line in lines:
            line_stripped = line.strip()

            # 跳过空行和注释
            if not line_stripped or line_stripped.startswith('--') or line_stripped.startswith('#'):
                if sql_started and not line_stripped:
                    continue  # SQL中的空行可以保留
                elif not sql_started:
                    continue

            # 检查是否是SQL开始
            if not sql_started and re.match(r'^\s*SELECT\s+', line, re.IGNORECASE):
                sql_started = True
                sql_lines.append(line.rstrip())  # 保留原始缩进，只去除右侧空白
            elif sql_started:
                # 检查是否是SQL语句的一部分
                # SQL关键字或者是字段/表名的延续
                if (re.match(
                        r'^\s*(FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|GROUP|ORDER|HAVING|LIMIT|OFFSET|UNION|INTERSECT|EXCEPT|ON|AND|OR|IN|EXISTS|BETWEEN|LIKE|IS|NOT|NULL|AS|DISTINCT|ALL|CASE|WHEN|THEN|ELSE|END)\s+',
                        line, re.IGNORECASE) or
                        line_stripped.startswith(',') or
                        line_stripped.endswith(',') or
                        line_stripped.startswith('(') or
                        line_stripped.endswith(')') or
                        '.' in line_stripped or  # 表.字段
                        line_stripped.endswith(';')):

                    sql_lines.append(line.rstrip())

                    # 如果以分号结尾，SQL语句结束
                    if line_stripped.endswith(';'):
                        break
                else:
                    # 如果不是SQL的一部分，可能SQL已经结束
                    if sql_lines and not line_stripped:
                        continue  # 空行可能只是格式化
                    else:
                        break  # 遇到非SQL内容，结束提取

        if sql_lines:
            sql_str = '\n'.join(sql_lines)
            cleaned_sql = self._clean_sql(sql_str)
            logger.info("成功从响应文本中提取SQL")
            return cleaned_sql, True

        # 最后尝试：查找任何包含SELECT...FROM的文本片段
        select_from_pattern = re.compile(r'(SELECT[\s\S]*?FROM[\s\S]*?)(?:;|$)', re.IGNORECASE | re.MULTILINE)
        matches = select_from_pattern.findall(response)
        if matches:
            # 选择最长的匹配（通常是最完整的SQL）
            longest_match = max(matches, key=len)
            cleaned_sql = self._clean_sql(longest_match)
            logger.info("使用正则表达式模式成功提取SQL")
            return cleaned_sql, True

        logger.warning("无法从LLM响应中提取SQL查询")
        logger.debug(f"LLM原始响应: {response[:500]}...")  # 只记录前500个字符
        return None, False

    def _load_prompt_template(self) -> str:
        """
        加载SQL查询生成的提示词模板

        返回:
            提示词模板字符串
        """
        try:
            template_path = os.path.join(self.prompt_dir, "nlq_to_initial_sql.txt")
            if os.path.exists(template_path):
                return PromptTemplateUtil.load_template_from_file(template_path)
            else:
                logger.warning(f"提示词模板文件不存在，路径为：{template_path}")
                raise FileNotFoundError(f"提示词模板文件不存在，路径为：{template_path}")
        except Exception as e:
            logger.error(f"加载提示词模板失败: {e}")
            raise e

    async def generate_sql_query(self, user_query: str, semantic_layer: Dict[str, Any], llm_name: str) -> Optional[str]:
        """
        根据用户问题和语义层信息生成SQL查询（向后兼容的方法）

        参数:
            user_query: 用户的自然语言查询问题
            semantic_layer: 语义层信息，包含数据模型、业务数据集等
            llm_name: 用于生成的LLM模型名称

        返回:
            生成的SQL查询字符串，如果生成失败则返回None
        """
        result = await self.generate_sql_query_with_models(user_query, semantic_layer, llm_name)
        return result['sql'] if result else None

    async def generate_sql_query_with_models(self, user_query: str, semantic_layer: Dict[str, Any], llm_name: str) -> \
            Optional[Dict[str, Any]]:
        """
        根据用户问题和语义层信息生成SQL查询，包含使用的模型信息

        参数:
            user_query: 用户的自然语言查询问题
            semantic_layer: 语义层信息，包含数据模型、业务数据集等
            llm_name: 用于生成的LLM模型名称

        返回:
            包含sql和usedModels的字典，如果生成失败则返回None
            格式: {"sql": "SELECT ...", "usedModels": ["模型1", "模型2"]}
        """
        try:
            # 初始化LLM模型
            llm_model_instance = LLMBundle(self.db, self.user_id, LLMType.CHAT, llm_name=llm_name)

            # 加载提示词模板
            prompt_template = self._load_prompt_template()

            # 将semantic_layer转换为格式化的JSON字符串
            semantic_layer_str = json.dumps(semantic_layer, ensure_ascii=False, indent=2)

            # 用参数填充模板
            prompt = PromptTemplateUtil.fill_template(
                prompt_template,
                {
                    "user_query": user_query,
                    "semantic_layer": semantic_layer_str,
                    "current_date": date.today().strftime("%Y-%m-%d"),
                    "database_type": self.database_type
                }
            )

            # 创建对话历史
            history = [{"role": "user", "content": prompt}]

            # LLM配置 - 使用较低的温度以获得更确定性的结果
            gen_conf = {
                "temperature": 0.1,  # 降低温度以获得更稳定的SQL生成
                "top_p": 0.9,
                "max_tokens": 4096
            }

            # 调用LLM生成SQL
            response = await asyncio.to_thread(
                llm_model_instance.chat,
                system="你是一个专业的SQL专家。请根据用户需求和语义层信息生成准确的SQL查询语句。",
                history=history,
                gen_conf=gen_conf
            )

            # 提取SQL查询和模型信息
            sql_query, used_models, success = self._extract_sql_and_models_from_response(response)

            if success and sql_query:
                logger.info("SQL查询生成成功")
                return {
                    "sql": sql_query,
                    "usedModels": used_models or []
                }
            else:
                logger.warning("无法从LLM响应中提取有效的SQL查询")
                # 记录原始响应以便调试
                logger.debug(f"LLM原始响应: {response}")
                return None

        except Exception as e:
            logger.error(f"生成SQL查询时出错: {e}", exc_info=True)
            return None

    def validate_sql_syntax(self, sql: str) -> Tuple[bool, str]:
        """
        基本的SQL语法验证

        参数:
            sql: 待验证的SQL查询字符串

        返回:
            (is_valid, error_message): 验证结果和错误信息的元组
        """
        if not sql or not sql.strip():
            return False, "SQL查询为空"

        sql_upper = sql.upper().strip()

        # 检查是否以SELECT开头
        if not sql_upper.startswith('SELECT'):
            return False, "SQL查询必须以SELECT开头"

        # 检查基本的SQL关键字存在性
        required_keywords = ['SELECT', 'FROM']
        for keyword in required_keywords:
            if keyword not in sql_upper:
                return False, f"SQL查询缺少必需的关键字: {keyword}"

        # 检查括号匹配
        open_parens = sql.count('(')
        close_parens = sql.count(')')
        if open_parens != close_parens:
            return False, "SQL查询中括号不匹配"

        return True, "SQL语法验证通过"

    async def generate_and_validate_sql(self, user_query: str, semantic_layer, llm_name: str) -> Tuple[
        Optional[str], bool, str]:
        """
        生成SQL查询并进行基本验证（向后兼容的方法）

        参数:
            user_query: 用户的自然语言查询问题
            semantic_layer: 语义层信息
            llm_name: LLM模型名称

        返回:
            (sql_query, is_valid, message): SQL查询、验证结果和消息的元组
        """
        # 生成SQL查询
        sql_query = await self.generate_sql_query(user_query, semantic_layer, llm_name)

        if sql_query is None:
            return None, False, "SQL查询生成失败"

        # 验证SQL语法
        is_valid, validation_message = self.validate_sql_syntax(sql_query)

        if is_valid:
            return sql_query, True, "SQL查询生成并验证成功"
        else:
            return sql_query, False, f"SQL语法验证失败: {validation_message}"

    async def generate_and_validate_sql_with_models(self, user_query: str, semantic_layer, llm_name: str) -> Tuple[
        Optional[Dict[str, Any]], bool, str]:
        """
        生成SQL查询并进行基本验证，包含使用的模型信息

        参数:
            user_query: 用户的自然语言查询问题
            semantic_layer: 语义层信息
            llm_name: LLM模型名称

        返回:
            (result_dict, is_valid, message): 包含SQL和模型信息的字典、验证结果和消息的元组
        """
        # 生成SQL查询和模型信息
        result = await self.generate_sql_query_with_models(user_query, semantic_layer, llm_name)

        if result is None:
            return None, False, "SQL查询生成失败"

        sql_query = result.get('sql')
        if not sql_query:
            return None, False, "SQL查询为空"

        # 验证SQL语法
        is_valid, validation_message = self.validate_sql_syntax(sql_query)

        if is_valid:
            return result, True, "SQL查询生成并验证成功"
        else:
            return result, False, f"SQL语法验证失败: {validation_message}"

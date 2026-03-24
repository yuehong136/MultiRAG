import os
import re
import logging
from typing import Any

from sqlalchemy.orm import Session

from api.db.services.llm_service import LLMBundle
from api.db.db_models import db_connection
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from api.utils.prompt_template_util import PromptTemplateUtil
from common.constants import LLMType
from common.misc_utils import thread_pool_exec

logger = logging.getLogger(__name__)


class SQLPaginationConverter:
    """负责将SQL语句转换为分页查询的服务类"""

    # 编译正则表达式以提高性能
    SQL_BLOCK_PATTERN = re.compile(r'```sql\s*([\s\S]*?)```', re.IGNORECASE)

    def __init__(self, db: Session, user_id: Any, prompt_dir: str = None):
        """
        初始化SQL分页转换器

        参数:
            db: 数据库会话
            user_id: 用户ID
            prompt_dir: 提示词模板目录路径，如果为None则使用默认路径
        """
        self.db = db
        self.user_id = user_id

        # 如果未提供提示词目录，使用默认路径
        if prompt_dir is None:
            current_dir = os.path.dirname(__file__)
            self.prompt_dir = os.path.join(os.path.dirname(current_dir), "prompt")
        else:
            self.prompt_dir = prompt_dir

    def _extract_sql_from_response(self, response: str) -> str:
        """
        从LLM响应中提取SQL语句

        参数:
            response: LLM返回的原始响应文本

        返回:
            提取的SQL语句
        """
        # 尝试从代码块中提取SQL
        sql_match = self.SQL_BLOCK_PATTERN.search(response)

        if sql_match:
            sql_str = sql_match.group(1).strip()
            return sql_str

        # 如果没有找到代码块，直接返回响应（假设整个响应就是SQL）
        return response.strip()

    async def convert_to_pagination(
            self,
            sql_query: str,
            database_type,
            llm_name: str
    ) -> str:
        """
        将SQL语句转换为分页查询

        参数:
            sql_query: 原始SQL查询语句
            database_type: 目标数据库类型（MySQL, PostgreSQL, SQLite, SQLServer2012+, SQLServer2008, Oracle12c+, Oracle11g）
            llm_name: 用于转换的LLM模型名称

        返回:
            转换后的分页SQL语句
        """
        try:
            # 从文件加载提示词模板
            template_path = os.path.join(self.prompt_dir, "sql_pagination_converter_prompt.txt")
            prompt_template = PromptTemplateUtil.load_template_from_file(template_path)

            # 准备模板参数
            template_values = {
                "sql": sql_query,
                "database_type": database_type
            }

            # 填充模板
            prompt = PromptTemplateUtil.fill_template(prompt_template, template_values)

            # 创建包含我们提示词的对话历史
            history = [{"role": "user", "content": prompt}]

            # LLM配置 - 对于SQL转换任务使用较低的temperature
            gen_conf = {
                "temperature": 0.0,  # 使用0以获得更确定性的结果
                "top_p": 0.9,
                "max_tokens": 2048
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

            # 调用LLM处理我们的提示词
            response = await thread_pool_exec(_chat_in_thread)

            # 提取并返回SQL语句
            paginated_sql = self._extract_sql_from_response(response)

            logger.info(f"Successfully converted SQL to pagination for {database_type}")
            return paginated_sql

        except Exception as e:
            logger.error(f"Error in convert_to_pagination: {e}", exc_info=True)
            raise
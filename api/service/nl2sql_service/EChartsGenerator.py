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


class EChartsGenerator:
    """负责生成基于用户查询和SQL数据的ECharts配置的服务类"""

    # 编译正则表达式以提高性能
    JS_PATTERN = re.compile(r'```javascript\s*([\s\S]*?)```')

    def __init__(self, db: Session, user_id: Any, prompt_dir: str = None):
        """
        初始化ECharts生成器

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

    def _extract_js_from_response(self, response: str) -> Tuple[Optional[str], bool]:
        """
        从响应中提取JavaScript代码。

        参数:
            response: LLM返回的原始响应文本

        返回:
            (extracted_js, success_flag): 提取的JS代码和成功标志的元组
        """
        # 尝试从代码块中提取JavaScript
        js_match = self.JS_PATTERN.search(response)

        if js_match:
            js_code = js_match.group(1).strip()
            # 检查是否包含generateEchartsOption函数
            if "function generateEchartsOption" in js_code:
                return js_code, True
            else:
                logger.warning("Found JavaScript code block but missing generateEchartsOption function")
        else:
            logger.warning("No JavaScript code block found in response")

        # 如果提取失败，返回None和False
        return None, False

    async def generate_echarts_config(self, user_query: str, sql_query: str, column_and_type: str,
                                      sample_data: str, llm_name: str) -> Optional[str]:
        """
        使用LLM生成ECharts配置代码。

        参数:
            user_query: 用户的原始查询
            sql_query: 对应的SQL查询语句
            column_and_type: 列名和数据类型信息
            sample_data: 示例数据
            llm_name: 用于生成的LLM模型名称

        返回:
            生成的ECharts配置JavaScript代码，如果生成失败则返回None
        """
        try:
            # 初始化LLM模型
            llm_model_instance = LLMBundle(self.db, self.user_id, LLMType.CHAT, llm_name=llm_name)

            # 从文件加载提示词模板
            template_path = os.path.join(self.prompt_dir, "echarts_chart_template.txt")
            prompt_template = PromptTemplateUtil.load_template_from_file(template_path)

            # 用参数填充模板
            prompt = PromptTemplateUtil.fill_template(
                prompt_template,
                {
                    "user_query": user_query,
                    "sql_query": sql_query,
                    "column_and_type": column_and_type,
                    "sample_data": sample_data
                }
            )

            # 创建包含我们提示词的对话历史
            history = [{"role": "user", "content": prompt}]

            # LLM配置
            gen_conf = {
                "temperature": 0.7,  # 稍微降低温度以获得更确定性的结果
                "top_p": 0.95,
                "max_tokens": 4096
            }

            # 调用LLM处理我们的提示词
            response = await asyncio.to_thread(
                llm_model_instance.chat,
                system="You are an expert data visualization developer specializing in ECharts.",
                history=history,
                gen_conf=gen_conf
            )

            # 提取和处理响应
            extracted_js, success = self._extract_js_from_response(response)

            if success:
                return extracted_js
            else:
                logger.error("Failed to extract valid ECharts configuration from LLM response")
                return None

        except Exception as e:
            logger.error(f"Error in generate_echarts_config: {e}", exc_info=True)
            return None  # 发生错误时，返回None

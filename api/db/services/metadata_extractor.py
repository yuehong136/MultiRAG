"""
元数据提取器 - 借鉴 core/flow/extractor/extractor.py 的设计

@project: multirag
@Author: AI Assistant
@date: 2025-11-05
"""

import asyncio
import logging
import re
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name, get_tenant_default_model_by_type
from api.db.services.llm_service import LLMBundle
from common.misc_utils import thread_pool_exec

logger = logging.getLogger(__name__)


class MetadataExtractor:
    """
    元数据提取器

    借鉴 core/flow/extractor/extractor.py 的设计理念：
    - 支持自定义字段名和提示词
    - 批量处理文本内容
    - 支持多种聚合策略
    """

    def __init__(self, db: Session, tenant_id: str, field_name: str, prompt: str, aggregate: str = "merge", llm_name: str | None = None, temperature: float = 0.1, max_tokens: int = 512):
        """
        初始化元数据提取器

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            field_name: 要提取的字段名（如 "tags", "summary", "authors"）
            prompt: LLM 提示词
            aggregate: 聚合策略 (merge | concat | deduplicate | first)
            llm_name: LLM 模型名称（可选）
            temperature: LLM 温度参数
            max_tokens: 最大生成 tokens
        """
        self.db = db
        self.tenant_id = tenant_id
        self.field_name = field_name
        self.prompt = prompt
        self.aggregate = aggregate
        self.llm_name = llm_name
        self.temperature = temperature
        self.max_tokens = max_tokens

        # 初始化 LLM
        if llm_name:
            model_config = get_model_config_by_type_and_name(db, tenant_id, "chat", llm_name)
        else:
            model_config = get_tenant_default_model_by_type(db, tenant_id, "chat")
        self.llm = LLMBundle(
            db,
            tenant_id,
            model_config,
        )

    async def extract_single(self, content: str) -> Any:
        """
        从单个文本内容中提取元数据

        Args:
            content: 要处理的文本内容

        Returns:
            提取的元数据值
        """
        try:
            # 构建消息
            messages = [{"role": "system", "content": self.prompt}, {"role": "user", "content": f"Content:\n\n{content}"}]

            # 调用 LLM
            result = await thread_pool_exec(
                self.llm.chat,
                "",  # system prompt 已在 messages 中
                messages,
                {"temperature": self.temperature, "max_tokens": self.max_tokens},
            )

            # 清理输出
            result = re.sub(r"^.*</think>", "", result, flags=re.DOTALL).strip()

            return result

        except Exception as e:
            logger.error(f"Error extracting {self.field_name}: {e}")
            return None

    async def extract_batch(self, contents: list[str], show_progress: bool = False) -> list[Any]:
        """
        批量提取元数据

        Args:
            contents: 要处理的文本列表
            show_progress: 是否显示进度

        Returns:
            提取的元数据列表
        """
        results = []
        total = len(contents)

        for i, content in enumerate(contents):
            if show_progress and i % max(1, total // 10) == 0:
                logger.info(f"Extracting {self.field_name}: {i + 1}/{total}")

            result = await self.extract_single(content)
            results.append(result)

        return results

    async def extract_and_aggregate(self, contents: list[str], show_progress: bool = False) -> Any:
        """
        提取元数据并聚合

        Args:
            contents: 要处理的文本列表
            show_progress: 是否显示进度

        Returns:
            聚合后的元数据
        """
        # 批量提取
        raw_results = await self.extract_batch(contents, show_progress)

        # 过滤空值
        valid_results = [r for r in raw_results if r]

        if not valid_results:
            return None

        # 聚合
        return self._aggregate_results(valid_results)

    def _aggregate_results(self, results: list[Any]) -> Any:
        """
        聚合提取结果

        Args:
            results: 提取结果列表

        Returns:
            聚合后的结果
        """
        if self.aggregate == "first":
            # 返回第一个非空值
            return results[0] if results else None

        elif self.aggregate == "concat":
            # 拼接文本
            return "\n\n".join([str(r) for r in results])

        elif self.aggregate == "merge":
            # 合并列表（支持逗号分隔或 JSON 数组）
            all_items = []
            for result in results:
                items = self._parse_list_result(result)
                all_items.extend(items)

            # 统计频次并排序
            counter = Counter(all_items)
            return [item for item, count in counter.most_common(10)]

        elif self.aggregate == "deduplicate":
            # 去重合并
            all_items = []
            for result in results:
                items = self._parse_list_result(result)
                all_items.extend(items)

            # 简单去重（保持顺序）
            seen = set()
            unique = []
            for item in all_items:
                if item not in seen:
                    seen.add(item)
                    unique.append(item)

            return unique

        else:
            # 默认：返回列表
            return results

    def _parse_list_result(self, result: Any) -> list[str]:
        """
        解析 LLM 输出为列表

        支持格式：
        - "标签1, 标签2, 标签3"
        - "标签1,标签2,标签3"
        - '["标签1", "标签2", "标签3"]'
        - "- 标签1\n- 标签2"

        Args:
            result: LLM 输出结果

        Returns:
            解析后的列表
        """
        if isinstance(result, list):
            return result

        if not isinstance(result, str):
            return [str(result)]

        result = result.strip()

        # 尝试解析 JSON 数组
        if result.startswith("[") and result.endswith("]"):
            try:
                import json

                parsed = json.loads(result)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed]
            except Exception:
                pass

        # 尝试按逗号分隔
        if "," in result:
            items = [item.strip() for item in result.split(",")]
            return [item for item in items if item]

        # 尝试按换行分隔（Markdown 列表格式）
        if "\n" in result:
            items = []
            for line in result.split("\n"):
                line = line.strip()
                # 移除 Markdown 列表标记
                line = re.sub(r"^[-*•]\s*", "", line)
                if line:
                    items.append(line)
            return items

        # 单个值
        return [result]


class BatchMetadataExtractor:
    """
    批量元数据提取器

    同时提取多个字段，减少重复处理
    """

    def __init__(self, db: Session, tenant_id: str):
        self.db = db
        self.tenant_id = tenant_id

    async def extract_multiple_fields(self, contents: list[str], field_configs: list[dict]) -> dict[str, Any]:
        """
        同时提取多个元数据字段

        Args:
            contents: 要处理的文本列表
            field_configs: 字段配置列表，每项包含:
                - field_name: 字段名
                - prompt: 提示词
                - aggregate: 聚合策略

        Returns:
            {field_name: extracted_value} 字典
        """
        result = {}

        # 为每个字段创建 Extractor 并并行提取
        tasks = []
        for config in field_configs:
            extractor = MetadataExtractor(
                db=self.db,
                tenant_id=self.tenant_id,
                field_name=config["field_name"],
                prompt=config["prompt"],
                aggregate=config.get("aggregate", "merge"),
                llm_name=config.get("llm_name"),
                temperature=config.get("temperature", 0.1),
                max_tokens=config.get("max_tokens", 512),
            )

            tasks.append(extractor.extract_and_aggregate(contents))

        # 并行执行
        extracted_values = await asyncio.gather(*tasks)

        # 组装结果
        for config, value in zip(field_configs, extracted_values):
            result[config["field_name"]] = value

        return result

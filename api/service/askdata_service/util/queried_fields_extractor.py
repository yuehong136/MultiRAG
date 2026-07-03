"""
查询字段提取器
从SQL组件和语义层中提取实际查询的维度和指标信息
"""

import re
from typing import Any

from api.service.askdata_service.util.askdata_logger import get_askdata_logger

logger = get_askdata_logger()


class QueriedFieldsExtractor:
    """从SQL组件中提取查询字段并匹配语义信息"""

    @staticmethod
    def _parse_select_fields(select_clause: str) -> list[dict[str, str]]:
        """
        解析SELECT子句，提取字段和别名

        Args:
            select_clause: SELECT子句内容

        Returns:
            字段列表，每个字段包含 field_expr 和 alias
        """
        if not select_clause or not select_clause.strip():
            return []

        fields = []
        # 移除SELECT关键字
        select_clause = re.sub(r"^\s*SELECT\s+", "", select_clause, flags=re.IGNORECASE).strip()

        # 按逗号分割字段（注意处理函数中的逗号）
        field_parts = []
        current_part = ""
        paren_depth = 0

        for char in select_clause:
            if char == "(":
                paren_depth += 1
            elif char == ")":
                paren_depth -= 1
            elif char == "," and paren_depth == 0:
                field_parts.append(current_part.strip())
                current_part = ""
                continue
            current_part += char

        if current_part.strip():
            field_parts.append(current_part.strip())

        # 解析每个字段
        for part in field_parts:
            if not part:
                continue

            # 匹配 "expression AS alias" 或 "expression alias"
            as_match = re.search(r"^(.+?)\s+AS\s+([^\s]+)$", part, re.IGNORECASE)
            if as_match:
                field_expr = as_match.group(1).strip()
                alias = as_match.group(2).strip().strip('"').strip("'")
                fields.append({"field_expr": field_expr, "alias": alias})
                continue

            # 匹配 "expression alias" (无AS关键字)
            space_match = re.search(r"^(.+?)\s+([^\s\(\)]+)$", part)
            if space_match and not re.match(r"^\w+\(", space_match.group(2)):
                field_expr = space_match.group(1).strip()
                alias = space_match.group(2).strip().strip('"').strip("'")
                fields.append({"field_expr": field_expr, "alias": alias})
                continue

            # 没有别名的情况
            fields.append({"field_expr": part.strip(), "alias": None})

        return fields

    @staticmethod
    def _extract_aggregation_info(field_expr: str) -> dict[str, str] | None:
        """
        从字段表达式中提取聚合函数信息

        Args:
            field_expr: 字段表达式，如 "COUNT(DISTINCT teacher_id)"

        Returns:
            包含聚合函数和内部字段的字典，如果不是聚合则返回None
        """
        # 匹配聚合函数: COUNT/SUM/AVG/MAX/MIN/COUNT DISTINCT等
        agg_pattern = r"^(COUNT|SUM|AVG|MAX|MIN)\s*\(\s*(DISTINCT\s+)?(.+?)\s*\)$"
        match = re.match(agg_pattern, field_expr, re.IGNORECASE)

        if match:
            agg_func = match.group(1).upper()
            has_distinct = bool(match.group(2))
            inner_field = match.group(3).strip()

            return {"aggregation": agg_func, "has_distinct": has_distinct, "inner_field": inner_field}

        return None

    @staticmethod
    def _normalize_field_name(field_name: str) -> str:
        """
        标准化字段名，移除表别名前缀

        Args:
            field_name: 字段名，可能包含表别名，如 "t1.teacher_name"

        Returns:
            标准化后的字段名
        """
        # 移除表别名前缀
        if "." in field_name:
            parts = field_name.split(".")
            return parts[-1].strip()
        return field_name.strip()

    @staticmethod
    def _match_dimension(field_info: dict[str, str], semantic_layer: dict[str, Any]) -> dict[str, Any] | None:
        """
        在语义层中匹配维度信息

        Args:
            field_info: 字段信息（包含field_expr和alias）
            semantic_layer: 语义层数据

        Returns:
            匹配到的维度信息，未匹配则返回None
        """
        field_expr = field_info["field_expr"]
        alias = field_info["alias"]

        # 标准化字段名
        normalized_field = QueriedFieldsExtractor._normalize_field_name(field_expr)

        # 遍历语义层中的维度
        dimensions = semantic_layer.get("dimensions", [])
        for dim in dimensions:
            dim_biz_name = dim.get("dimensionBizName") or dim.get("dimension_biz_name", "")
            dim_id = dim.get("dimensionId") or dim.get("dimension_id", "")
            dim_name = dim.get("dimensionName") or dim.get("dimension_name", "")

            # 匹配逻辑：字段表达式或别名与业务名称匹配
            if normalized_field == dim_biz_name or alias == dim_biz_name or normalized_field == dim_id:
                return {
                    "dimension_id": dim_id,
                    "dimension_name": dim_name,
                    "dimension_biz_name": dim_biz_name,
                    "model_id": dim.get("modelId") or dim.get("model_id", ""),
                    "model_name": dim.get("modelName") or dim.get("model_name", ""),
                    "data_type": dim.get("dataType") or dim.get("data_type", ""),
                    "alias": alias or dim_biz_name,
                    "field_expr": field_expr,
                }

        return None

    @staticmethod
    def _match_metric(field_info: dict[str, str], semantic_layer: dict[str, Any]) -> dict[str, Any] | None:
        """
        在语义层中匹配指标信息

        Args:
            field_info: 字段信息（包含field_expr和alias）
            semantic_layer: 语义层数据

        Returns:
            匹配到的指标信息，未匹配则返回None
        """
        field_expr = field_info["field_expr"]
        alias = field_info["alias"]

        # 提取聚合信息
        agg_info = QueriedFieldsExtractor._extract_aggregation_info(field_expr)

        # 遍历语义层中的指标
        metrics = semantic_layer.get("metrics", [])
        for metric in metrics:
            metric_biz_name = metric.get("metricBizName") or metric.get("metric_biz_name", "")
            metric_id = metric.get("metricId") or metric.get("metric_id", "")
            metric_name = metric.get("metricName") or metric.get("metric_name", "")

            # 匹配逻辑：别名与业务名称匹配，或者字段ID匹配
            if alias == metric_biz_name or field_expr == metric_id:
                result = {
                    "metric_id": metric_id,
                    "metric_name": metric_name,
                    "metric_biz_name": metric_biz_name,
                    "model_id": metric.get("modelId") or metric.get("model_id", ""),
                    "model_name": metric.get("modelName") or metric.get("model_name", ""),
                    "alias": alias or metric_biz_name,
                    "field_expr": field_expr,
                }

                # 如果提取到了聚合信息，添加到结果中
                if agg_info:
                    result["aggregation"] = agg_info["aggregation"]
                    result["has_distinct"] = agg_info["has_distinct"]

                return result

        return None

    # 匹配聚合函数（含嵌套，如 ROUND(AVG(score), 2)）
    _AGG_PATTERN = re.compile(r"\b(COUNT|SUM|AVG|MAX|MIN)\s*\(", re.IGNORECASE)

    @staticmethod
    def _parse_group_by_fields(group_by_clause: str) -> set[str]:
        """
        解析GROUP BY子句，提取被分组的字段名（标准化后）

        Args:
            group_by_clause: GROUP BY子句内容

        Returns:
            标准化后的字段名集合
        """
        if not group_by_clause or not group_by_clause.strip():
            return set()

        clause = re.sub(r"^\s*GROUP\s+BY\s+", "", group_by_clause, flags=re.IGNORECASE).strip()
        fields = set()
        for part in clause.split(","):
            part = part.strip()
            if part:
                fields.add(QueriedFieldsExtractor._normalize_field_name(part))
        return fields

    @staticmethod
    def _is_metric_field(field_info: dict[str, str], group_by_fields: set[str]) -> bool:
        """
        判断一个未匹配字段是否为指标

        判断逻辑：
        1. 字段表达式包含聚合函数 → 指标
        2. 存在GROUP BY且该字段不在GROUP BY中 → 指标
        """
        field_expr = field_info["field_expr"]

        # 包含聚合函数 → 指标
        if QueriedFieldsExtractor._AGG_PATTERN.search(field_expr):
            return True

        # 有GROUP BY子句时，不在GROUP BY中的字段 → 指标
        if group_by_fields:
            normalized = QueriedFieldsExtractor._normalize_field_name(field_expr)
            alias = field_info.get("alias")
            if normalized not in group_by_fields and (not alias or alias not in group_by_fields):
                return True

        return False

    @staticmethod
    def extract_queried_fields(sql_components: dict[str, Any], semantic_layer: dict[str, Any], used_models: list[str]) -> dict[str, Any]:
        """
        从SQL组件和语义层中提取查询字段信息

        Args:
            sql_components: SQL组件（包含select、from、where等）
            semantic_layer: 语义层数据
            used_models: SQL中使用的模型列表

        Returns:
            包含维度和指标信息的字典
        """
        result = {"dimensions": [], "metrics": [], "unmatched_dimensions": [], "unmatched_metrics": []}

        # 解析SELECT子句
        select_clause = sql_components.get("select", "")
        if not select_clause:
            logger.warning("SQL组件中缺少select子句")
            return result

        parsed_fields = QueriedFieldsExtractor._parse_select_fields(select_clause)
        logger.info(f"从SELECT子句解析出 {len(parsed_fields)} 个字段")

        # 解析GROUP BY子句，用于辅助判断未匹配字段的类型
        group_by_fields = QueriedFieldsExtractor._parse_group_by_fields(sql_components.get("groupBy", ""))

        # 匹配每个字段
        for field_info in parsed_fields:
            # 先尝试匹配维度
            dim_match = QueriedFieldsExtractor._match_dimension(field_info, semantic_layer)
            if dim_match:
                result["dimensions"].append(dim_match)
                continue

            # 再尝试匹配指标
            metric_match = QueriedFieldsExtractor._match_metric(field_info, semantic_layer)
            if metric_match:
                result["metrics"].append(metric_match)
                continue

            # 未匹配字段：根据聚合函数和GROUP BY判断是维度还是指标
            unmatched = {"field_expr": field_info["field_expr"], "alias": field_info["alias"]}

            if QueriedFieldsExtractor._is_metric_field(field_info, group_by_fields):
                # 补充聚合信息
                agg_info = QueriedFieldsExtractor._extract_aggregation_info(field_info["field_expr"])
                if agg_info:
                    unmatched["aggregation"] = agg_info["aggregation"]
                    unmatched["has_distinct"] = agg_info["has_distinct"]
                result["unmatched_metrics"].append(unmatched)
            else:
                result["unmatched_dimensions"].append(unmatched)

        logger.info(f"字段匹配完成: 维度={len(result['dimensions'])}, 指标={len(result['metrics'])}, 未匹配维度={len(result['unmatched_dimensions'])}, 未匹配指标={len(result['unmatched_metrics'])}")

        return result

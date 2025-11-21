"""
权限条件注入辅助函数

用于将模型的行级权限条件注入到 SQL 组装器中
"""

from typing import Dict, Any, List
import logging

from api.service.askdata_service.util.build_model_permissions_map import convert_row_filter_to_sql_conditions

logger = logging.getLogger(__name__)


def apply_permissions_to_assembler(
        assembler,
        model_permissions_map: Dict[str, Dict[str, Any]],
        model_table_alias_mapping_list: List[Dict[str, Any]]
):
    """
    将所有模型的权限条件应用到 SQL 组装器

    Args:
        assembler: FlexibleSQLAssembler 实例
        model_permissions_map: 模型ID到权限信息的映射（从 build_model_permissions_map 获得）
        model_table_alias_mapping_list: 模型表别名映射列表

    Example:
        >>> model_permissions_map = {
        ...     "39216258580409344": {
        ...         "alias": "t4",
        ...         "table": "t_jzg_xxjl",
        ...         "rowFilter": {
        ...             "logicalOperator": "OR",
        ...             "rules": [
        ...                 {"expression": "byxx = '南京大学'"},
        ...                 {"expression": "age > 25"}
        ...             ]
        ...         }
        ...     }
        ... }
        >>> apply_permissions_to_assembler(assembler, model_permissions_map, mapping_list)
        # 将会添加 WHERE 条件: (t4.byxx = '南京大学' OR t4.age > 25)
    """
    if not model_permissions_map:
        logger.info("没有需要应用的权限条件")
        return

    for model_id, perm_info in model_permissions_map.items():
        alias = perm_info["alias"]
        table = perm_info["table"]
        row_filter = perm_info["rowFilter"]

        # 构建该模型的权限条件
        permission_condition = build_permission_condition(row_filter, alias)

        if permission_condition:
            logger.info(f"为表 {table}({alias}) 添加权限条件: {permission_condition}")
            # 添加为原始 WHERE 条件（已经是完整的条件，包含括号）
            assembler.add_raw_where(permission_condition)
        else:
            logger.warning(f"表 {table}({alias}) 的权限条件为空，跳过")


def build_permission_condition(row_filter: Dict[str, Any], table_alias: str) -> str:
    """
    构建单个模型的权限条件

    Args:
        row_filter: 行级权限过滤器
        table_alias: 表别名

    Returns:
        完整的 SQL 条件字符串，如果有多个规则会用括号包裹

    Example:
        >>> row_filter = {
        ...     "logicalOperator": "OR",
        ...     "rules": [
        ...         {"expression": "byxx = '南京大学'"},
        ...         {"expression": "age > 25"}
        ...     ]
        ... }
        >>> build_permission_condition(row_filter, "t4")
        "(t4.byxx = '南京大学' OR t4.age > 25)"

        >>> row_filter = {
        ...     "logicalOperator": "AND",
        ...     "rules": [
        ...         {"expression": "mc = '男'"}
        ...     ]
        ... }
        >>> build_permission_condition(row_filter, "t1")
        "(t1.mc = '男')"
    """

    logical_operator = row_filter.get("logicalOperator", "OR")

    # 转换所有规则为 SQL 条件
    conditions = convert_row_filter_to_sql_conditions(row_filter, table_alias)

    if not conditions:
        return ""

    # 如果只有一个条件，直接用括号包裹返回
    if len(conditions) == 1:
        return f"({conditions[0]})"

    # 多个条件时，用逻辑操作符连接，并用括号包裹
    # 每个条件也用括号包裹，确保优先级正确
    wrapped_conditions = [f"({cond})" for cond in conditions]
    combined = f" {logical_operator} ".join(wrapped_conditions)

    return f"({combined})"


def get_involved_model_ids_from_query(
        table_config: Dict[str, Any],
        all_semantic_fields: List[Dict[str, Any]],
        chart_type: str
) -> List[str]:
    """
    从查询配置中提取所有涉及的模型ID

    这个函数会遍历查询中使用的所有语义字段，提取它们所属的模型ID

    Args:
        table_config: 表配置
        all_semantic_fields: 所有语义字段列表
        chart_type: 图表类型

    Returns:
        涉及的模型ID列表（去重）
    """
    involved_model_ids = set()

    # 创建字段ID到模型ID的映射
    field_id_to_model_id = {}
    for field in all_semantic_fields:
        field_id_to_model_id[field["id"]] = field["from_model_id"]

    # 根据 chart_type 确定要检查的字段
    if chart_type == "table-row":
        fields_to_check = ["columns", "filters", "order_by"]
    else:  # table-aggr, bar, pie, line, area, matrix, bubble
        fields_to_check = ["dimensions", "metrics", "where_conditions", "having_conditions", "order_by"]

    # 遍历所有字段，提取模型ID
    for field_name in fields_to_check:
        items = table_config.get(field_name, [])
        for item in items:
            if item.get("is_semantic_field"):
                field_id = item.get("id")
                if field_id in field_id_to_model_id:
                    model_id = field_id_to_model_id[field_id]
                    involved_model_ids.add(model_id)

    return list(involved_model_ids)


# 测试示例
if __name__ == "__main__":
    # 测试 build_permission_condition
    print("=" * 60)
    print("测试: build_permission_condition")
    print("=" * 60)

    # 测试用例1：单个规则
    row_filter1 = {
        "logicalOperator": "OR",
        "rules": [
            {"expression": "mc = '男'"}
        ]
    }
    result1 = build_permission_condition(row_filter1, "t1")
    print(f"\n✓ 单个规则:")
    print(f"  输入: {row_filter1}")
    print(f"  结果: {result1}")
    print(f"  期望: (t1.mc = '男')")

    # 测试用例2：多个规则 - OR
    row_filter2 = {
        "logicalOperator": "OR",
        "rules": [
            {"expression": "byxx = '南京大学'"},
            {"expression": "age > 25"}
        ]
    }
    result2 = build_permission_condition(row_filter2, "t4")
    print(f"\n✓ 多个规则 (OR):")
    print(f"  输入: {row_filter2}")
    print(f"  结果: {result2}")
    print(f"  期望: ((t4.byxx = '南京大学') OR (t4.age > 25))")

    # 测试用例3：多个规则 - AND
    row_filter3 = {
        "logicalOperator": "AND",
        "rules": [
            {"expression": "byxx = '南京大学'"},
            {"expression": "age > 25"},
            {"expression": "status = 'active'"}
        ]
    }
    result3 = build_permission_condition(row_filter3, "t4")
    print(f"\n✓ 多个规则 (AND):")
    print(f"  输入: {row_filter3}")
    print(f"  结果: {result3}")
    print(f"  期望: ((t4.byxx = '南京大学') AND (t4.age > 25) AND (t4.status = 'active'))")

    print("\n" + "=" * 60)
    print("✅ 测试完成!")
    print("=" * 60)
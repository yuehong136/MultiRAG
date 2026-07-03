from typing import Any


def extract_manually_adjusted_field_ids(table_config: dict[str, Any]) -> list[str]:
    """
    从 table_config 中提取用户手动调整的语义字段 ID

    Args:
        table_config: 表配置对象

    Returns:
        用户手动调整的语义字段 ID 列表

    Example:
        >>> table_config = {
        ...     "chart_type": "table-aggr",
        ...     "where_conditions": [
        ...         {
        ...             "id": "39216187049700352",
        ...             "is_semantic_field": True,
        ...             "changed_manually_by_user": True
        ...         },
        ...         {
        ...             "id": "39216306412776448",
        ...             "is_semantic_field": True,
        ...             "added_manually_by_user": True
        ...         }
        ...     ]
        ... }
        >>> extract_manually_adjusted_field_ids(table_config)
        ['39216187049700352', '39216306412776448']
    """
    adjusted_field_ids = []
    chart_type = table_config.get("chart_type", "")

    # 根据图表类型确定要检查的字段列表
    if chart_type == "table-row":
        fields_to_check = ["columns", "filters", "order_by"]
    else:  # table-aggr, bar, pie, line, area, matrix, bubble 等
        fields_to_check = ["dimensions", "metrics", "where_conditions", "having_conditions", "order_by"]

    # 遍历所有需要检查的字段
    for field_name in fields_to_check:
        items = table_config.get(field_name, [])
        if not items:
            continue

        for item in items:
            # 检查是否为语义字段
            is_semantic = item.get("is_semantic_field", False)

            # 检查是否被用户手动调整
            added_manually = item.get("added_manually_by_user", False)
            changed_manually = item.get("changed_manually_by_user", False)

            # 如果是语义字段且被手动调整，则提取 ID
            if is_semantic and (added_manually or changed_manually):
                field_id = item.get("id")
                if field_id and field_id not in adjusted_field_ids:
                    adjusted_field_ids.append(field_id)

    return adjusted_field_ids

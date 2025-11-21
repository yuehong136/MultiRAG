from typing import Dict, Any, List


def build_model_permissions_map(
        permissions_response: Dict[str, Any],
        model_table_alias_mapping_list: List[Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    """
    从权限响应中提取每个模型的行级权限，构建模型ID到权限信息的映射

    Args:
        permissions_response: get_user_semantic_permissions_async 的返回结果
        model_table_alias_mapping_list: 模型表别名映射列表

    Returns:
        模型ID到权限信息的映射字典，格式：
        {
            "model_id": {
                "alias": "t1",
                "table": "t_jzg_xxjl",
                "rowFilter": {
                    "logicalOperator": "OR",
                    "rules": [
                        {
                            "permissionId": "xxx",
                            "expression": "byxx = '南京大学'",
                            "permissionName": "xxx"
                        }
                    ]
                }
            }
        }
    """
    model_permissions_map = {}

    # 获取权限数据
    data_permissions = permissions_response.get("dataPermissions", {})
    models_permissions = data_permissions.get("models", [])

    # 创建模型ID到别名和表名的映射
    model_id_to_info = {
        m["modelId"]: {"alias": m["alias"], "table": m["table"]}
        for m in model_table_alias_mapping_list
    }

    # 遍历所有有权限信息的模型
    for model_perm in models_permissions:
        model_id = model_perm.get("modelId")
        row_filter = model_perm.get("rowFilter")

        # 只处理有 rowFilter 的模型
        if model_id and row_filter and row_filter.get("rules"):
            # 获取该模型的表信息
            model_info = model_id_to_info.get(model_id, {})

            model_permissions_map[model_id] = {
                "alias": model_info.get("alias", ""),
                "table": model_info.get("table", ""),
                "rowFilter": row_filter
            }

    return model_permissions_map


def convert_row_filter_to_sql_conditions(
        row_filter: Dict[str, Any],
        table_alias: str
) -> List[str]:
    """
    将 rowFilter 转换为 SQL WHERE 条件列表

    Args:
        row_filter: 行级权限过滤器
        table_alias: 表别名

    Returns:
        SQL 条件表达式列表

    Example:
        >>> row_filter = {
        ...     "logicalOperator": "OR",
        ...     "rules": [
        ...         {"expression": "mc = '男'"},
        ...         {"expression": "dm = '001'"}
        ...     ]
        ... }
        >>> convert_row_filter_to_sql_conditions(row_filter, "t1")
        ["t1.mc = '男'", "t1.dm = '001'"]
    """
    rules = row_filter.get("rules", [])

    if not rules:
        return []

    sql_conditions = []

    for rule in rules:
        expression = rule.get("expression", "").strip()
        if not expression:
            continue

        # 为表达式中的字段添加表别名
        # 需要智能识别字段名，避免给已经有表别名的字段重复添加
        # 简单处理：如果表达式中的字段没有 "." 前缀，则添加表别名
        enhanced_expression = add_table_alias_to_expression(expression, table_alias)
        sql_conditions.append(enhanced_expression)

    return sql_conditions


def add_table_alias_to_expression(expression: str, table_alias: str) -> str:
    """
    为表达式中的字段添加表别名

    Args:
        expression: SQL 表达式，如 "mc = '男'" 或 "byxx = '南京大学'"
        table_alias: 表别名

    Returns:
        添加了表别名的表达式

    Example:
        >>> add_table_alias_to_expression("mc = '男'", "t1")
        "t1.mc = '男'"
        >>> add_table_alias_to_expression("t2.mc = '男'", "t1")
        "t2.mc = '男'"  # 已有别名，不修改
    """
    import re

    # 匹配字段名的模式：字母开头，后跟字母、数字或下划线
    # 使用负向前瞻确保前面不是点号
    pattern = r'(?<![a-zA-Z0-9_.])\b([a-zA-Z_][a-zA-Z0-9_]*)\b'

    def replace_field(match):
        field_name = match.group(1)
        start_pos = match.start()

        # 跳过 SQL 关键字和函数名
        sql_keywords = {
            'AND', 'OR', 'NOT', 'IN', 'LIKE', 'BETWEEN', 'IS', 'NULL',
            'TRUE', 'FALSE', 'CAST', 'DATE', 'COUNT', 'SUM', 'AVG', 'MAX', 'MIN'
        }

        if field_name.upper() in sql_keywords:
            return field_name

        # 检查是否在引号内（简单检查）
        before_match = expression[:start_pos]
        single_quotes = before_match.count("'")
        double_quotes = before_match.count('"')

        # 如果在引号内，不添加别名
        if single_quotes % 2 != 0 or double_quotes % 2 != 0:
            return field_name

        # 检查后面是否跟着点号（说明已经是表别名的一部分）
        # 例如: "t2" 在 "t2.mc" 中
        after_match = expression[match.end():]
        if after_match.startswith('.'):
            return field_name

        # 添加表别名
        return f"{table_alias}.{field_name}"

    return re.sub(pattern, replace_field, expression)


# 测试示例
if __name__ == "__main__":
    # 测试 add_table_alias_to_expression
    print(add_table_alias_to_expression("mc = '男'", "t1"))
    # 输出: t1.mc = '男'

    print(add_table_alias_to_expression("byxx = '南京大学' AND age > 30", "t4"))
    # 输出: t4.byxx = '南京大学' AND t4.age > 30

    print(add_table_alias_to_expression("t2.mc = '男'", "t1"))
    # 输出: t2.mc = '男' (已有别名，不修改)

    # 测试 convert_row_filter_to_sql_conditions
    row_filter = {
        "logicalOperator": "OR",
        "rules": [
            {"expression": "mc = '男'"},
            {"expression": "dm = '001'"}
        ]
    }
    conditions = convert_row_filter_to_sql_conditions(row_filter, "t1")
    print(conditions)
    # 输出: ["t1.mc = '男'", "t1.dm = '001'"]
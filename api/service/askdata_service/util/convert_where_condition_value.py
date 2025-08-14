from typing import Any

from api.service.askdata_service.util.parse_sql_in_values import parse_sql_in_values


def convert_where_condition_value(value: Any, field_type: str, operator: str) -> Any:
    """
    根据字段类型和操作符转换WHERE条件的值

    Args:
        value: 原始值
        field_type: 字段数据类型，如 "int", "varchar", "date", "float" 等
        operator: 操作符，如 "=", "IN", ">" 等

    Returns:
        转换后的值
    """
    # IN 操作符特殊处理
    if operator == "IN":
        if isinstance(value, str):
            # 解析 IN 值字符串
            parsed_values = parse_sql_in_values(value)
            # 对每个值进行类型转换
            return [_convert_single_value(v, field_type) for v in parsed_values]
        elif isinstance(value, list):
            # 如果已经是列表，对每个元素进行转换
            return [_convert_single_value(v, field_type) for v in value]
        else:
            return value
    else:
        # 非 IN 操作符，转换单个值
        return _convert_single_value(value, field_type)


def _convert_single_value(value: Any, field_type: str) -> Any:
    """
    转换单个值

    Args:
        value: 要转换的值
        field_type: 字段数据类型

    Returns:
        转换后的值
    """
    if not isinstance(value, str):
        return value

    field_type_lower = field_type.lower()

    try:
        # 整数类型
        if any(int_type in field_type_lower for int_type in ['int', 'integer', 'bigint', 'smallint', 'tinyint']):
            return int(value)

        # 浮点数类型
        elif any(float_type in field_type_lower for float_type in ['float', 'double', 'decimal', 'numeric', 'real']):
            float_val = float(value)
            # 如果是整数值，保持为 float（因为字段类型是浮点型）
            return float_val

        # 布尔类型
        elif any(bool_type in field_type_lower for bool_type in ['bool', 'boolean']):
            if value.lower() in ('true', '1', 'yes', 'on'):
                return True
            elif value.lower() in ('false', '0', 'no', 'off'):
                return False
            else:
                return value  # 保持原值，让数据库处理

        # 日期时间类型 - 保持字符串，让数据库处理
        elif any(date_type in field_type_lower for date_type in ['date', 'time', 'timestamp', 'datetime']):
            return value

        # 字符串类型 - 保持原值
        else:
            return value

    except (ValueError, TypeError):
        # 转换失败时返回原值
        return value


def process_where_condition(where_condition: dict, semantic_field: dict, table_alias: str):
    """
    处理WHERE条件，包含完整的值转换逻辑

    Args:
        where_condition: WHERE条件字典
        semantic_field: 语义字段信息
        table_alias: 表别名

    Returns:
        tuple: (column_name, operator, converted_value, needs_special_handling, special_sql)
    """
    field_type = semantic_field['field_detail']['dataType']
    column_name = f"{table_alias}.{semantic_field['semantic_field_name']}"

    # 如果有原始SQL组件，使用它
    if where_condition.get("original_sql_component", {}).get("field"):
        column_name = where_condition['original_sql_component']['field']

    operator = where_condition["operator"]
    value = where_condition["value"]

    # 转换值
    converted_value = convert_where_condition_value(value, field_type, operator)

    # 检查是否需要特殊处理（如日期类型的 CAST）
    field_type_lower = field_type.lower()
    if any(date_type in field_type_lower for date_type in ['date']) and operator not in ["IN"]:
        # 需要 CAST 处理
        special_sql = f"{column_name} {operator} CAST(%s AS DATE)"
        return column_name, operator, converted_value, True, special_sql

    return column_name, operator, converted_value, False, None


# 使用示例和测试
if __name__ == "__main__":
    # 测试 convert_where_value
    test_cases = [
        # 整数类型
        ("123", "int", "=", 123),
        ("456", "integer", ">", 456),

        # 浮点数类型
        ("123.45", "float", "=", 123.45),
        ("100.0", "decimal", "<", 100.0),

        # 布尔类型
        ("true", "boolean", "=", True),
        ("false", "bool", "=", False),
        ("1", "boolean", "=", True),

        # 日期类型
        ("2023-12-01", "date", "=", "2023-12-01"),
        ("2023-12-01 10:30:00", "datetime", ">", "2023-12-01 10:30:00"),

        # 字符串类型
        ("hello", "varchar", "=", "hello"),
        ("world", "text", "LIKE", "world"),

        # IN 操作符
        ("('学院A', '学院B')", "varchar", "IN", ["学院A", "学院B"]),
        ("('1', '2', '3')", "int", "IN", [1, 2, 3]),
        ("('1.1', '2.2')", "float", "IN", [1.1, 2.2]),
    ]

    print("值转换测试:")
    print("-" * 80)

    for value, field_type, operator, expected in test_cases:
        result = convert_where_condition_value(value, field_type, operator)
        status = "✓" if result == expected else "✗"
        print(f"{status} {field_type:<10} {operator:<5} {str(value):<25} -> {result}")
        if result != expected:
            print(f"   期望: {expected}")

    print("\n" + "=" * 80)
    print("完整流程测试:")

    # 模拟完整的处理流程
    mock_where_condition = {
        "operator": "IN",
        "value": "('物理学院', '数学学院')",
        "original_sql_component": {}
    }

    mock_semantic_field = {
        'semantic_field_name': 'college_name',
        'field_detail': {'dataType': 'varchar'}
    }

    column_name, operator, converted_value, needs_special, special_sql = process_where_condition(
        mock_where_condition, mock_semantic_field, "t"
    )

    print(f"列名: {column_name}")
    print(f"操作符: {operator}")
    print(f"转换后的值: {converted_value}")
    print(f"需要特殊处理: {needs_special}")
    if needs_special:
        print(f"特殊SQL: {special_sql}")
import re
from typing import Any


def convert_aggregation_value(column_name: str, value: Any) -> Any:
    """
    根据SQL聚合函数类型智能转换值

    Args:
        column_name: 包含聚合函数的列名，如 "COUNT(id)", "SUM(amount)", "MAX(date)"
        value: 需要转换的值

    Returns:
        转换后的值
    """
    if not isinstance(value, str):
        return value

    # 提取函数名（不区分大小写）
    func_match = re.match(r"(\w+)\s*\(", column_name.upper().strip())
    if not func_match:
        return value

    func_name = func_match.group(1)

    try:
        if func_name == "COUNT":
            # COUNT 总是返回整数
            return int(value)

        elif func_name in ["SUM", "AVG"]:
            # SUM 和 AVG 可能是整数或浮点数
            # 先尝试转为 float
            float_val = float(value)
            # 如果是整数值，转为 int（避免 1.0 这种表示）
            if float_val.is_integer():
                return int(float_val)
            return float_val

        elif func_name in ["MAX", "MIN"]:
            # MAX 和 MIN 需要智能判断数据类型
            return _smart_convert_value(value)

        else:
            # 其他聚合函数（如自定义函数），保持原值
            return value

    except (ValueError, TypeError):
        # 转换失败时返回原值
        return value


def _smart_convert_value(value: str) -> Any:
    """
    智能转换值，尝试推断最合适的数据类型

    Args:
        value: 字符串值

    Returns:
        转换后的值
    """
    value = value.strip()

    # 尝试转为整数
    try:
        if "." not in value and "e" not in value.lower():
            return int(value)
    except ValueError:
        pass

    # 尝试转为浮点数
    try:
        float_val = float(value)
        # 如果是整数值，返回整数
        if float_val.is_integer():
            return int(float_val)
        return float_val
    except ValueError:
        pass

    # 尝试转为布尔值
    if value.lower() in ("true", "false"):
        return value.lower() == "true"

    # 日期格式检查（简单的格式）
    date_patterns = [
        r"^\d{4}-\d{2}-\d{2}$",  # YYYY-MM-DD
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",  # YYYY-MM-DD HH:MM:SS
        r"^\d{2}/\d{2}/\d{4}$",  # MM/DD/YYYY
    ]

    for pattern in date_patterns:
        if re.match(pattern, value):
            return value  # 保持字符串格式，让数据库处理

    # 默认返回原字符串
    return value


# 使用示例和测试
if __name__ == "__main__":
    test_cases = [
        # COUNT 测试
        ("COUNT(id)", "123", 123),
        ("count(user_id)", "0", 0),
        # SUM 测试
        ("SUM(amount)", "1000", 1000),
        ("SUM(price)", "123.45", 123.45),
        ("SUM(quantity)", "100.0", 100),
        # AVG 测试
        ("AVG(score)", "85.5", 85.5),
        ("AVG(age)", "25.0", 25),
        # MAX/MIN 测试
        ("MAX(price)", "999.99", 999.99),
        ("MAX(price)", "1000", 1000),
        ("MAX(created_at)", "2023-12-01", "2023-12-01"),
        ("MIN(name)", "Alice", "Alice"),
        ("MAX(is_active)", "true", True),
        # 边界情况
        ("CUSTOM_FUNC(field)", "123", "123"),  # 未知函数保持原值
        ("COUNT(id)", 123, 123),  # 非字符串输入
        ("SUM(amount)", "invalid", "invalid"),  # 无效数值
    ]

    print("测试结果:")
    print("-" * 60)

    for column_name, input_value, expected in test_cases:
        result = convert_aggregation_value(column_name, input_value)
        status = "✓" if result == expected else "✗"
        print(f"{status} {column_name:<20} {input_value!s:<15} -> {result} ({type(result).__name__})")
        if result != expected:
            print(f"   期望: {expected} ({type(expected).__name__})")

    print("\n" + "=" * 60)
    print("实际使用示例:")

    # 模拟你的代码使用场景
    test_expressions = [
        ("COUNT(t.teacher_id)", "150"),
        ("SUM(t.salary)", "45000.50"),
        ("AVG(s.score)", "87.3"),
        ("MAX(s.birth_date)", "1995-03-15"),
        ("MIN(s.name)", "Alice"),
    ]

    for expr, val in test_expressions:
        converted = convert_aggregation_value(expr, val)
        print(f"{expr:<25} {val:<15} -> {converted} ({type(converted).__name__})")

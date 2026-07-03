import re


def add_table_alias_to_fields(expressions: str | list[str], table_alias: str) -> str | list[str]:
    """
    为SQL表达式中的字段添加表别名前缀

    Args:
        expressions: SQL表达式，可以是单个字符串或字符串列表
                    例如: "count(teacher_id)", ["count(teacher_id)", "max(birth_date)"]
        table_alias: 表别名字符串
                    例如: "t", "user_info"

    Returns:
        添加了表别名前缀的SQL表达式，类型与输入的expressions相同
    """

    def process_single_expression(expr: str) -> str:
        """处理单个SQL表达式"""
        # 匹配SQL函数中的字段名
        # 匹配模式：函数名(字段名) 或 函数名(表名.字段名)
        pattern = r"(\w+)\s*\(\s*(\w+(?:\.\w+)?)\s*\)"

        def replace_field(match):
            func_name = match.group(1)  # 函数名如count, max等
            field_part = match.group(2)  # 字段部分

            # 如果已经有表名前缀（包含点号），保持原样
            if "." in field_part:
                return match.group(0)
            else:
                # 没有表名前缀的情况，添加表别名
                field_name = field_part
                return f"{func_name}({table_alias}.{field_name})"

        return re.sub(pattern, replace_field, expr)

    # 根据输入类型处理
    if isinstance(expressions, str):
        return process_single_expression(expressions)
    elif isinstance(expressions, list):
        return [process_single_expression(expr) for expr in expressions]
    else:
        raise ValueError("expressions必须是字符串或字符串列表")


# 使用示例
if __name__ == "__main__":
    # 示例1: 表达式列表
    expressions = ["count(teacher_id)", "max(birth_date)", "avg(salary)", "sum(score)"]
    table_alias = "t"

    result = add_table_alias_to_fields(expressions, table_alias)
    print("示例1结果:")
    for i, expr in enumerate(expressions):
        print(f"  {expr} -> {result[i]}")

    print("\n" + "=" * 50 + "\n")

    # 示例2: 单个表达式
    single_expr = "count(teacher_id)"
    single_result = add_table_alias_to_fields(single_expr, "user")
    print(f"示例2结果: {single_expr} -> {single_result}")

    print("\n" + "=" * 50 + "\n")

    # 示例3: 已经有表前缀的表达式（保持原样）
    expressions_with_prefix = ["count(t.teacher_id)", "max(birth_date)", "avg(other_table.salary)"]
    result_with_prefix = add_table_alias_to_fields(expressions_with_prefix, "main")
    print("示例3结果:")
    for i, expr in enumerate(expressions_with_prefix):
        print(f"  {expr} -> {result_with_prefix[i]}")

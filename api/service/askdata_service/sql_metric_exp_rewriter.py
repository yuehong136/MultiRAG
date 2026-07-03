import re


class SQLFieldAliasProcessor:
    """SQL表达式字段别名处理器"""

    def __init__(self):
        # 常见的聚合函数
        self.aggregation_functions = [
            'SUM', 'AVG', 'COUNT', 'MAX', 'MIN', 'STDDEV', 'VARIANCE', 'VAR',
            'GROUP_CONCAT', 'STRING_AGG', 'LISTAGG', 'ARRAY_AGG', 'COLLECT'
        ]

        # SQL关键字
        self.sql_keywords = {
            'AND', 'OR', 'NOT', 'IN', 'LIKE', 'BETWEEN', 'IS', 'NULL', 'TRUE', 'FALSE',
            'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'IF', 'DISTINCT', 'ALL',
            'ASC', 'DESC', 'ORDER', 'BY', 'GROUP', 'HAVING', 'WHERE'
        }

        # 常见函数名
        self.common_functions = {
            'SUBSTRING', 'SUBSTR', 'LENGTH', 'UPPER', 'LOWER', 'TRIM', 'LTRIM', 'RTRIM',
            'CONCAT', 'REPLACE', 'LEFT', 'RIGHT', 'REVERSE', 'REPEAT', 'SPACE',
            'ABS', 'CEIL', 'FLOOR', 'ROUND', 'SQRT', 'POWER', 'MOD', 'RAND', 'RANDOM',
            'COALESCE', 'ISNULL', 'NULLIF', 'CAST', 'CONVERT', 'DATE', 'TIME', 'YEAR',
            'MONTH', 'DAY', 'HOUR', 'MINUTE', 'SECOND', 'NOW', 'GETDATE', 'CURRENT_TIMESTAMP'
        }

    def add_table_alias_to_expression(self, expression: str, table_alias: str) -> str:
        """
        为表达式中的字段添加表别名

        Args:
            expression: SQL表达式，如 "COUNT(DISTINCT user_id)"
            table_alias: 表别名，如 "t1"

        Returns:
            添加别名后的表达式，如 "COUNT(DISTINCT t1.user_id)"
        """
        # 提取所有需要添加别名的字段
        fields_to_replace = self._extract_fields_for_alias(expression)

        # 按字段长度倒序排列，避免短字段名替换长字段名的一部分
        fields_to_replace = sorted(fields_to_replace, key=len, reverse=True)

        result = expression
        for field in fields_to_replace:
            # 构建替换模式，确保只替换完整的字段名
            pattern = self._build_field_replacement_pattern(field)
            replacement = f"{table_alias}.{field}"
            result = re.sub(pattern, replacement, result)

        return result

    def _extract_fields_for_alias(self, expression: str) -> list[str]:
        """提取需要添加别名的字段"""
        # 临时移除字符串常量
        temp_expr = self._remove_string_literals(expression)

        # 字段模式：字母开头，可包含字母、数字、下划线，但不包含已有别名的字段
        field_pattern = r'\b[a-zA-Z_][a-zA-Z0-9_]*\b'
        potential_fields = re.findall(field_pattern, temp_expr, re.IGNORECASE)

        valid_fields = []
        for field in potential_fields:
            if self._is_valid_field_for_alias(field, temp_expr):
                valid_fields.append(field)

        # 去重并保持顺序
        seen = set()
        result = []
        for field in valid_fields:
            if field not in seen:
                seen.add(field)
                result.append(field)

        return result

    def _is_valid_field_for_alias(self, field: str, expression: str) -> bool:
        """判断字段是否需要添加别名"""
        field_upper = field.upper()

        # 排除SQL关键字
        if field_upper in self.sql_keywords:
            return False

        # 排除聚合函数名
        if field_upper in self.aggregation_functions:
            return False

        # 排除常见函数名
        if field_upper in self.common_functions:
            return False

        # 排除已经有别名的字段（包含点号的）
        if f".{field}" in expression:
            return False

        # 排除紧跟在函数名后面的字段（避免误识别函数名）
        func_pattern = rf'\b(?:{"|".join(self.aggregation_functions + list(self.common_functions))})\s*\(\s*{re.escape(field)}'
        if re.search(func_pattern, expression, re.IGNORECASE) and field_upper in (
                self.aggregation_functions + list(self.common_functions)):
            return False

        return True

    def _build_field_replacement_pattern(self, field: str) -> str:
        """构建字段替换的正则模式"""
        # 确保只替换完整的字段名，不替换字段名的一部分
        # 使用负向前瞻和负向后瞻确保字段前后不是字母、数字或下划线
        escaped_field = re.escape(field)
        return rf'(?<![a-zA-Z0-9_.])\b{escaped_field}\b(?![a-zA-Z0-9_.])'

    def _remove_string_literals(self, expression: str) -> str:
        """移除字符串常量"""
        # 移除单引号字符串
        expr = re.sub(r"'[^']*'", "''", expression)
        # 移除双引号字符串
        expr = re.sub(r'"[^"]*"', '""', expr)
        return expr

    def extract_fields_from_expression(self, expression: str) -> list[str]:
        """
        从表达式中提取字段名（用于调试和验证）

        Args:
            expression: SQL表达式

        Returns:
            提取到的字段名列表
        """
        return self._extract_fields_for_alias(expression)


# 便捷函数
def add_table_alias(expression: str, table_alias: str) -> str:
    """
    便捷函数：为SQL表达式中的字段添加表别名

    Args:
        expression: SQL表达式，如 "COUNT(DISTINCT user_id)"
        table_alias: 表别名，如 "t1"

    Returns:
        添加别名后的表达式，如 "COUNT(DISTINCT t1.user_id)"
    """
    processor = SQLFieldAliasProcessor()
    return processor.add_table_alias_to_expression(expression, table_alias)


# 测试函数
def test_alias_addition():
    """测试用例"""
    processor = SQLFieldAliasProcessor()

    test_cases = [
        ("COUNT(DISTINCT user_id)", "t1"),
        ("SUM(price * quantity)", "t1"),
        ("AVG(score)", "t2"),
        ("MAX(created_date)", "u"),
        ("SUM(CASE WHEN status = 'active' THEN amount ELSE 0 END)", "o"),
        ("COUNT(id) + SUM(total)", "t1"),
        ("AVG(rating) - MIN(min_rating)", "r"),
        ("SUM(t2.amount + discount)", "t1"),  # 部分已有别名
        ("GROUP_CONCAT(DISTINCT category SEPARATOR ',')", "p"),
        ("COUNT(*)", "t1"),  # 通配符，不应该被替换
        ("COALESCE(name, 'Unknown')", "u"),
        ("SUBSTRING(description, 1, 100)", "p")
    ]

    print("测试结果：")
    print("-" * 70)

    for expression, alias in test_cases:
        # 先显示提取的字段
        fields = processor.extract_fields_from_expression(expression)
        # 然后显示添加别名后的结果
        result = processor.add_table_alias_to_expression(expression, alias)

        print(f"原表达式: {expression}")
        print(f"提取字段: {fields}")
        print(f"表别名:   {alias}")
        print(f"结果:     {result}")
        print()


if __name__ == "__main__":
    test_alias_addition()

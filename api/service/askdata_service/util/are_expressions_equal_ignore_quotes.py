import re


def are_expressions_equal_ignore_quotes(expr1, expr2):
    """
    判断两个表达式是否等价，忽略引号差异
    主要处理单引号、双引号和无引号的情况

    Args:
        expr1 (str): 第一个表达式
        expr2 (str): 第二个表达式

    Returns:
        bool: 如果两个表达式忽略引号后相等返回True，否则返回False
    """

    def normalize_expression(expr):
        """
        标准化SQL表达式：
        1. 去除首尾空格
        2. 转换为小写
        3. 统一引号格式（将单引号和双引号都替换为标准格式）
        4. 处理括号内外的空格
        """
        # 去除首尾空格并转小写
        expr = expr.strip().lower()

        # 使用正则表达式匹配函数调用格式：function_name(quoted_field)
        # 匹配模式：函数名 + 括号 + 可能的引号 + 字段名 + 可能的引号 + 括号
        pattern = r'(\w+)\s*\(\s*["\']?(\w+)["\']?\s*\)'
        match = re.match(pattern, expr)

        if match:
            function_name = match.group(1)
            field_name = match.group(2)
            # 返回标准化格式
            return f"{function_name}({field_name})"

        # 如果不匹配函数调用格式，则进行基本的引号标准化
        # 将所有的单引号和双引号替换为空（用于比较字段名）
        expr = re.sub(r'["\']', '', expr)
        # 移除多余空格
        expr = re.sub(r'\s+', ' ', expr)

        return expr.strip()

    # 标准化两个表达式并比较
    norm_expr1 = normalize_expression(expr1)
    norm_expr2 = normalize_expression(expr2)

    return norm_expr1 == norm_expr2


# 测试用例
if __name__ == "__main__":
    test_cases = [
        ("count(teacher_id)", 'COUNT("teacher_id")', True),
        ("count(teacher_id)", "COUNT('teacher_id')", True),
        ('COUNT("teacher_id")', "count('teacher_id')", True),
        ("sum(student_id)", 'COUNT("teacher_id")', False),
        ("count(teacher_id)", "count(student_id)", False),
        ("  COUNT  (  'teacher_id'  )  ", 'count("teacher_id")', True),
        ("avg(score)", "AVG('score')", True),
        ("max(age)", "min(age)", False),
    ]

    print("测试结果：")
    for expr1, expr2, expected in test_cases:
        result = are_expressions_equal_ignore_quotes(expr1, expr2)
        status = "✓" if result == expected else "✗"
        print(f"{status} '{expr1}' vs '{expr2}' -> {result} (期望: {expected})")

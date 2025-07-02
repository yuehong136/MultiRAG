import re


def parse_sql_in_values(input_string):
    """
    解析SQL中IN语句的值，形如 "('物理学院', '数学学院', '经济管理学院', '化学工程学院')" 的字符串

    Args:
        input_string (str): 要解析的SQL IN值字符串

    Returns:
        list: 解析出的字符串数组
    """
    # 使用正则表达式匹配单引号内的内容
    # 模式说明：'([^']*)'  匹配单引号包围的任意非单引号字符
    pattern = r"'([^']*)'"

    # 找到所有匹配的内容
    matches = re.findall(pattern, input_string)

    return matches


# 测试示例
if __name__ == "__main__":
    # 测试用例1
    test_string1 = "('物理学院', '数学学院', '经济管理学院', '化学工程学院')"
    result1 = parse_sql_in_values(test_string1)
    print(f"输入: {test_string1}")
    print(f"输出: {result1}")
    print()

    # 测试用例2
    test_string2 = "('计算机科学与技术', '软件工程')"
    result2 = parse_sql_in_values(test_string2)
    print(f"输入: {test_string2}")
    print(f"输出: {result2}")
    print()

    # 测试用例3 - 空元组
    test_string3 = "()"
    result3 = parse_sql_in_values(test_string3)
    print(f"输入: {test_string3}")
    print(f"输出: {result3}")
    print()

    # 测试用例4 - 单个元素
    test_string4 = "('外国语学院',)"
    result4 = parse_sql_in_values(test_string4)
    print(f"输入: {test_string4}")
    print(f"输出: {result4}")

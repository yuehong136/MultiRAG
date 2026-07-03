# 常见的SQL聚合函数列表 (可以根据你使用的数据库方言进行增删)
# 列表已转为大写，以匹配后续处理逻辑
AGGREGATE_FUNCTIONS: list[str] = [
    "COUNT",
    "SUM",
    "AVG",
    "MIN",
    "MAX",
    "ARRAY_AGG",
    "STRING_AGG",
    "GROUP_CONCAT",
    "BIT_AND",
    "BIT_OR",
    "BIT_XOR",
    "STDDEV",
    "STDDEV_POP",
    "STDDEV_SAMP",
    "VARIANCE",
    "VAR_POP",
    "VAR_SAMP",
]


def find_aggregate_columns(selected_columns: list[str]) -> list[str]:
    """
    从给定的select列名列表中，识别并返回所有聚合列。

    Args:
        selected_columns: 一个包含从SQL的SELECT子句中提取出的列的列表。

    Returns:
        一个只包含被识别为聚合列的列表。
    """
    aggregate_columns = []
    for col_expr in selected_columns:
        # 将列表达式的开头部分移除前后空格并转为大写，以便与我们的列表比较
        # 例如 '  count(*) as c' -> 'COUNT(*) AS C'
        trimmed_expr = col_expr.strip().upper()

        # 检查表达式是否以列表中的某个聚合函数和左括号'('开头
        for func in AGGREGATE_FUNCTIONS:
            if trimmed_expr.startswith(func + "("):
                aggregate_columns.append(col_expr)
                # 找到一个匹配后，就可以停止对当前列的检查，继续检查下一列
                break

    return aggregate_columns


# 使用 __name__ == '__main__' 是Python的最佳实践，确保代码只在直接运行时执行
if __name__ == "__main__":
    print("--- 开始进行聚合列识别测试 ---\n")

    # 定义一系列测试用例
    test_cases = {
        "原始问题": {"input": ["EXTRACT(YEAR FROM t1.hire_date) AS hire_year", "COUNT(*) AS teacher_count"], "expected": ["COUNT(*) AS teacher_count"]},
        "简单聚合": {"input": ["SUM(salary)", "department_id"], "expected": ["SUM(salary)"]},
        "包含大小写和空格": {"input": ["  avg( score ) AS average_score", "student_name"], "expected": ["  avg( score ) AS average_score"]},
        "多个聚合和普通列": {"input": ["department", "MAX(hire_date)", "MIN(salary)"], "expected": ["MAX(hire_date)", "MIN(salary)"]},
        "不含聚合列": {"input": ["id", "name", "email"], "expected": []},
        "包含非聚合函数": {"input": ["LOWER(name)", "TRIM(email)", "MAX(login_attempts)"], "expected": ["MAX(login_attempts)"]},
        "空列表输入": {"input": [], "expected": []},
        "一个已知的局限（窗口函数）": {"input": ["name", "COUNT(*) OVER (PARTITION BY department)"], "expected": ["COUNT(*) OVER (PARTITION BY department)"]},
    }

    # 遍历并执行所有测试用例
    all_passed = True
    for test_name, case in test_cases.items():
        print(f"--- 测试: {test_name} ---")
        input_cols = case["input"]
        expected_cols = case["expected"]

        print(f"输入: {input_cols}")

        # 调用我们的核心函数进行识别
        actual_cols = find_aggregate_columns(input_cols)

        print(f"识别结果: {actual_cols}")
        print(f"期望结果: {expected_cols}")

        # 检查结果是否与期望一致
        if sorted(actual_cols) == sorted(expected_cols):
            print("✅ 测试通过\n")
        else:
            print("❌ 测试失败\n")
            all_passed = False

    print("--- 所有测试完成 ---")
    if all_passed:
        print("🎉 恭喜！所有定义的测试用例均已通过！")
    else:
        print("⚠️ 部分测试用例未通过，请检查代码逻辑。")

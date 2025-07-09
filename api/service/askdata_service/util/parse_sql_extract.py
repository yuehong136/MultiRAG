import re

def parse_sql_extract(extract_string: str) -> dict:
    """
    解析 SQL EXTRACT 函数字符串，提取时间粒度 (unit) 和涉及到的字段 (source)。

    Args:
        extract_string (str): 包含 EXTRACT 函数的 SQL 字符串，例如 "EXTRACT(YEAR FROM order_date)"。

    Returns:
        dict: 包含 'unit' 和 'source' 键的字典。
              如果无法解析，则返回 {'unit': None, 'source': None}。
    """
    # 匹配 EXTRACT(UNIT FROM SOURCE) 模式
    # pattern 解释：
    # EXTRACT\s*\(           - 匹配 "EXTRACT("，并允许中间有空格
    # (\w+)                  - 捕获组 1：匹配一个或多个单词字符，作为 UNIT (例如 YEAR, MONTH)
    # \s+FROM\s+             - 匹配 " FROM "，并允许中间有空格
    # ([a-zA-Z0-9_.]+)      - 捕获组 2：匹配一个或多个字母、数字、下划线或点，作为 SOURCE (例如 order_date, table.column)
    # \)                     - 匹配 ")"
    pattern = re.compile(r"EXTRACT\s*\((\w+)\s+FROM\s+([a-zA-Z0-9_.]+)\)", re.IGNORECASE)

    match = pattern.search(extract_string)

    if match:
        unit = match.group(1).upper()  # 将 unit 转换为大写，保持一致性
        source = match.group(2)
        return {'unit': unit, 'source': source}
    else:
        return {'unit': None, 'source': None}

# --- 示例用法 ---
if __name__ == "__main__":
    # 常见用例
    print(parse_sql_extract("EXTRACT(YEAR FROM hire_date)"))
    # 输出: {'unit': 'YEAR', 'source': 'order_date'}

    print(parse_sql_extract("EXTRACT(MONTH FROM creation_timestamp)"))
    # 输出: {'unit': 'MONTH', 'source': 'creation_timestamp'}

    print(parse_sql_extract("EXTRACT(HOUR FROM my_table.event_time)"))
    # 输出: {'unit': 'HOUR', 'source': 'my_table.event_time'}

    # 包含空格的用例
    print(parse_sql_extract("  EXTRACT (  DAY   FROM   purchase_date  )  "))
    # 输出: {'unit': 'DAY', 'source': 'purchase_date'}

    # 大小写不敏感测试
    print(parse_sql_extract("extract(quarter from sale_date)"))
    # 输出: {'unit': 'QUARTER', 'source': 'sale_date'}

    # 无法解析的用例
    print(parse_sql_extract("NOT_AN_EXTRACT_FUNCTION"))
    # 输出: {'unit': None, 'source': None}

    print(parse_sql_extract("EXTRACT(FROM order_date)")) # 缺少 unit
    # 输出: {'unit': None, 'source': None}

    print(parse_sql_extract("EXTRACT(YEAR FROM)")) # 缺少 source
    # 输出: {'unit': None, 'source': None}
import re


def extract_sql(text):
    """
    从文本中提取SELECT查询语句

    Args:
        text (str): 包含SQL语句的文本

    Returns:
        str: 提取到的SELECT查询语句，如果没有找到则返回空字符串
    """
    # 匹配以下两种情况：
    # 1. 被```sql和```包围的SELECT语句
    # 2. 直接的SELECT语句

    # 首先尝试匹配markdown代码块中的SQL
    sql_block_pattern = r"```sql\s*(SELECT.*?)\s*```"
    matches = re.findall(sql_block_pattern, text, re.DOTALL | re.IGNORECASE)

    if matches:
        return matches[0].strip()

    # 如果没有找到代码块，尝试直接匹配SELECT语句
    direct_sql_pattern = r"SELECT.*?(?:;|$)"
    matches = re.findall(direct_sql_pattern, text, re.DOTALL | re.IGNORECASE)

    if matches:
        return matches[0].strip()

    return ""


def test_extractor():
    """
    测试SQL提取函数
    """
    # 测试用例1：markdown代码块中的SELECT语句
    test_text1 = """
    ```sql
    SELECT xb, COUNT(*) AS count
    FROM t_jzg_basic
    GROUP BY xb;
    ```
    """

    # 测试用例2：带注释的SELECT语句
    test_text2 = """
    根据用户查询"查询教职工男女比例"，我们可以直接从表t_jzg_basic中统计性别为男和女的数量。以下是相应的SQL查询语句：
    ```sql
    -- 查询教职工男女比例
    SELECT
        xb, -- 性别
        COUNT(*) AS number -- 统计每个性别的教职工数量
    FROM
        t_jzg_basic
    WHERE
        xb IN ('男', '女') -- 只统计性别为男和女的记录
    GROUP BY
        xb; -- 按性别分组
    ```
    """

    # 测试用例3：直接的SELECT语句
    test_text3 = """
    SELECT xb, COUNT(*) AS count
    FROM t_jzg_basic
    GROUP BY xb;
    """

    print("测试用例1结果:")
    print(extract_sql(test_text1))
    print("\n测试用例2结果:")
    print(extract_sql(test_text2))
    print("\n测试用例3结果:")
    print(extract_sql(test_text3))


# 运行测试
if __name__ == "__main__":
    test_extractor()

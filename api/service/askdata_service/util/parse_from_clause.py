from typing import Any


def parse_from_clause(from_sentence: str) -> dict[str, Any]:
    """
    解析 FROM 子句，提取主表和所有已存在的表

    Args:
        from_sentence: FROM 子句内容（不包含 FROM 关键字）
        例如: ' t_jzg_jbxx t1 LEFT JOIN t_code_dw t2 ON t1.szdw = t2.dm'

    Returns:
        包含主表和所有表列表的字典
        {
            "main_table": "t_jzg_jbxx",
            "existing_tables": ["t_jzg_jbxx", "t_code_dw", "t_code_xb"]
        }
    """
    import re

    # 清理字符串
    from_sentence = from_sentence.strip()

    if not from_sentence:
        return {"main_table": None, "existing_tables": []}

    tables = []

    # 按各种 JOIN 关键字分割
    # 支持: LEFT JOIN, RIGHT JOIN, INNER JOIN, OUTER JOIN, FULL JOIN, JOIN
    join_pattern = r"\s+(?:LEFT\s+(?:OUTER\s+)?JOIN|RIGHT\s+(?:OUTER\s+)?JOIN|INNER\s+JOIN|OUTER\s+JOIN|FULL\s+(?:OUTER\s+)?JOIN|CROSS\s+JOIN|JOIN)\s+"
    parts = re.split(join_pattern, from_sentence, flags=re.IGNORECASE)

    for part in parts:
        if not part.strip():
            continue

        # 移除 ON 条件部分（ON 后面的内容）
        part_without_on = re.split(r"\s+ON\s+", part, maxsplit=1, flags=re.IGNORECASE)[0].strip()

        if part_without_on:
            # 提取表名（第一个词，忽略别名）
            # 使用正则匹配，处理可能的特殊字符（如反引号、方括号等）
            tokens = part_without_on.split()
            if tokens:
                # 表名是第一个 token，可能包含引号或方括号
                table_name = tokens[0].strip("`[]\"'")
                tables.append(table_name)

    return {"main_table": tables[0] if tables else None, "existing_tables": tables}

"""
SQL 标识符（表名/别名/列名/`table.col` 表达式）归一化比较工具。

背景：LLM 生成的 SQL 常给标识符加引号（`"t_jzg_jbxx"`、`"nl"`、`[col]`、`` `col` ``），
而语义层 schema 里的名字是裸名（`t_jzg_jbxx`、`nl`、`t_jzg_jbxx.nl`）。原先 table_config_generator
多处用「带引号 vs 不带引号」直接 == 比较，全部 mismatch → 字段被误判为非语义、表被误判为新增模型
（生成幻影别名），最终导致分页 re-query 报 `integer > character varying` / 悬空别名。

本模块提供统一的去引号 + 大小写无关比较，作为这些标识符匹配点的唯一归一化口径。
仓内原本没有可导入的「全引号」strip 助手（are_expressions_equal_ignore_quotes 只处理 ' 和 "，
sql_assembler 内的 clean_quotes 是不可导入的闭包），字符集沿用 parse_from_clause.py 的 `[]"' 。
"""

# 标识符可能携带的引号字符：反引号、双引号、单引号、方括号
_QUOTE_CHARS = "`\"'[]"


def strip_identifier_quotes(s: str | None) -> str | None:
    """
    去掉标识符两端的引号；对 `a.b`（含 `"a"."b"`）按 `.` 逐段去引号后再拼回。

    仅供「标识符比较」使用，不改变任何用于构造输出 SQL 的字符串。

        >>> strip_identifier_quotes('"t_jzg_jbxx"')
        't_jzg_jbxx'
        >>> strip_identifier_quotes('"t_jzg_jbxx"."nl"')
        't_jzg_jbxx.nl'
        >>> strip_identifier_quotes('[t1].[col]')
        't1.col'
        >>> strip_identifier_quotes('nl')
        'nl'
    """
    if s is None:
        return None
    # 不含 '.' 的普通标识符直接去引号；含 '.' 的限定名逐段处理，避免把段内的引号留下
    if "." not in s:
        return s.strip().strip(_QUOTE_CHARS)
    return ".".join(seg.strip().strip(_QUOTE_CHARS) for seg in s.split("."))


def identifiers_equal(a: str | None, b: str | None) -> bool:
    """
    去引号 + 大小写无关地比较两个标识符 / `table.col` 表达式是否等价。

    顺手解决历史上 `metric['expression'].lower() == table_name + '.' + column_name.lower()`
    这类只对一侧/一段做 .lower() 的大小写比较 bug —— 这里两侧整体 casefold。
    """
    if a is None or b is None:
        return False
    return strip_identifier_quotes(a).casefold() == strip_identifier_quotes(b).casefold()

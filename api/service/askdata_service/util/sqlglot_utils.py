from __future__ import annotations

from typing import Any

try:
    import sqlglot
    from sqlglot import exp
except Exception:
    sqlglot = None
    exp = None


def normalize_sql_components(components: dict[str, Any] | None) -> dict[str, Any] | None:
    if not components:
        return None

    normalized: dict[str, Any] = {}
    keyword_map = {
        "select": "SELECT",
        "from": "FROM",
        "where": "WHERE",
        "groupBy": "GROUP BY",
        "having": "HAVING",
        "orderBy": "ORDER BY",
    }

    for key, keyword in keyword_map.items():
        value = components.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            value = str(value)
        value = value.strip()
        if value and not value.upper().startswith(keyword):
            value = f"{keyword} {value}"
        normalized[key] = value

    pagination = components.get("pagination")
    limit = ""
    offset = ""
    if isinstance(pagination, dict):
        limit = str(pagination.get("limit", "")).strip()
        offset = str(pagination.get("offset", "")).strip()
    else:
        limit = str(components.get("limit", "")).strip()
        offset = str(components.get("offset", "")).strip()

    normalized["pagination"] = {
        "limit": limit if limit else "",
        "offset": offset if offset else "",
    }

    return normalized


def _extract_int(expr_value: Any, dialect: str) -> int | None:
    if expr_value is None:
        return None
    if exp is not None and isinstance(expr_value, exp.Literal) and expr_value.is_int:
        try:
            return int(expr_value.this)
        except Exception:
            return None
    text = ""
    try:
        text = expr_value.sql(dialect=dialect)
    except Exception:
        text = str(expr_value)
    text = text.strip()
    return int(text) if text.isdigit() else None


def try_extract_components(sql_query: str, dialect: str = "postgres") -> dict[str, Any] | None:
    if not sqlglot or not sql_query or not sql_query.strip():
        return None

    try:
        expression = sqlglot.parse_one(sql_query, read=dialect)
    except Exception:
        return None

    select_expr = expression if exp is not None and isinstance(expression, exp.Select) else expression.find(exp.Select)
    if not select_expr:
        return None

    components: dict[str, Any] = {
        "select": "",
        "from": "",
        "where": "",
        "groupBy": "",
        "having": "",
        "orderBy": "",
        "pagination": {"limit": "", "offset": ""},
    }

    if select_expr.expressions:
        select_sql = ", ".join(item.sql(dialect=dialect) for item in select_expr.expressions)
        components["select"] = f"SELECT {select_sql}" if select_sql else ""

    # sqlglot 将 FROM 主表存储在 "from_" key 中，JOIN 部分存储在 "joins" key 中
    from_expr = select_expr.args.get("from_")
    joins_expr = select_expr.args.get("joins")
    from_parts: list[str] = []
    if from_expr is not None:
        from_parts.append(from_expr.sql(dialect=dialect))
    if joins_expr:
        for join in joins_expr:
            from_parts.append(join.sql(dialect=dialect))
    if from_parts:
        components["from"] = " ".join(from_parts)

    where_expr = select_expr.args.get("where")
    if where_expr is not None:
        components["where"] = where_expr.sql(dialect=dialect)

    group_expr = select_expr.args.get("group")
    if group_expr is not None:
        components["groupBy"] = group_expr.sql(dialect=dialect)

    having_expr = select_expr.args.get("having")
    if having_expr is not None:
        components["having"] = having_expr.sql(dialect=dialect)

    order_expr = select_expr.args.get("order")
    if order_expr is not None:
        components["orderBy"] = order_expr.sql(dialect=dialect)

    limit_expr = select_expr.args.get("limit")
    offset_expr = select_expr.args.get("offset")

    if limit_expr is not None:
        limit_value = _extract_int(limit_expr.args.get("expression"), dialect)
        if limit_value is not None:
            components["pagination"]["limit"] = str(limit_value)

    if offset_expr is not None:
        offset_value = _extract_int(offset_expr.args.get("expression"), dialect)
        if offset_value is not None:
            components["pagination"]["offset"] = str(offset_value)

    return components


def try_apply_pagination(sql_query: str, limit: int, offset: int = 0, dialect: str = "postgres") -> str | None:
    if not sqlglot or not sql_query or not sql_query.strip():
        return None

    try:
        expression = sqlglot.parse_one(sql_query, read=dialect)
    except Exception:
        return None

    if not isinstance(expression, exp.Select):
        select_expr = expression.find(exp.Select)
        if select_expr is None:
            return None
        expression = select_expr

    try:
        updated = expression.limit(limit)
        if offset:
            updated = updated.offset(offset)
        return updated.sql(dialect=dialect)
    except Exception:
        return None

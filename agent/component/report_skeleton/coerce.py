"""
report_skeleton 解析用的低层强转 + 合法取值集合。移植自前端 designer/ai-skeleton/coerce.ts。
纯函数 / 常量,不依赖骨架结构本身。
"""

from __future__ import annotations

from typing import Any

# ---- 合法取值(与前端 types 的字面量联合一致)----
LAYOUTS = ["full", "two-column", "three-column", "sidebar-left", "sidebar-right"]
BLOCK_KINDS = [
    "heading",
    "paragraph",
    "callout",
    "list",
    "stat-card",
    "stat-card-group",
    "table",
    "comparison-matrix",
    "timeline",
    "chart",
]
CHART_TYPES = ["bar", "line", "area", "pie", "donut", "radar", "funnel", "scatter"]
VARIANTS = ["info", "success", "warning", "insight"]
TRENDS = ["up", "down", "neutral"]
CARTESIAN = {"bar", "line", "area"}
PROPORTION = {"pie", "donut", "funnel"}
SIDEBAR = {"sidebar-left", "sidebar-right"}


def is_obj(v: Any) -> bool:
    """对应 TS isObj:dict 才算对象(数组 / None 不算)。"""
    return isinstance(v, dict)


def to_str(v: Any, fallback: str = "") -> str:
    """对应 TS str():字符串原样;None → fallback;其余 → str()。"""
    if isinstance(v, str):
        return v
    return fallback if v is None else str(v)


def opt_str(v: Any) -> str | None:
    """对应 TS optStr:非空字符串才返回,否则 None。"""
    return v if isinstance(v, str) and v else None


def str_arr(v: Any) -> list[str]:
    """对应 TS strArr:数组逐项转字符串,非数组 → []。"""
    return [to_str(x) for x in v] if isinstance(v, list) else []


def one_of(v: Any, allowed: list[str], fallback: str) -> str:
    """对应 TS oneOf:命中白名单则取之,否则回落。"""
    return v if isinstance(v, str) and v in allowed else fallback


def opt_enum(v: Any, allowed: list[str]) -> str | None:
    """对应 TS optEnum:命中白名单返回,否则 None。"""
    return v if isinstance(v, str) and v in allowed else None

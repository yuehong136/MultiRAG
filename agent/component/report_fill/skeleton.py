"""
报告骨架内核(纯函数、零 IO):字段路径寻址 + 图表行键推导 + 确定性 merge。

移植自前端 `skeleton-utils.ts` 的「填值所需部分」。merge 不依赖随机/时间,保证
「同一骨架 + 同一填充值」永远产出同一 ReportSchema。

字段路径约定(见 docs/html-report README「字段路径约定」):
    'value' / 'title'        顶层字段
    'items[0].value'         数组下标 + 子字段
    'series[1].dataKey'      嵌套数组
    'data'                   chart 的整段数据数组
"""

from __future__ import annotations

import re
from typing import Any

OPEN_REGION = "open-region"

_INDEX_PATT = re.compile(r"\[(\d+)\]")


def is_open_region(block: dict[str, Any]) -> bool:
    """是否为生成区占位块(运行期不渲染,merge 时过滤)。"""
    return block.get("type") == OPEN_REGION


def parse_path(path: str) -> list[str | int]:
    """'items[0].value' -> ['items', 0, 'value'];全数字段视为数组下标。"""
    normalized = _INDEX_PATT.sub(r".\1", path)
    segments: list[str | int] = []
    for seg in normalized.split("."):
        if not seg:
            continue
        segments.append(int(seg) if seg.isdigit() else seg)
    return segments


def _set_in(target: Any, segments: list[str | int], value: Any) -> Any:
    """按路径段不可变写入;按需创建中间数组/对象(稀疏数组以 None 补位)。"""
    if not segments:
        return value
    head, rest = segments[0], segments[1:]
    if isinstance(head, int):
        arr = list(target) if isinstance(target, list) else []
        while len(arr) <= head:
            arr.append(None)
        arr[head] = _set_in(arr[head], rest, value)
        return arr
    obj = dict(target) if isinstance(target, dict) else {}
    obj[head] = _set_in(obj.get(head), rest, value)
    return obj


def set_field_value(target: Any, path: str, value: Any) -> Any:
    """按路径不可变写入,返回新对象。"""
    return _set_in(target, parse_path(path), value)


def chart_row_keys(block: dict[str, Any]) -> tuple[str, list[str]]:
    """
    图表行对象的字段名:类别键 + 一组数值键,由形状字段(xAxisKey / radarKeys /
    nameKey / valueKey / series)推导。填值导出行 schema、强转 chartData 都靠它——
    单一真源,避免漂移。与前端 `chartRowKeys` 一致。
    """
    f = block.get("fields") or {}
    radar = f.get("radarKeys") or []
    category = f.get("xAxisKey") or (radar[0] if radar else None) or f.get("nameKey") or "name"
    values: list[str] = []

    def add(key: Any) -> None:
        if key and key not in values:
            values.append(key)

    for series in f.get("series") or []:
        if series.get("xKey") and series.get("yKey"):
            add(series["xKey"])
            add(series["yKey"])
        elif series.get("dataKey"):
            add(series["dataKey"])
    if not values:
        add(f.get("valueKey") or "value")
    return category, values


_TREND_NUM_PATT = re.compile(r"^[+\-−]?\s*[\d,]+(?:\.\d+)?")


def _trend_from_change(change: Any) -> str | None:
    """变化率字符串 → 涨跌向(渲染器据此给红绿上色):带符号或前导数值才认,>0 up / <0 down / ==0 neutral。
    形如「持平」「—」等非数值文本认不出,返回 None(渲染落灰),宁可不上色也不上错色。"""
    if not isinstance(change, str):
        return None
    m = _TREND_NUM_PATT.match(change.strip())
    if not m:
        return None
    try:
        num = float(m.group(0).replace("−", "-").replace(",", "").replace(" ", ""))
    except ValueError:
        return None
    if num > 0:
        return "up"
    if num < 0:
        return "down"
    return "neutral"


def _apply_change_trend(node: dict[str, Any]) -> None:
    """指标卡填完 change 后据其符号补 trend;已显式带 trend 则保留(尊重作者/历史骨架的静态选择)。"""
    change = node.get("change")
    if not change or node.get("trend"):
        return
    trend = _trend_from_change(change)
    if trend:
        node["trend"] = trend


def merge_block(block: dict[str, Any], filled: dict[str, Any]) -> dict[str, Any]:
    """
    单个骨架 Block + 填好的叶子值 → 运行时 Block。以 `fields`(钉死的静态结构)为底,
    按路径覆盖填充值,最后回挂 `role`(渲染器侧栏布局靠 `role` 分主/侧列)。
    指标卡填完 change 后,按其正负号补 trend(渲染器据 trend 给变化率红绿上色)。
    """
    result: dict[str, Any] = dict(block.get("fields") or {})
    result["id"] = block.get("id")
    result["type"] = block.get("type")
    for path, value in filled.items():
        result = set_field_value(result, path, value)
    btype = result.get("type")
    if btype == "stat-card":
        _apply_change_trend(result)
    elif btype == "stat-card-group":
        for item in result.get("items") or []:
            if isinstance(item, dict):
                _apply_change_trend(item)
    if block.get("role"):
        result["role"] = block["role"]
    return result


def merge_skeleton(
    skeleton: dict[str, Any],
    filled_by_block: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """整份骨架 + 各 Block 的填充值 → 完整 ReportSchema(annotation 不进运行时)。"""
    sections_out: list[dict[str, Any]] = []
    for section in skeleton.get("sections") or []:
        blocks_out = [merge_block(block, filled_by_block.get(block.get("id"), {})) for block in (section.get("blocks") or []) if not is_open_region(block)]
        sec: dict[str, Any] = {
            "id": section.get("id"),
            "layout": section.get("layout"),
            "blocks": blocks_out,
        }
        if section.get("title") is not None:
            sec["title"] = section.get("title")
        if section.get("subtitle") is not None:
            sec["subtitle"] = section.get("subtitle")
        sections_out.append(sec)

    out: dict[str, Any] = {"title": skeleton.get("title"), "sections": sections_out}
    if skeleton.get("subtitle") is not None:
        out["subtitle"] = skeleton.get("subtitle")
    if skeleton.get("theme") is not None:
        out["theme"] = skeleton.get("theme")
    return out

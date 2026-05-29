"""
把 LLM 给的「扁平块」JSON 归一成骨架 Block(框架静态 / 内容 llm 指令)。移植自前端
designer/ai-skeleton/build-block.ts + block-defaults.ts 的 buildChartFields。

「纯模板」:结构性字段落 `fields` 静态;变量内容转 `fieldDirectives` 的 `llm` 指令。
提示词(hint)按块归属落位、不重复:
 - 单内容块(段落 / 标注 / 单指标卡):落到该内容字段的 `llm.hint`,不写块级 annotation。
 - 多字段块(图表 / 表格 / 对比 / 指标卡组 / 时间线 / 列表):落到块级 annotation,
   其整段 / 逐项指令留空 `llm()`(运行时回落到 annotation)。
缺失框架用默认兜底,不丢弃块(尤其图表不再因缺数据被丢)。heading 不产出(节标题已是抬头)。
"""

from __future__ import annotations

import uuid
from typing import Any

from .coerce import (
    BLOCK_KINDS,
    CARTESIAN,
    CHART_TYPES,
    PROPORTION,
    TRENDS,
    VARIANTS,
    is_obj,
    one_of,
    opt_enum,
    opt_str,
    str_arr,
    to_str,
)

# 显示块级注解的多槽块(与前端 block-meta.ts 的 ANNOTATABLE_BLOCKS 一致)。
ANNOTATABLE_BLOCKS = {"chart", "table", "comparison-matrix", "stat-card-group", "timeline", "list"}


def make_id(prefix: str) -> str:
    """生成 Section / Block 的稳定唯一 id(对应前端 makeId,后端用 uuid)。"""
    return f"{prefix}-{uuid.uuid4().hex[:7]}"


def _llm(hint: str | None = None) -> dict[str, str]:
    """内容字段的 llm 填充指令;hint 为空则省略(运行时回落到 block/section annotation)。"""
    if hint and hint.strip():
        return {"mode": "llm", "hint": hint}
    return {"mode": "llm"}


def build_chart_fields(chart_type: str) -> dict[str, Any]:
    """按图表类型造默认形状字段(形状全 static,data 留空待指令填)。移植自 block-defaults.ts。"""
    base: dict[str, Any] = {"type": "chart", "chartType": chart_type, "data": []}
    if chart_type in ("pie", "donut", "funnel"):
        return {**base, "nameKey": "name", "valueKey": "value"}
    if chart_type == "radar":
        return {**base, "radarKeys": ["dimension"], "series": [{"dataKey": "value"}]}
    if chart_type == "scatter":
        return {**base, "series": [{"dataKey": "points", "xKey": "x", "yKey": "y"}]}
    return {**base, "xAxisKey": "x", "series": [{"dataKey": "y"}]}


def _norm_series(v: Any) -> list[dict[str, Any]]:
    """chart 系列的形状键(不含数据);保留命名键,丢弃非法项。"""
    if not isinstance(v, list):
        return []
    out: list[dict[str, Any]] = []
    for s in v:
        if not is_obj(s):
            continue
        item: dict[str, Any] = {"dataKey": to_str(s.get("dataKey"))}
        if opt_str(s.get("name")):
            item["name"] = s["name"]
        if opt_str(s.get("xKey")):
            item["xKey"] = s["xKey"]
        if opt_str(s.get("yKey")):
            item["yKey"] = s["yKey"]
        out.append(item)
    return out


def _build_chart(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """chart:形状键静态(缺则 build_chart_fields 兜底),data 作整段 llm 指令(说明落块注解)。"""
    chart_type = opt_enum(raw.get("chartType"), CHART_TYPES) or "bar"
    fields = dict(build_chart_fields(chart_type))
    if opt_str(raw.get("title")):
        fields["title"] = raw["title"]
    if chart_type in CARTESIAN:
        if opt_str(raw.get("xAxisKey")):
            fields["xAxisKey"] = raw["xAxisKey"]
        series = _norm_series(raw.get("series"))
        if series:
            fields["series"] = series
    elif chart_type in PROPORTION:
        if opt_str(raw.get("nameKey")):
            fields["nameKey"] = raw["nameKey"]
        if opt_str(raw.get("valueKey")):
            fields["valueKey"] = raw["valueKey"]
    elif chart_type == "radar":
        radar_keys = str_arr(raw.get("radarKeys"))
        if radar_keys:
            fields["radarKeys"] = radar_keys
        series = _norm_series(raw.get("series"))
        if series:
            fields["series"] = series
    else:  # scatter
        series = [s for s in _norm_series(raw.get("series")) if isinstance(s.get("xKey"), str) and isinstance(s.get("yKey"), str)]
        if series:
            fields["series"] = series
    return fields, {"data": _llm()}


def _build_block(btype: str, raw: dict[str, Any], hint: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """按块类型造「框架 fields + 内容 directives」。"""
    if btype == "callout":
        fields: dict[str, Any] = {"type": btype, "variant": one_of(raw.get("variant"), VARIANTS, "info")}
        if opt_str(raw.get("title")):
            fields["title"] = raw["title"]
        return fields, {"content": _llm(hint)}
    if btype == "list":
        items = str_arr(raw.get("items"))
        slots = items if items else [""]
        fields = {"type": btype, "ordered": raw.get("ordered") is True, "items": slots}
        if opt_str(raw.get("title")):
            fields["title"] = raw["title"]
        directives = {f"items[{i}]": _llm(txt) for i, txt in enumerate(slots)}
        return fields, directives
    if btype == "stat-card":
        fields = {"type": btype, "label": to_str(raw.get("label"))}
        trend = opt_enum(raw.get("trend"), TRENDS)
        if trend:
            fields["trend"] = trend
        directives = {"value": _llm(hint)}
        change = opt_str(raw.get("change"))
        if change:  # 源料确有对比 → 加变化率空槽(填值期按符号推导 trend 红绿上色)
            directives["change"] = _llm(change)
        return fields, directives
    if btype == "stat-card-group":
        raws = [it for it in raw.get("items", []) if is_obj(it)] if isinstance(raw.get("items"), list) else []
        sources = raws if raws else [{}]
        items: list[dict[str, Any]] = []
        directives: dict[str, Any] = {}
        for i, it in enumerate(sources):
            card: dict[str, Any] = {"label": to_str(it.get("label"))}
            trend = opt_enum(it.get("trend"), TRENDS)
            if trend:
                card["trend"] = trend
            items.append(card)
            directives[f"items[{i}].value"] = _llm()
            change = opt_str(it.get("change"))
            if change:  # 该 KPI 源料有对比 → 加变化率空槽
                directives[f"items[{i}].change"] = _llm(change)
        return {"type": btype, "items": items}, directives
    if btype == "table":
        headers = str_arr(raw.get("headers"))
        fields = {"type": btype, "headers": headers if headers else ["", ""]}
        if opt_str(raw.get("title")):
            fields["title"] = raw["title"]
        return fields, {"rows": _llm()}
    if btype == "comparison-matrix":
        items_list = str_arr(raw.get("items"))
        fields = {"type": btype, "items": items_list if items_list else ["", ""]}
        if opt_str(raw.get("title")):
            fields["title"] = raw["title"]
        return fields, {"criteria": _llm()}
    if btype == "timeline":
        raws = [it for it in raw.get("items", []) if is_obj(it)] if isinstance(raw.get("items"), list) else []
        sources = raws if raws else [{}]
        items = [{"date": to_str(it.get("date"))} for it in sources]
        fields = {"type": btype, "items": items}
        if opt_str(raw.get("title")):
            fields["title"] = raw["title"]
        directives = {f"items[{i}].title": _llm() for i in range(len(items))}
        return fields, directives
    if btype == "chart":
        return _build_chart(raw)
    # paragraph(及未知类型兜底)
    return {"type": "paragraph"}, {"content": _llm(hint)}


def normalize_block(raw: Any, sidebar: bool) -> dict[str, Any] | None:
    """扁平块 → 骨架 Block;非对象返回 None(其余一律兜底,不丢)。heading 不产出。"""
    if not is_obj(raw):
        return None
    btype = one_of(raw.get("type"), BLOCK_KINDS, "paragraph")
    # AI 生成不产出独立标题块:小节 title/subtitle 已渲染为该节抬头,再来个 heading 会重复。
    if btype == "heading":
        return None
    hint = opt_str(raw.get("hint"))
    fields, directives = _build_block(btype, raw, hint)
    block: dict[str, Any] = {"id": make_id("blk"), "type": btype, "fields": fields}
    if directives:
        block["fieldDirectives"] = directives
    if sidebar:
        block["role"] = "side" if raw.get("role") == "side" else "main"
    # 多字段块:整块说明落 annotation(单内容块的 hint 已在字段指令上,避免重复)。
    if hint and btype in ANNOTATABLE_BLOCKS:
        block["annotation"] = hint
    return block

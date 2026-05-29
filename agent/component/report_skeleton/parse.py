"""
把 LLM 文本解析成模板结构。移植自前端 designer/ai-skeleton/parse.ts。三个入口:
- parse_outline           大纲调用产物:有序的节(标题/布局/意图),不含块。
- parse_section           单节调用产物:`{blocks:[...]}` → 一个 SkeletonSection。
- parse_skeleton_response 单次整篇生成产物(大纲失败时的回退路径)。

块归一(框架静态 / 内容 llm 指令)共用 build_block.normalize_block。LLM 的 JSON 不可靠:可能带
markdown 围栏 / 散文、字段缺失、枚举非法,这里防御式处理(json 失败再用 json_repair 兜底)。
"""

from __future__ import annotations

import json
import re
from typing import Any

import json_repair

from .build_block import make_id, normalize_block
from .coerce import LAYOUTS, SIDEBAR, is_obj, one_of, opt_str, str_arr, to_str

# 默认主题(与前端 constants.ts 的 DEFAULT_THEME 对齐)。模型不产出 theme(契约无该字段),
# 故 normalize_theme 实际总回落到此;骨架返回前端后由预设选择器决定最终主题。
DEFAULT_THEME = {
    "primaryColor": "#1677ff",
    "colorPalette": ["#1677ff", "#36cfc9", "#ffc53d", "#ff7a45", "#9254de"],
}


class SkeletonParseError(Exception):
    """解析失败(无法定位 / 解析 JSON,或归一化后无任何合法内容)。"""


def _extract_json(raw: str) -> Any:
    """剥 <think>…</think> 与 markdown 围栏,定位首尾花括号 → JSON;json 失败用 json_repair 兜底。"""
    text = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE)
    text = re.sub(r"<think>[\s\S]*$", "", text, flags=re.IGNORECASE).strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise SkeletonParseError("no JSON object found in model output")
    snippet = text[start : end + 1]
    try:
        return json.loads(snippet)
    except Exception:
        try:
            return json_repair.loads(snippet)
        except Exception as exc:
            raise SkeletonParseError("model output is not valid JSON") from exc


# ============================================================
# 大纲(第①步)
# ============================================================


def parse_outline(raw: str) -> dict[str, Any]:
    """文本 → 报告大纲(有序的节);无合法节抛 SkeletonParseError。"""
    obj = _extract_json(raw)
    if not is_obj(obj):
        raise SkeletonParseError("outline is not an object")
    sections_raw = obj.get("sections") if isinstance(obj.get("sections"), list) else []
    sections: list[dict[str, Any]] = []
    for s in sections_raw:
        if not is_obj(s):
            continue
        out: dict[str, Any] = {"layout": one_of(s.get("layout"), LAYOUTS, "full")}
        title = opt_str(s.get("title"))
        if title:
            out["title"] = title
        intent = opt_str(s.get("intent"))
        if intent:
            out["intent"] = intent
        sections.append(out)
    if not sections:
        raise SkeletonParseError("outline has no sections")
    return {"title": to_str(obj.get("title")), "sections": sections}


# ============================================================
# 单节(第②步)
# ============================================================


def parse_section(raw: str, outline: dict[str, Any]) -> dict[str, Any]:
    """`{blocks:[...]}` + 大纲里的节信息 → 一个 SkeletonSection。"""
    obj = _extract_json(raw)
    blocks_raw = obj.get("blocks") if is_obj(obj) and isinstance(obj.get("blocks"), list) else []
    sidebar = outline.get("layout") in SIDEBAR
    blocks = [nb for b in blocks_raw if (nb := normalize_block(b, sidebar)) is not None]
    if not blocks:
        raise SkeletonParseError("section has no valid blocks")
    section: dict[str, Any] = {"id": make_id("sec"), "layout": outline.get("layout"), "blocks": blocks}
    if outline.get("title"):
        section["title"] = outline["title"]
    if outline.get("intent"):
        section["annotation"] = outline["intent"]
    return section


# ============================================================
# 整篇(回退:大纲失败时单次生成)
# ============================================================


def _normalize_section(raw: Any) -> dict[str, Any] | None:
    if not is_obj(raw):
        return None
    layout = one_of(raw.get("layout"), LAYOUTS, "full")
    sidebar = layout in SIDEBAR
    blocks_raw = raw.get("blocks") if isinstance(raw.get("blocks"), list) else []
    blocks = [nb for b in blocks_raw if (nb := normalize_block(b, sidebar)) is not None]
    if not blocks:
        return None
    section: dict[str, Any] = {"id": make_id("sec"), "layout": layout, "blocks": blocks}
    if opt_str(raw.get("title")):
        section["title"] = raw["title"]
    if opt_str(raw.get("subtitle")):
        section["subtitle"] = raw["subtitle"]
    if opt_str(raw.get("annotation")):
        section["annotation"] = raw["annotation"]
    return section


def _normalize_theme(v: Any) -> dict[str, Any]:
    if not is_obj(v):
        return dict(DEFAULT_THEME)
    theme: dict[str, Any] = {}
    primary = opt_str(v.get("primaryColor"))
    if primary:
        theme["primaryColor"] = primary
    palette = str_arr(v.get("colorPalette"))
    if palette:
        theme["colorPalette"] = palette
    return theme if theme else dict(DEFAULT_THEME)


def parse_skeleton_response(raw: str) -> dict[str, Any]:
    """文本 → 合法 SkeletonSchema 模板(整篇);失败抛 SkeletonParseError。"""
    obj = _extract_json(raw)
    if not is_obj(obj):
        raise SkeletonParseError("model output is not an object")
    sections_raw = obj.get("sections") if isinstance(obj.get("sections"), list) else []
    sections = [ns for s in sections_raw if (ns := _normalize_section(s)) is not None]
    if not sections:
        raise SkeletonParseError("model output has no valid sections")
    skeleton: dict[str, Any] = {
        "title": to_str(obj.get("title")),
        "sections": sections,
        "theme": _normalize_theme(obj.get("theme")),
    }
    if opt_str(obj.get("subtitle")):
        skeleton["subtitle"] = obj["subtitle"]
    return skeleton

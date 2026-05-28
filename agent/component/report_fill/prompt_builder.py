"""
按节填值的提示词构建(纯函数)。移植自前端 `prompt-builder.ts` + `fill-doc.ts`。

一节一调:collect_fill_plan 收集本节所有 `llm` 空槽 → build_fill_schema 导出极小 JSON
Schema → build_fill_messages 拼「源料 + 本节框架可读清单 + 返回契约」。变量字段(`variable`)
与静态字段不在此列——前者由 fill 用 resolve_ref 直接解析,后者就地取骨架值。

回填键扁平化:f"{block_id}__{path}"(block_id 不含分隔符、path 无 `__`,故可安全拆分)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .skeleton import chart_row_keys

# 语义枚举合法值(与前端 types 的字面量联合一致);fallback 为模型给非法值时的回落。
VARIANT_VALUES = ["info", "success", "warning", "insight"]
TREND_VALUES = ["up", "down", "neutral"]

KEY_SEP = "__"

# 按节填空的 system 提示(面向模型,故用英文;移植自 fill-doc.ts 的 FILL_SYSTEM):
# 只补空槽、不改框架、严格回 JSON。
FILL_SYSTEM = """You fill in the blanks of ONE section of a report TEMPLATE, using the
source text provided. The template's structure — titles, table headers, chart axes/series, stat
labels, list/section layout — is FIXED. You do NOT change it; you only produce values for the
listed blank slots.

Rules:
- Return ONE JSON object whose keys are EXACTLY the slot keys listed, nothing more, nothing less.
- Each value MUST match the given schema type: a string; a string[][] (table rows); an array of
  row objects (chart data) using EXACTLY the given field keys; or one of the allowed enum values.
- For table rows, every row must have exactly the stated number of cells. For comparison criteria,
  "values" must have exactly the stated number of entries (column order preserved).
- A slot may carry guidance after an em-dash "—" in the list: follow it. It states what that slot's
  content must be (topic, ordering, units, tone). Honor it together with the section's overall focus.
- Write in the SAME LANGUAGE as the source text. Be concise and faithful to the source; never invent
  facts. If the source lacks a value, give the closest faithful summary rather than fabricating.
- Output ONLY the JSON object: no markdown code fences, no comments, no prose before or after."""


@dataclass
class ValueSpec:
    """某个空槽要模型产出的值的形状,决定 schema 与 fill 的强转。"""

    kind: str  # text | enum | rows | criteria | chartData
    options: list[str] = field(default_factory=list)
    fallback: str = ""
    columns: int = 1
    category: str = ""
    values: list[str] = field(default_factory=list)


@dataclass
class FillItem:
    key: str  # 扁平回填键 f"{block_id}__{path}"
    block_id: str
    path: str
    spec: ValueSpec
    description: str  # 字段提示 → 块注解 逐级回落(小节注解走整节口径行)


@dataclass
class FillPlan:
    items: list[FillItem] = field(default_factory=list)


def fill_key(block_id: str, path: str) -> str:
    return f"{block_id}{KEY_SEP}{path}"


def split_fill_key(key: str) -> tuple[str, str]:
    """拆回填键;block_id 不含分隔符,故按首个 `__` 切。"""
    i = key.find(KEY_SEP)
    if i == -1:
        return key, ""
    return key[:i], key[i + len(KEY_SEP) :]


def _read_arr(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _leaf_of(path: str) -> str:
    return path[path.rfind(".") + 1 :] if "." in path else path


def spec_for(block: dict[str, Any], path: str) -> ValueSpec:
    """由(块类型, 路径)推出该空槽的值形状。"""
    f = block.get("fields") or {}
    btype = block.get("type")
    if btype == "table" and path == "rows":
        return ValueSpec(kind="rows", columns=len(_read_arr(f.get("headers"))) or 1)
    if btype == "comparison-matrix" and path == "criteria":
        return ValueSpec(kind="criteria", columns=len(_read_arr(f.get("items"))) or 1)
    if btype == "chart" and path == "data":
        category, values = chart_row_keys(block)
        return ValueSpec(kind="chartData", category=category, values=values)
    leaf = _leaf_of(path)
    if leaf == "variant":
        return ValueSpec(kind="enum", options=list(VARIANT_VALUES), fallback="info")
    if leaf == "trend":
        return ValueSpec(kind="enum", options=list(TREND_VALUES), fallback="neutral")
    return ValueSpec(kind="text")


def collect_fill_plan(section: dict[str, Any]) -> FillPlan:
    """收集本节所有 `llm` 空槽 → 待填计划。"""
    items: list[FillItem] = []
    for block in section.get("blocks") or []:
        dirs = block.get("fieldDirectives") or {}
        for path, directive in dirs.items():
            if (directive or {}).get("mode") != "llm":
                continue
            # 逐槽说明:字段提示 → 块注解。小节注解不在此兜底——它已作为整节口径出现在
            # build_fill_messages 的 (about: …) 行,避免每个空槽重复同一句。
            hint = (directive.get("hint") or "").strip()
            annotation = (block.get("annotation") or "").strip()
            items.append(
                FillItem(
                    key=fill_key(block["id"], path),
                    block_id=block["id"],
                    path=path,
                    spec=spec_for(block, path),
                    description=hint or annotation or "",
                )
            )
    return FillPlan(items=items)


# ============================================================
# 极小 JSON Schema(前端当 prompt 契约,后端额外可当 response_format)
# ============================================================


def _schema_for_item(item: FillItem) -> dict[str, Any]:
    spec = item.spec
    desc = {"description": item.description} if item.description else {}
    if spec.kind == "text":
        return {"type": "string", **desc}
    if spec.kind == "enum":
        return {"type": "string", "enum": list(spec.options), **desc}
    if spec.kind == "rows":
        return {
            "type": "array",
            **desc,
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": spec.columns,
                "maxItems": spec.columns,
            },
        }
    if spec.kind == "criteria":
        return {
            "type": "array",
            **desc,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "values"],
                "properties": {
                    "name": {"type": "string"},
                    "values": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": spec.columns,
                        "maxItems": spec.columns,
                    },
                },
            },
        }
    # chartData
    properties: dict[str, Any] = {spec.category: {"type": "string"}}
    for v in spec.values:
        properties[v] = {"type": "number"}
    return {
        "type": "array",
        **desc,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": [spec.category, *spec.values],
            "properties": properties,
        },
    }


def build_fill_schema(plan: FillPlan) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [item.key for item in plan.items],
        "properties": {item.key: _schema_for_item(item) for item in plan.items},
    }


# ============================================================
# 可读清单(让模型看见本节框架,而非一串裸键)
# ============================================================


def _block_summary(block: dict[str, Any]) -> str:
    f = block.get("fields") or {}
    title = f.get("title")
    title_str = f' "{title}"' if isinstance(title, str) and title else ""
    btype = block.get("type")
    if btype == "callout":
        return f"callout{title_str}"
    if btype == "list":
        return f"{'numbered' if f.get('ordered') else 'bulleted'} list{title_str}"
    if btype == "stat-card":
        return f'stat-card "{f.get("label") or ""}"'
    if btype == "stat-card-group":
        return "stat-card group"
    if btype == "table":
        cols = ", ".join(str(h) for h in _read_arr(f.get("headers")))
        return f"table{title_str}, columns [{cols}]"
    if btype == "comparison-matrix":
        cols = ", ".join(str(i) for i in _read_arr(f.get("items")))
        return f"comparison{title_str}, columns [{cols}]"
    if btype == "timeline":
        return f"timeline{title_str}"
    if btype == "chart":
        return f"{f.get('chartType') or 'bar'} chart{title_str}"
    return str(btype)


def _humanize_path(path: str) -> str:
    out = re.sub(r"items\[(\d+)\]", lambda m: f"item {int(m.group(1)) + 1}", path)
    return out.replace(".", " ")


def _slot_hint(spec: ValueSpec) -> str:
    if spec.kind == "enum":
        return f" (one of: {' / '.join(spec.options)})"
    if spec.kind == "rows":
        return f" ({spec.columns} cells per row)"
    if spec.kind == "criteria":
        return f" (name + {spec.columns} values)"
    if spec.kind == "chartData":
        return f" (rows of {{{', '.join([spec.category, *spec.values])}}})"
    return ""


def describe_section(section: dict[str, Any], plan: FillPlan) -> str:
    """把本节有空槽的块列成「框架 + 槽位」可读清单。"""
    by_block: dict[str, list[FillItem]] = {}
    for item in plan.items:
        by_block.setdefault(item.block_id, []).append(item)
    lines: list[str] = []
    for block in section.get("blocks") or []:
        items = by_block.get(block.get("id"))
        if not items:
            continue
        lines.append(f"- {_block_summary(block)}:")
        for item in items:
            guide = f" — {item.description}" if item.description else ""
            lines.append(f"    · [{item.key}] {_humanize_path(item.path)}{_slot_hint(item.spec)}{guide}")
    return "\n".join(lines)


def build_fill_messages(
    *,
    report_title: str,
    section: dict[str, Any],
    source_text: str,
    toc_titles: list[str],
    plan: FillPlan,
    schema: dict[str, Any],
) -> list[dict[str, str]]:
    focus = f'"{section.get("title")}"' if section.get("title") else "this section"
    intent = f" (about: {section.get('annotation')})" if section.get("annotation") else ""
    toc = f"Report sections: {' / '.join(toc_titles)}\n" if toc_titles else ""
    user = "\n".join(
        [
            f"Source text:\n{source_text.strip()}",
            "---",
            f'Report: "{report_title}"',
            f"{toc}Fill ONLY the section {focus}{intent}.",
            "",
            "Slots to fill:",
            describe_section(section, plan),
            "",
            "Return ONE JSON object with EXACTLY these keys and matching types:",
            json.dumps(schema, ensure_ascii=False),
        ]
    )
    return [
        {"role": "system", "content": FILL_SYSTEM},
        {"role": "user", "content": user},
    ]

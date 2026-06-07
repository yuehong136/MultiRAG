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

# 按节填空的 system 提示(中文给模型;移植自 fill-doc.ts 的 FILL_SYSTEM):
# 只补空槽、不改框架、严格回 JSON。
FILL_SYSTEM = """你为报告模板的「一个小节」填空,依据是提供的源文。模板的结构 —— 标题、表头、图表
坐标轴/系列、指标卡标签、列表/小节布局 —— 是「固定」的。你不改它;你只为下面列出的空槽产出值。

规则:
- 只返回一个 JSON 对象,其键「恰好」是下面列出的槽键,不多不少。
- 每个值「必须」匹配给定的 schema 类型:一个 string;一个 string[][](表格行);一组行对象(图表
  数据),用「恰好」给定的字段键;或某个允许的枚举值之一。
- 表格行:每行必须恰好有所述数量的单元格。对比矩阵的 "values":必须恰好有所述数量的条目(保持列序)。
- 槽位在清单里可能在破折号 "—" 后带一段指引:遵循它。它说明该槽的内容必须是什么(主题、排序、单位、
  语气)。把它与整节的总体焦点一起遵守。
- 每个槽「只」填与「该块特定主题/角色」相符的内容。不要从源文别处抓无关数字来凑某个槽。若该块的特定
  主题在源文里没有数据,就把该槽留空(见下),而不是塞无关内容。
- 列表/项目符号:每一项必须是自洽的短语,把标签与其数值「绑在一起」(如「年度科研经费 18.6 亿元,
  同比增长 12.5%」),「绝不」是剥离了所度量对象的裸数字或裸比率(不是单独的「18.6 亿元」,也不是
  单独的「+12.5%」)。
- 用与源文「相同的语言」书写。简洁、忠于源文;绝不杜撰事实。若源文「确实」缺某个槽的数据,就为它返回
  一个「空值」—— 空字符串 ""(表格行/图表数据则为空数组 [])。不要道歉,不要写「未提及」/「无相关
  数据」/「暂无数据」/「数据未提及」,也不要复述标签。空槽会被自动丢弃 —— 源文对它无料时,留空才是对的。
- 指标卡的 "value"「只」是数字本身 —— 一个带单位或百分号的数(如「38600 人」「78%」「18.6 亿元」);
  「绝不」是一句话,「绝不」复述标签。写「38600 人」,而非「2025 年在校生总数 38600 人,较上年增长
  4.3%」。把任何环比对比放进该卡的 "change" 槽,作为带符号的比率(「+4.3%」/「−3.2%」),而非塞进 value。
- 只输出该 JSON 对象:没有 markdown 代码围栏、没有注释、前后没有散文。"""


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
    if btype in ("stat-card", "stat-card-group"):
        # 指标卡:value 是一个干净的数字,change 是带符号的变化率——别让模型填成整句。
        if leaf == "value":
            return ValueSpec(kind="metric")
        if leaf == "change":
            return ValueSpec(kind="change")
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
    if spec.kind in ("text", "metric", "change"):
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
    title_str = f"「{title}」" if isinstance(title, str) and title else ""
    btype = block.get("type")
    if btype == "callout":
        return f"提示框{title_str}"
    if btype == "list":
        return f"{'有序' if f.get('ordered') else '无序'}列表{title_str}"
    if btype == "stat-card":
        return f"指标卡「{f.get('label') or ''}」"
    if btype == "stat-card-group":
        return "指标卡组"
    if btype == "table":
        cols = ", ".join(str(h) for h in _read_arr(f.get("headers")))
        return f"表格{title_str},列 [{cols}]"
    if btype == "comparison-matrix":
        cols = ", ".join(str(i) for i in _read_arr(f.get("items")))
        return f"对比矩阵{title_str},列 [{cols}]"
    if btype == "timeline":
        return f"时间线{title_str}"
    if btype == "chart":
        return f"{f.get('chartType') or 'bar'} 图表{title_str}"
    return str(btype)


def _humanize_path(path: str) -> str:
    out = re.sub(r"items\[(\d+)\]", lambda m: f"第 {int(m.group(1)) + 1} 项", path)
    return out.replace(".", " ")


_ITEM_SLOT_PATT = re.compile(r"^items\[(\d+)\]\.(.+)$")


def _humanize_slot(block: dict[str, Any], path: str) -> str:
    """可读槽名。stat-card-group 的 item 槽额外缀上该项的静态 label,让模型按标签对号入座,
    而不是靠槽位顺序去猜哪一格配哪个指标(单张 stat-card 的 label 已由 _block_summary 给出)。"""
    base = _humanize_path(path)
    if block.get("type") != "stat-card-group":
        return base
    m = _ITEM_SLOT_PATT.match(path)
    if not m:
        return base
    idx = int(m.group(1))
    items = (block.get("fields") or {}).get("items")
    if not isinstance(items, list) or idx >= len(items):
        return base
    item = items[idx]
    label = item.get("label") if isinstance(item, dict) else None
    if not isinstance(label, str) or not label.strip():
        return base
    return f"第 {idx + 1} 项 ({label.strip()}) {_humanize_path(m.group(2))}"


def _slot_hint(spec: ValueSpec) -> str:
    if spec.kind == "enum":
        return f"(取以下之一:{' / '.join(spec.options)})"
    if spec.kind == "rows":
        return f"(每行 {spec.columns} 个单元格)"
    if spec.kind == "criteria":
        return f"(name + {spec.columns} 个 values)"
    if spec.kind == "chartData":
        return f"(行形如 {{{', '.join([spec.category, *spec.values])}}})"
    if spec.kind == "metric":
        return "(「只」要数字 —— 一个带单位或 % 的数,如「38600 人」/「78%」;不是一句话,不要复述标签)"
    if spec.kind == "change":
        return "(带符号的变化率,以 + 或 − 开头,如「+4.3%」/「−3.2%」)"
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
            lines.append(f"    · [{item.key}] {_humanize_slot(block, item.path)}{_slot_hint(item.spec)}{guide}")
    return "\n".join(lines)


def build_fill_messages(
    *,
    report_title: str,
    section: dict[str, Any],
    source_text: str,
    toc_titles: list[str],
    plan: FillPlan,
    schema: dict[str, Any],
    layout_first: bool = False,
) -> list[dict[str, str]]:
    lines = [f"源文:\n{source_text.strip()}", "---"]
    if layout_first:
        # 布局优先:报告名 / 小节标题 / 注解 / 目录都还是样报主题口径(标题另在运行时按源文重生成),
        # 对新主题是错的上下文,一律不喂——填值只依据源文 + 已按源文重建的槽位框架。
        lines.append("只填充本节。")
    else:
        focus = f"「{section.get('title')}」" if section.get("title") else "本节"
        intent = f"(主题:{section.get('annotation')})" if section.get("annotation") else ""
        toc = f"报告各节:{' / '.join(toc_titles)}\n" if toc_titles else ""
        lines.append(f"报告:「{report_title}」")
        lines.append(f"{toc}只填充{focus}{intent}这一节。")
    lines += [
        "",
        "待填槽位:",
        describe_section(section, plan),
        "",
        "返回一个 JSON 对象,键「恰好」是这些、类型匹配:",
        json.dumps(schema, ensure_ascii=False),
    ]
    return [
        {"role": "system", "content": FILL_SYSTEM},
        {"role": "user", "content": "\n".join(lines)},
    ]


# ============================================================
# 报告标题生成(titleDirective.mode=='llm' 时单独一调)
# ============================================================

# 只产标题、跟随源语言、忠于源文、严格回 JSON。
TITLE_SYSTEM = """你根据提供的源文,写出一篇报告的「标题」。

规则:
- 返回一个 JSON 对象:{"title": string} —— 别的什么都不要。
- 标题是一个简短的名词短语,点明报告的主题与类型(几个字),不是一句话,也不是对全文的复述。
- 用与源文「相同的语言」书写。忠于源文;绝不杜撰源文不支持的主题。
- 若提供了指引,遵循它。
- 只输出该 JSON 对象:没有 markdown 代码围栏、没有注释、没有散文。"""


def build_title_messages(
    *,
    source_text: str,
    hint: str,
    toc_titles: list[str],
) -> list[dict[str, str]]:
    """报告标题(模型态)单调一次的消息:源料 + 可选 guidance + 章节目录 → {"title": "..."}。"""
    guide = f"\n指引:{hint.strip()}" if hint.strip() else ""
    toc = f"\n报告各节:{' / '.join(toc_titles)}" if toc_titles else ""
    user = "\n".join(
        [
            f"源文:\n{source_text.strip()}",
            "---",
            f"写出报告标题。{guide}{toc}",
            "",
            '返回一个 JSON 对象:{"title": "..."}',
        ]
    )
    return [
        {"role": "system", "content": TITLE_SYSTEM},
        {"role": "user", "content": user},
    ]


# ============================================================
# 报告副标题生成(subtitleDirective.mode=='llm' 时单独一调)
# ============================================================

# 只产副标题(标题下方一行概述)、跟随源语言、忠于源文、严格回 JSON。
SUBTITLE_SYSTEM = """你根据提供的源文,写出一篇报告的「副标题」——放在大标题下方的一行概述。

规则:
- 返回一个 JSON 对象:{"subtitle": string} —— 别的什么都不要。
- 副标题是「一行」话(一个短句,不是一段),点出报告的核心结论或覆盖范围;它补充标题,
  不要照抄或复述标题,也不要罗列各章节。
- 用与源文「相同的语言」书写。忠于源文;绝不杜撰源文不支持的结论。
- 若提供了指引,遵循它。
- 只输出该 JSON 对象:没有 markdown 代码围栏、没有注释、没有散文。"""


def build_subtitle_messages(
    *,
    source_text: str,
    hint: str,
    report_title: str = "",
    toc_titles: list[str],
) -> list[dict[str, str]]:
    """报告副标题(模型态)单调一次的消息:源料 + 标题 + 可选 guidance + 章节目录 → {"subtitle": "..."}。"""
    guide = f"\n指引:{hint.strip()}" if hint.strip() else ""
    head = f"\n报告标题:{report_title.strip()}" if report_title.strip() else ""
    toc = f"\n报告各节:{' / '.join(toc_titles)}" if toc_titles else ""
    user = "\n".join(
        [
            f"源文:\n{source_text.strip()}",
            "---",
            f"写出报告副标题(标题下方的一行概述)。{head}{guide}{toc}",
            "",
            '返回一个 JSON 对象:{"subtitle": "..."}',
        ]
    )
    return [
        {"role": "system", "content": SUBTITLE_SYSTEM},
        {"role": "user", "content": user},
    ]


# ============================================================
# 小节标题批量重生成(布局优先:section.titleDirective.mode=='llm' 时单批一调)
# ============================================================

# 把模板现标题当「角色」,按源文逐节重命名;同序同量,严格回 JSON。
SECTION_TITLES_SYSTEM = """你为一个「新主题」重写报告模板各小节的标题,依据是源文。给你的是模板当前
各小节标题(「按顺序」)—— 把每个都当作那一节的「角色」(如概览、规模/指标节、展望节)。为每个产出一个
「新」标题,描述该节对「本」源文而言变成了什么。

规则:
- 返回一个 JSON 对象:{"titles": [string, ...]} —— 每个输入恰好一个标题,「顺序相同」。
- 每个标题是简短的名词短语(几个字),不是一句话。
- 「不要」带任何小节编号或序号前缀(如「一、」「1.」「(一)」「Part 1」)—— 只输出裸标题。
- 用与源文「相同的语言」书写。忠于源文;绝不杜撰源文不支持的主题。
- 只输出该 JSON 对象:没有 markdown 代码围栏、没有注释、没有散文。"""


def build_section_titles_messages(
    *,
    source_text: str,
    current_titles: list[str],
    report_title: str = "",
) -> list[dict[str, str]]:
    """小节标题(模型态)批量一调的消息:现标题(当角色)+ 源料 → {"titles":[...]}(同序同量)。"""
    listing = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(current_titles))
    head = f"报告:{report_title.strip()}\n\n" if report_title.strip() else ""
    user = "\n".join(
        [
            f"源文:\n{source_text.strip()}",
            "---",
            head + "当前各小节标题(每节一个,按顺序)—— 为上面的源文逐个重写:",
            listing,
            "",
            f'返回一个 JSON 对象 {{"titles": [...]}},恰好 {len(current_titles)} 个标题,顺序相同。',
        ]
    )
    return [
        {"role": "system", "content": SECTION_TITLES_SYSTEM},
        {"role": "user", "content": user},
    ]

"""
把「一整篇报告文本」转成可复用报告模板的提示词。移植自前端 designer/ai-skeleton/prompt.ts。

- build_outline_messages   整篇 → 分节大纲(只规划,不出块)
- build_section_messages   整篇 + 指定某节 → 该节的块(柔性组件 + 候选写进 hint)
- build_region_messages    生成区 brief → 该区域的块(运行时展开 pass 用)
- build_skeleton_messages  回退:大纲失败时「单次整篇生成」

含各 SYSTEM 文案(图表优先导向一并带上)。契约/样例见 schema_doc.py。
SYSTEM 常量用拼接(非 f-string)组装,因契约/样例文本含 `{}` 花括号。
"""

from __future__ import annotations

from .schema_doc import (
    FEW_SHOT_EXAMPLE,
    FEW_SHOT_LAYOUT_FIRST,
    FEW_SHOT_SECTION,
    LAYOUT_FIRST_CONTRACT,
    OUTLINE_CONTRACT,
    SKELETON_CONTRACT,
)

# ============================================================
# ① 大纲:整篇 → 分节(只规划,不出块)
# ============================================================
_OUTLINE_HEAD = """You are a report-outlining engine. Read a complete report and produce ONLY a
high-level outline of its sections — no content blocks. Identify the report's logical top-level
sections in order; for each give a short title (from the report), a layout, and a one-line "intent"
describing what that section covers. Keep to the report's real structure; do not invent sections."""

OUTLINE_SYSTEM = _OUTLINE_HEAD + "\n\n" + OUTLINE_CONTRACT + "\n\nOutput ONE JSON object and nothing else: no markdown code fences, no comments, no prose."


def build_outline_messages(report_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": OUTLINE_SYSTEM},
        {"role": "user", "content": f"Outline the following report.\n\n---\n{report_text.strip()}"},
    ]


# ============================================================
# ② 逐节:整篇 + 指定某节 → 该节的块
# ============================================================
_SECTION_HEAD = """You are a report-TEMPLATE engine working ONE SECTION AT A TIME. You are given a
full report and told which single section to build. Produce THAT section's content blocks as a
reusable template: keep the structure solid but DO NOT hard-commit component types — pick a sensible
COMMON default and note the alternatives in each block's "hint".

Principles:
- Map content to fitting blocks, but prefer common, flexible components (paragraph, list, table,
  bar/line chart, stat-card-group). Avoid niche types unless the source clearly calls for it.
- For plottable numbers prefer a CHART (a trend/series, a distribution, a composition/share, or a
  ranking); reserve a table for genuinely tabular data (heterogeneous or text-heavy columns, exact
  lookup). When both could work, default to the chart and note the table as an alternative in "hint".
- FRAMEWORK fields are real (titles, table headers, chart axis/series field names, stat labels,
  list/section structure). CONTENT is NOT written out — describe it in "hint".
- Each "hint" says TWO things (source language): (1) which part of the report this area covers /
  visualizes, (2) which components could be used here.
- Build ONLY the requested section. Output ONE JSON object {"blocks":[...]} and nothing else."""

SECTION_SYSTEM = _SECTION_HEAD + "\n\n" + SKELETON_CONTRACT + "\n\nEXAMPLE section (illustration only — produce blocks from the user's actual report):\n" + FEW_SHOT_SECTION


def build_section_messages(report_text: str, section: dict[str, str]) -> list[dict[str, str]]:
    title = section.get("title")
    intent = section.get("intent")
    focus = f'the section titled "{title}"' if title else "the next section"
    about = f" (about: {intent})" if intent else ""
    user = "From the report below, build ONLY " + focus + about + '. Output {"blocks":[...]} only.\n\n---\n' + report_text.strip()
    return [
        {"role": "system", "content": SECTION_SYSTEM},
        {"role": "user", "content": user},
    ]


# ============================================================
# 生成区:作者给 brief → 该区域的块(运行时展开,严格遵循作者的组件编排)
# ============================================================
_REGION_HEAD = """You are a report-TEMPLATE engine building ONE region of a report, guided by the
AUTHOR'S OWN INSTRUCTION for that region. The author tells you (1) which part of the report this region
should cover and (2) which components to use — how many, of what kind, in what order. FOLLOW the
author's component instruction precisely: honor the requested kinds, counts, and ordering. When the
author is vague (e.g. "three charts, you pick the type"), choose sensible common types yourself.

Principles:
- FRAMEWORK fields are real (titles, table headers, chart axis/series field names, stat labels,
  list/section structure). CONTENT is NOT written out — describe it in each block's "hint".
- Each "hint" says, in the source language, which part of the report this block covers / visualizes.
- When the author leaves the component kind open, prefer a CHART over a table for plottable numbers
  (a trend/series, a distribution, a composition/share, or a ranking); a table is for heterogeneous
  or text-heavy or exact-lookup data.
- Build ONLY this region's blocks. Output ONE JSON object {"blocks":[...]} and nothing else."""

REGION_SYSTEM = _REGION_HEAD + "\n\n" + SKELETON_CONTRACT + "\n\nEXAMPLE blocks (illustration only — produce blocks from the user's actual report + instruction):\n" + FEW_SHOT_SECTION


def build_region_messages(report_text: str, *, section_title: str | None, brief: str) -> list[dict[str, str]]:
    where = f' This region sits in the section titled "{section_title}".' if section_title else ""
    brief_text = brief.strip() or "(no instruction — infer a sensible region from the report)"
    user = (
        "Author's instruction for this region:\n"
        + brief_text
        + "\n\nBuild ONLY this region's blocks from the report below, following the instruction above."
        + where
        + ' Output {"blocks":[...]} only.\n\n---\n'
        + report_text.strip()
    )
    return [
        {"role": "system", "content": REGION_SYSTEM},
        {"role": "user", "content": user},
    ]


# ============================================================
# 回退:大纲失败时,单次整篇生成
# ============================================================
_SYSTEM_PROMPT_HEAD = """You are a report-TEMPLATE engine. Given one complete report written in plain
text, you reverse-engineer a REUSABLE template for that KIND of report — a tree of typed components
(sections, layouts, content blocks) whose STRUCTURE is fixed but whose CONTENT is filled in later
from fresh data. You are NOT copying this report's content; you are extracting its skeleton.

How to think (do this before emitting JSON):
1. Identify the report's logical sections and their order.
2. For each section, map each piece to the most fitting block type: numbers/KPIs ->
   stat-card-group; option-vs-criteria comparisons -> comparison-matrix; chronological items ->
   timeline; standout takeaways/risks -> callout; enumerations -> list; narrative -> paragraph;
   plottable numbers (a trend/series, a distribution, a composition/share, or a ranking) -> chart,
   PREFERRED over a table; reserve table for genuinely tabular data (heterogeneous or text-heavy
   columns, many columns, exact-value lookup). Section/sub-section titles go into the section's own
   "title"/"subtitle", NOT into a content block.
3. Choose a layout per section (default "full"; multi-column/sidebar only for clearly parallel
   or primary+supporting content).
4. For each block, fill the FRAMEWORK fields with real values from the source, and describe the
   variable CONTENT with a one-line "hint" instead of writing it out.

Hard rules:
- FRAMEWORK (recurs in every report of this kind) is real: section titles, table headers,
  comparison column items, chart type + axis/series field names, stat-card labels, callout variant,
  list ordered flag. CONTENT (changes each time) is described by "hint", never
  written out: narrative prose, metric values, table rows, chart data, the wording of list items,
  timeline events.
- Do NOT emit "heading" blocks — section "title"/"subtitle" already serve as the heading.
- Do NOT fabricate framework the source does not support. Keep titles/labels/headers faithful.
- For chart blocks the shape keys NAME the data fields; never emit a "data" array.
- Write all framework text and hints in the SAME LANGUAGE as the source report.
- Output ONE JSON object and nothing else: no markdown code fences, no comments, no prose before
  or after."""

SYSTEM_PROMPT = _SYSTEM_PROMPT_HEAD + "\n\n" + SKELETON_CONTRACT + "\n\nEXAMPLE (illustrates the template shape only — produce structure from the user's actual report):\n" + FEW_SHOT_EXAMPLE


def build_skeleton_messages(report_text: str) -> list[dict[str, str]]:
    user = f"Build a reusable report template from the following report, following the JSON contract above. Output only the JSON object.\n\n---\n{report_text.strip()}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ============================================================
# 布局优先(layout-first):把样报抽象成「布局 + 角色」,每块吐成 open-region 占位(换主题套同版式)
# ============================================================
_LAYOUT_FIRST_SECTION_HEAD = """You are a report-TEMPLATE engine working ONE SECTION AT A TIME in
LAYOUT-FIRST mode. You are given a full sample report and told which single section to build.
Capture this section's LAYOUT and the ROLE of each block — NOT this report's subject — because the
template will be reused for OTHER subjects. Emit each block as a generative region (open-region).
Output ONE JSON object {"blocks":[...]} and nothing else: no markdown fences, no prose."""

LAYOUT_FIRST_SECTION_SYSTEM = (
    _LAYOUT_FIRST_SECTION_HEAD + "\n\n" + LAYOUT_FIRST_CONTRACT + "\n\nEXAMPLE section (illustration only — produce open-regions from the user's actual report):\n" + FEW_SHOT_LAYOUT_FIRST
)


def build_layout_first_section_messages(report_text: str, section: dict[str, str]) -> list[dict[str, str]]:
    title = section.get("title")
    intent = section.get("intent")
    focus = f'the section titled "{title}"' if title else "the next section"
    about = f" (about: {intent})" if intent else ""
    user = "From the report below, build ONLY " + focus + about + ' as layout-first open-regions. Output {"blocks":[...]} only.\n\n---\n' + report_text.strip()
    return [
        {"role": "system", "content": LAYOUT_FIRST_SECTION_SYSTEM},
        {"role": "user", "content": user},
    ]


# 布局优先回退:大纲失败时单次整篇生成(各节 blocks 全是 open-region)
_LAYOUT_FIRST_SKELETON_HEAD = """You are a report-TEMPLATE engine in LAYOUT-FIRST mode. From one
complete sample report, reverse-engineer a REUSABLE template that captures its LAYOUT and the ROLE
of each block — NOT this report's subject (the template is reused for OTHER subjects). Output a
single JSON object:
{
  "title": string,
  "sections": [
    { "title"?: string,
      "layout": "full" | "two-column" | "three-column" | "sidebar-left" | "sidebar-right",
      "blocks": [ { "type": "open-region", "hint": string }, ... ] }
  ]
}
Section titles may stay (they recur in this kind of report); block content does not. Output ONE
JSON object and nothing else: no markdown fences, no comments, no prose."""

LAYOUT_FIRST_SKELETON_SYSTEM = _LAYOUT_FIRST_SKELETON_HEAD + "\n\n" + LAYOUT_FIRST_CONTRACT + "\n\nEXAMPLE blocks within a section (illustration only):\n" + FEW_SHOT_LAYOUT_FIRST


def build_layout_first_skeleton_messages(report_text: str) -> list[dict[str, str]]:
    user = "Build a reusable LAYOUT-FIRST report template (every block an open-region) from the report below. Output only the JSON object.\n\n---\n" + report_text.strip()
    return [
        {"role": "system", "content": LAYOUT_FIRST_SKELETON_SYSTEM},
        {"role": "user", "content": user},
    ]

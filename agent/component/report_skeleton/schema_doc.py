"""
喂给 LLM 的「报告模板」JSON 契约(英文,给模型的结构指令;模板文字语言由 prompt 要求跟随源文)。
移植自前端 designer/ai-skeleton/schema-doc.ts —— 含「图表优先」选型导向(可绘制数字优先用图表,
表格留给真正表格型数据)。

产物是可复用模板而非成品报告:结构性字段填真实值,变量内容不写出、用每块 hint + 每节 annotation
描述。parse.py 据此把内容字段转成 llm 填充指令。形状/字段名与渲染器消费的键严格对齐。
"""

from __future__ import annotations

# 完整 JSON 契约 + 块选型指南(模板版)。
SKELETON_CONTRACT = """OUTPUT — a single JSON object describing a REUSABLE report TEMPLATE
(not a finished report):
{
  "title": string,                 // report title (required)
  "sections": Section[]            // ordered top-level sections (required, non-empty)
}

Section:
{
  "title"?: string,
  "subtitle"?: string,
  "layout": "full" | "two-column" | "three-column" | "sidebar-left" | "sidebar-right",
  "annotation"?: string,           // one line: what this whole section is for (guides fill-in)
  "blocks": Block[]
}
Do NOT emit "id" fields — they are generated downstream.
Layout: default "full". Use "two-column"/"three-column" only when blocks are clearly parallel;
use "sidebar-left"/"sidebar-right" only for one primary + one supporting block, and then set each
block's "role": "main" | "side". Otherwise omit "role".

TEMPLATE PRINCIPLE — separate FRAMEWORK from CONTENT:
- FRAMEWORK = the parts that recur in every report of this kind. Fill them with REAL values from
  the source: section titles, table headers, comparison column items, chart type and
  axis/series FIELD NAMES, stat-card labels, callout variant, list ordered flag.
- CONTENT = the variable text/numbers that change each time (narrative prose, metric values,
  table rows, chart data points, the wording of list items, timeline events). DO NOT write the
  actual content. Describe it with a one-line "hint" so the runtime can fill it later.

Every block carries "hint": string — a one-line note (source language) that says TWO things:
(1) WHICH part of the report this block covers / visualizes, and (2) WHICH components could be
used here. Example: "five-year enrollment numbers; a line or bar chart, or a table, all work".
Describe the content — never write the actual values.

BLOCK TYPES — pick the MOST SPECIFIC type for each piece of content. Never dump everything as
paragraphs.

1. paragraph — narrative prose (CONTENT). Give a hint, not the prose.
   { "type":"paragraph", "hint":string }

2. callout — a key takeaway / risk / tip (FRAMEWORK variant & title; CONTENT body).
   { "type":"callout", "variant":"info"|"success"|"warning"|"insight", "title"?:string, "hint":string }

3. list — a bulleted/numbered enumeration. Give a SHORT topic/label per intended bullet in
   "items"; the wording is rewritten from data at fill time.
   { "type":"list", "ordered":boolean, "title"?:string, "items":string[], "hint"?:string }

4. stat-card — ONE KPI. "label" is FRAMEWORK; its value is CONTENT. Add "change" ONLY when the
   source gives a comparison for this KPI (period-over-period, year-over-year, or an explicit delta):
   its string is a one-line hint for that change rate. At fill time the runtime writes a SIGNED rate
   (e.g. "+12.5%" / "−3.2%") and the card colors it green/red by sign — so OMIT "change" for a plain
   KPI the source reports only as a level. Do not emit "trend"; it is derived from the change's sign.
   { "type":"stat-card", "label":string, "change"?:string, "hint":string }

5. stat-card-group — a row of KPIs. Each item "label" is FRAMEWORK; the values are CONTENT. Give an
   item a "change" (same rule and meaning as #4) only for KPIs the source actually compares; omit it
   for plain snapshot metrics.
   { "type":"stat-card-group", "items":[{ "label":string, "change"?:string }], "hint":string }

6. table — "headers" are FRAMEWORK; the rows are CONTENT (do NOT emit rows). Use a table only for
   genuinely tabular data (heterogeneous or text-heavy columns, many columns, exact-value lookup);
   if the numbers are plottable, prefer a chart instead.
   { "type":"table", "title"?:string, "headers":string[], "hint":string }

7. comparison-matrix — "items" (the compared objects / column heads) are FRAMEWORK; the
   per-criterion values are CONTENT (do NOT emit criteria).
   { "type":"comparison-matrix", "title"?:string, "items":string[], "hint":string }

8. timeline — each entry "date" is FRAMEWORK; its title/description are CONTENT.
   { "type":"timeline", "title"?:string, "items":[{ "date":string }], "hint":string }

9. chart — "chartType" + shape keys are FRAMEWORK; the data is CONTENT (do NOT emit "data").
    Prefer a chart over a table whenever the data is a trend/series, a distribution, a
    composition/share, or a ranking. Match "chartType" to the data shape: a trend over time or an
    ordered series -> line (area to stress cumulative magnitude); a comparison or ranking across
    categories -> bar; a composition / share of a whole -> pie or donut; stage-to-stage drop-off
    through a process -> funnel; a multi-metric profile of one or a few entities -> radar; the
    relationship between two numeric variables -> scatter. The shape keys NAME the data fields (they
    are not themselves data). "chartType" decides them:
    bar | line | area:
      { "type":"chart","chartType":"bar","title"?:string,"xAxisKey":string,
        "series":[{ "dataKey":string,"name"?:string }],"hint":string }
    pie | donut | funnel:
      { "type":"chart","chartType":"pie","nameKey":string,"valueKey":string,"hint":string }
    radar:
      { "type":"chart","chartType":"radar","radarKeys":[string],
        "series":[{ "dataKey":string,"name"?:string }],"hint":string }
    scatter:
      { "type":"chart","chartType":"scatter",
        "series":[{ "dataKey":string,"xKey":string,"yKey":string,"name"?:string }],"hint":string }

RULES:
- Reconstruct the full structure: every section and block the report implies, in order.
- Do NOT emit "heading" blocks. A section's "title"/"subtitle" already render as its heading; an
  in-flow heading would duplicate it. Put heading text into the section's "title"/"subtitle".
- Prefer common, flexible components (paragraph, list, table, bar/line chart, stat-card-group).
  Pick a sensible default; do NOT rigidly commit to a niche type — note the alternatives in "hint".
- Emit a chart/table/stat/comparison/timeline whenever the source describes that KIND of content,
  even though you are NOT writing the numbers — the template captures the intent of each block.
- Prefer a CHART over a table for plottable numbers — a trend/series, a distribution, a
  composition/share, or a ranking. Reserve "table" for genuinely tabular data: heterogeneous or
  text-heavy columns, many columns, or exact-value lookup. When both fit, default to the chart —
  matching its chartType to the data shape (above) — and name the table as the alternative in "hint".
- Do NOT invent framework the source does not support; keep titles/labels/headers faithful.
- Write all framework text and hints in the SAME LANGUAGE as the source report.
- Output ONE JSON object and nothing else: no markdown code fences, no comments, no prose."""


# 一份紧凑的「文本 → 模板」few-shot 样例(依从性的最大杠杆)。
FEW_SHOT_EXAMPLE = """{
  "title": "Quarterly Business Review",
  "sections": [
    {
      "title": "Executive Summary",
      "layout": "full",
      "annotation": "High-level recap of the quarter's performance.",
      "blocks": [
        { "type": "paragraph", "hint": "Two or three sentences on overall performance and the main driver." },
        { "type": "stat-card-group",
          "items": [
            { "label": "Total Revenue", "change": "growth vs. the prior quarter" },
            { "label": "Active Customers" },
            { "label": "Churn", "change": "change in the churn rate vs. the prior quarter" }
          ],
          "hint": "Current value of each KPI; the items given a change also carry a period-over-period rate." },
        { "type": "callout", "variant": "insight", "title": "Key insight",
          "hint": "The single most important takeaway of the quarter." }
      ]
    },
    {
      "title": "Revenue Trend",
      "layout": "full",
      "blocks": [
        { "type": "chart", "chartType": "bar", "title": "Revenue by quarter",
          "xAxisKey": "quarter", "series": [{ "dataKey": "revenue", "name": "Revenue" }],
          "hint": "Revenue for each quarter of the reporting period." }
      ]
    }
  ]
}"""


# 大纲调用的 JSON 契约(只列分节,不含块)。
OUTLINE_CONTRACT = """OUTPUT — a single JSON object, the section OUTLINE only:
{
  "title": string,                 // report title
  "sections": [                    // ordered, one entry per logical top-level section
    {
      "title": string,             // short section title (from the report)
      "layout": "full" | "two-column" | "three-column" | "sidebar-left" | "sidebar-right",
      "intent": string             // one line: what this section covers / its purpose
    }
  ]
}
Default "layout" to "full" unless the section clearly has parallel ("two-column"/"three-column")
or primary+supporting ("sidebar-*") content. Do NOT include content blocks — only the section list."""


# 单节 few-shot:产出 {blocks:[...]},组件柔性、hint 写「内容范围 + 候选组件」(含图表优先示例)。
FEW_SHOT_SECTION = """{
  "blocks": [
    { "type": "stat-card-group",
      "items": [
        { "label": "Enrollment", "change": "year-over-year change in total students" },
        { "label": "Programs" }
      ],
      "hint": "Headline figures for this section: Enrollment carries a YoY change, Programs is a current count." },
    { "type": "paragraph", "hint": "Opening narrative of this section: scale and positioning; a prose paragraph." },
    { "type": "chart", "chartType": "line", "title": "Enrollment trend",
      "xAxisKey": "year", "series": [{ "dataKey": "students", "name": "Students" }],
      "hint": "Five-year enrollment numbers — a trend, so a line (or bar) chart is the default here." },
    { "type": "chart", "chartType": "pie", "title": "Students by level",
      "nameKey": "level", "valueKey": "students",
      "hint": "Share of students across degree levels — a composition, so a pie (or donut)." },
    { "type": "table", "title": "Programs at a glance", "headers": ["Program", "Department", "Start"],
      "hint": "Per-program facts (name, department, start date) — heterogeneous text, so a table, not a chart." }
  ]
}"""


# 布局优先(layout-first)契约:把样报「抽象成布局 + 角色」,每块吐成 open-region 占位 —— 内容 / 具体
# label 全不写出,由运行时按 brief 重生,从而「换主题套同一套版式」。外层 envelope 由各 prompt 自行指定。
LAYOUT_FIRST_CONTRACT = """Each block is a GENERATIVE REGION (open-region) — a placeholder the
runtime expands for the new subject:
  { "type": "open-region", "hint": string }

GOAL — capture the LAYOUT and the ROLE of each block, NOT this report's subject (the template is
reused for OTHER subjects). Each "hint" says, in the SOURCE LANGUAGE, TWO things:
  (1) the block's ROLE — its job in this KIND of report, generalized (e.g. "the most important
      headline KPIs", "the main metric's trend over time", "the composition / share of the whole",
      "the key item-by-item breakdown") — NEVER this report's literal labels, numbers, or names;
  (2) the COMPONENT to render it with, named explicitly so the layout stays consistent across
      subjects (e.g. "a stat-card group", "a line chart", "a pie chart", "a table").

RULES:
- One "open-region" per intended block (fine-grained) — each slot pins its own component.
- Generalize: turn a concrete metric like "营收 ¥12.4M" into a role such as "the headline
  revenue-type KPI", never the literal label or value. The runtime fills the real label + content.
- For plottable roles prefer a CHART over a table (a trend/series, a distribution, a
  composition/share, or a ranking) and name the chart type in the hint; reserve a table for
  genuinely tabular data (heterogeneous / text-heavy columns, exact lookup).
- Reconstruct each section's full set of blocks, in order; keep the count faithful to the report.
- Emit ONLY "open-region" blocks — never concrete block types, "fields", or "data"."""


# 布局优先 few-shot:一节抽象成若干 open-region,brief = 角色 + 钉住的组件(示例用英文,实际跟随源语言)。
FEW_SHOT_LAYOUT_FIRST = """{
  "blocks": [
    { "type": "open-region", "hint": "The 3-4 most important headline KPIs for this section, as a stat-card group" },
    { "type": "open-region", "hint": "The main metric's trend over time, as a line chart" },
    { "type": "open-region", "hint": "The composition / share of the whole, as a pie chart" },
    { "type": "open-region", "hint": "The key item-by-item breakdown, as a table" }
  ]
}"""

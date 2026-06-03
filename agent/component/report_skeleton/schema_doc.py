"""
喂给 LLM 的「报告模板」JSON 契约(中文指令;协议字段名/枚举值保持英文,模型据此输出结构)。
原移植自前端 designer/ai-skeleton/schema-doc.ts —— 含「图表优先」选型导向(可绘制数字优先用图表,
表格留给真正表格型数据)。

产物是可复用模板而非成品报告:结构性字段填真实值,变量内容不写出、用每块 hint + 每节 annotation
描述。parse.py 据此把内容字段转成 llm 填充指令。形状/字段名与渲染器消费的键严格对齐。
"""

from __future__ import annotations

# 完整 JSON 契约 + 块选型指南(模板版)。
SKELETON_CONTRACT = """输出 —— 一个描述「可复用报告模板」(而非成品报告)的 JSON 对象:
{
  "title": string,                 // 报告标题(必填)
  "sections": Section[]            // 有序的顶层小节(必填,非空)
}

Section:
{
  "title"?: string,
  "subtitle"?: string,
  "layout": "full" | "two-column" | "three-column" | "sidebar-left" | "sidebar-right",
  "annotation"?: string,           // 一句话:整节是干什么的(指导后续填充)
  "blocks": Block[]
}
不要输出 "id" 字段 —— 由下游生成。
布局:默认 "full"。仅当块明显并列时用 "two-column"/"three-column";仅当一主一辅时用
"sidebar-left"/"sidebar-right",此时给每个块设 "role": "main" | "side";否则省略 "role"。

模板原则 —— 区分「框架」与「内容」:
- 框架 = 这类报告每次都复现的部分,用源文里的真实值填:小节标题、表头、对比矩阵列项、
  图表类型与坐标轴/系列的字段名、指标卡标签、callout 的 variant、列表的 ordered 标志。
- 内容 = 每次都会变的文字/数字(叙述性正文、指标数值、表格行、图表数据点、列表项措辞、
  时间线事件)。不要写出真实内容,用一句 "hint" 描述,留给运行时填充。

每个块都带 "hint": string —— 一句话(用源文语言)说明两件事:(1) 这个块覆盖/可视化报告的哪
部分,(2) 这里可以用哪些组件。例:"近五年在校生人数;折线图或柱状图,或表格,都可以"。
描述内容即可 —— 永远不要写出真实数值。

块类型 —— 为每段内容挑最贴切的类型,不要把一切都堆成段落。

1. paragraph —— 叙述性正文(内容)。给 hint,不要写正文。
   { "type":"paragraph", "hint":string }

2. callout —— 一条要点/风险/提示(variant 与 title 是框架;正文是内容)。
   { "type":"callout", "variant":"info"|"success"|"warning"|"insight", "title"?:string, "hint":string }

3. list —— 项目符号/编号列表。在 "items" 里给每个预期条目一个简短主题/标签;措辞在填充时按
   数据改写。
   { "type":"list", "ordered":boolean, "title"?:string, "items":string[], "hint"?:string }

4. stat-card —— 单个 KPI。"label" 是框架;其数值是内容。仅当源文给了该 KPI 的对比(环比、同比
   或明确的增量)时才加 "change":其字符串是该变化率的一句 hint。填充时运行时会写一个带符号的比率
   (如 "+12.5%" / "−3.2%"),卡片按符号着绿/红色 —— 所以源文只报告水平值的纯 KPI 要省略
   "change"。不要输出 "trend";它由 change 的符号推导。
   { "type":"stat-card", "label":string, "change"?:string, "hint":string }

5. stat-card-group —— 一排 KPI。每个 item 的 "label" 是框架;数值是内容。仅给源文确实做了对比的
   KPI 加 "change"(规则与含义同 #4);纯快照指标省略。
   { "type":"stat-card-group", "items":[{ "label":string, "change"?:string }], "hint":string }

6. table —— "headers" 是框架;行是内容(不要输出 rows)。仅用于真正表格型数据(异质或文字密集的
   列、多列、精确查值);若数字可绘图,优先用图表。
   { "type":"table", "title"?:string, "headers":string[], "hint":string }

7. comparison-matrix —— "items"(被对比的对象/列头)是框架;各准则下的取值是内容(不要输出
   criteria)。
   { "type":"comparison-matrix", "title"?:string, "items":string[], "hint":string }

8. timeline —— 每个条目的 "date" 是框架;其标题/描述是内容。
   { "type":"timeline", "title"?:string, "items":[{ "date":string }], "hint":string }

9. chart —— "chartType" 与形状键是框架;数据是内容(不要输出 "data")。
    只要数据是趋势/序列、分布、构成/占比或排名,就优先用图表而非表格。让 "chartType" 匹配数据
    形状:随时间的趋势或有序序列 -> line(强调累积量级用 area);跨类别的比较或排名 -> bar;
    整体的构成/占比 -> pie 或 donut;流程逐级流失 -> funnel;一个或少数实体的多指标画像 -> radar;
    两个数值变量的关系 -> scatter。形状键是给数据字段「命名」(它们本身不是数据)。由 "chartType"
    决定用哪组:
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

规则:
- 重建完整结构:报告隐含的每一节、每一块,按顺序。
- 不要输出 "heading" 块。小节的 "title"/"subtitle" 已经渲染成标题;流内标题会重复。把标题文字放进
  小节的 "title"/"subtitle"。
- 优先用常见、灵活的组件(paragraph、list、table、bar/line 图、stat-card-group)。挑一个合理的
  默认;不要死板地钉死冷门类型 —— 把备选记到 "hint" 里。
- 只要源文描述了某种内容,就输出对应的 chart/table/stat/comparison/timeline —— 哪怕你不写数字,
  模板捕捉的是每个块的意图。
- 可绘制的数字优先用图表而非表格 —— 趋势/序列、分布、构成/占比或排名。把 "table" 留给真正表格型
  数据:异质或文字密集的列、多列、精确查值。两者都适用时默认图表 —— 让 chartType 匹配上面的数据
  形状 —— 并把表格作为备选写进 "hint"。
- 不要捏造源文不支持的框架;标题/标签/表头务必忠实。
- 所有框架文字与 hint 都用与源文相同的语言书写。
- 只输出一个 JSON 对象,别的什么都不要:没有 markdown 代码围栏、没有注释、没有散文。"""


# 一份紧凑的「文本 → 模板」few-shot 样例(依从性的最大杠杆;示例用中文,输出跟随源文语言)。
FEW_SHOT_EXAMPLE = """{
  "title": "季度经营回顾",
  "sections": [
    {
      "title": "执行摘要",
      "layout": "full",
      "annotation": "对本季度业绩的高层次回顾。",
      "blocks": [
        { "type": "paragraph", "hint": "两三句话概述整体业绩及主要驱动因素。" },
        { "type": "stat-card-group",
          "items": [
            { "label": "总营收", "change": "相比上一季度的增长" },
            { "label": "活跃客户" },
            { "label": "流失率", "change": "流失率相比上一季度的变化" }
          ],
          "hint": "每个 KPI 的当前值;带 change 的条目还附环比变化率。" },
        { "type": "callout", "variant": "insight", "title": "关键洞察",
          "hint": "本季度最重要的单条结论。" }
      ]
    },
    {
      "title": "营收趋势",
      "layout": "full",
      "blocks": [
        { "type": "chart", "chartType": "bar", "title": "各季度营收",
          "xAxisKey": "季度", "series": [{ "dataKey": "营收", "name": "营收" }],
          "hint": "报告期内每个季度的营收。" }
      ]
    }
  ]
}"""


# 大纲调用的 JSON 契约(只列分节,不含块)。
OUTLINE_CONTRACT = """输出 —— 一个 JSON 对象,只含小节大纲:
{
  "title": string,                 // 报告标题
  "sections": [                    // 有序,每个逻辑顶层小节一项
    {
      "title": string,             // 简短的小节标题(取自报告)
      "layout": "full" | "two-column" | "three-column" | "sidebar-left" | "sidebar-right",
      "intent": string             // 一句话:这节覆盖什么/其目的
    }
  ]
}
"layout" 默认 "full",除非该节明显是并列("two-column"/"three-column")或一主一辅("sidebar-*")
内容。不要包含内容块 —— 只要小节列表。"""


# 单节 few-shot:产出 {blocks:[...]},组件柔性、hint 写「内容范围 + 候选组件」(含图表优先示例)。
FEW_SHOT_SECTION = """{
  "blocks": [
    { "type": "stat-card-group",
      "items": [
        { "label": "在校生人数", "change": "在校生总数的同比变化" },
        { "label": "专业数量" }
      ],
      "hint": "本节的标题性数字:在校生人数带同比变化,专业数量是当前计数。" },
    { "type": "paragraph", "hint": "本节的开篇叙述:规模与定位;一段散文。" },
    { "type": "chart", "chartType": "line", "title": "在校生趋势",
      "xAxisKey": "学年", "series": [{ "dataKey": "在校生", "name": "在校生" }],
      "hint": "近五年在校生人数 —— 是趋势,所以这里默认折线图(或柱状图)。" },
    { "type": "chart", "chartType": "pie", "title": "各层次在校生",
      "nameKey": "层次", "valueKey": "在校生",
      "hint": "各学历层次在校生占比 —— 是构成,所以用饼图(或环图)。" },
    { "type": "table", "title": "专业一览", "headers": ["专业", "院系", "开设年份"],
      "hint": "逐专业事实(名称、院系、开设年份)—— 异质文本,所以用表格而非图表。" }
  ]
}"""


# 布局优先(layout-first)契约:把样报「抽象成布局 + 角色」,每块吐成 open-region 占位 —— 内容 / 具体
# label 全不写出,由运行时按 brief 重生,从而「换主题套同一套版式」。外层 envelope 由各 prompt 自行指定。
LAYOUT_FIRST_CONTRACT = """每个块都是一个「生成区」(open-region)—— 一个占位,运行时会为新主题展开:
  { "type": "open-region", "hint": string }

目标 —— 捕捉每个块的「布局」与「角色」,而非本报告的主题(模板会被复用到其它主题)。每个 "hint"
用源文语言说明两件事:
  (1) 块的角色 —— 它在这类报告里的职责,泛化表述(如「最重要的标题性 KPI」「主指标随时间的趋势」
      「整体的构成/占比」「关键的逐项拆解」)—— 绝不写本报告字面的标签、数字或名称;
  (2) 用来渲染它的组件,显式命名,以便不同主题间布局一致(如「一个指标卡组」「一张折线图」
      「一张饼图」「一张表格」)。

规则:
- 每个预期块一个 "open-region"(细粒度)—— 每个槽位各自钉住自己的组件。
- 泛化:把像「营收 ¥12.4M」这样的具体指标,变成「标题性营收类 KPI」这样的角色,绝不写字面标签或
  数值。运行时会填入真实标签 + 内容。
- 可绘制的角色优先用图表而非表格(趋势/序列、分布、构成/占比或排名),并在 hint 里点明图表类型;
  把表格留给真正表格型数据(异质/文字密集的列、精确查值)。
- 按顺序重建每节的完整块集合;块数与报告保持一致。
- 每个 "hint" 都用与源文相同的语言书写(中文源→中文 hint),无视本契约或下方样例的语言。
- 只输出 "open-region" 块 —— 绝不输出具体块类型、"fields" 或 "data"。"""


# 布局优先 few-shot:一节抽象成若干 open-region,brief = 角色 + 钉住的组件(示例用中文,输出跟随源语言)。
FEW_SHOT_LAYOUT_FIRST = """{
  "blocks": [
    { "type": "open-region", "hint": "本节最重要的 3-4 个标题性 KPI,用指标卡组" },
    { "type": "open-region", "hint": "主指标随时间的趋势,用折线图" },
    { "type": "open-region", "hint": "整体的构成/占比,用饼图" },
    { "type": "open-region", "hint": "关键的逐项拆解,用表格" }
  ]
}"""

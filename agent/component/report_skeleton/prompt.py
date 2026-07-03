"""
把「一整篇报告文本」转成可复用报告模板的提示词。原移植自前端 designer/ai-skeleton/prompt.ts。

- build_outline_messages   整篇 → 分节大纲(只规划,不出块)
- build_section_messages   整篇 + 指定某节 → 该节的块(柔性组件 + 候选写进 hint)
- build_region_messages    生成区 brief → 该区域的块(运行时展开 pass 用)
- build_skeleton_messages  回退:大纲失败时「单次整篇生成」

各 SYSTEM 文案用中文给模型(图表优先导向一并带上);协议字段名/枚举值保持英文。契约/样例见
schema_doc.py。SYSTEM 常量用拼接(非 f-string)组装,因契约/样例文本含 `{}` 花括号。
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
_OUTLINE_HEAD = """你是一个报告大纲引擎。读完一整篇报告,只产出它各小节的高层大纲 —— 不出内容块。
按顺序识别报告的逻辑顶层小节;每节给一个简短标题(取自报告)、一个 layout,以及一句话的 "intent"
描述这节覆盖什么。忠于报告真实结构;不要捏造小节。"""

OUTLINE_SYSTEM = _OUTLINE_HEAD + "\n\n" + OUTLINE_CONTRACT + "\n\n只输出一个 JSON 对象,别的什么都不要:没有 markdown 代码围栏、没有注释、没有散文。"


def build_outline_messages(report_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": OUTLINE_SYSTEM},
        {"role": "user", "content": f"为下面这篇报告生成大纲。\n\n---\n{report_text.strip()}"},
    ]


# ============================================================
# ② 逐节:整篇 + 指定某节 → 该节的块
# ============================================================
_SECTION_HEAD = """你是一个「报告模板」引擎,一次只处理一个小节。给你一整篇报告,并告诉你要构建
哪一节。把那一节的内容块产出为可复用模板:结构要扎实,但不要硬钉死组件类型 —— 挑一个合理的「常见」
默认,并把备选记到每个块的 "hint" 里。

原则:
- 把内容映射到贴切的块,但优先用常见、灵活的组件(paragraph、list、table、bar/line 图、
  stat-card-group)。除非源文明确需要,否则避免冷门类型。
- 可绘制的数字优先用图表(趋势/序列、分布、构成/占比或排名);把表格留给真正表格型数据(异质或
  文字密集的列、精确查值)。两者都行时默认图表,并把表格作为备选写进 "hint"。
- 框架字段是真实的(标题、表头、图表坐标轴/系列字段名、指标卡标签、列表/小节结构)。内容不写出 ——
  在 "hint" 里描述。
- 每个 "hint" 用源文语言说两件事:(1) 这块覆盖/可视化报告的哪部分,(2) 这里可以用哪些组件。
- 只构建被指定的那一节。只输出一个 JSON 对象 {"blocks":[...]},别的什么都不要。"""

SECTION_SYSTEM = _SECTION_HEAD + "\n\n" + SKELETON_CONTRACT + "\n\n示例小节(仅作说明 —— 请根据用户的实际报告产出块):\n" + FEW_SHOT_SECTION


def build_section_messages(report_text: str, section: dict[str, str]) -> list[dict[str, str]]:
    title = section.get("title")
    intent = section.get("intent")
    focus = f"标题为「{title}」的小节" if title else "下一节"
    about = f"(主题:{intent})" if intent else ""
    user = "从下面这篇报告中,只构建" + focus + about + '。只输出 {"blocks":[...]}。\n\n---\n' + report_text.strip()
    return [
        {"role": "system", "content": SECTION_SYSTEM},
        {"role": "user", "content": user},
    ]


# ============================================================
# 生成区:作者给 brief → 该区域的块(运行时展开,严格遵循作者的组件编排)
# ============================================================
_REGION_HEAD = """你是一个「报告模板」引擎,在作者对某个区域的「亲笔指令」指导下构建报告的一个区域。
作者告诉你 (1) 这个区域应覆盖报告的哪部分,(2) 用哪些组件 —— 几个、什么类型、什么顺序。精确遵循
作者的组件指令:尊重要求的类型、数量与顺序。当作者含糊时(如「三张图,类型你定」),自行挑选合理的
常见类型。

原则:
- 框架字段是真实的(标题、表头、图表坐标轴/系列字段名、指标卡标签、列表/小节结构)。内容不写出 ——
  在每个块的 "hint" 里描述。
- 每个 "hint" 用源文语言说明这块覆盖/可视化报告的哪部分。
- 当作者没指定组件类型时,可绘制的数字优先用图表而非表格(趋势/序列、分布、构成/占比或排名);
  表格用于异质、文字密集或精确查值的数据。
- 只构建本区域的块。只输出一个 JSON 对象 {"blocks":[...]},别的什么都不要。"""

REGION_SYSTEM = _REGION_HEAD + "\n\n" + SKELETON_CONTRACT + "\n\n示例块(仅作说明 —— 请根据用户的实际报告 + 指令产出块):\n" + FEW_SHOT_SECTION


def build_region_messages(report_text: str, *, section_title: str | None, brief: str) -> list[dict[str, str]]:
    where = f"本生成区位于标题为「{section_title}」的小节内。" if section_title else ""
    brief_text = brief.strip() or "(无指令 —— 从报告中推断一个合理的区域)"
    user = "作者对本生成区的指令:\n" + brief_text + "\n\n按上面的指令,只从下面这篇报告构建本区域的块。" + where + '只输出 {"blocks":[...]}。\n\n---\n' + report_text.strip()
    return [
        {"role": "system", "content": REGION_SYSTEM},
        {"role": "user", "content": user},
    ]


# ============================================================
# 布局优先·展开:把布局优先骨架的 open-region 按「角色 + 组件」展开成具体块——区别于上面的
# build_region_messages(遵循作者亲写 brief),这里 brief 只供角色/组件,**框架文字一律从新源文取**,
# 换主题不照搬样报名词;源文对该角色无料则回空块(自然收缩)。
# ============================================================
_LAYOUT_FIRST_REGION_HEAD = """你为一个「新主题」展开报告模板的一个区域,依据是下面的「源报告」。
brief 只给你这个区域的「角色」和要用的「组件」—— 几个块、什么类型、什么顺序。它不会给你主题。

为那个角色构建具体的块,并把「每一处框架文字」—— 块/小节标题、表头、图表坐标轴/系列字段名、
指标卡标签、列表框架 —— 都从「源报告」里取,贴合该角色。brief 里可能提到某个主题、标签、数字或
名称:那描述的是模板所源自的「旧样报」,不是本源文 —— 绝不照搬。真实标签取自「本」源文。

原则:
- 尊重 brief 的组件指令(类型、数量、顺序);所有措辞都取自源文。
- 宽松地理解「角色」—— 把它映射到「新主题」里最接近的对应物。一个「城市概览」角色,在本主题(一所
  学校、一家公司……)里就变成对本主题的概览;一个「游客量趋势」就变成本主题的主量级趋势;一个
  「消费构成」就变成本主题的主构成。重定向角色;绝不照搬样报的名词。
- 内容(数值、表格行、图表数据、正文)不写出 —— 在每个块的 "hint" 里用源文语言描述。
- 当 brief 没指定组件时,可绘制的数字优先用图表而非表格。
- 只有当源文对这个角色「完全没有」合理对应物时,才返回 {"blocks": []} —— 不要捏造块,不要把样报的
  内容搬过来,也不要用无关材料凑数。
- 只构建本区域的块。只输出一个 JSON 对象 {"blocks":[...]},别的什么都不要。"""

LAYOUT_FIRST_REGION_SYSTEM = _LAYOUT_FIRST_REGION_HEAD + "\n\n" + SKELETON_CONTRACT + "\n\n示例块(仅作说明 —— 请根据用户的实际「源文」 + brief 的角色产出块):\n" + FEW_SHOT_SECTION


def build_layout_first_region_messages(report_text: str, *, section_title: str | None, brief: str) -> list[dict[str, str]]:
    where = f"本生成区位于扮演「{section_title}」角色的小节内。" if section_title else ""
    brief_text = brief.strip() or "(无 brief —— 从源文为这个位置推断一个合理的块)"
    user = (
        "本生成区的 brief(只含角色 + 组件 —— 其中任何主题/标签描述的是旧样报,不是源文):\n"
        + brief_text
        + "\n\n为新主题只构建本区域的块,所有标签/表头/标题都取自下面的源文。"
        + where
        + '若源文对该角色无料,返回 {"blocks": []}。只输出 {"blocks":[...]}。\n\n---\n'
        + report_text.strip()
    )
    return [
        {"role": "system", "content": LAYOUT_FIRST_REGION_SYSTEM},
        {"role": "user", "content": user},
    ]


# ============================================================
# 回退:大纲失败时,单次整篇生成
# ============================================================
_SYSTEM_PROMPT_HEAD = """你是一个「报告模板」引擎。给你一整篇纯文本报告,你要反向工程出这「类」报告的
可复用模板 —— 一棵带类型的组件树(小节、布局、内容块),其「结构」固定,而「内容」之后由新数据填充。
你不是在复制这篇报告的内容;你是在抽取它的骨架。

如何思考(产出 JSON 前先做):
1. 识别报告的逻辑小节及其顺序。
2. 每节里,把每段内容映射到最贴切的块类型:数字/KPI -> stat-card-group;选项对准则的对比 ->
   comparison-matrix;按时间排列的条目 -> timeline;突出的要点/风险 -> callout;枚举 -> list;
   叙述 -> paragraph;可绘制的数字(趋势/序列、分布、构成/占比或排名)-> chart,优先于表格;
   把 table 留给真正表格型数据(异质或文字密集的列、多列、精确查值)。小节/子节标题放进小节自己的
   "title"/"subtitle",而非内容块。
3. 给每节选一个 layout(默认 "full";只有明显并列或一主一辅的内容才用多栏/侧栏)。
4. 每个块里,用源文真实值填「框架」字段,并用一句 "hint" 描述可变的「内容」而非写出它。

硬性规则:
- 框架(这类报告每次都复现)是真实的:小节标题、表头、对比列项、图表类型 + 坐标轴/系列字段名、
  指标卡标签、callout variant、列表 ordered 标志。内容(每次都变)用 "hint" 描述,绝不写出:
  叙述性正文、指标数值、表格行、图表数据、列表项措辞、时间线事件。
- 不要输出 "heading" 块 —— 小节的 "title"/"subtitle" 已充当标题。
- 不要捏造源文不支持的框架。标题/标签/表头务必忠实。
- chart 块的形状键是给数据字段「命名」;绝不输出 "data" 数组。
- 所有框架文字与 hint 都用与源文相同的语言书写。
- 只输出一个 JSON 对象,别的什么都不要:前后都没有 markdown 代码围栏、没有注释、没有散文。"""

SYSTEM_PROMPT = _SYSTEM_PROMPT_HEAD + "\n\n" + SKELETON_CONTRACT + "\n\n示例(仅示意模板形状 —— 请根据用户的实际报告产出结构):\n" + FEW_SHOT_EXAMPLE


def build_skeleton_messages(report_text: str) -> list[dict[str, str]]:
    user = f"按上面的 JSON 契约,从下面这篇报告构建一个可复用的报告模板。只输出该 JSON 对象。\n\n---\n{report_text.strip()}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ============================================================
# 布局优先(layout-first):把样报抽象成「布局 + 角色」,每块吐成 open-region 占位(换主题套同版式)
# ============================================================
_LAYOUT_FIRST_SECTION_HEAD = """你是一个「报告模板」引擎,在「布局优先」模式下一次只处理一个小节。
给你一整篇样报,并告诉你要构建哪一节。捕捉这一节的「布局」与每个块的「角色」—— 而非本报告的主题 ——
因为模板会被复用到「其它」主题。把每个块都产出为一个生成区(open-region)。每个 "hint" 都用与源文
相同的语言书写(中文报告→中文 hint),无视本指令或下方样例的语言。只输出一个 JSON 对象
{"blocks":[...]},别的什么都不要:没有 markdown 围栏、没有散文。"""

LAYOUT_FIRST_SECTION_SYSTEM = _LAYOUT_FIRST_SECTION_HEAD + "\n\n" + LAYOUT_FIRST_CONTRACT + "\n\n示例小节(仅作说明 —— 请根据用户的实际报告产出 open-region):\n" + FEW_SHOT_LAYOUT_FIRST


def build_layout_first_section_messages(report_text: str, section: dict[str, str]) -> list[dict[str, str]]:
    title = section.get("title")
    intent = section.get("intent")
    focus = f"标题为「{title}」的小节" if title else "下一节"
    about = f"(主题:{intent})" if intent else ""
    user = "从下面这篇报告中,只把" + focus + about + '构建为布局优先的 open-region。只输出 {"blocks":[...]}。\n\n---\n' + report_text.strip()
    return [
        {"role": "system", "content": LAYOUT_FIRST_SECTION_SYSTEM},
        {"role": "user", "content": user},
    ]


# 布局优先回退:大纲失败时单次整篇生成(各节 blocks 全是 open-region)
_LAYOUT_FIRST_SKELETON_HEAD = """你是一个「布局优先」模式下的「报告模板」引擎。从一整篇样报反向工程出
一个可复用模板,捕捉它的「布局」与每个块的「角色」—— 而非本报告的主题(模板会被复用到「其它」主题)。
输出一个 JSON 对象:
{
  "title": string,
  "sections": [
    { "title"?: string,
      "layout": "full" | "two-column" | "three-column" | "sidebar-left" | "sidebar-right",
      "blocks": [ { "type": "open-region", "hint": string }, ... ] }
  ]
}
小节标题可以保留(它们在这类报告里复现);块内容不保留。每个 "hint" 都用与源文相同的语言书写
(中文报告→中文 hint),无视本指令或样例的语言。只输出一个 JSON 对象,别的什么都不要:没有
markdown 围栏、没有注释、没有散文。"""

LAYOUT_FIRST_SKELETON_SYSTEM = _LAYOUT_FIRST_SKELETON_HEAD + "\n\n" + LAYOUT_FIRST_CONTRACT + "\n\n小节内的示例块(仅作说明):\n" + FEW_SHOT_LAYOUT_FIRST


def build_layout_first_skeleton_messages(report_text: str) -> list[dict[str, str]]:
    user = "从下面这篇报告构建一个可复用的「布局优先」报告模板(每个块都是 open-region)。只输出该 JSON 对象。\n\n---\n" + report_text.strip()
    return [
        {"role": "system", "content": LAYOUT_FIRST_SKELETON_SYSTEM},
        {"role": "user", "content": user},
    ]

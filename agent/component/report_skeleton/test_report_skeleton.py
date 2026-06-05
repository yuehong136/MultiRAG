"""
report_skeleton 纯逻辑单测(AI 生成骨架 + 生成区展开)。

桩注入 call_llm,不触网。覆盖:normalize_block(剔 heading / chart 兜底 / 注解归属 / sidebar role)、
parse(大纲 / 单节 / 整篇回退 + 防御解析)、expand(原位替换 + 继承 role + 某区失败跳过)、
generate(大纲→逐节 / 某节失败跳过 / 大纲失败回退 / 全失败抛错)。异步用 asyncio.run,免 pytest-asyncio。
"""

import asyncio
import json
import re

import pytest

from agent.component.report_fill.fill import fill_skeleton
from agent.component.report_skeleton import expand_open_regions, generate_skeleton
from agent.component.report_skeleton.build_block import normalize_block
from agent.component.report_skeleton.coerce import one_of, opt_enum
from agent.component.report_skeleton.generate import GenerateError
from agent.component.report_skeleton.parse import (
    SkeletonParseError,
    parse_outline,
    parse_section,
    parse_skeleton_response,
)
from agent.component.report_skeleton.prompt import (
    build_layout_first_section_messages,
    build_layout_first_skeleton_messages,
)


def _make_call_llm(responses):
    seq = list(responses)
    calls = []

    async def call_llm(messages):
        calls.append(messages)
        return seq.pop(0)

    return call_llm, calls


# ----------------------------------------------------------------------------
# coerce
# ----------------------------------------------------------------------------


def test_coerce_one_of_and_opt_enum():
    assert one_of("line", ["bar", "line"], "bar") == "line"
    assert one_of("bogus", ["bar", "line"], "bar") == "bar"
    assert opt_enum("up", ["up", "down", "neutral"]) == "up"
    assert opt_enum("sideways", ["up", "down", "neutral"]) is None


# ----------------------------------------------------------------------------
# build_block.normalize_block
# ----------------------------------------------------------------------------


def test_normalize_block_drops_heading_and_non_dict():
    assert normalize_block({"type": "heading", "level": 2, "content": "x"}, False) is None
    assert normalize_block("not a dict", False) is None
    assert normalize_block(["array"], False) is None


def test_normalize_block_paragraph_hint_on_field_not_annotation():
    blk = normalize_block({"type": "paragraph", "hint": "intro narrative"}, False)
    assert blk["type"] == "paragraph"
    assert blk["fieldDirectives"]["content"] == {"mode": "llm", "hint": "intro narrative"}
    # 单内容块:hint 落字段指令,不重复到块级 annotation
    assert "annotation" not in blk


def test_normalize_block_chart_shape_and_annotation():
    blk = normalize_block(
        {"type": "chart", "chartType": "line", "xAxisKey": "year", "series": [{"dataKey": "rev", "name": "Rev"}], "hint": "yearly revenue"},
        False,
    )
    assert blk["fields"]["chartType"] == "line"
    assert blk["fields"]["xAxisKey"] == "year"
    assert blk["fields"]["series"] == [{"dataKey": "rev", "name": "Rev"}]
    assert blk["fieldDirectives"]["data"] == {"mode": "llm"}
    # chart ∈ ANNOTATABLE_BLOCKS:hint 落块级注解
    assert blk["annotation"] == "yearly revenue"


def test_normalize_block_chart_defaults_when_missing():
    blk = normalize_block({"type": "chart"}, False)
    assert blk["fields"]["chartType"] == "bar"
    assert blk["fields"]["xAxisKey"] == "x"
    assert blk["fields"]["series"] == [{"dataKey": "y"}]
    assert blk["fields"]["data"] == []


def test_normalize_block_stat_card_group_indexed_directives():
    blk = normalize_block(
        {"type": "stat-card-group", "items": [{"label": "A"}, {"label": "B", "trend": "up"}], "hint": "kpis"},
        False,
    )
    assert blk["fields"]["items"] == [{"label": "A"}, {"label": "B", "trend": "up"}]
    assert set(blk["fieldDirectives"]) == {"items[0].value", "items[1].value"}
    assert blk["annotation"] == "kpis"


def test_normalize_block_stat_card_change_directive():
    # 带 change → 额外 change 空槽(hint = change 文案);不带 → 只有 value 槽
    blk = normalize_block({"type": "stat-card", "label": "营收", "change": "环比变化", "hint": "Q3 营收"}, False)
    assert blk["fieldDirectives"]["value"] == {"mode": "llm", "hint": "Q3 营收"}
    assert blk["fieldDirectives"]["change"] == {"mode": "llm", "hint": "环比变化"}
    plain = normalize_block({"type": "stat-card", "label": "营收", "hint": "Q3 营收"}, False)
    assert "change" not in plain["fieldDirectives"]


def test_normalize_block_stat_card_group_change_per_item():
    # 仅对有 change 的 item 加 items[i].change;无 change 的 item 只有 value 槽
    blk = normalize_block(
        {"type": "stat-card-group", "items": [{"label": "营收", "change": "环比"}, {"label": "客户数"}], "hint": "kpis"},
        False,
    )
    assert set(blk["fieldDirectives"]) == {"items[0].value", "items[0].change", "items[1].value"}
    assert blk["fieldDirectives"]["items[0].change"] == {"mode": "llm", "hint": "环比"}


def test_normalize_block_stat_card_icon_kept_and_validated():
    # 合法 icon → 落 fields(框架);非法 icon / 未给 → 不写(渲染端按 label 启发式兜底)
    ok = normalize_block({"type": "stat-card", "label": "营收", "icon": "money", "hint": "Q3 营收"}, False)
    assert ok["fields"]["icon"] == "money"
    bad = normalize_block({"type": "stat-card", "label": "营收", "icon": "rocket", "hint": "Q3 营收"}, False)
    assert "icon" not in bad["fields"]
    none = normalize_block({"type": "stat-card", "label": "营收", "hint": "Q3 营收"}, False)
    assert "icon" not in none["fields"]


def test_normalize_block_stat_card_group_icon_per_item():
    # 逐项携带合法 icon;非法或缺省的 item 不带 icon
    blk = normalize_block(
        {
            "type": "stat-card-group",
            "items": [{"label": "客流", "icon": "users"}, {"label": "X", "icon": "bogus"}, {"label": "Y"}],
            "hint": "kpis",
        },
        False,
    )
    items = blk["fields"]["items"]
    assert items[0]["icon"] == "users"
    assert "icon" not in items[1]
    assert "icon" not in items[2]


def test_normalize_block_sidebar_role():
    assert normalize_block({"type": "paragraph", "role": "side"}, True)["role"] == "side"
    assert normalize_block({"type": "paragraph"}, True)["role"] == "main"
    assert "role" not in normalize_block({"type": "paragraph"}, False)


def test_normalize_block_open_region_preserved():
    # 布局优先:open-region 占位原样保留,brief(hint)落 annotation,不造 fields/directives。
    blk = normalize_block({"type": "open-region", "hint": "最重要的 KPI,指标卡组"}, False)
    assert blk["type"] == "open-region"
    assert blk["annotation"] == "最重要的 KPI,指标卡组"
    assert "fields" not in blk and "fieldDirectives" not in blk
    assert blk["id"].startswith("blk-")
    # sidebar 下继承分列 role
    assert normalize_block({"type": "open-region", "hint": "x", "role": "side"}, True)["role"] == "side"
    assert normalize_block({"type": "open-region", "hint": "x"}, True)["role"] == "main"
    # 空 brief:不写 annotation(expand 容忍空 brief)
    assert "annotation" not in normalize_block({"type": "open-region"}, False)


# ----------------------------------------------------------------------------
# parse
# ----------------------------------------------------------------------------


def test_parse_outline_valid_and_layout_fallback():
    raw = '{"title":"Q3","sections":[{"title":"Overview","layout":"full","intent":"recap"},{"layout":"bogus"}]}'
    outline = parse_outline(raw)
    assert outline["title"] == "Q3"
    assert outline["sections"][0] == {"layout": "full", "title": "Overview", "intent": "recap"}
    assert outline["sections"][1]["layout"] == "full"  # 非法 layout 回落 full


def test_parse_outline_no_sections_raises():
    with pytest.raises(SkeletonParseError):
        parse_outline('{"title":"x","sections":[]}')


def test_parse_section_strips_think_and_fences():
    raw = '<think>planning {a:b}</think>\n```json\n{"blocks":[{"type":"paragraph","hint":"x"}]}\n```'
    sec = parse_section(raw, {"layout": "full", "title": "T", "intent": "i"})
    assert sec["title"] == "T"
    assert sec["annotation"] == "i"
    assert sec["layout"] == "full"
    assert sec["blocks"][0]["type"] == "paragraph"


def test_parse_section_no_valid_blocks_raises():
    with pytest.raises(SkeletonParseError):
        parse_section('{"blocks":[]}', {"layout": "full"})
    # 仅 heading(被剔除)→ 无合法块
    with pytest.raises(SkeletonParseError):
        parse_section('{"blocks":[{"type":"heading","content":"x"}]}', {"layout": "full"})


def test_parse_skeleton_response_fallback_with_default_theme():
    raw = '{"title":"R","sections":[{"layout":"full","blocks":[{"type":"paragraph","hint":"x"}]}]}'
    sk = parse_skeleton_response(raw)
    assert sk["title"] == "R"
    assert sk["theme"]["colorPalette"][0] == "#1677ff"
    assert sk["sections"][0]["blocks"][0]["type"] == "paragraph"


def test_extract_json_invalid_raises():
    with pytest.raises(SkeletonParseError):
        parse_skeleton_response("no json object here at all")


# ----------------------------------------------------------------------------
# expand_open_regions
# ----------------------------------------------------------------------------


def test_expand_no_open_regions_passthrough():
    skel = {"title": "t", "sections": [{"id": "s", "layout": "full", "blocks": [{"id": "b", "type": "paragraph", "fields": {}}]}]}
    call_llm, calls = _make_call_llm([])
    res = asyncio.run(expand_open_regions(skel, "src", call_llm))
    assert res.open_regions == 0
    assert res.skeleton is skel  # 无生成区时原样返回,不调模型
    assert len(calls) == 0


def test_expand_replaces_in_place_and_inherits_role():
    skel = {
        "title": "t",
        "sections": [
            {
                "id": "s",
                "layout": "sidebar-left",
                "title": "Sec",
                "blocks": [
                    {"id": "keep", "type": "paragraph", "role": "main", "fields": {"content": "x"}},
                    {"id": "og", "type": "open-region", "role": "side", "annotation": "three KPIs"},
                ],
            }
        ],
    }
    region_json = '{"blocks":[{"type":"stat-card","label":"A","hint":"a"},{"type":"stat-card","label":"B","hint":"b"}]}'
    call_llm, calls = _make_call_llm([region_json])
    res = asyncio.run(expand_open_regions(skel, "src", call_llm))

    assert res.open_regions == 1
    assert res.ok_regions == 1
    blocks = res.skeleton["sections"][0]["blocks"]
    assert [b["type"] for b in blocks] == ["paragraph", "stat-card", "stat-card"]
    assert all(b["type"] != "open-region" for b in blocks)
    # 展开块继承占位块的 role "side"
    assert blocks[1]["role"] == "side"
    assert blocks[2]["role"] == "side"
    assert len(calls) == 1


def test_expand_region_failure_is_skipped():
    skel = {
        "title": "t",
        "sections": [
            {
                "id": "s",
                "layout": "full",
                "blocks": [
                    {"id": "og", "type": "open-region", "annotation": "brief"},
                    {"id": "keep", "type": "paragraph", "fields": {"content": "x"}},
                ],
            }
        ],
    }
    call_llm, _ = _make_call_llm(["not valid json"])
    res = asyncio.run(expand_open_regions(skel, "src", call_llm))
    assert res.open_regions == 1
    assert res.ok_regions == 0
    assert len(res.errors) == 1
    blocks = res.skeleton["sections"][0]["blocks"]
    assert [b["id"] for b in blocks] == ["keep"]  # 失败区丢弃,保其余


def test_expand_layout_first_uses_source_derive_prompt_and_stamps_flag():
    # 全块皆 open-region ⇒ 布局优先:走 source-derive 展开提示词(而非遵循 brief 的 build_region_messages),
    # 并把 layoutFirst 盖回返回骨架供下游收缩。
    skel = {
        "title": "t",
        "sections": [{"id": "s", "layout": "full", "title": "学校概况", "blocks": [{"id": "og", "type": "open-region", "annotation": "学校招生人数,指标卡组"}]}],
    }
    call_llm, calls = _make_call_llm(['{"blocks":[{"type":"stat-card","label":"游客接待量","hint":"年接待游客"}]}'])
    res = asyncio.run(expand_open_regions(skel, "文旅源文", call_llm))
    assert res.skeleton["layoutFirst"] is True  # 探测并盖回信号
    user = calls[0][1]["content"]
    assert "为新主题" in user  # 布局优先源文重建提示词
    assert "作者对本生成区的指令" not in user  # 不是遵循 brief 的 build_region_messages


def test_expand_layout_first_empty_region_drops_without_error():
    # 布局优先:源文对某区无料 → 模型合法回 {"blocks":[]} → 该区 0 块、不计错(自然收缩)。
    skel = {
        "title": "t",
        "sections": [{"id": "s", "layout": "full", "title": "无料节", "blocks": [{"id": "og", "type": "open-region", "annotation": "some role"}]}],
    }
    call_llm, calls = _make_call_llm(['{"blocks":[]}'])
    res = asyncio.run(expand_open_regions(skel, "src", call_llm))
    assert res.open_regions == 1
    assert res.ok_regions == 1  # 合法空区算成功,不计错
    assert res.errors == []
    assert res.skeleton["sections"][0]["blocks"] == []  # 该区贡献 0 块
    assert res.skeleton["layoutFirst"] is True
    assert len(calls) == 1


def _open_region_sections(n: int) -> dict:
    """n 个小节,各含一个 open-region;节标题 节{i} 供桩按内容寻址。"""
    return {
        "title": "t",
        "sections": [
            {"id": f"s{i}", "layout": "full", "title": f"节{i}", "blocks": [{"id": f"og{i}", "type": "open-region", "annotation": "指标"}]}
            for i in range(n)
        ],
    }


def _peak_tracking_region_llm(n: int):
    """记录同时在飞调用峰值的桩:进入 +1、让出循环制造重叠、退出 -1。按节标题寻址回
    「i+1 个 paragraph」(块数编码区身份,验回填位置)。返回 (call_llm, peak_getter)。"""
    state = {"in_flight": 0, "peak": 0}

    async def call_llm(messages):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        await asyncio.sleep(0)  # 让出循环,逼出真重叠
        state["in_flight"] -= 1
        user = messages[1]["content"]
        i = next(k for k in range(n) if f"节{k}」" in user)  # 节标题 「节{i}」 寻址
        blocks = ",".join('{"type":"paragraph","hint":"p%d_%d"}' % (i, j) for j in range(i + 1))
        return '{"blocks":[%s]}' % blocks

    return call_llm, lambda: state["peak"]


def test_expand_regions_concurrent_bounded_and_positional():
    # 各区并发展开,gather 按作业序回收 → 第 i 区结果(i+1 块)回填到自身位置(顺序无关)。
    n = 4
    skel = _open_region_sections(n)
    call_llm, peak = _peak_tracking_region_llm(n)
    res = asyncio.run(expand_open_regions(skel, "src", call_llm, concurrency=2))

    secs = res.skeleton["sections"]
    for i in range(n):
        assert len(secs[i]["blocks"]) == i + 1  # 第 i 区结果落回自身位置(块数=身份)
        assert all(b["type"] == "paragraph" for b in secs[i]["blocks"])
    assert res.ok_regions == n
    assert res.errors == []
    assert peak() > 1  # 确有并发(非退化串行)
    assert peak() <= 2  # 不越并发上限


def test_expand_concurrency_one_is_serial():
    # concurrency=1 → 信号量封顶 1,同时在飞恒为 1(退化串行),回填仍正确。
    n = 3
    skel = _open_region_sections(n)
    call_llm, peak = _peak_tracking_region_llm(n)
    res = asyncio.run(expand_open_regions(skel, "src", call_llm, concurrency=1))

    secs = res.skeleton["sections"]
    for i in range(n):
        assert len(secs[i]["blocks"]) == i + 1
    assert res.ok_regions == n
    assert peak() == 1  # 串行:任一时刻至多一调在飞


# ----------------------------------------------------------------------------
# generate_skeleton — 编排
# ----------------------------------------------------------------------------


def test_generate_outline_then_sections():
    outline = '{"title":"Annual","sections":[{"title":"Intro","layout":"full","intent":"overview"},{"title":"Trend","layout":"full","intent":"yearly"}]}'
    sec1 = '{"blocks":[{"type":"paragraph","hint":"intro"}]}'
    sec2 = '{"blocks":[{"type":"chart","chartType":"line","xAxisKey":"year","series":[{"dataKey":"rev"}],"hint":"yearly"}]}'
    call_llm, calls = _make_call_llm([outline, sec1, sec2])
    res = asyncio.run(generate_skeleton("report text", call_llm))

    assert res.used_fallback is False
    assert res.skeleton["title"] == "Annual"
    assert len(res.skeleton["sections"]) == 2
    assert res.skeleton["sections"][0]["title"] == "Intro"
    assert res.skeleton["sections"][1]["blocks"][0]["type"] == "chart"
    assert res.skeleton["theme"]["colorPalette"][0] == "#1677ff"
    assert len(calls) == 3  # 1 大纲 + 2 节


def test_generate_layout_first_mode_emits_open_regions():
    # mode='layout':复用大纲→逐节编排,但逐节产 open-region 占位(brief = 角色 + 组件)。
    outline = '{"title":"R","sections":[{"title":"概览","layout":"full","intent":"recap"},{"title":"趋势","layout":"two-column","intent":"trend"}]}'
    sec1 = '{"blocks":[{"type":"open-region","hint":"最重要的 KPI,指标卡组"}]}'
    sec2 = '{"blocks":[{"type":"open-region","hint":"主指标趋势,折线图"},{"type":"open-region","hint":"构成占比,饼图"}]}'
    call_llm, calls = _make_call_llm([outline, sec1, sec2])
    res = asyncio.run(generate_skeleton("report text", call_llm, mode="layout"))

    assert res.used_fallback is False
    secs = res.skeleton["sections"]
    assert len(secs) == 2
    s1_blocks = secs[0]["blocks"]
    assert [b["type"] for b in s1_blocks] == ["open-region"]
    assert s1_blocks[0]["annotation"] == "最重要的 KPI,指标卡组"
    assert "fields" not in s1_blocks[0]  # 占位块不带 concrete 框架
    assert [b["type"] for b in secs[1]["blocks"]] == ["open-region", "open-region"]
    assert len(calls) == 3  # 1 大纲 + 2 节
    # 布局优先默认:盖 layoutFirst 信号 + 报告/有标题小节标题为模型态(运行时按新源文重生成)
    assert res.skeleton["layoutFirst"] is True
    assert res.skeleton["titleDirective"]["mode"] == "llm"
    assert secs[0]["titleDirective"]["mode"] == "llm"
    assert secs[1]["titleDirective"]["mode"] == "llm"
    # 大纲 intent(样报口径)不落到小节注解:布局优先不携带,免设计器误导 + 填值弱扰
    assert "annotation" not in secs[0]
    assert "annotation" not in secs[1]


def test_generate_layout_first_fallback_emits_open_regions():
    # 大纲失败 → 布局优先回退:整篇单次生成,各节 blocks 仍为 open-region。
    whole = '{"title":"W","sections":[{"layout":"full","blocks":[{"type":"open-region","hint":"概览,指标卡组"}]}]}'
    call_llm, calls = _make_call_llm(["not json — outline parse fails", whole])
    res = asyncio.run(generate_skeleton("text", call_llm, mode="layout"))
    assert res.used_fallback is True
    blk = res.skeleton["sections"][0]["blocks"][0]
    assert blk["type"] == "open-region"
    assert blk["annotation"] == "概览,指标卡组"
    assert len(calls) == 2  # 大纲尝试 + 整篇回退


def test_layout_first_prompts_demand_source_language_hints():
    # few-shot 样例是英文,会把模型带去英文 annotation;两个布局优先系统头必须显式要求
    # hint 跟随源文语言(中文源→中文 hint),否则生成区在设计器里全是英文。
    section_sys = build_layout_first_section_messages("报告正文", {"title": "概览", "intent": "recap"})[0]["content"]
    fallback_sys = build_layout_first_skeleton_messages("报告正文")[0]["content"]
    assert "与源文相同的语言" in section_sys
    assert "与源文相同的语言" in fallback_sys


def test_generate_section_parse_failure_skipped():
    outline = '{"title":"R","sections":[{"title":"A","layout":"full"},{"title":"B","layout":"full"}]}'
    call_llm, _ = _make_call_llm([outline, "garbage", '{"blocks":[{"type":"paragraph","hint":"b"}]}'])
    res = asyncio.run(generate_skeleton("text", call_llm))
    assert len(res.skeleton["sections"]) == 1  # A 失败跳过,B 保留
    assert res.skeleton["sections"][0]["title"] == "B"
    assert len(res.errors) == 1


def test_generate_outline_failure_falls_back_to_whole():
    whole = '{"title":"Whole","sections":[{"layout":"full","blocks":[{"type":"paragraph","hint":"x"}]}]}'
    call_llm, calls = _make_call_llm(["not json — outline parse fails", whole])
    res = asyncio.run(generate_skeleton("text", call_llm))
    assert res.used_fallback is True
    assert res.skeleton["title"] == "Whole"
    assert len(calls) == 2  # 大纲尝试 + 整篇回退


def test_generate_all_sections_fail_raises():
    outline = '{"title":"R","sections":[{"title":"A","layout":"full"}]}'
    call_llm, _ = _make_call_llm([outline, "garbage"])
    with pytest.raises(GenerateError):
        asyncio.run(generate_skeleton("text", call_llm))


# ----------------------------------------------------------------------------
# 算子路径组合(html_report._invoke_async 的 expand → fill 序列,免 canvas/DB)
# ----------------------------------------------------------------------------


def test_expand_then_fill_composition():
    """生成区被展开成 stat-card,且其 value 槽被模型填入。call_llm 按调用类型分流。"""
    skel = {
        "title": "Q",
        "sections": [{"id": "s", "layout": "full", "blocks": [{"id": "og", "type": "open-region", "annotation": "one KPI"}]}],
    }

    async def call_llm(messages):
        user = messages[-1]["content"]
        if "本区域的块" in user:  # 展开调用
            return '{"blocks":[{"type":"stat-card","label":"Revenue","hint":"total revenue"}]}'
        # 填值调用:槽键含动态 block id,从消息里抓出来再回填
        m = re.search(r"blk-[0-9a-f]+__value", user)
        return json.dumps({m.group(0): "1.2亿"} if m else {})

    async def run():
        expanded = await expand_open_regions(skel, "src", call_llm)
        return await fill_skeleton(expanded.skeleton, "src", lambda _r: None, call_llm)

    result = asyncio.run(run())
    blocks = result.schema["sections"][0]["blocks"]
    assert [b["type"] for b in blocks] == ["stat-card"]  # 生成区已展开,无 open-region 残留
    assert blocks[0]["label"] == "Revenue"
    assert blocks[0]["value"] == "1.2亿"  # 填值生效

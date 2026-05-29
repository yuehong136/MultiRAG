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


def test_normalize_block_sidebar_role():
    assert normalize_block({"type": "paragraph", "role": "side"}, True)["role"] == "side"
    assert normalize_block({"type": "paragraph"}, True)["role"] == "main"
    assert "role" not in normalize_block({"type": "paragraph"}, False)


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
    assert sk["theme"]["primaryColor"] == "#1677ff"
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
    assert res.skeleton["theme"]["primaryColor"] == "#1677ff"
    assert len(calls) == 3  # 1 大纲 + 2 节


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
        if "Build ONLY this region" in user:  # 展开调用
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

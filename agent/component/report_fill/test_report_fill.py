"""
HTMLReport 纯填值逻辑单测(agent/component/report_fill)。

桩注入 call_llm / resolve_ref,不触网、不依赖 canvas/DB。对齐前端 schema-fill 单测口径:
路径写入 + merge、coerce(枚举回落 / 行补齐 / chartData 数值化)、变量解析、某节失败跳过、
全静态不调模型、生成区过滤。异步 fill_skeleton 用 asyncio.run 跑,免依赖 pytest-asyncio。
"""

import asyncio

from agent.component.report_fill.fill import fill_skeleton
from agent.component.report_fill.prompt_builder import build_fill_schema, collect_fill_plan
from agent.component.report_fill.skeleton import (
    chart_row_keys,
    merge_skeleton,
    set_field_value,
)

SKELETON = {
    "title": "Q3 报告",
    "sections": [
        {
            "id": "sec1",
            "title": "概览",
            "layout": "full",
            "blocks": [
                {
                    "id": "blkA",
                    "type": "stat-card",
                    "fields": {"label": "总营收", "value": ""},
                    "fieldDirectives": {
                        "value": {"mode": "llm", "hint": "Q3 总营收"},
                        "trend": {"mode": "llm"},
                        "change": {"mode": "variable", "ref": "node1@output.delta"},
                    },
                },
                {"id": "blkB", "type": "paragraph", "fields": {"content": "固定说明"}},
            ],
        },
        {
            "id": "sec2",
            "title": "明细",
            "layout": "full",
            "blocks": [
                {
                    "id": "blkT",
                    "type": "table",
                    "fields": {"headers": ["指标", "值"], "rows": []},
                    "fieldDirectives": {"rows": {"mode": "llm"}},
                }
            ],
        },
    ],
}


def _make_call_llm(responses):
    seq = list(responses)
    calls = []

    async def call_llm(messages):
        calls.append(messages)
        return seq.pop(0)

    return call_llm, calls


# ----------------------------------------------------------------------------
# skeleton.py
# ----------------------------------------------------------------------------


def test_set_field_value_nested_array():
    assert set_field_value({}, "items[1].value", "x") == {"items": [None, {"value": "x"}]}


def test_set_field_value_is_immutable():
    src = {"a": {"b": 1}}
    out = set_field_value(src, "a.c", 2)
    assert out == {"a": {"b": 1, "c": 2}}
    assert src == {"a": {"b": 1}}  # 原对象不被改


def test_chart_row_keys_cartesian():
    block = {"type": "chart", "fields": {"xAxisKey": "month", "series": [{"dataKey": "rev"}]}}
    assert chart_row_keys(block) == ("month", ["rev"])


def test_chart_row_keys_radar_and_fallback():
    block = {"type": "chart", "fields": {"radarKeys": ["dim"], "series": [{"dataKey": "a"}, {"dataKey": "b"}]}}
    assert chart_row_keys(block) == ("dim", ["a", "b"])
    assert chart_row_keys({"type": "chart", "fields": {}}) == ("name", ["value"])


def test_merge_skeleton_filters_open_region_and_keeps_role():
    skel = {
        "title": "t",
        "sections": [
            {
                "id": "s",
                "layout": "sidebar-left",
                "blocks": [
                    {"id": "og", "type": "open-region", "annotation": "brief"},
                    {"id": "b", "type": "paragraph", "role": "side", "fields": {"content": "x"}},
                ],
            }
        ],
    }
    schema = merge_skeleton(skel, {})
    blocks = schema["sections"][0]["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["id"] == "b"
    assert blocks[0]["role"] == "side"


# ----------------------------------------------------------------------------
# prompt_builder.py
# ----------------------------------------------------------------------------


def test_collect_plan_excludes_variable_and_static():
    plan = collect_fill_plan(SKELETON["sections"][0])
    assert {it.key for it in plan.items} == {"blkA__value", "blkA__trend"}


def test_build_fill_schema_shapes():
    plan = collect_fill_plan(SKELETON["sections"][0])
    schema = build_fill_schema(plan)
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"blkA__value", "blkA__trend"}
    assert schema["properties"]["blkA__trend"]["enum"] == ["up", "down", "neutral"]
    assert schema["properties"]["blkA__value"]["type"] == "string"


# ----------------------------------------------------------------------------
# fill.py — 端到端编排
# ----------------------------------------------------------------------------


def test_fill_end_to_end():
    call_llm, calls = _make_call_llm(
        [
            '{"blkA__value": "1.2亿", "blkA__trend": "rising"}',  # rising 非法 → 回落
            '```json\n{"blkT__rows": [["营收","1.2亿"],["利润"]]}\n```',  # 第二行补齐到 2 列
        ]
    )

    def resolve_ref(ref):
        return "+12%" if ref == "node1@output.delta" else None

    result = asyncio.run(fill_skeleton(SKELETON, "源料正文", resolve_ref, call_llm))

    assert result.llm_sections == 2
    assert result.ok_sections == 2
    assert result.errors == []
    assert len(calls) == 2

    secs = {s["id"]: s for s in result.schema["sections"]}
    blk_a = secs["sec1"]["blocks"][0]
    assert blk_a["value"] == "1.2亿"  # llm 填入
    assert blk_a["trend"] == "neutral"  # 非法枚举回落
    assert blk_a["change"] == "+12%"  # variable 解析
    assert blk_a["label"] == "总营收"  # static 保留
    assert secs["sec1"]["blocks"][1]["content"] == "固定说明"  # 全静态块原样
    assert secs["sec2"]["blocks"][0]["rows"] == [["营收", "1.2亿"], ["利润", ""]]


def test_all_static_skeleton_does_not_call_llm():
    skel = {
        "title": "t",
        "sections": [{"id": "s", "layout": "full", "blocks": [{"id": "b", "type": "paragraph", "fields": {"content": "x"}}]}],
    }
    called = False

    async def call_llm(_messages):
        nonlocal called
        called = True
        return "{}"

    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm))
    assert called is False
    assert result.llm_sections == 0
    assert result.schema["sections"][0]["blocks"][0]["content"] == "x"


def test_section_failure_is_skipped_others_kept():
    skel = {
        "title": "t",
        "sections": [
            {
                "id": "s1",
                "layout": "full",
                "blocks": [{"id": "b1", "type": "paragraph", "fields": {"content": ""}, "fieldDirectives": {"content": {"mode": "llm"}}}],
            },
            {
                "id": "s2",
                "layout": "full",
                "blocks": [{"id": "b2", "type": "paragraph", "fields": {"content": ""}, "fieldDirectives": {"content": {"mode": "llm"}}}],
            },
        ],
    }
    call_llm, _ = _make_call_llm(["not json at all", '{"b2__content":"ok"}'])
    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm))

    assert result.llm_sections == 2
    assert result.ok_sections == 1
    assert len(result.errors) == 1
    secs = {s["id"]: s for s in result.schema["sections"]}
    assert secs["s1"]["blocks"][0]["content"] == ""  # 失败 → 留静态值
    assert secs["s2"]["blocks"][0]["content"] == "ok"


def test_chart_data_coercion():
    skel = {
        "title": "t",
        "sections": [
            {
                "id": "s",
                "layout": "full",
                "blocks": [
                    {
                        "id": "c",
                        "type": "chart",
                        "fields": {"chartType": "bar", "xAxisKey": "month", "series": [{"dataKey": "rev"}], "data": []},
                        "fieldDirectives": {"data": {"mode": "llm"}},
                    }
                ],
            }
        ],
    }
    # "100"(字符串)→ 数值;非法/缺值 → 0
    call_llm, _ = _make_call_llm(['{"c__data":[{"month":"Jan","rev":"100"},{"month":"Feb","rev":"oops"}]}'])
    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm))
    data = result.schema["sections"][0]["blocks"][0]["data"]
    assert data == [{"month": "Jan", "rev": 100.0}, {"month": "Feb", "rev": 0}]

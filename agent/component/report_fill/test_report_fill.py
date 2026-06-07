"""
HTMLReport 纯填值逻辑单测(agent/component/report_fill)。

桩注入 call_llm / resolve_ref,不触网、不依赖 canvas/DB。对齐前端 schema-fill 单测口径:
路径写入 + merge、coerce(枚举回落 / 行补齐 / chartData 数值化)、变量解析、某节失败跳过、
全静态不调模型、生成区过滤。异步 fill_skeleton 用 asyncio.run 跑,免依赖 pytest-asyncio。
"""

import asyncio
import json

from agent.component.report_fill.fill import (
    _SKIP,
    DEFAULT_FILL_CONCURRENCY,
    _coerce_value,
    fill_skeleton,
    resolve_fill_concurrency,
)
from agent.component.report_fill.prompt_builder import (
    ValueSpec,
    build_fill_messages,
    build_fill_schema,
    collect_fill_plan,
    describe_section,
    spec_for,
)
from agent.component.report_fill.skeleton import (
    chart_row_keys,
    dedupe_sections,
    drop_admission_blocks,
    is_no_content_title,
    merge_block,
    merge_skeleton,
    set_field_value,
    strip_ordinal_prefix,
)

MINUS = "−"  # Unicode 减号 U+2212(区别于 ASCII 连字符 -)

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


def test_merge_skeleton_passes_through_hero_top_fields():
    # 设计器手选的头图 / eyebrow / 副标题须进 ReportSchema,否则真实报告丢 Hero。
    skel = {
        "title": "t",
        "eyebrow": "2026 年度",
        "subtitle": "一句话概述",
        "headerArt": "watercolor-book",
        "headerLayout": "card",
        "sections": [],
    }
    schema = merge_skeleton(skel, {})
    assert schema["eyebrow"] == "2026 年度"
    assert schema["subtitle"] == "一句话概述"
    assert schema["headerArt"] == "watercolor-book"
    assert schema["headerLayout"] == "card"


def test_merge_skeleton_omits_hero_top_fields_when_absent():
    # 未填则不应凭空出现这些键(保持 ReportSchema 干净,渲染回退纯文字 Hero)。
    schema = merge_skeleton({"title": "t", "sections": []}, {})
    for key in ("eyebrow", "subtitle", "headerArt", "headerLayout"):
        assert key not in schema


def test_merge_block_derives_trend_from_change_sign():
    def trend_of(change):
        blk = {"id": "x", "type": "stat-card", "fields": {"label": "L", "value": "v", "change": change}}
        return merge_block(blk, {}).get("trend")

    assert trend_of("+12.5%") == "up"
    assert trend_of(MINUS + "3.2%") == "down"  # Unicode 减号
    assert trend_of("-3%") == "down"  # ASCII 连字符
    assert trend_of("0%") == "neutral"
    assert trend_of("持平") is None  # 非数值:不臆测方向,渲染落灰
    assert trend_of("") is None


def test_merge_block_derives_trend_from_filled_change():
    # change 由 llm 空槽填入(真实路径):填完按符号补 trend
    blk = {
        "id": "x",
        "type": "stat-card",
        "fields": {"label": "营收", "value": ""},
        "fieldDirectives": {"value": {"mode": "llm"}, "change": {"mode": "llm"}},
    }
    out = merge_block(blk, {"value": "1.2亿", "change": "+12.5%"})
    assert out["change"] == "+12.5%"
    assert out["trend"] == "up"


def test_merge_block_keeps_explicit_trend():
    # 已显式 trend=neutral,即便 change 为正也不覆盖(尊重静态选择)
    blk = {"id": "x", "type": "stat-card", "fields": {"label": "L", "value": "v", "change": "+5%", "trend": "neutral"}}
    assert merge_block(blk, {}).get("trend") == "neutral"


def test_merge_block_group_derives_trend_per_item():
    blk = {
        "id": "g",
        "type": "stat-card-group",
        "fields": {"items": [{"label": "营收"}, {"label": "流失率"}, {"label": "客户数"}]},
    }
    out = merge_block(blk, {"items[0].change": "+8%", "items[1].change": "-2%"})
    items = out["items"]
    assert items[0]["change"] == "+8%" and items[0]["trend"] == "up"
    assert items[1]["change"] == "-2%" and items[1]["trend"] == "down"
    assert "trend" not in items[2]  # 无 change 的 item 不补 trend


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


def test_spec_for_stat_value_and_change_are_text_like():
    # 指标卡 value→metric、change→change(都按字符串处理,不被误当 chartData)
    card = {"type": "stat-card", "fields": {"label": "L"}}
    assert spec_for(card, "value").kind == "metric"
    assert spec_for(card, "change").kind == "change"
    grp = {"type": "stat-card-group", "fields": {"items": [{"label": "A"}]}}
    assert spec_for(grp, "items[0].value").kind == "metric"
    assert spec_for(grp, "items[0].change").kind == "change"
    # 强转:metric/change 当文本透传(非 _SKIP、非数组)
    assert _coerce_value(ValueSpec(kind="metric"), "38600 人") == "38600 人"
    assert _coerce_value(ValueSpec(kind="change"), "+4.3%") == "+4.3%"


def test_describe_section_surfaces_group_item_labels():
    # stat-card-group 每个 item 槽要缀上该项静态 label,模型据此对号入座而非按位置猜。
    section = {
        "id": "s",
        "title": "关键指标",
        "layout": "full",
        "blocks": [
            {
                "id": "g",
                "type": "stat-card-group",
                "fields": {"items": [{"label": "营收"}, {"label": "流失率"}]},
                "fieldDirectives": {
                    "items[0].value": {"mode": "llm"},
                    "items[0].change": {"mode": "llm"},
                    "items[1].value": {"mode": "llm"},
                },
            }
        ],
    }
    text = describe_section(section, collect_fill_plan(section))
    assert "第 1 项 (营收) value" in text
    assert "第 1 项 (营收) change" in text
    assert "第 2 项 (流失率) value" in text
    assert "第 1 项 value" not in text  # 不再是裸位置名


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


# ----------------------------------------------------------------------------
# fill.py — 并发上限(asyncio.gather + Semaphore)
# ----------------------------------------------------------------------------


def _n_llm_sections(n: int) -> dict:
    """n 个各含一个 llm 段的小节;每段 key 为 p{i}__content。"""
    return {
        "title": "t",
        "sections": [{"id": f"s{i}", "layout": "full", "blocks": [_llm_para(f"p{i}")]} for i in range(n)],
    }


def _peak_tracking_llm(payload: str):
    """记录同时在飞调用峰值的桩:进入计数 +1、让出事件循环制造重叠、退出 -1。
    回固定 payload(各节按自身 key 取值,故与到达顺序无关)。返回 (call_llm, peak_getter)。"""
    state = {"in_flight": 0, "peak": 0}

    async def call_llm(_messages):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        await asyncio.sleep(0)  # 让出循环,逼出真重叠
        state["in_flight"] -= 1
        return payload

    return call_llm, lambda: state["peak"]


def test_concurrent_fill_is_bounded_and_order_independent():
    # 每调回「全键 superset」,各节都能取到自身 key → 不依赖响应到达顺序,正好验并发的顺序无关性。
    n = 6
    skel = _n_llm_sections(n)
    superset = json.dumps({f"p{i}__content": f"内容{i}" for i in range(n)})
    call_llm, peak = _peak_tracking_llm(superset)

    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm, concurrency=3))

    secs = {s["id"]: s for s in result.schema["sections"]}
    for i in range(n):
        assert secs[f"s{i}"]["blocks"][0]["content"] == f"内容{i}"  # 各节取到自身 key(顺序无关)
    assert result.ok_sections == n
    assert peak() > 1  # 确有并发(非退化串行)
    assert peak() <= 3  # 不越并发上限


def test_concurrency_one_is_serial():
    # concurrency=1 → 信号量封顶 1,同时在飞恒为 1(退化串行),结果仍正确。
    n = 4
    skel = _n_llm_sections(n)
    superset = json.dumps({f"p{i}__content": f"内容{i}" for i in range(n)})
    call_llm, peak = _peak_tracking_llm(superset)

    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm, concurrency=1))

    assert result.ok_sections == n
    assert peak() == 1  # 串行:任一时刻至多一调在飞


def test_concurrency_zero_degrades_to_serial_not_crash():
    # 非法并发数(<=0)被钳到 1,不抛、结果正确。
    skel = _n_llm_sections(3)
    superset = json.dumps({f"p{i}__content": f"内容{i}" for i in range(3)})
    call_llm, peak = _peak_tracking_llm(superset)

    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm, concurrency=0))

    assert result.ok_sections == 3
    assert peak() == 1


def test_resolve_fill_concurrency_node_config():
    # 关并行 → 1(无视 cap);开并行 → max(1, cap)。
    assert resolve_fill_concurrency(False, 8) == 1
    assert resolve_fill_concurrency(True, 8) == 8
    assert resolve_fill_concurrency(True, 1) == 1
    assert resolve_fill_concurrency(True, 0) == 1  # 钳到 >=1
    assert resolve_fill_concurrency(True, -3) == 1


def test_resolve_fill_concurrency_bad_cap_falls_back():
    # cap 非整数 → 并行时回落默认,串行时仍 1。
    assert resolve_fill_concurrency(True, "x") == DEFAULT_FILL_CONCURRENCY  # type: ignore[arg-type]
    assert resolve_fill_concurrency(False, "x") == 1  # type: ignore[arg-type]


def test_resolve_fill_concurrency_env_ceiling_only_clamps_down():
    # env 硬上限:只向下钳,绝不反超节点配置;非整数忽略。
    assert resolve_fill_concurrency(True, 8, "4") == 4  # env 4 < cap 8 → 钳到 4
    assert resolve_fill_concurrency(True, 2, "10") == 2  # env 10 > cap 2 → 不反超,仍 2
    assert resolve_fill_concurrency(False, 8, "4") == 1  # 关并行,env 不能把它拉上去
    assert resolve_fill_concurrency(True, 8, "bad") == 8  # 非整数 env 忽略
    assert resolve_fill_concurrency(True, 8, "") == 8  # 空 env 忽略
    assert resolve_fill_concurrency(True, 8, "0") == 1  # env 0 → 钳到 >=1 后取 min


# ----------------------------------------------------------------------------
# fill.py — 报告标题(titleDirective.mode=='llm')
# ----------------------------------------------------------------------------

# 全静态节(无 fieldDirectives)→ 不产生逐节调用,唯一的 LLM 调用就是标题这一调。
_STATIC_SECTIONS = [{"id": "s", "layout": "full", "blocks": [{"id": "b", "type": "paragraph", "fields": {"content": "x"}}]}]


def test_fill_title_llm_generates_and_overrides():
    skel = {"title": "占位标题", "titleDirective": {"mode": "llm", "hint": "按主题命名"}, "sections": _STATIC_SECTIONS}
    call_llm, calls = _make_call_llm(['{"title": "云岭市文旅报告"}'])
    result = asyncio.run(fill_skeleton(skel, "源料正文", lambda _r: None, call_llm))
    assert result.schema["title"] == "云岭市文旅报告"  # 模型生成覆盖静态占位
    assert result.errors == []
    assert result.llm_sections == 0  # 全静态节不计
    assert len(calls) == 1  # 仅标题这一调


def test_fill_title_static_keeps_title_and_skips_call():
    skel = {"title": "固定标题", "sections": _STATIC_SECTIONS}  # 无 titleDirective
    called = False

    async def call_llm(_messages):
        nonlocal called
        called = True
        return "{}"

    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm))
    assert result.schema["title"] == "固定标题"
    assert called is False  # 静态标题不调模型


def test_fill_title_llm_failure_falls_back_to_static():
    skel = {"title": "回落标题", "titleDirective": {"mode": "llm", "hint": "x"}, "sections": _STATIC_SECTIONS}
    call_llm, _ = _make_call_llm(["not json at all"])
    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm))
    assert result.schema["title"] == "回落标题"  # 解析失败 → 回落静态
    assert len(result.errors) == 1  # 记一条软告警,不判败


# ----------------------------------------------------------------------------
# fill.py — 报告副标题(subtitleDirective.mode=='llm';对称 _fill_title)
# ----------------------------------------------------------------------------


def test_fill_subtitle_llm_generates_and_overrides():
    # 仅副标题为模型态(标题静态、全静态节)→ 唯一 LLM 调用就是副标题这一调。
    skel = {"title": "固定标题", "subtitle": "占位副标题", "subtitleDirective": {"mode": "llm", "hint": "一行概述"}, "sections": _STATIC_SECTIONS}
    call_llm, calls = _make_call_llm(['{"subtitle": "全年文旅总览与核心结论"}'])
    result = asyncio.run(fill_skeleton(skel, "源料正文", lambda _r: None, call_llm))
    assert result.schema["subtitle"] == "全年文旅总览与核心结论"  # 模型生成覆盖静态占位
    assert result.schema["title"] == "固定标题"  # 标题静态不变
    assert result.errors == []
    assert len(calls) == 1  # 仅副标题这一调


def test_fill_subtitle_static_keeps_and_skips_call():
    skel = {"title": "t", "subtitle": "静态副标题", "sections": _STATIC_SECTIONS}  # 无 subtitleDirective
    called = False

    async def call_llm(_messages):
        nonlocal called
        called = True
        return "{}"

    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm))
    assert result.schema["subtitle"] == "静态副标题"  # merge 透传静态值
    assert called is False  # 静态副标题不调模型


def test_fill_subtitle_llm_failure_falls_back_to_static():
    skel = {"title": "t", "subtitle": "回落副标题", "subtitleDirective": {"mode": "llm", "hint": "x"}, "sections": _STATIC_SECTIONS}
    call_llm, _ = _make_call_llm(["not json at all"])
    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm))
    assert result.schema["subtitle"] == "回落副标题"  # 解析失败 → 回落静态
    assert len(result.errors) == 1  # 记一条软告警,不判败


def test_fill_subtitle_absent_keeps_schema_clean():
    # 既无静态副标题也无指令 → 不凭空造 "subtitle" 键(与 merge_skeleton 缺省省略一致)。
    skel = {"title": "t", "sections": _STATIC_SECTIONS}
    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, _make_call_llm([])[0]))
    assert "subtitle" not in result.schema


# ----------------------------------------------------------------------------
# fill.py — 布局优先收缩(layoutFirst):空槽 → 丢块 → 丢节;coerce 空白 → _SKIP
# ----------------------------------------------------------------------------


def test_coerce_blank_and_empty_collections_skip():
    # 空白文本 / 空集合 → _SKIP(不进 filled),收缩时据此丢块;非空仍采用。
    assert _coerce_value(ValueSpec(kind="text"), "") is _SKIP
    assert _coerce_value(ValueSpec(kind="text"), "   ") is _SKIP
    assert _coerce_value(ValueSpec(kind="metric"), "") is _SKIP
    assert _coerce_value(ValueSpec(kind="rows", columns=2), []) is _SKIP
    assert _coerce_value(ValueSpec(kind="chartData", category="x", values=["y"]), []) is _SKIP
    assert _coerce_value(ValueSpec(kind="text"), "实") == "实"
    assert _coerce_value(ValueSpec(kind="rows", columns=2), [["a", "b"]]) == [["a", "b"]]


def _llm_para(block_id: str) -> dict:
    return {"id": block_id, "type": "paragraph", "fields": {"content": "占位"}, "fieldDirectives": {"content": {"mode": "llm"}}}


def test_build_fill_messages_layout_first_drops_sample_context():
    # 布局优先:样报口径的报告名 / 小节标题 / 注解 / 目录都不进填值 prompt(标题另在运行时按源文
    # 重生成),只留源文 + 已按源文重建的槽位;非布局优先照旧带上,确保改动只作用于布局优先。
    section = {
        "id": "s",
        "title": "学校概况",
        "annotation": "学校概况与办学定位",
        "layout": "full",
        "blocks": [_llm_para("p")],
    }
    plan = collect_fill_plan(section)
    common = dict(
        report_title="北辰大学发展报告",
        section=section,
        source_text="文旅源文正文",
        toc_titles=["学校概况", "办学规模"],
        plan=plan,
        schema=build_fill_schema(plan),
    )
    lf = build_fill_messages(**common, layout_first=True)[1]["content"]
    assert "只填充本节。" in lf
    assert "文旅源文正文" in lf  # 源文仍在
    for leaked in ("学校概况", "学校概况与办学定位", "北辰大学发展报告", "办学规模"):
        assert leaked not in lf

    base = build_fill_messages(**common)[1]["content"]  # 非布局优先:照旧带上整节口径
    assert "报告:「北辰大学发展报告」" in base
    assert "(主题:学校概况与办学定位)" in base
    assert "报告各节:学校概况 / 办学规模" in base


def test_layout_first_shrink_drops_empty_blocks_and_sections():
    skel = {
        "title": "t",
        "layoutFirst": True,
        "sections": [
            {"id": "s1", "title": "有料", "layout": "full", "blocks": [_llm_para("p1"), _llm_para("p2")]},
            {"id": "s2", "title": "无料", "layout": "full", "blocks": [_llm_para("p3")]},
        ],
    }
    # s1:p1 填实、p2 回空;s2:p3 回空 → 整节皆空
    call_llm, _ = _make_call_llm(['{"p1__content":"有内容","p2__content":""}', '{"p3__content":""}'])
    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm))
    secs = {s["id"]: s for s in result.schema["sections"]}
    assert set(secs) == {"s1"}  # s2 整节皆空 → 丢
    assert [b["id"] for b in secs["s1"]["blocks"]] == ["p1"]  # p2 空块 → 丢
    assert secs["s1"]["blocks"][0]["content"] == "有内容"


def test_non_layout_first_keeps_empty_blocks():
    # 非布局优先:空槽不丢,留骨架占位值(回归保护:coerce 空→_SKIP 不应改变非布局优先口径)。
    skel = {"title": "t", "sections": [{"id": "s", "layout": "full", "blocks": [_llm_para("p")]}]}
    call_llm, _ = _make_call_llm(['{"p__content":""}'])
    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm))
    assert result.schema["sections"][0]["blocks"][0]["content"] == "占位"


def test_fill_section_titles_batch_rename():
    skel = {
        "title": "报告",
        "layoutFirst": True,
        "sections": [
            {"id": "a", "title": "学校概况", "titleDirective": {"mode": "llm"}, "layout": "full", "blocks": [_llm_para("pa")]},
            {"id": "b", "title": "发展展望", "titleDirective": {"mode": "llm"}, "layout": "full", "blocks": [_llm_para("pb")]},
        ],
    }
    # 调用序:2 节填值(每节有料,免被收缩)→ 小节标题批量一调
    call_llm, calls = _make_call_llm(['{"pa__content":"城市概况内容"}', '{"pb__content":"展望内容"}', '{"titles":["城市概况","未来展望"]}'])
    result = asyncio.run(fill_skeleton(skel, "文旅源文", lambda _r: None, call_llm))
    secs = {s["id"]: s for s in result.schema["sections"]}
    assert secs["a"]["title"] == "城市概况"  # 按源文重命名
    assert secs["b"]["title"] == "未来展望"
    assert result.schema["title"] == "报告"  # 无报告 titleDirective → 静态不变
    assert len(calls) == 3  # 2 节填值 + 1 小节标题(无报告标题调用)


def test_fill_section_titles_count_mismatch_falls_back():
    skel = {
        "title": "报告",
        "layoutFirst": True,
        "sections": [{"id": "a", "title": "学校概况", "titleDirective": {"mode": "llm"}, "layout": "full", "blocks": [_llm_para("pa")]}],
    }
    call_llm, _ = _make_call_llm(['{"pa__content":"有料"}', '{"titles":["太多","了"]}'])  # 回 2 个、实际 1 节 → 不符
    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm))
    assert result.schema["sections"][0]["title"] == "学校概况"  # 回落静态
    assert len(result.errors) == 1  # 软告警,不判败


# ----------------------------------------------------------------------------
# skeleton.py — 后置确定性去重(布局优先:模板槽多于源文内容 → 多槽塌缩成重复块)
# ----------------------------------------------------------------------------


def test_dedupe_drops_structurally_identical_sibling_blocks():
    # 列表项相同(抬头不同)→ 判重留首份;条目不同 → 保留。
    sections = [
        {
            "id": "s",
            "layout": "full",
            "blocks": [
                {"id": "l1", "type": "list", "ordered": False, "items": ["a", "b"], "title": "战略方向"},
                {"id": "l2", "type": "list", "ordered": False, "items": ["a", "b"], "title": "发展规划"},  # 同项异抬头 → 重复
                {"id": "l3", "type": "list", "ordered": False, "items": ["a", "c"], "title": "其它"},  # 异项 → 保留
            ],
        }
    ]
    assert [b["id"] for b in dedupe_sections(sections)[0]["blocks"]] == ["l1", "l3"]


def test_dedupe_normalizes_whitespace_in_chart_data():
    # 两图数据相同、仅类别串空格不同(2021 学年 vs 2021学年)、抬头不同 → 仍判重。
    def chart(bid, label, title):
        return {
            "id": bid,
            "type": "chart",
            "chartType": "line",
            "xAxisKey": "学年",
            "series": [{"dataKey": "在校生人数"}],
            "data": [{"学年": label, "在校生人数": 100}],
            "title": title,
        }

    sections = [{"id": "s", "layout": "full", "blocks": [chart("c1", "2021学年", "增长趋势"), chart("c2", "2021 学年", "趋势")]}]
    assert [b["id"] for b in dedupe_sections(sections)[0]["blocks"]] == ["c1"]


def test_dedupe_is_global_and_drops_emptied_section():
    # 跨节去重:s2 唯一块与 s1 的块重复 → s2 整节丢。
    blk = {"type": "stat-card", "label": "总数", "value": "100"}
    sections = [
        {"id": "s1", "layout": "full", "blocks": [{**blk, "id": "a"}]},
        {"id": "s2", "layout": "full", "blocks": [{**blk, "id": "b"}]},
    ]
    assert [s["id"] for s in dedupe_sections(sections)] == ["s1"]


def test_dedupe_keeps_near_but_distinct_blocks():
    # 条目数不同的近似指标组 → 不判重(确定性,不臆测语义相同)。
    g1 = {"id": "g1", "type": "stat-card-group", "items": [{"label": "在校生", "value": "1"}]}
    g2 = {"id": "g2", "type": "stat-card-group", "items": [{"label": "在校生", "value": "1"}, {"label": "教师", "value": "2"}]}
    assert [b["id"] for b in dedupe_sections([{"id": "s", "layout": "full", "blocks": [g1, g2]}])[0]["blocks"]] == ["g1", "g2"]


def test_dedupe_in_fill_is_gated_on_layout_first():
    # 两段填出相同内容:布局优先 → 去重留一;非布局优先 → 都留(gated)。
    def skel(layout_first):
        s = {"title": "t", "sections": [{"id": "s", "layout": "full", "blocks": [_llm_para("p1"), _llm_para("p2")]}]}
        if layout_first:
            s["layoutFirst"] = True
        return s

    resp = '{"p1__content":"同样的话","p2__content":"同样的话"}'
    lf = asyncio.run(fill_skeleton(skel(True), "src", lambda _r: None, _make_call_llm([resp])[0]))
    assert [b["id"] for b in lf.schema["sections"][0]["blocks"]] == ["p1"]  # 去重
    nf = asyncio.run(fill_skeleton(skel(False), "src", lambda _r: None, _make_call_llm([resp])[0]))
    assert [b["id"] for b in nf.schema["sections"][0]["blocks"]] == ["p1", "p2"]  # 非布局优先不去重


# ----------------------------------------------------------------------------
# skeleton.py — 小节标题剥序号前缀(源文 markdown 抬头带进来,收缩后会断号)
# ----------------------------------------------------------------------------


def test_strip_ordinal_prefix():
    assert strip_ordinal_prefix("二、北辰大学办学规模与师资情况") == "北辰大学办学规模与师资情况"
    assert strip_ordinal_prefix("一、城市概况") == "城市概况"
    assert strip_ordinal_prefix("十一、其它") == "其它"
    assert strip_ordinal_prefix("1. Overview") == "Overview"
    assert strip_ordinal_prefix("（一）总览") == "总览"
    assert strip_ordinal_prefix("第一章 绪论") == "绪论"
    assert strip_ordinal_prefix("云岭市城市概况") == "云岭市城市概况"  # 无序号不动
    assert strip_ordinal_prefix("2025 年度报告") == "2025 年度报告"  # 数字非序号(无分隔符)不动
    assert strip_ordinal_prefix("三、") == "三、"  # 纯序号剥光 → 回落原值
    assert strip_ordinal_prefix("") == ""


def test_layout_first_strips_section_ordinal_in_fill():
    # 模型重命名仍带序号("三、X")→ 布局优先确定性剥掉。
    skel = {
        "title": "报告",
        "layoutFirst": True,
        "sections": [{"id": "a", "title": "二、产业规模", "titleDirective": {"mode": "llm"}, "layout": "full", "blocks": [_llm_para("pa")]}],
    }
    call_llm, _ = _make_call_llm(['{"pa__content":"有料"}', '{"titles":["三、北辰大学办学规模"]}'])
    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm))
    assert result.schema["sections"][0]["title"] == "北辰大学办学规模"


def test_non_layout_first_keeps_section_ordinal():
    # 非布局优先:不剥序号(gated,不改原口径)。
    skel = {"title": "t", "sections": [{"id": "a", "title": "二、产业规模", "layout": "full", "blocks": [_llm_para("pa")]}]}
    call_llm, _ = _make_call_llm(['{"pa__content":"有料"}'])
    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm))
    assert result.schema["sections"][0]["title"] == "二、产业规模"


# ----------------------------------------------------------------------------
# skeleton.py — 丢「无对应内容」自认占位小节(模型承认模板某节在新源文无对应)
# ----------------------------------------------------------------------------


def test_is_no_content_title():
    assert is_no_content_title("无对应内容")
    assert is_no_content_title("无相关数据")
    assert is_no_content_title("（无对应内容）")  # 出现即判
    assert is_no_content_title("No corresponding content")
    assert is_no_content_title("N/A")
    assert not is_no_content_title("北辰大学国际合作")
    assert not is_no_content_title("发展展望")
    assert not is_no_content_title("")
    assert not is_no_content_title(None)


def test_layout_first_drops_no_content_titled_section_in_fill():
    skel = {
        "title": "报告",
        "layoutFirst": True,
        "sections": [
            {"id": "keep", "title": "规模", "titleDirective": {"mode": "llm"}, "layout": "full", "blocks": [_llm_para("p1")]},
            {"id": "drop", "title": "文化资源", "titleDirective": {"mode": "llm"}, "layout": "full", "blocks": [_llm_para("p2")]},
        ],
    }
    # 节填值 ×2 → 小节标题批量(drop 节被模型标成「无对应内容」)
    call_llm, _ = _make_call_llm(['{"p1__content":"规模内容"}', '{"p2__content":"凑数内容"}', '{"titles":["北辰大学规模","无对应内容"]}'])
    result = asyncio.run(fill_skeleton(skel, "src", lambda _r: None, call_llm))
    assert [s["id"] for s in result.schema["sections"]] == ["keep"]  # 「无对应内容」节整节丢


# ----------------------------------------------------------------------------
# skeleton.py — 丢道歉占位块(generous 映射后模型对无料角色写的「未提及」散文,非空躲过收缩)
# ----------------------------------------------------------------------------


def test_drop_admission_blocks():
    sections = [
        {"id": "keep", "layout": "full", "blocks": [{"id": "p1", "type": "paragraph", "content": "北辰大学创建于 1958 年。"}]},
        {
            "id": "drop",
            "layout": "full",
            "blocks": [
                {"id": "l", "type": "list", "items": ["计算机学科学生数量未提及；机械工程未提及"]},
                {"id": "p2", "type": "paragraph", "content": "文本中未提及景区相关内容，无法提供介绍。"},
            ],
        },
        {
            "id": "mixed",
            "layout": "full",
            "blocks": [
                {"id": "p3", "type": "paragraph", "content": "正常内容。"},
                {"id": "p4", "type": "paragraph", "content": "源文本未提及该项。"},
            ],
        },
    ]
    out = {s["id"]: [b["id"] for b in s["blocks"]] for s in drop_admission_blocks(sections)}
    assert "drop" not in out  # 两块都是道歉 → 整节丢
    assert out["keep"] == ["p1"]
    assert out["mixed"] == ["p3"]  # 只丢道歉块 p4,正常块留


def test_admission_drop_in_fill_gated_on_layout_first():
    def skel(layout_first):
        s = {"title": "t", "sections": [{"id": "keep", "layout": "full", "blocks": [_llm_para("p1")]}, {"id": "adm", "layout": "full", "blocks": [_llm_para("p2")]}]}
        if layout_first:
            s["layoutFirst"] = True
        return s

    responses = ['{"p1__content":"真实内容"}', '{"p2__content":"源文本未提及该项内容"}']
    lf = asyncio.run(fill_skeleton(skel(True), "src", lambda _r: None, _make_call_llm(responses)[0]))
    assert [s["id"] for s in lf.schema["sections"]] == ["keep"]  # 布局优先:道歉块所在节丢
    nf = asyncio.run(fill_skeleton(skel(False), "src", lambda _r: None, _make_call_llm(responses)[0]))
    assert {s["id"] for s in nf.schema["sections"]} == {"keep", "adm"}  # 非布局优先:不丢

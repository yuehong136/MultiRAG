"""
生成区展开(纯,运行时前置 pass)。移植自前端 designer/ai-skeleton/expand-regions.ts。

把骨架里的 open-region 占位块按其 brief 交给模型,展开成真块(模板块:框架静态、内容标 llm),
产出一份「无生成区」的骨架,再交给 report_fill.fill_skeleton 填值。复用既有逐节生成机器:
build_region_messages(brief → 提示词)+ parse_section(文本 → 一节的块,经 normalize_block 归一)。
一个生成区 = 一次「单区域生成」,取回 blocks 在原位置替换,继承占位块 role(sidebar 分列)。
某区失败则丢弃该占位 + 记错,保其余。call_llm 注入式,零 IO、可单测。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agent.component.report_fill.skeleton import is_open_region

from .parse import SkeletonParseError, parse_section
from .prompt import build_layout_first_region_messages, build_region_messages

CallLLM = Callable[[list[dict[str, str]]], Awaitable[str]]


class ExpandError(Exception):
    """展开阶段错误(模型对某生成区没产出合法块等);某区失败即记一条,跳过保其余。"""


@dataclass
class ExpandResult:
    skeleton: dict[str, Any]
    errors: list[ExpandError] = field(default_factory=list)
    open_regions: int = 0  # 检测到的生成区总数
    ok_regions: int = 0  # 其中成功展开的区数


def _count_open_regions(skeleton: dict[str, Any]) -> int:
    return sum(1 for section in skeleton.get("sections") or [] for block in section.get("blocks") or [] if is_open_region(block))


def _all_blocks_open_region(skeleton: dict[str, Any]) -> bool:
    """全篇每个块都是 open-region ⇒ 这是布局优先骨架(无 flag 的旧骨架兜底探测)。"""
    blocks = [block for section in skeleton.get("sections") or [] for block in section.get("blocks") or []]
    return bool(blocks) and all(is_open_region(block) for block in blocks)


async def expand_open_regions(
    skeleton: dict[str, Any],
    source_text: str,
    call_llm: CallLLM,
    on_progress: Callable[[int, int], None] | None = None,
) -> ExpandResult:
    """骨架 + 源料 → 无生成区的骨架:逐个 open-region 调模型展开、按位置替换。

    布局优先(显式 layoutFirst 或全块皆 open-region)走 build_layout_first_region_messages:框架文字
    从新源文重建、不照搬 brief 主题,源文无料则容空(自然收缩);并把 layoutFirst 盖回返回骨架,
    让下游 fill/merge 据此收缩。否则走 build_region_messages(遵循作者亲写 brief,原行为不变)。"""
    total = _count_open_regions(skeleton)
    layout_first = bool(skeleton.get("layoutFirst")) or _all_blocks_open_region(skeleton)
    if total == 0:
        out = {**skeleton, "layoutFirst": True} if layout_first else skeleton
        return ExpandResult(skeleton=out, open_regions=0, ok_regions=0)

    errors: list[ExpandError] = []
    done = 0
    ok_regions = 0
    sections_out: list[dict[str, Any]] = []

    for section in skeleton.get("sections") or []:
        blocks_out: list[dict[str, Any]] = []
        for block in section.get("blocks") or []:
            if not is_open_region(block):
                blocks_out.append(block)
                continue
            done += 1
            if on_progress:
                on_progress(done, total)
            brief = (block.get("annotation") or "").strip()
            builder = build_layout_first_region_messages if layout_first else build_region_messages
            try:
                text = await call_llm(builder(source_text, section_title=section.get("title"), brief=brief))
                generated = parse_section(
                    text,
                    {"layout": section.get("layout"), "title": section.get("title"), "intent": brief},
                    allow_empty=layout_first,  # 布局优先:源文对此角色无料 → 0 块,不报错(收缩)
                )
                # 继承占位块 role(sidebar 分列);丢弃 parse_section 自造的节标题/注解。
                for gen in generated["blocks"]:
                    blocks_out.append({**gen, "role": block["role"]} if block.get("role") else gen)
                ok_regions += 1
            except SkeletonParseError as err:
                errors.append(ExpandError(str(err)))
            except Exception as err:  # noqa: BLE001 - 任何失败都降级为本区失败,保其余
                errors.append(ExpandError(str(err)))
        sections_out.append({**section, "blocks": blocks_out})

    out_skeleton = {**skeleton, "sections": sections_out}
    if layout_first:
        out_skeleton["layoutFirst"] = True  # 盖回信号,供 fill/merge 收缩
    return ExpandResult(
        skeleton=out_skeleton,
        errors=errors,
        open_regions=total,
        ok_regions=ok_regions,
    )

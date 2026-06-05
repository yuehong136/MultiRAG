"""
生成区展开(纯,运行时前置 pass)。移植自前端 designer/ai-skeleton/expand-regions.ts。

把骨架里的 open-region 占位块按其 brief 交给模型,展开成真块(模板块:框架静态、内容标 llm),
产出一份「无生成区」的骨架,再交给 report_fill.fill_skeleton 填值。复用既有逐节生成机器:
build_region_messages(brief → 提示词)+ parse_section(文本 → 一节的块,经 normalize_block 归一)。
一个生成区 = 一次「单区域生成」,取回 blocks 在原位置替换,继承占位块 role(sidebar 分列)。
某区失败则丢弃该占位 + 记错,保其余。call_llm 注入式,零 IO、可单测。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agent.component.report_fill.fill import DEFAULT_FILL_CONCURRENCY
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
    concurrency: int = DEFAULT_FILL_CONCURRENCY,
) -> ExpandResult:
    """骨架 + 源料 → 无生成区的骨架:各 open-region 并发调模型展开、按原位置回填。

    各生成区互不依赖(prompt 只吃源文 + 节标题 + brief),故经一个并发上限(`concurrency`,
    钳到 >=1;<=0 退化为串行)的单个 `asyncio.gather` 一起跑;`gather` 按作业顺序回收结果,
    据各区原 (sec_i, blk_i) 坐标回填 → 块序与并发完成序无关。某区失败丢弃其占位、记一条
    错误(按文档序),保其余。

    布局优先(显式 layoutFirst 或全块皆 open-region)走 build_layout_first_region_messages:框架文字
    从新源文重建、不照搬 brief 主题,源文无料则容空(自然收缩);并把 layoutFirst 盖回返回骨架,
    让下游 fill/merge 据此收缩。否则走 build_region_messages(遵循作者亲写 brief,原行为不变)。"""
    total = _count_open_regions(skeleton)
    layout_first = bool(skeleton.get("layoutFirst")) or _all_blocks_open_region(skeleton)
    if total == 0:
        out = {**skeleton, "layoutFirst": True} if layout_first else skeleton
        return ExpandResult(skeleton=out, open_regions=0, ok_regions=0)

    sections = skeleton.get("sections") or []
    # 收集所有 open-region 作业,记其 (sec_i, blk_i);并发展开后据此坐标回填,保留块序。
    jobs: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
    for si, section in enumerate(sections):
        for bi, block in enumerate(section.get("blocks") or []):
            if is_open_region(block):
                jobs.append((si, bi, section, block))

    sem = asyncio.Semaphore(max(1, int(concurrency)))
    done = 0

    async def expand_one(
        section: dict[str, Any], block: dict[str, Any]
    ) -> tuple[list[dict[str, Any]] | None, ExpandError | None]:
        nonlocal done
        brief = (block.get("annotation") or "").strip()
        builder = build_layout_first_region_messages if layout_first else build_region_messages
        try:
            async with sem:
                text = await call_llm(builder(source_text, section_title=section.get("title"), brief=brief))
            generated = parse_section(
                text,
                {"layout": section.get("layout"), "title": section.get("title"), "intent": brief},
                allow_empty=layout_first,  # 布局优先:源文对此角色无料 → 0 块,不报错(收缩)
            )
            # 继承占位块 role(sidebar 分列);丢弃 parse_section 自造的节标题/注解。
            blocks = [{**gen, "role": block["role"]} if block.get("role") else gen for gen in generated["blocks"]]
            result: tuple[list[dict[str, Any]] | None, ExpandError | None] = (blocks, None)
        except SkeletonParseError as err:
            result = (None, ExpandError(str(err)))
        except Exception as err:  # noqa: BLE001 - 任何失败都降级为本区失败,保其余
            result = (None, ExpandError(str(err)))
        if on_progress:
            # 单线程事件循环:此处读写 done 之间无 await,不会竞态。
            done += 1
            on_progress(done, total)
        return result

    results = await asyncio.gather(*[expand_one(section, block) for _, _, section, block in jobs])

    # 据 (sec_i, blk_i) 索引展开块;失败区不入表 → 回填时该占位贡献 0 块(等价丢弃占位)。
    replacement: dict[tuple[int, int], list[dict[str, Any]]] = {}
    errors: list[ExpandError] = []
    ok_regions = 0
    for (si, bi, _section, _block), (blocks, err) in zip(jobs, results):
        if err is not None:
            errors.append(err)
        else:
            ok_regions += 1
            replacement[(si, bi)] = blocks or []

    sections_out: list[dict[str, Any]] = []
    for si, section in enumerate(sections):
        blocks_out: list[dict[str, Any]] = []
        for bi, block in enumerate(section.get("blocks") or []):
            if is_open_region(block):
                blocks_out.extend(replacement.get((si, bi), []))  # 失败区 → 无贡献(丢弃占位)
            else:
                blocks_out.append(block)
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

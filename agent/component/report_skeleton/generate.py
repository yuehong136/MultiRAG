"""
AI 生成报告骨架的编排(纯)。移植自前端 designer/ai-skeleton/use-generate-skeleton.ts 的编排部分。

两步:① 大纲调用拿到分节;② 逐节调用拿到每节的块——复用同一 parser 顺序跑,拼成骨架。
大纲解析失败 → 回退「单次整篇生成」;某节解析失败 → 跳过保其余;全失败 → 抛 GenerateError。
call_llm 注入式(端点注入自身 LLM,测试注入桩),故本文件零 IO、可单测。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .parse import (
    DEFAULT_THEME,
    SkeletonParseError,
    parse_outline,
    parse_section,
    parse_skeleton_response,
)
from .prompt import (
    build_layout_first_section_messages,
    build_layout_first_skeleton_messages,
    build_outline_messages,
    build_section_messages,
    build_skeleton_messages,
)

CallLLM = Callable[[list[dict[str, str]]], Awaitable[str]]
# 进度回调:(phase, current, total);phase ∈ {"outline", "sections"}。
OnProgress = Callable[[str, int, int], None]


class GenerateError(Exception):
    """骨架生成失败(模型没吐合法 JSON,或全部节解析失败)。"""


def _mark_layout_first(skeleton: dict[str, Any]) -> dict[str, Any]:
    """布局优先骨架:盖上 layoutFirst 信号,并默认报告 + 有标题的小节标题为「模型态」——
    跨主题运行时按新源文重生成标题(静态原标题留作回落/设计器静态值)。"""
    skeleton["layoutFirst"] = True
    skeleton["titleDirective"] = {"mode": "llm"}
    for section in skeleton.get("sections") or []:
        if section.get("title"):
            section["titleDirective"] = {"mode": "llm"}
    return skeleton


@dataclass
class GenerateResult:
    skeleton: dict[str, Any]
    errors: list[str] = field(default_factory=list)  # 被跳过的节的解析错误
    used_fallback: bool = False  # 是否走了「大纲失败 → 整篇生成」回退


async def generate_skeleton(
    report_text: str,
    call_llm: CallLLM,
    on_progress: OnProgress | None = None,
    *,
    mode: str = "detailed",
) -> GenerateResult:
    """报告文本 → 可复用骨架模板:大纲 → 逐节 → 拼装;大纲失败回退整篇。

    mode='detailed'(默认)产 concrete 块;mode='layout'(布局优先)每块产 open-region 占位,
    内容 / label 留给运行时按 brief 重生 —— 复用同一编排,只切换逐节 / 回退的 prompt builder。
    """
    # ① 大纲:解析失败 → 回退整篇;调用 / 网络错向上抛(端点报错)。
    if on_progress:
        on_progress("outline", 0, 0)
    outline: dict[str, Any] | None = None
    try:
        outline = parse_outline(await call_llm(build_outline_messages(report_text)))
    except SkeletonParseError:
        outline = None

    # ② 回退:大纲失败 → 单次整篇生成(按 mode 选 concrete / 布局优先 builder)。
    if outline is None:
        fallback_messages = build_layout_first_skeleton_messages(report_text) if mode == "layout" else build_skeleton_messages(report_text)
        skeleton = parse_skeleton_response(await call_llm(fallback_messages))
        if mode == "layout":
            _mark_layout_first(skeleton)
        return GenerateResult(skeleton=skeleton, used_fallback=True)

    # ③ 逐节:某节解析失败跳过,保其余;调用 / 网络错向上抛。按 mode 选逐节 builder。
    section_builder = build_layout_first_section_messages if mode == "layout" else build_section_messages
    sections: list[dict[str, Any]] = []
    errors: list[str] = []
    outline_sections = outline["sections"]
    total = len(outline_sections)
    for i, outline_section in enumerate(outline_sections):
        if on_progress:
            on_progress("sections", i + 1, total)
        try:
            section = parse_section(await call_llm(section_builder(report_text, outline_section)), outline_section)
            sections.append(section)
        except SkeletonParseError as err:
            errors.append(str(err))

    if not sections:
        raise GenerateError("all sections failed to parse")

    skeleton = {
        "title": outline["title"],
        "sections": sections,
        "theme": dict(DEFAULT_THEME),
    }
    if mode == "layout":
        _mark_layout_first(skeleton)
    return GenerateResult(skeleton=skeleton, errors=errors)

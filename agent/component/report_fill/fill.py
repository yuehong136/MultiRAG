"""
运行时填值编排(纯、自包含)——骨架 + 源料 → 完整 ReportSchema。移植自前端 `schema-fill.ts`。

按节、顺序:① 变量字段(`variable`)用 resolve_ref 全局解析一次;② 逐节调 LLM 只补本节
`llm` 空槽(全篇源料 + 本节框架)→ 防御解析 + 按 ValueSpec 强转 → 累积;③ 确定性
merge_skeleton 回骨架。某节失败跳过,保其余。

`call_llm` / `resolve_ref` 都注入:后端注入真上游 + 自身 LLM,测试注入桩。故本文件零 IO、可单测。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import json_repair

from .prompt_builder import (
    FillPlan,
    ValueSpec,
    build_fill_messages,
    build_fill_schema,
    build_section_titles_messages,
    build_title_messages,
    collect_fill_plan,
    split_fill_key,
)
from .skeleton import dedupe_sections, drop_admission_blocks, is_no_content_title, merge_skeleton, strip_ordinal_prefix

# 调一次 LLM 返回累计文本;取变量真值。两者皆注入。
CallLLM = Callable[[list[dict[str, str]]], Awaitable[str]]
ResolveRef = Callable[[str], Any]


class FillError(Exception):
    """填值阶段错误(解析不出合法 JSON 等);某节失败即记一条,跳过保其余。"""


@dataclass
class FillResult:
    schema: dict[str, Any]
    errors: list[FillError] = field(default_factory=list)
    llm_sections: int = 0  # 有 llm 空槽、需调模型的节数
    ok_sections: int = 0  # 其中成功填好的节数


# ============================================================
# 防御解析 + 按 ValueSpec 强转
# ============================================================


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE)
    text = re.sub(r"<think>[\s\S]*$", "", text, flags=re.IGNORECASE).strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise FillError("no JSON object in model output")
    snippet = text[start : end + 1]
    try:
        parsed: Any = json.loads(snippet)
    except Exception:
        try:
            parsed = json_repair.loads(snippet)
        except Exception as exc:
            raise FillError("model output is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise FillError("model output is not a JSON object")
    return parsed


def _str_or_none(value: Any) -> str | None:
    """对应 TS `raw == null ? undefined : String(raw)`:仅 None 跳过。"""
    return None if value is None else str(value)


def _str_nullish(value: Any) -> str:
    """对应 TS `String(x ?? '')`:仅 None → 空串。"""
    return "" if value is None else str(value)


def _to_num(value: Any) -> float | int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else 0
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0
    return n if math.isfinite(n) else 0


def _fit_row(row: list[Any], columns: int) -> list[str]:
    """把一行补齐/截断到固定列数,每格转字符串。"""
    cells = [_str_nullish(c) for c in row]
    while len(cells) < columns:
        cells.append("")
    return cells[:columns]


# 哨兵:区分「跳过(留骨架原值)」与「合法的 None 值」。coerce 用它表达「不采用」。
_SKIP = object()


def _coerce_value(spec: ValueSpec, raw: Any) -> Any:
    """按 spec 强转模型给的原始值;无法采用返回 `_SKIP` → 留骨架原值。"""
    if spec.kind in ("text", "metric", "change"):
        text = _str_or_none(raw)
        return _SKIP if text is None or not text.strip() else text  # 空白槽不采用 → 收缩时丢块
    if spec.kind == "enum":
        s = raw if isinstance(raw, str) else ""
        if not s:
            return _SKIP
        return s if s in spec.options else spec.fallback
    if spec.kind == "rows":
        if not isinstance(raw, list):
            return _SKIP
        rows = [_fit_row(r, spec.columns) for r in raw if isinstance(r, list)]
        return rows if rows else _SKIP  # 空表 → 不采用 → 收缩时丢块
    if spec.kind == "criteria":
        if not isinstance(raw, list):
            return _SKIP
        out: list[dict[str, Any]] = []
        for c in raw:
            if not isinstance(c, dict):
                continue
            vals = c.get("values")
            out.append(
                {
                    "name": _str_nullish(c.get("name")),
                    "values": _fit_row(vals if isinstance(vals, list) else [], spec.columns),
                }
            )
        return out if out else _SKIP  # 空对比 → 不采用 → 收缩时丢块
    if spec.kind == "chartData":
        if not isinstance(raw, list):
            return _SKIP
        rows: list[dict[str, Any]] = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            row: dict[str, Any] = {spec.category: _str_nullish(r.get(spec.category))}
            for v in spec.values:
                row[v] = _to_num(r.get(v))
            rows.append(row)
        return rows if rows else _SKIP  # 空图数据 → 不采用 → 收缩时丢块
    return _SKIP


def _apply_fill_json(raw_text: str, plan: FillPlan) -> dict[str, dict[str, Any]]:
    """模型回的 JSON → 本节的 block_id→path→值;多余键忽略,缺键/非法跳过。"""
    obj = _extract_json_object(raw_text)
    out: dict[str, dict[str, Any]] = {}
    for item in plan.items:
        value = _coerce_value(item.spec, obj.get(item.key))
        if value is _SKIP:
            continue
        block_id, path = split_fill_key(item.key)
        out.setdefault(block_id, {})[path] = value
    return out


# ============================================================
# 变量解析 + 编排
# ============================================================


def _resolve_variable_fills(
    skeleton: dict[str, Any],
    resolve_ref: ResolveRef,
) -> dict[str, dict[str, Any]]:
    """全骨架的 `variable` 字段 → resolve_ref 取真值(不进 LLM)。"""
    out: dict[str, dict[str, Any]] = {}
    for section in skeleton.get("sections") or []:
        for block in section.get("blocks") or []:
            dirs = block.get("fieldDirectives") or {}
            for path, directive in dirs.items():
                directive = directive or {}
                if directive.get("mode") != "variable" or not directive.get("ref"):
                    continue
                value = resolve_ref(directive["ref"])
                if value is not None:
                    out.setdefault(block["id"], {})[path] = value
    return out


def _merge_into(target: dict[str, dict[str, Any]], src: dict[str, dict[str, Any]]) -> None:
    for block_id, fields in src.items():
        target[block_id] = {**target.get(block_id, {}), **fields}


async def _fill_title(
    skeleton: dict[str, Any],
    source_text: str,
    toc_titles: list[str],
    call_llm: CallLLM,
) -> tuple[str, FillError | None]:
    """titleDirective.mode=='llm' → 单调一次 LLM 生成报告标题;否则用骨架静态 title。
    失败(解析不出 / 调用异常)回落静态 title 并返回一条 FillError(软降级,不判败)。"""
    static_title = skeleton.get("title") or ""
    directive = skeleton.get("titleDirective") or {}
    if directive.get("mode") != "llm":
        return static_title, None
    messages = build_title_messages(
        source_text=source_text,
        hint=directive.get("hint") or "",
        toc_titles=toc_titles,
    )
    try:
        obj = _extract_json_object(await call_llm(messages))
        title = obj.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip(), None
        return static_title, None
    except FillError as err:
        return static_title, err
    except Exception as err:  # noqa: BLE001 - 任何调用失败都回落静态 title,保其余
        return static_title, FillError(str(err))


async def _fill_section_titles(
    skeleton: dict[str, Any],
    source_text: str,
    call_llm: CallLLM,
) -> tuple[dict[str, str], FillError | None]:
    """对 titleDirective.mode=='llm' 的有标题小节,**一次**批量按源文重命名(现标题当角色)。
    返回 {section_id: new_title}(仅成功重命名的);长度不符 / 调用失败回落静态标题并记一条软告警(不判败)。"""
    targets = [s for s in (skeleton.get("sections") or []) if (s.get("titleDirective") or {}).get("mode") == "llm" and s.get("title")]
    if not targets:
        return {}, None
    messages = build_section_titles_messages(
        source_text=source_text,
        current_titles=[s.get("title") or "" for s in targets],
        report_title=skeleton.get("title") or "",
    )
    try:
        obj = _extract_json_object(await call_llm(messages))
        titles = obj.get("titles")
        if not isinstance(titles, list) or len(titles) != len(targets):
            return {}, FillError("section-title count mismatch")
        out: dict[str, str] = {}
        for sec, new in zip(targets, titles):
            if isinstance(new, str) and new.strip():
                out[sec["id"]] = new.strip()
        return out, None
    except FillError as err:
        return {}, err
    except Exception as err:  # noqa: BLE001 - 任何调用失败都回落静态标题,保其余
        return {}, FillError(str(err))


async def fill_skeleton(
    skeleton: dict[str, Any],
    source_text: str,
    resolve_ref: ResolveRef,
    call_llm: CallLLM,
    on_progress: Callable[[int, int], None] | None = None,
) -> FillResult:
    """骨架 + 源料 → ReportSchema:变量全局解析 + 逐节 LLM 填空 + 确定性 merge。"""
    filled: dict[str, dict[str, Any]] = {}
    _merge_into(filled, _resolve_variable_fills(skeleton, resolve_ref))

    sections = skeleton.get("sections") or []
    toc_titles = [s.get("title") for s in sections if s.get("title")]
    errors: list[FillError] = []
    llm_sections = 0
    ok_sections = 0
    total = len(sections)

    for i, section in enumerate(sections):
        if on_progress:
            on_progress(i + 1, total)
        plan = collect_fill_plan(section)
        if not plan.items:  # 全静态/变量的节不调 LLM
            continue
        llm_sections += 1
        messages = build_fill_messages(
            report_title=skeleton.get("title") or "",
            section=section,
            source_text=source_text,
            toc_titles=toc_titles,
            plan=plan,
            schema=build_fill_schema(plan),
        )
        try:
            text = await call_llm(messages)
            _merge_into(filled, _apply_fill_json(text, plan))
            ok_sections += 1
        except FillError as err:
            errors.append(err)
        except Exception as err:  # noqa: BLE001 - 任何调用失败都降级为本节失败,保其余
            errors.append(FillError(str(err)))

    # 标题(模型态):小节标题批量一调 + 报告标题单调,merge 后覆盖(静态态为 no-op)。
    section_titles, sec_title_err = await _fill_section_titles(skeleton, source_text, call_llm)
    if sec_title_err:
        errors.append(sec_title_err)
    title_value, title_err = await _fill_title(skeleton, source_text, toc_titles, call_llm)
    if title_err:
        errors.append(title_err)

    schema = merge_skeleton(skeleton, filled)
    schema["title"] = title_value
    layout_first = bool(skeleton.get("layoutFirst"))
    if layout_first:
        # generous 映射后模型对无料角色写的道歉散文(非空,躲过收缩)→ 丢块(空节随之丢)。
        schema["sections"] = drop_admission_blocks(schema.get("sections") or [])
        # 模板槽位多于源文内容时多槽塌缩成重复块 → 确定性去重(空节随之丢)。
        schema["sections"] = dedupe_sections(schema.get("sections") or [])
    # 回写重命名后的小节标题(被收缩/去重丢掉的节自然不在 schema 里);
    # 布局优先再剥掉源文带来的序号前缀——收缩后序号会断(如 二/三/五/六/八)。
    for sec in schema.get("sections") or []:
        new_title = section_titles.get(sec.get("id")) if section_titles else None
        if new_title:
            sec["title"] = new_title
        if layout_first and sec.get("title"):
            sec["title"] = strip_ordinal_prefix(sec["title"])
    # 布局优先:模型把映不上的小节标成「无对应内容」之类自认占位 → 整节丢(连带其凑数内容)。
    if layout_first:
        schema["sections"] = [sec for sec in (schema.get("sections") or []) if not is_no_content_title(sec.get("title"))]
    return FillResult(
        schema=schema,
        errors=errors,
        llm_sections=llm_sections,
        ok_sections=ok_sections,
    )

"""
报告骨架内核(纯函数、零 IO):字段路径寻址 + 图表行键推导 + 确定性 merge。

移植自前端 `skeleton-utils.ts` 的「填值所需部分」。merge 不依赖随机/时间,保证
「同一骨架 + 同一填充值」永远产出同一 ReportSchema。

字段路径约定(见 docs/html-report README「字段路径约定」):
    'value' / 'title'        顶层字段
    'items[0].value'         数组下标 + 子字段
    'series[1].dataKey'      嵌套数组
    'data'                   chart 的整段数据数组
"""

from __future__ import annotations

import json
import re
from typing import Any

OPEN_REGION = "open-region"

_INDEX_PATT = re.compile(r"\[(\d+)\]")


def is_open_region(block: dict[str, Any]) -> bool:
    """是否为生成区占位块(运行期不渲染,merge 时过滤)。"""
    return block.get("type") == OPEN_REGION


def parse_path(path: str) -> list[str | int]:
    """'items[0].value' -> ['items', 0, 'value'];全数字段视为数组下标。"""
    normalized = _INDEX_PATT.sub(r".\1", path)
    segments: list[str | int] = []
    for seg in normalized.split("."):
        if not seg:
            continue
        segments.append(int(seg) if seg.isdigit() else seg)
    return segments


def _set_in(target: Any, segments: list[str | int], value: Any) -> Any:
    """按路径段不可变写入;按需创建中间数组/对象(稀疏数组以 None 补位)。"""
    if not segments:
        return value
    head, rest = segments[0], segments[1:]
    if isinstance(head, int):
        arr = list(target) if isinstance(target, list) else []
        while len(arr) <= head:
            arr.append(None)
        arr[head] = _set_in(arr[head], rest, value)
        return arr
    obj = dict(target) if isinstance(target, dict) else {}
    obj[head] = _set_in(obj.get(head), rest, value)
    return obj


def set_field_value(target: Any, path: str, value: Any) -> Any:
    """按路径不可变写入,返回新对象。"""
    return _set_in(target, parse_path(path), value)


def chart_row_keys(block: dict[str, Any]) -> tuple[str, list[str]]:
    """
    图表行对象的字段名:类别键 + 一组数值键,由形状字段(xAxisKey / radarKeys /
    nameKey / valueKey / series)推导。填值导出行 schema、强转 chartData 都靠它——
    单一真源,避免漂移。与前端 `chartRowKeys` 一致。
    """
    f = block.get("fields") or {}
    radar = f.get("radarKeys") or []
    category = f.get("xAxisKey") or (radar[0] if radar else None) or f.get("nameKey") or "name"
    values: list[str] = []

    def add(key: Any) -> None:
        if key and key not in values:
            values.append(key)

    for series in f.get("series") or []:
        if series.get("xKey") and series.get("yKey"):
            add(series["xKey"])
            add(series["yKey"])
        elif series.get("dataKey"):
            add(series["dataKey"])
    if not values:
        add(f.get("valueKey") or "value")
    return category, values


_TREND_NUM_PATT = re.compile(r"^[+\-−]?\s*[\d,]+(?:\.\d+)?")


def _trend_from_change(change: Any) -> str | None:
    """变化率字符串 → 涨跌向(渲染器据此给红绿上色):带符号或前导数值才认,>0 up / <0 down / ==0 neutral。
    形如「持平」「—」等非数值文本认不出,返回 None(渲染落灰),宁可不上色也不上错色。"""
    if not isinstance(change, str):
        return None
    m = _TREND_NUM_PATT.match(change.strip())
    if not m:
        return None
    try:
        num = float(m.group(0).replace("−", "-").replace(",", "").replace(" ", ""))
    except ValueError:
        return None
    if num > 0:
        return "up"
    if num < 0:
        return "down"
    return "neutral"


def _apply_change_trend(node: dict[str, Any]) -> None:
    """指标卡填完 change 后据其符号补 trend;已显式带 trend 则保留(尊重作者/历史骨架的静态选择)。"""
    change = node.get("change")
    if not change or node.get("trend"):
        return
    trend = _trend_from_change(change)
    if trend:
        node["trend"] = trend


def merge_block(block: dict[str, Any], filled: dict[str, Any]) -> dict[str, Any]:
    """
    单个骨架 Block + 填好的叶子值 → 运行时 Block。以 `fields`(钉死的静态结构)为底,
    按路径覆盖填充值,最后回挂 `role`(渲染器侧栏布局靠 `role` 分主/侧列)。
    指标卡填完 change 后,按其正负号补 trend(渲染器据 trend 给变化率红绿上色)。
    """
    result: dict[str, Any] = dict(block.get("fields") or {})
    result["id"] = block.get("id")
    result["type"] = block.get("type")
    for path, value in filled.items():
        result = set_field_value(result, path, value)
    btype = result.get("type")
    if btype == "stat-card":
        _apply_change_trend(result)
    elif btype == "stat-card-group":
        for item in result.get("items") or []:
            if isinstance(item, dict):
                _apply_change_trend(item)
    if block.get("role"):
        result["role"] = block["role"]
    return result


def _block_is_empty(block: dict[str, Any], filled: dict[str, Any]) -> bool:
    """块有 llm 内容槽但无一被填(源文对其无料)⇒ 空块。纯静态 / 变量块永不算空。
    `filled` 的键即 directive 路径(与 collect_fill_plan / _apply_fill_json 同源),故可直接比对。"""
    directives = block.get("fieldDirectives") or {}
    llm_paths = [path for path, directive in directives.items() if (directive or {}).get("mode") == "llm"]
    if not llm_paths:
        return False
    return all(path not in filled for path in llm_paths)


def merge_skeleton(
    skeleton: dict[str, Any],
    filled_by_block: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """整份骨架 + 各 Block 的填充值 → 完整 ReportSchema(annotation 不进运行时)。

    布局优先(layoutFirst)收缩:源文无料的空块丢弃,整节皆空则整节丢——不输出道歉文案 / 空表 / 空壳节。
    非布局优先保持原行为(空槽留骨架占位值,不丢)。"""
    layout_first = bool(skeleton.get("layoutFirst"))
    sections_out: list[dict[str, Any]] = []
    for section in skeleton.get("sections") or []:
        blocks_out: list[dict[str, Any]] = []
        for block in section.get("blocks") or []:
            if is_open_region(block):
                continue
            filled = filled_by_block.get(block.get("id"), {})
            if layout_first and _block_is_empty(block, filled):
                continue  # 收缩:空块丢
            blocks_out.append(merge_block(block, filled))
        if layout_first and not blocks_out:
            continue  # 收缩:整节皆空则整节丢
        sec: dict[str, Any] = {
            "id": section.get("id"),
            "layout": section.get("layout"),
            "blocks": blocks_out,
        }
        if section.get("title") is not None:
            sec["title"] = section.get("title")
        if section.get("subtitle") is not None:
            sec["subtitle"] = section.get("subtitle")
        sections_out.append(sec)

    out: dict[str, Any] = {"title": skeleton.get("title"), "sections": sections_out}
    # Hero 顶层字段(设计器静态填):透传进 ReportSchema,否则真实报告丢头图 / eyebrow / 副标题。
    for key in ("eyebrow", "subtitle", "headerArt", "headerLayout"):
        if skeleton.get(key) is not None:
            out[key] = skeleton.get(key)
    if skeleton.get("theme") is not None:
        out["theme"] = skeleton.get("theme")
    return out


# 内容指纹排除的非内容噪声键(id 唯一、title 是抬头、role 是布局)。
_SIG_EXCLUDE = {"id", "title", "role"}


def _strip_ws(value: Any) -> Any:
    """递归折叠字符串里的所有空白(中英文空格/换行),使「2021 学年」与「2021学年」同指纹。"""
    if isinstance(value, str):
        return re.sub(r"\s+", "", value)
    if isinstance(value, list):
        return [_strip_ws(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_ws(v) for k, v in value.items()}
    return value


def _content_signature(block: dict[str, Any]) -> str:
    """块的内容指纹:排除 id/title/role,字符串折叠空白后稳定序列化。
    同图数据 / 同列表项 / 同表行 / 同指标组(即便抬头不同)→ 同指纹 → 判重。
    措辞不同的同义段落、条目数不同的近似指标组**不**算同(确定性,不臆测)。"""
    payload = {k: v for k, v in block.items() if k not in _SIG_EXCLUDE}
    return json.dumps(_strip_ws(payload), sort_keys=True, ensure_ascii=False)


# 小节标题开头的序号前缀(源文 markdown 抬头「## 一、…」带进来的;跨主题收缩后会断号)。
_ORDINAL_PREFIX = re.compile(
    r"^\s*(?:"
    r"[（(]\s*[一二三四五六七八九十百零〇\d]+\s*[)）]"  # （一） (1)
    r"|第\s*[一二三四五六七八九十百零〇\d]+\s*[章节部分篇讲]"  # 第一章 第1部分
    r"|[一二三四五六七八九十百零〇]+\s*[、.．。)）]"  # 一、 二.
    r"|\d+\s*[、.．。)）]"  # 1、 1.
    r")\s*"
)


def strip_ordinal_prefix(title: str) -> str:
    """剥掉小节标题开头的序号前缀(一、/1./（一）/第一章…)。

    序号本是源文 markdown 抬头的噪声;布局优先跨主题收缩后,留下来的小节序号还会断号
    (如 二/三/五/六/八)。剥光则回落原值(不把纯序号标题清成空)。"""
    if not title:
        return title
    stripped = _ORDINAL_PREFIX.sub("", title, count=1).strip()
    return stripped or title


# 模型给映不上的小节起的「自认无对应」标题(跨主题时模板某节在新源文里没对应物)。
_NO_CONTENT_TITLE = re.compile(
    r"无对应|无相关(?:内容|数据|信息|章节)?|无匹配|没有对应|暂无对应|无可对应"
    r"|no\s+(?:corresponding|matching|relevant|applicable)|not\s+applicable|^n\s*/?\s*a$",
    re.IGNORECASE,
)


def is_no_content_title(title: str | None) -> bool:
    """标题是否为模型「自认无对应」的占位(如「无对应内容」/「No corresponding content」)。
    布局优先跨主题时,这类小节是模板节在新源文里没对应、却被凑数填了内容 → 整节应丢。"""
    return bool(title) and bool(_NO_CONTENT_TITLE.search(title.strip()))


# 文本块里的「道歉占位」——generous 映射后,模型对无料角色不回空、改写道歉散文(非空,躲过收缩)。
_ADMISSION = re.compile(
    r"未提及|未涉及|未提供|无法提供|没有提及|无相关(?:内容|信息|数据)|源文本?中?未|文本中未|暂无(?:相关|对应)"
    r"|not\s+mentioned|not\s+provided|no\s+relevant|not\s+available",
    re.IGNORECASE,
)


def _is_admission_text(value: Any) -> bool:
    return isinstance(value, str) and bool(_ADMISSION.search(value))


def _is_admission_block(block: dict[str, Any]) -> bool:
    """块的填充内容是否为「未提及/无法提供」之类道歉占位。仅查文本型块(段落/标注/列表);
    图表/表格/指标卡的「无料」由收缩(空→丢)处理,道歉散文只落在文本块。"""
    btype = block.get("type")
    if btype in ("paragraph", "callout"):
        return _is_admission_text(block.get("content"))
    if btype == "list":
        items = [it for it in (block.get("items") or []) if isinstance(it, str) and it.strip()]
        return bool(items) and all(_is_admission_text(it) for it in items)
    return False


def drop_admission_blocks(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """丢弃内容是道歉占位(未提及/无法提供…)的文本块;某节因此空则整节丢。
    与收缩(空→丢)互补:收缩管模型回空的情形,本 pass 管模型改写道歉散文的情形。"""
    out: list[dict[str, Any]] = []
    for section in sections:
        kept = [block for block in (section.get("blocks") or []) if not _is_admission_block(block)]
        if not kept:
            continue
        out.append({**section, "blocks": kept})
    return out


def dedupe_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """全报告范围去重:结构相同的兄弟块只留首次出现,其余丢弃;某节被丢空则整节丢。

    布局优先跨主题复用时,模板槽位常多于源文内容 → 多个槽塌缩到同一份内容产生重复块
    (如同一趋势图出现两次、同一列表重复)。此 pass 做确定性清理(同步幅、低风险)。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for section in sections:
        kept: list[dict[str, Any]] = []
        for block in section.get("blocks") or []:
            signature = _content_signature(block)
            if signature in seen:
                continue  # 结构相同的更晚兄弟块 → 丢
            seen.add(signature)
            kept.append(block)
        if not kept:
            continue  # 整节皆为重复块 → 整节丢
        out.append({**section, "blocks": kept})
    return out

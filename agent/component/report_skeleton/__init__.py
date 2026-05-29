"""
report_skeleton —— AI 生成报告骨架 + 生成区展开(纯逻辑、零 IO、可单测)。

镜像前端 designer/ai-skeleton/ 的纯模块,作为后端唯一真源:
- generate_skeleton  报告文本 → 可复用骨架模板(大纲 → 逐节 → 拼装,带整篇回退)。
- expand_open_regions  骨架里的生成区占位 → 按 brief 展开成真块(运行时填值前置 pass)。

填值(report_fill.fill_skeleton)在另一个包,运行顺序为「expand → fill → merge」。
"""

from .expand import ExpandError, ExpandResult, expand_open_regions
from .generate import GenerateError, GenerateResult, generate_skeleton

__all__ = [
    "ExpandError",
    "ExpandResult",
    "GenerateError",
    "GenerateResult",
    "expand_open_regions",
    "generate_skeleton",
]

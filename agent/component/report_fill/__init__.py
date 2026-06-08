"""
HTMLReport 运行期填值的纯逻辑包(零 IO、可单测)。

移植自前端 multrag-web `src/pages/agent/form/html-report/` 的 TS 参考实现
(`skeleton-utils.ts` / `prompt-builder.ts` / `fill-doc.ts` / `schema-fill.ts`),
按相同规则在后端落地——见 docs/html-report README「执行落点 = 后端只产 ReportSchema」。

`fill_skeleton` 把「调 LLM」`call_llm` 与「取变量真值」`resolve_ref` 都做成注入函数,
故本包不触网、不依赖 canvas,可直接单测。HTMLReport 组件在 `agent/component/html_report.py`
注入自身 LLM 与上游解析。
"""

from .fill import FillError, FillResult, fill_skeleton

__all__ = ["FillError", "FillResult", "fill_skeleton"]

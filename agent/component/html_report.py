"""
HTMLReport 运行期算子:按骨架 + 源料,先展开生成区(open-region)再逐节调 LLM 填值,产出 ReportSchema。

决策 #29:后端**只产 ReportSchema**,不拼 HTML、不碰 ECharts——`buildReportHtml` 是消费端
(runtime-chat / datav)的事。本组件只负责:取上游源料 → 注入自身 LLM → 出 outputs。
真正的填值/解析/merge 在纯模块 `report_fill` 里(可单测,与前端 TS 参考同规则)。

参数对齐前端 FormSheet 契约:`skeleton`(骨架 dict)、`query`(源料上游引用)、
`llm_id`(填充模型)、`temperature`。
"""

import asyncio
import json
import logging
import os
import re
from functools import partial
from typing import Any

from agent.component.base import ComponentBase, ComponentParamBase
from api.db.db_models import db_connection
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import TenantLLMService
from common.connection_utils import timeout

from .report_fill.fill import DEFAULT_FILL_CONCURRENCY, fill_skeleton, resolve_fill_concurrency
from .report_skeleton import expand_open_regions


class HTMLReportParam(ComponentParamBase):
    """HTMLReport 组件参数。"""

    def __init__(self):
        super().__init__()
        self.skeleton = {}  # 设计期搭好的报告骨架
        self.query = ""  # 源料:运行时通读的主语料,绑一个上游变量
        self.llm_id = ""  # 填充模型
        self.temperature = 0.1  # 生成温度(与前端试运行一致的低发散取值)
        self.parallel_fill = True  # 是否并行填充各小节(关则逐节串行;默认开,保持现有速度)
        self.fill_concurrency = DEFAULT_FILL_CONCURRENCY  # 并行时的并发上限(同时在飞的 LLM 调用数)
        self.outputs = {
            "report_schema": {"value": None, "type": "object"},
            "success": {"value": False, "type": "boolean"},
        }

    def check(self):
        self.check_empty(self.llm_id, "[HTMLReport] LLM")
        if not isinstance(self.skeleton, dict) or "sections" not in self.skeleton:
            raise ValueError("[HTMLReport] skeleton must be an object with 'sections'.")
        self.check_decimal_float(float(self.temperature), "[HTMLReport] Temperature")
        self.check_boolean(self.parallel_fill, "[HTMLReport] Parallel fill")
        self.check_positive_integer(self.fill_concurrency, "[HTMLReport] Fill concurrency")


class HTMLReport(ComponentBase):
    component_name = "HTMLReport"

    def __init__(self, canvas, component_id, param: ComponentParamBase):
        super().__init__(canvas, component_id, param)
        self.chat_mdl = None
        try:
            with db_connection() as db:
                model_config = get_model_config_by_type_and_name(
                    db,
                    self._canvas.get_tenant_id(),
                    TenantLLMService.llm_id2llm_type(self._param.llm_id),
                    self._param.llm_id,
                )
                self.chat_mdl = LLMBundle(
                    db,
                    self._canvas.get_tenant_id(),
                    model_config,
                    max_retries=self._param.max_retries,
                    retry_interval=self._param.delay_after_error,
                )
        except LookupError as e:
            logging.warning(
                "HTMLReport %s: model '%s' not found, will fail at run time: %s",
                component_id,
                self._param.llm_id,
                e,
            )

    def get_input_form(self) -> dict[str, dict]:
        return {"query": {"name": "Source material", "type": "text"}}

    @staticmethod
    async def _collect_partial(value: Any) -> Any:
        """上游若是流式 partial,拉完拼成整串(仿 PDFGenerator;兼容同步/异步生成器)。"""
        if not isinstance(value, partial):
            return value
        result = value()
        if hasattr(result, "__aiter__"):
            chunks = [chunk async for chunk in result]
            return "".join(str(c) for c in chunks)
        if hasattr(result, "__iter__") and not isinstance(result, (str, bytes, dict)):
            return "".join(str(c) for c in result)
        return result

    @staticmethod
    def _stringify(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)

    async def _resolve_source_text(self) -> str:
        """解析 query 里的上游引用 → 通读源料文本(仿 PDFGenerator)。

        前端 QueryVariable 存的是**裸引用**(如 `begin@report`,无花括号——normalizeVariableReference
        会主动剥掉花括号),而 PDFGenerator 风格的 variable_ref_patt 正则只匹配 `{…}`。两者约定不一致:
        裸引用能过 is_reff 却匹配不到任何 `{…}`,旧实现会把字面量 "begin@report" 当源料 → 报告永远空数据。
        故此处区分两形态:整串本身就是一个引用时直接取值;带花括号的模板文本仍走逐个替换。
        """
        raw = self._param.query
        if not isinstance(raw, str) or not raw.strip():
            return ""
        stripped = raw.strip()
        if not self._canvas.is_reff(stripped):
            return raw

        matches = re.findall(self.variable_ref_patt, raw, flags=re.DOTALL)
        if not matches:
            # 裸引用:前端单变量绑定字段存的就是这种形态
            value = await self._collect_partial(self._canvas.get_variable_value(stripped))
            return self._stringify(value)

        text = raw
        for match in matches:
            value = await self._collect_partial(self._canvas.get_variable_value(match))
            text = text.replace("{" + match + "}", self._stringify(value))
        return text

    @timeout(int(os.environ.get("COMPONENT_EXEC_TIMEOUT", 10 * 60)))
    async def _invoke_async(self, **kwargs):
        if self.chat_mdl is None:
            raise LookupError(f"LLM model '{self._param.llm_id}' is not available. Please configure the model API key in the model management page.")
        if self.check_if_canceled("HTMLReport processing"):
            return

        source_text = await self._resolve_source_text()
        gen_conf = {"temperature": float(self._param.temperature)}

        async def call_llm(messages: list[dict[str, str]]) -> str:
            has_sys = bool(messages) and messages[0].get("role") == "system"
            system = messages[0]["content"] if has_sys else ""
            history = messages[1:] if has_sys else messages
            return await self.chat_mdl.async_chat(system, history, gen_conf)

        def resolve_ref(ref: str) -> Any:
            try:
                return self._canvas.get_variable_value(ref)
            except Exception:
                return None

        # 报告 LLM 扇出并发上限:以节点配置(parallel_fill + fill_concurrency)为准,关并行则串行;
        # REPORT_FILL_CONCURRENCY 作可选部署级硬上限(只向下钳)。展开与填值不同时进行,共用此上限。
        fill_concurrency = resolve_fill_concurrency(
            bool(self._param.parallel_fill),
            self._param.fill_concurrency,
            os.environ.get("REPORT_FILL_CONCURRENCY"),
        )

        # 先展开生成区(open-region)→ 无生成区骨架,再填值。与设计期试运行同一条路径(预览=生产);
        # 展开失败非致命,全失败也继续填值。
        expanded = await expand_open_regions(self._param.skeleton, source_text, call_llm, concurrency=fill_concurrency)
        result = await fill_skeleton(expanded.skeleton, source_text, resolve_ref, call_llm, concurrency=fill_concurrency)

        # 需调模型的节全军覆没 → 视为失败;否则成功(可能带部分告警)。
        if result.llm_sections > 0 and result.ok_sections == 0:
            self.set_output("success", False)
            self.set_output("_ERROR", "HTMLReport: all sections failed to fill")
            return

        self.set_output("report_schema", result.schema)
        self.set_output("success", True)

    @timeout(int(os.environ.get("COMPONENT_EXEC_TIMEOUT", 10 * 60)))
    def _invoke(self, **kwargs):
        return asyncio.run(self._invoke_async(**kwargs))

    def thoughts(self) -> str:
        return "Filling the report template section by section…"

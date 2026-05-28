"""
HTMLReport 运行期算子:按骨架 + 源料逐节调 LLM 填值,产出 ReportSchema。

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

from .report_fill.fill import fill_skeleton


class HTMLReportParam(ComponentParamBase):
    """HTMLReport 组件参数。"""

    def __init__(self):
        super().__init__()
        self.skeleton = {}  # 设计期搭好的报告骨架
        self.query = ""  # 源料:运行时通读的主语料,绑一个上游变量
        self.llm_id = ""  # 填充模型
        self.temperature = 0.1  # 生成温度(与前端试运行一致的低发散取值)
        self.outputs = {
            "report_schema": {"value": None, "type": "object"},
            "success": {"value": False, "type": "boolean"},
        }

    def check(self):
        self.check_empty(self.llm_id, "[HTMLReport] LLM")
        if not isinstance(self.skeleton, dict) or "sections" not in self.skeleton:
            raise ValueError("[HTMLReport] skeleton must be an object with 'sections'.")
        self.check_decimal_float(float(self.temperature), "[HTMLReport] Temperature")


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

    async def _resolve_source_text(self) -> str:
        """解析 query 里的上游引用 → 通读源料文本(仿 PDFGenerator)。"""
        raw = self._param.query
        if not isinstance(raw, str):
            return ""
        if not raw.strip() or not self._canvas.is_reff(raw.strip()):
            return raw
        text = raw
        for match in re.findall(self.variable_ref_patt, raw, flags=re.DOTALL):
            value = await self._collect_partial(self._canvas.get_variable_value(match))
            if not isinstance(value, str):
                try:
                    value = json.dumps(value, ensure_ascii=False)
                except Exception:
                    value = str(value)
            text = text.replace("{" + match + "}", value)
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

        result = await fill_skeleton(self._param.skeleton, source_text, resolve_ref, call_llm)

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

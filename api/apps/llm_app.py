import asyncio
import threading
import logging
import json
import os
import re
from datetime import datetime
from typing import Any, Literal
import base64
from array import array

from fastapi import APIRouter, Depends, HTTPException, Request, Body, Form, File, UploadFile
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from api.apps import manager, executor
from agent.component.agent_with_tools import Agent, AgentParam
from api.db.services.tenant_llm_service import LLMFactoriesService, TenantLLMService
from api.db.services.llm_service import LLMService, LLMBundle
from api.db.services.user_service import TenantService
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name, get_tenant_default_model_by_type
from api.utils.api_utils import get_json_result, server_error_response, get_data_error_result, get_allowed_llm_factories
from common.constants import StatusEnum, LLMType
from api.db.db_models import TenantLLM, get_db, db_connection
from core.utils.base64_image import test_image
from core.llm import EmbeddingModel, ChatModel, CvModel, RerankModel, TTSModel, OcrModel, Seq2txtModel

from core.prompts.generator import kb_prompt
from core.utils.tavily_conn import Tavily
from api.db.services.dialog_service import _stream_with_think_delta
from api.db.services.mcp_server_service import MCPServerService
from api.utils.web_utils import CONTENT_TYPE_MAP
from common import settings
from common.misc_utils import get_uuid, thread_pool_exec
from common.mcp_tool_call_conn import close_multiple_mcp_toolcall_sessions


class ChatAgentAdapter:
    """对话Agent适配器，直接复用Agent类但适配对话场景"""

    def __init__(self, tenant_id: str, llm_name: str, system_prompt: str = "", 
                 mcp_ids: list[str] = None, output_schema: dict = None):
        self.tenant_id = tenant_id
        self.llm_name = llm_name
        self.system_prompt = system_prompt
        self.mcp_ids = mcp_ids or []
        self.output_schema = output_schema  # 结构化输出 schema

        # 创建简化的Canvas mock - 只提供Agent需要的接口
        self.canvas_mock = self._create_canvas_mock()

        # 创建Agent参数，直接复用AgentParam
        agent_param = AgentParam()
        agent_param.llm_id = llm_name
        agent_param.sys_prompt = system_prompt or "You are a helpful AI assistant."
        agent_param.prompts = [{"role": "user", "content": "{sys.query}"}]
        agent_param.mcp = self._prepare_mcp_config()
        agent_param.tools = []
        agent_param.max_rounds = 5
        agent_param.cite = True
        agent_param.temperature = 0.1
        agent_param.max_tokens = 0
        
        # 设置结构化输出 schema
        if output_schema:
            agent_param.outputs = {"structured": output_schema}

        # 创建Agent实例，直接复用完整的Agent功能
        self.agent = Agent(self.canvas_mock, "chat_agent", agent_param)

    def _create_canvas_mock(self):
        """创建最小化的Canvas mock，满足Agent的依赖"""
        from agent.canvas import Canvas

        class CanvasMock(Canvas):
            def __init__(self, tenant_id):
                # 使用最小化的DSL初始化Canvas
                minimal_dsl = json.dumps({
                    "components": {
                        "begin": {
                            "obj": {
                                "component_name": "Begin",
                                "params": {
                                    "prologue": "Hi there!"
                                }
                            },
                            "downstream": [],
                            "upstream": [],
                            "parent_id": ""
                        }
                    },
                    "history": [],
                    "path": [],
                    "retrieval": {"chunks": [], "doc_aggs": []},
                    "memory": [],
                    "globals": {
                        "sys.query": "",
                        "sys.user_id": tenant_id,
                        "sys.conversation_turns": 0,
                        "sys.files": []
                    }
                })
                super().__init__(minimal_dsl, tenant_id=tenant_id)
                self._tenant_id = tenant_id
                self.history = []
                self.retrieval = {"chunks": [], "doc_aggs": []}
                self.memory = []
                self.globals = {
                    "sys.query": "",
                    "sys.user_id": tenant_id,
                    "sys.conversation_turns": 0,
                    "sys.files": []
                }

            def get_tenant_id(self):
                return self._tenant_id

            def get_history(self, window_size=10):
                return self.history[-window_size:] if self.history else []

            def get_reference(self):
                return self.retrieval

            def get_memory(self):
                return self.memory

            def add_memory(self, user, assist, summ):
                self.memory.append((user, assist, summ))

            def tool_use_callback(self, component_id, *args, **kwargs):
                logging.debug(f"Tool callback: component_id={component_id}, args={args}")

            def get_variable_value(self, var_name):
                return self.globals.get(var_name, "")

            def set_variable_value(self, var_name, value):
                self.globals[var_name] = value

            def get_component(self, cpn_id):
                """重写 get_component，返回安全的组件信息"""
                # 对于 ChatAgentAdapter，我们只有一个虚拟的 Agent 组件
                # 返回一个不包含 Message 下游组件的配置，避免触发 canvas.py 中的 Message 处理逻辑
                if cpn_id in self.components:
                    comp = self.components[cpn_id]
                    # 确保 downstream 总是空列表，避免触发 Message 组件处理
                    if "downstream" in comp:
                        comp["downstream"] = []
                    return comp
                # 返回一个默认的组件结构，downstream 为空数组
                return {
                    "obj": None,
                    "downstream": [],
                    "upstream": [],
                    "parent_id": ""
                }

            def get_component_obj(self, cpn_id):
                """重写 get_component_obj，安全获取组件对象"""
                cpn = self.get_component(cpn_id)
                if cpn and "obj" in cpn and cpn["obj"] is not None:
                    return cpn["obj"]
                
                # 返回一个安全的 Mock 对象，避免 None 引用错误
                class SafeComponentMock:
                    component_name = "Mock"
                    def output(self, key=None):
                        return ""
                    def error(self):
                        return None
                    def set_output(self, key, value):
                        pass
                
                return SafeComponentMock()

            def get_component_name(self, cpn_id):
                """获取组件名称"""
                for n in self.dsl.get("graph", {}).get("nodes", []):
                    if cpn_id == n["id"]:
                        return n["data"]["name"]
                # 如果找不到，返回组件ID本身
                return cpn_id

            def add_reference(self, chunks: list, doc_infos: list):
                """添加检索参考信息"""
                if not self.retrieval:
                    self.retrieval = {"chunks": [], "doc_aggs": []}
                
                # 简化版本，直接添加
                if isinstance(self.retrieval, dict):
                    if "chunks" not in self.retrieval:
                        self.retrieval["chunks"] = []
                    if "doc_aggs" not in self.retrieval:
                        self.retrieval["doc_aggs"] = []
                    
                    if chunks:
                        self.retrieval["chunks"].extend(chunks)
                    if doc_infos:
                        self.retrieval["doc_aggs"].extend(doc_infos)

            def get_component_type(self, cpn_id):
                """获取组件类型"""
                cpn_obj = self.get_component_obj(cpn_id)
                if cpn_obj and hasattr(cpn_obj, 'component_name'):
                    return cpn_obj.component_name
                return "Unknown"

            def is_reff(self, exp: str) -> bool:
                """检查表达式是否是变量引用"""
                if not exp:
                    return False
                exp = exp.strip("{").strip("}")
                if exp.find("@") < 0:
                    return exp in self.globals
                return False

            def run(self, **kwargs):
                """重写 run 方法，避免触发完整的 workflow 执行逻辑"""
                # ChatAgentAdapter 不需要完整的 workflow 执行
                # 直接返回，避免触发 canvas.py 中可能导致 NoneType 错误的代码
                logging.debug("CanvasMock.run() called but skipped for ChatAgentAdapter")
                return iter([])  # 返回空迭代器

        return CanvasMock(self.tenant_id)

    def _prepare_mcp_config(self):
        """准备MCP配置，直接复用现有的MCP服务查询逻辑"""
        mcp_config = []

        if self.mcp_ids:
            for mcp_id in self.mcp_ids:
                try:
                    with db_connection() as db:
                        # 使用正确的MCPServerService调用方式
                        mcp_server = MCPServerService.get_by_id(db, mcp_id)
                    if mcp_server and mcp_server.tenant_id == self.tenant_id:
                        cached_tools = (mcp_server.variables or {}).get("tools", {})
                        if cached_tools:
                            mcp_config.append({
                                "mcp_id": mcp_id,
                                "tools": cached_tools
                            })
                except Exception as e:
                    logging.warning(f"Failed to load MCP server {mcp_id}: {e}")

        return mcp_config

    def _get_schema_prompt(self) -> str:
        """获取结构化输出的 prompt"""
        if not self.output_schema:
            return ""
        
        from core.prompts.generator import structured_output_prompt
        schema = json.dumps(self.output_schema, ensure_ascii=False, indent=2)
        return structured_output_prompt(schema)

    async def chat_with_tools_stream_async(self, query: str, messages: list[dict] = None, knowledge_context: str = "", files: list[str] = None):
        """
        异步版本的工具流式对话，直接复用Agent的异步流式能力
        """
        # 准备历史记录
        history = []
        if messages:
            history = messages.copy()
        if query:
            history.append({"role": "user", "content": query})

        self.canvas_mock.history = history

        # 准备输入变量
        self.canvas_mock.globals["sys.query"] = query
        if files:
            self.canvas_mock.globals["sys.files"] = files

        # 如果有知识上下文，临时修改系统提示词
        original_prompt = self.agent._param.sys_prompt or ""
        if knowledge_context:
            enhanced_prompt = original_prompt + "\n\n" + knowledge_context
            self.agent._param.sys_prompt = enhanced_prompt
        elif not original_prompt:
            self.agent._param.sys_prompt = "You are a helpful AI assistant."

        try:
            kwargs = {
                "user_prompt": query,
                "reasoning": "Direct chat request",
                "context": "Chat conversation context"
            }

            if self.agent.tools:
                prompt, msg, _ = self.agent._prepare_prompt_variables()

                from core.prompts.generator import message_fit_in
                _, msg = message_fit_in([{"role": "system", "content": prompt}, *msg], int(self.agent.chat_mdl.max_length * 0.97))

                use_tools = []
                schema_prompt = self._get_schema_prompt()

                yield "🔧 Starting tool analysis...\n"

                previous_tool_count = 0
                # 使用异步版本的 _react_with_tools_streamly_async_simple
                async for delta_ans, _ in self.agent._react_with_tools_streamly_async_simple(prompt, msg, use_tools, schema_prompt=schema_prompt):
                    if len(use_tools) > previous_tool_count:
                        new_tools = use_tools[previous_tool_count:]
                        for tool_call in new_tools:
                            tool_name = tool_call.get('name', 'Unknown')
                            tool_args = tool_call.get('arguments', {})

                            args_preview = ""
                            if isinstance(tool_args, dict) and tool_args:
                                key_args = []
                                for k, v in list(tool_args.items()):
                                    key_args.append(f"{k}={v}")
                                args_preview = f"({', '.join(key_args)})"

                            yield f"\n🔧 **工具调用**: {tool_name}{args_preview}\n"

                            tool_results = tool_call.get('results', '')
                            if tool_results:
                                results_preview = str(tool_results)
                                yield f"📋 **结果**: {results_preview}\n\n"

                        previous_tool_count = len(use_tools)
                    if delta_ans:
                        yield delta_ans

                self.agent._last_use_tools = use_tools
                if use_tools:
                    self.agent.set_output("use_tools", use_tools)

            else:
                self.agent._param.prompts = [{"role": "user", "content": query}]
                prompt, msg, _ = self.agent._prepare_prompt_variables()

                # 使用异步流式输出
                async for delta in self.agent._stream_output_async(prompt, msg):
                    yield delta

        finally:
            if knowledge_context:
                self.agent._param.sys_prompt = original_prompt or "You are a helpful AI assistant."

    async def chat_with_tools_stream_structured_async(self, query: str, messages: list[dict] = None,
                                                      knowledge_context: str = "", files: list[str] = None):
        """
        异步版本的结构化工具流式对话
        返回结构化的SSE消息，每个文本消息都包含累积内容
        """
        messages = messages or []
        history = []

        for msg in messages:
            history.append({
                "role": msg.get("role", "user"),
                "content": msg.get("content", "")
            })

        if query:
            history.append({"role": "user", "content": query})

        original_prompt = self.agent._param.sys_prompt or ""
        if knowledge_context:
            self.agent._param.sys_prompt = (self.agent._param.sys_prompt or "") + "\n" + knowledge_context
        elif not original_prompt:
            self.agent._param.sys_prompt = "You are a helpful AI assistant."

        def _emit_text_delta(delta: str, in_think: bool):
            """解析 delta 中的 <think></think> 标记，返回结构化事件列表。
            marker 事件统一使用 start_to_think/end_to_think，与其他 SSE 端点保持一致。
            think 块内的文本同样作为 {"type": "text"} 事件输出，客户端通过 marker 判断上下文。
            """
            events = []
            text = delta
            while text:
                if not in_think:
                    pos = text.find("<think>")
                    if pos == -1:
                        events.append({"type": "text", "content": text})
                        text = ""
                    else:
                        if pos > 0:
                            events.append({"type": "text", "content": text[:pos]})
                        in_think = True
                        events.append({"start_to_think": True})
                        text = text[pos + len("<think>"):]
                else:
                    pos = text.find("</think>")
                    if pos == -1:
                        events.append({"type": "text", "content": text})
                        text = ""
                    else:
                        if pos > 0:
                            events.append({"type": "text", "content": text[:pos]})
                        in_think = False
                        events.append({"end_to_think": True})
                        text = text[pos + len("</think>"):]
            return events, in_think

        try:
            self.canvas_mock.history = history
            self.agent._param.prompts = history
            prompt, msg, _ = self.agent._prepare_prompt_variables()

            in_think = False

            if self.agent.tools:
                from core.prompts.generator import message_fit_in
                _, msg = message_fit_in([{"role": "system", "content": prompt}, *msg], int(self.agent.chat_mdl.max_length * 0.97))

                use_tools = []
                schema_prompt = self._get_schema_prompt()

                yield {"type": "tool_start", "content": "Starting tool analysis..."}

                previous_tool_count = 0
                call_id_counter = 0

                # 使用异步版本
                async for delta_ans, _ in self.agent._react_with_tools_streamly_async_simple(prompt, msg, use_tools, schema_prompt=schema_prompt):
                    if len(use_tools) > previous_tool_count:
                        new_tools = use_tools[previous_tool_count:]
                        for tool_call in new_tools:
                            call_id_counter += 1
                            call_id = f"call_{call_id_counter}"

                            tool_name = tool_call.get('name', 'Unknown')
                            tool_args = tool_call.get('arguments', {})

                            yield {
                                "type": "tool_call",
                                "content": {
                                    "tool_name": tool_name,
                                    "arguments": tool_args,
                                    "call_id": call_id
                                }
                            }

                            tool_results = tool_call.get('results', '')
                            if tool_results:
                                yield {
                                    "type": "tool_result",
                                    "content": {
                                        "tool_name": tool_name,
                                        "result": tool_results,
                                        "call_id": call_id,
                                        "success": True
                                    }
                                }

                            tool_logs = tool_call.get("server_logs", [])
                            if tool_logs:
                                yield {
                                    "type": "tool_logs",
                                    "content": {
                                        "tool_name": tool_name,
                                        "logs": tool_logs,
                                        "call_id": call_id,
                                    }
                                }

                        previous_tool_count = len(use_tools)

                    if delta_ans:
                        events, in_think = _emit_text_delta(delta_ans, in_think)
                        for event in events:
                            yield event

                if use_tools:
                    yield {
                        "type": "tool_end",
                        "content": {
                            "total_calls": len(use_tools),
                            "summary": f"Used {len(use_tools)} tool(s)"
                        }
                    }

                self.agent._last_use_tools = use_tools
                if use_tools:
                    self.agent.set_output("use_tools", use_tools)

            else:
                self.agent._param.prompts = [{"role": "user", "content": query}]
                prompt, msg, _ = self.agent._prepare_prompt_variables()

                # 使用异步流式输出
                async for delta in self.agent._stream_output_async(prompt, msg):
                    if delta:
                        events, in_think = _emit_text_delta(delta, in_think)
                        for event in events:
                            yield event

        except Exception as e:
            yield {
                "type": "error",
                "content": {
                    "error": str(e),
                    "code": 500
                }
            }

        finally:
            if knowledge_context:
                self.agent._param.sys_prompt = original_prompt or "You are a helpful AI assistant."

    async def chat_async(self, query: str, messages: list[dict] = None,
                         knowledge_context: str = "", files: list[str] = None) -> str:
        """
        非流式异步对话方法 - 用于不需要流式输出的场景
        
        Args:
            query: 用户查询
            messages: 历史消息列表
            knowledge_context: 知识上下文
            files: 文件列表
            
        Returns:
            str: 完整的回复内容
        """
        result_content = ""
        async for delta in self.chat_with_tools_stream_async(
            query=query,
            messages=messages,
            knowledge_context=knowledge_context,
            files=files
        ):
            if delta:
                result_content += delta
        return result_content

    async def chat_structured_async(self, query: str, messages: list[dict] = None,
                                    knowledge_context: str = "", files: list[str] = None) -> dict:
        """
        非流式结构化异步对话方法 - 返回最终的结构化结果
        
        Args:
            query: 用户查询
            messages: 历史消息列表
            knowledge_context: 知识上下文
            files: 文件列表
            
        Returns:
            dict: 包含最终文本和工具使用信息的字典
        """
        result = {
            "text": "",
            "tool_calls": [],
            "tool_results": []
        }
        async for message in self.chat_with_tools_stream_structured_async(
            query=query,
            messages=messages,
            knowledge_context=knowledge_context,
            files=files
        ):
            msg_type = message.get("type")
            content = message.get("content")
            
            if msg_type == "text":
                result["text"] = content  # 累积内容，最后一个是完整的
            elif msg_type == "tool_call":
                result["tool_calls"].append(content)
            elif msg_type == "tool_result":
                result["tool_results"].append(content)
            elif msg_type == "error":
                raise Exception(content.get("error", "Unknown error"))
        
        return result


def prepare_knowledge_context(db: Session, messages: list[dict], tavily_api_key: str, tenant_id: str, llm_name: str) -> str:
    """准备知识上下文，复用现有的Tavily集成"""
    knowledge_context = ""

    if tavily_api_key and messages:
        try:
            llm_model_config = get_model_config_by_type_and_name(db, tenant_id, LLMType.CHAT.value, llm_name)
            max_tokens = llm_model_config.get("max_tokens", 8192)

            kbinfos = {"total": 0, "chunks": [], "doc_aggs": []}
            questions = [m["content"] for m in messages if m["role"] == "user"]

            if questions:
                tav = Tavily(tavily_api_key)
                tav_res = tav.retrieve_chunks(" ".join(questions))
                kbinfos["chunks"].extend(tav_res["chunks"])
                kbinfos["doc_aggs"].extend(tav_res["doc_aggs"])
                kbinfos["total"] = len(kbinfos["chunks"])

            if kbinfos["total"] > 0:
                knowledges = kb_prompt(kbinfos, max_tokens)
                knowledge_context = "\n------\n" + "\n\n------\n\n".join(knowledges)

        except Exception as e:
            logging.warning(f"Tavily search failed: {e}")

    return knowledge_context


class SetAPIKeyRequest(BaseModel):
    llm_factory: str
    api_key: str
    base_url: str | None = None
    source_fid: str | None = None
    verify: bool = False


class AddLLMRequest(BaseModel):
    llm_factory: str
    llm_name: str
    mdl_type: str
    api_key: str | dict = None  # 支持字符串或字典（MinerU 等厂商传递配置对象）
    api_base: str | None = None
    max_tokens: int | None = 8192
    ark_api_key: str | None = None
    endpoint_id: str | None = None
    auth_mode: str | None = None
    bedrock_ak: str | None = None
    bedrock_sk: str | None = None
    bedrock_region: str | None = None
    aws_role_arn: str | None = None
    fish_audio_ak: str | None = None
    fish_audio_refid: str | None = None
    tencent_cloud_sid: str | None = None
    tencent_cloud_sk: str | None = None
    spark_api_password: str | None = None
    spark_app_id: str | None = None
    spark_api_secret: str | None = None
    spark_api_key: str | None = None
    google_project_id: str | None = None
    google_region: str | None = None
    google_service_account_key: str | None = None
    verify: bool = False


class DeleteLLMRequest(BaseModel):
    llm_factory: str
    llm_name: str


class ListLLMRequest(BaseModel):
    mdl_type: str | None = None


class EnableLLMRequest(BaseModel):
    llm_factory: str
    llm_name: str
    status: str = "1"


class DeleteFactoryRequest(BaseModel):
    llm_factory: str | None = None


class LLMServiceRequest(BaseModel):
    prompt: str = ""
    messages: list[dict]
    llm_name: str
    stream: bool = False
    gen_conf: dict[str, Any]
    image: str = ""
    tavily_api_key: str = ""
    delta_stream: bool = True


class ChatRequest(BaseModel):
    """聊天请求模型"""
    prompt: str = ""
    messages: list[dict[str, Any]]
    llm_name: str
    stream: bool = True
    gen_conf: dict[str, Any] = {}
    image: str = ""
    tavily_api_key: str = ""
    # MCP 集成相关
    mcp_ids: list[str] = []
    mcp_timeout: float = 10.0
    verbose_tool_use: bool = False
    files: list[str] = []
    # 结构化输出控制
    structured_output: bool = False  # 是否使用结构化的SSE消息格式
    delta_stream: bool = True


class EmbeddingsRequest(BaseModel):
    """2025标准向量化接口请求体（对齐OpenAI v1/embeddings风格）"""
    model: str | None = Field(default=None, description="嵌入模型名称，不填则使用租户默认")
    input: list[str] | str | None = Field(default=None, description="要向量化的文本或文本数组；多模态场景可为空")
    input_type: str = Field(default="document", description="document|query（部分模型对查询向量有专项优化）")
    encoding_format: str = Field(default="float", description="float|base64")
    user: str | None = Field(default=None, description="可选的用户标识")


class VolcEmbeddingMedia(BaseModel):
    type: Literal["text", "image_url", "video_url"] = Field(description="内容类型")
    text: str | None = Field(default=None, description="当 type=text 时必填")
    image_url: dict[str, str] | None = Field(default=None, description="当 type=image_url 时必填，包含 url")
    video_url: dict[str, str] | None = Field(default=None, description="当 type=video_url 时必填，包含 url")

    @field_validator("text")
    @classmethod
    def _check_text(cls, value: str | None, info):
        if info.data.get("type") == "text" and not value:
            raise ValueError("当 type=text 时，text 字段必填")
        return value

    @field_validator("image_url")
    @classmethod
    def _check_image(cls, value: dict[str, str] | None, info):
        if info.data.get("type") == "image_url":
            if not value or not value.get("url"):
                raise ValueError("当 type=image_url 时，image_url.url 必填")
        return value

    @field_validator("video_url")
    @classmethod
    def _check_video(cls, value: dict[str, str] | None, info):
        if info.data.get("type") == "video_url":
            if not value or not value.get("url"):
                raise ValueError("当 type=video_url 时，video_url.url 必填")
        return value


class EmbeddingsMultiModalRequest(EmbeddingsRequest):
    media: list[VolcEmbeddingMedia] | None = Field(default=None, description="多模态输入，按火山格式提供；与 input 同时存在时会分批调用")
    merge_mode: str = Field(default="independent", description="多文件处理模式: independent=每个文件独立向量（默认）, merged=合并为单个向量")


class FinePromptRequest(BaseModel):
    prompt: str
    llm_name: str
    gen_conf: dict[str, Any]


class SuggestionRequest(BaseModel):
    llm_name: str  # 模型名称
    last_response: str  # 模型的最后一轮回复
    messages: list[dict]  # 当前对话上下文
    gen_conf: dict[str, Any] = None # 大模型的配置信息
    num: int = Field(3)  # 返回建议的条数


class CandidateForm(BaseModel):
    form_id: int
    name: str
    description: str | None = ""


class RecognizeIntentRequest(BaseModel):
    user_text: str = Field(..., description="用户的自然语言输入")
    candidate_forms: list[CandidateForm] = Field(
        ..., description="候选表单列表，建议 ≤ 10 个"
    )
    llm_name: str = Field(..., description="用于意图识别的对话模型名称")
    gen_conf: dict[str, Any] = Field(
        default_factory=lambda: {"temperature": 0.0},
        description="可选的大模型生成参数"
    )


class RecognizeIntentResponse(BaseModel):
    intent_id: int
    confidence: float


class FieldMeta(BaseModel):
    field_id: int
    name: str
    type: str  # "text" | "enum" | "datetime"
    required: bool = False
    options: list[str] | None = None  # 仅 enum 时有效
    description: str | None = None  # 给 LLM 的说明，可带示例


class FillFieldsRequest(BaseModel):
    user_text: str = Field(..., description="用户的自然语言输入")
    fields: list[FieldMeta]
    llm_name: str = Field(..., description="调用的对话模型名称")
    gen_conf: dict[str, Any] = Field(default_factory=lambda: {"temperature": 0.0})
    retry: bool = Field(default=True, description="是否允许内部再追问一次")


class FillFieldsResponse(BaseModel):
    field_values: dict[str, Any]
    missing: list[str]
    invalid: dict[str, str]


router = APIRouter()


@router.get('/factories', summary="获取模型供应商信息", response_description="成功获取到所有模型供应商信息")
def factories(db: Session = Depends(get_db), user=Depends(manager)):
    try:
        fac = get_allowed_llm_factories(db)
        # fac = [f.to_dict() for f in fac if f.name not in ["Youdao", "FastEmbed", "BAAI", "Builtin"]]
        fac = [f.to_dict() for f in fac if f.name not in ["siliconflow_intl"]]
        llms = LLMService.get_all(db)
        mdl_types = {}
        for m in llms:
            if m.status != StatusEnum.VALID.value:
                continue
            if m.fid not in mdl_types:
                mdl_types[m.fid] = set([])
            mdl_types[m.fid].add(m.mdl_type)
        for f in fac:
            f["model_types"] = list(
                mdl_types.get(
                    f["name"],
                    [LLMType.CHAT, LLMType.EMBEDDING, LLMType.RERANK, LLMType.IMAGE2TEXT, LLMType.SPEECH2TEXT, LLMType.TTS, LLMType.OCR],
                )
            )

        return get_json_result(data=fac)
    except Exception as e:
        return server_error_response(e)


@router.post('/set_api_key', summary="新增模型厂商api key", response_description="成功保存该模型服务厂商的api key")
async def set_api_key(request: SetAPIKeyRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req = request.model_dump()
    chat_passed, embd_passed, rerank_passed = False, False, False
    factory = req["llm_factory"]
    base_url = req.get("base_url", "")
    source_factory = req.get("source_fid") or factory
    extra = {"provider": factory}
    timeout_seconds = int(os.environ.get("LLM_TIMEOUT_SECONDS", 10))
    source_llms = list(LLMService.query(db, fid=source_factory))
    if not source_llms:
        msg = f"No models configured for {factory} (source: {source_factory})."
        if req.get("verify", False):
            return get_json_result(data={"message": msg, "success": False})
        return get_data_error_result(retmsg=msg)

    msg = ""
    for llm in source_llms:
        if not embd_passed and llm.mdl_type == LLMType.EMBEDDING.value:
            assert factory in EmbeddingModel, f"Embedding model from {factory} is not supported yet."
            mdl = EmbeddingModel[factory](req["api_key"], llm.llm_name, base_url=base_url)
            try:
                arr, tc = await asyncio.wait_for(
                    thread_pool_exec(mdl.encode, ["Test if the api key is available"]),
                    timeout=timeout_seconds,
                )
                if len(arr[0]) == 0:
                    raise Exception("Fail")
                embd_passed = True
            except Exception as e:
                msg += f"\nFail to access embedding model({llm.llm_name}) using this api key." + str(e)
        elif not chat_passed and llm.mdl_type == LLMType.CHAT.value:
            assert factory in ChatModel, f"Chat model from {factory} is not supported yet."
            mdl = ChatModel[factory](req["api_key"], llm.llm_name, base_url=base_url, **extra)
            try:
                async with asyncio.timeout(timeout_seconds):
                    async for chunk in mdl.async_chat_streamly(
                        "",
                        [{"role": "user", "content": "Hi"}],
                        {"temperature": 0.9},
                    ):
                        if chunk and isinstance(chunk, str) and chunk.find("**ERROR**") < 0:
                            chat_passed = True
                            break
                    if not chat_passed:
                        raise Exception("No valid response received")
            except Exception as e:
                msg += f"\nFail to access model({llm.fid}/{llm.llm_name}) using this api key." + str(e)
        elif not rerank_passed and llm.mdl_type == LLMType.RERANK.value:
            assert factory in RerankModel, f"Re-rank model from {factory} is not supported yet."
            mdl = RerankModel[factory](req["api_key"], llm.llm_name, base_url=base_url)
            try:
                arr, tc = await asyncio.wait_for(
                    thread_pool_exec(mdl.similarity, "What's the weather?", ["Is it sunny today?"]),
                    timeout=timeout_seconds,
                )
                if len(arr) == 0 or tc == 0:
                    raise Exception("Fail")
                rerank_passed = True
                logging.debug(f'passed model rerank {llm.llm_name}')
            except Exception as e:
                msg += f"\nFail to access model({llm.fid}/{llm.llm_name}) using this api key." + str(e)

    if req.get("verify", False):
        return get_json_result(data={"message": msg, "success": len(msg.strip()) == 0})

    if msg:
        return get_data_error_result(retmsg=msg)

    llm_config = {
        "api_key": req["api_key"],
        "api_base": base_url
    }
    for n in ["mdl_type", "llm_name"]:
        if n in req:
            llm_config[n] = req[n]

    for llm in source_llms:
        llm_config["max_tokens"] = llm.max_tokens
        if not TenantLLMService.filter_update(
                db,
                [TenantLLM.tenant_id == user.id,
                 TenantLLM.llm_factory == factory,
                 TenantLLM.llm_name == llm.llm_name],
                llm_config
        ):
            TenantLLMService.save(
                db,
                tenant_id=user.id,
                llm_factory=factory,
                llm_name=llm.llm_name,
                mdl_type=llm.mdl_type,
                api_key=llm_config["api_key"],
                api_base=llm_config["api_base"],
                max_tokens=llm_config["max_tokens"]
            )

    return get_json_result(data=True)


@router.post('/add_llm', summary="新增模型", response_description="成功新增该模型")
async def add_llm(request: AddLLMRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
# POST /add_llm

## 接口描述
用于向系统中添加一个新的大语言模型(LLM)，并验证相关连接是否有效。

## 请求方式
POST

## 接口权限
需要用户认证

## 请求参数
| 参数名称 | 类型 | 必填 | 描述 |
|----------|------|------|------|
| llm_factory | string | 是 | 模型提供商/工厂，例如: "OpenAI", "Azure-OpenAI", "Bedrock", "VolcEngine", "Tencent Hunyuan", "Tencent Cloud", "LocalAI", "HuggingFace", "OpenAI-API-Compatible", "XunFei Spark", "Fish Audio", "Google Cloud" 等 |
| llm_name | string | 是 | 模型名称 |
| mdl_type | string | 是 | 模型类型，可以是 "chat" (聊天), "embedding" (嵌入), "rerank" (重排序), "image2text" (图像转文本), "tts" (文本转语音) |
| api_key | string | 否 | API密钥，根据不同的工厂可能需要或者不需要 |
| api_base | string | 否 | API基础URL地址 |
| ark_api_key | string | 否 | VolcEngine专用: ARK API密钥 |
| endpoint_id | string | 否 | VolcEngine专用: 终端节点ID |
| bedrock_ak | string | 否 | AWS Bedrock专用: Access Key |
| bedrock_sk | string | 否 | AWS Bedrock专用: Secret Key |
| bedrock_region | string | 否 | AWS Bedrock专用: 区域名称 |
| fish_audio_ak | string | 否 | Fish Audio专用: Access Key |
| fish_audio_refid | string | 否 | Fish Audio专用: 参考ID |
| hunyuan_sid | string | 否 | 腾讯混元专用: SID |
| hunyuan_sk | string | 否 | 腾讯混元专用: Secret Key |
| tencent_cloud_sid | string | 否 | 腾讯云专用: SID |
| tencent_cloud_sk | string | 否 | 腾讯云专用: Secret Key |
| spark_api_password | string | 否 | 讯飞星火专用: API密码 (用于chat模型) |
| spark_app_id | string | 否 | 讯飞星火专用: 应用ID (用于tts模型) |
| spark_api_secret | string | 否 | 讯飞星火专用: API Secret (用于tts模型) |
| spark_api_key | string | 否 | 讯飞星火专用: API Key (用于tts模型) |
| google_project_id | string | 否 | Google Cloud专用: 项目ID |
| google_region | string | 否 | Google Cloud专用: 区域 |
| google_service_account_key | string | 否 | Google Cloud专用: 服务账号密钥 |

## 响应参数
| 参数名称 | 类型 | 描述 |
|----------|------|------|
| success | boolean | 操作是否成功 |
| data | boolean | 返回true表示添加成功 |
| message | string | 当操作失败时，返回错误信息 |

## 特性
1. 根据不同的模型提供商，自动组装API密钥格式
2. 添加模型前，会进行连接测试，确保API密钥和URL有效
3. 对于不同类型的模型，执行不同的有效性验证:
   - 对于embedding模型: 验证能否成功编码测试文本
   - 对于chat模型: 验证能否成功生成回复
   - 对于rerank模型: 验证能否成功计算相似度
   - 对于image2text模型: 验证能否成功描述图像
   - 对于tts模型: 验证能否成功生成语音

## 错误码
| 错误码 | 描述 |
|--------|------|
| 400 | 请求参数错误或模型已存在或模型验证失败 |
| 401 | 用户未认证 |

## 示例

### 请求示例 (添加OpenAI模型)
```json
{
  "llm_factory": "OpenAI",
  "llm_name": "gpt-4",
  "mdl_type": "chat",
  "api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "api_base": "https://api.openai.com/v1"
}
```

### 响应示例 (成功)
```json
{
  "success": true,
  "data": true,
  "message": ""
}
```

### 响应示例 (失败)
```json
{
  "success": false,
  "data": null,
  "message": "Fail to access model(gpt-4). Invalid API key."
}
```

## 注意事项
1. 不同的模型提供商需要不同的API密钥格式，请确保提供正确的参数组合
2. 添加前，系统会验证模型连接，如果验证失败将返回400错误
3. 对于同一个租户，相同工厂和名称的模型不能重复添加
4. 对于某些特殊的模型工厂，系统会自动在模型名称后添加后缀，例如LocalAI会添加"___LocalAI"
    """
    req = request.model_dump()
    factory = req["llm_factory"]
    api_key = req.get("api_key", "x")
    llm_name = req["llm_name"]
    timeout_seconds = int(os.environ.get("LLM_TIMEOUT_SECONDS", 10))

    # 验证工厂是否在允许列表中
    allowed_factories = get_allowed_llm_factories(db)
    allowed_factory_names = [f.name for f in allowed_factories]
    if factory not in allowed_factory_names:
        return get_data_error_result(retmsg=f"LLM factory {factory} is not allowed")

    def apikey_json(keys):
        nonlocal req
        return json.dumps({k: req.get(k, "") for k in keys})

    if factory == "VolcEngine":
        # For VolcEngine, due to its special authentication method
        # Assemble ark_api_key endpoint_id into api_key
        api_key = apikey_json(["ark_api_key", "endpoint_id"])

    elif factory == "Tencent Cloud":
        req["api_key"] = apikey_json(["tencent_cloud_sid", "tencent_cloud_sk"])
        return set_api_key(SetAPIKeyRequest(**req), db, user)

    elif factory == "Bedrock":
        api_key = apikey_json(["auth_mode", "bedrock_ak", "bedrock_sk", "bedrock_region", "aws_role_arn"])

    elif factory == "LocalAI":
        llm_name += "___LocalAI"

    elif factory == "HuggingFace":
        llm_name += "___HuggingFace"

    elif factory == "OpenAI-API-Compatible":
        llm_name += "___OpenAI-API"

    elif factory == "VLLM":
        llm_name += "___VLLM"

    elif factory == "XunFei Spark":
        if req["mdl_type"] == "chat":
            api_key = req.get("spark_api_password", "")
        elif req["mdl_type"] == "tts":
            api_key = apikey_json(["spark_app_id", "spark_api_secret", "spark_api_key"])

    elif factory == "Fish Audio":
        api_key = apikey_json(["fish_audio_ak", "fish_audio_refid"])

    elif factory == "Google Cloud":
        api_key = apikey_json(["google_project_id", "google_region", "google_service_account_key"])

    elif factory == "Azure-OpenAI":
        api_key = apikey_json(["api_key", "api_version"])

    elif factory == "OpenRouter":
        api_key = apikey_json(["api_key", "provider_order"])

    elif factory == "MinerU":
        api_key = apikey_json(["api_key", "provider_order"])

    elif factory == "PaddleOCR":
        api_key = apikey_json(["api_key", "provider_order"])

    llm = {
        "tenant_id": user.id,
        "llm_factory": factory,
        "mdl_type": req["mdl_type"],
        "llm_name": llm_name,
        "api_base": req.get("api_base", ""),
        "api_key": api_key,
        "max_tokens": req.get("max_tokens")
    }

    msg = ""
    mdl_nm = llm["llm_name"].split("___")[0]
    extra = {"provider": factory}
    model_type = llm["mdl_type"]
    model_api_key = llm["api_key"]
    model_base_url = llm.get("api_base", "")
    match model_type:
        case LLMType.EMBEDDING.value:
            assert factory in EmbeddingModel, f"Embedding model from {factory} is not supported yet."
            mdl = EmbeddingModel[factory](key=model_api_key, model_name=mdl_nm, base_url=model_base_url)
            try:
                arr, tc = await asyncio.wait_for(
                    thread_pool_exec(mdl.encode, ["Test if the api key is available"]),
                    timeout=timeout_seconds,
                )
                if len(arr[0]) == 0:
                    raise Exception("Fail")
            except Exception as e:
                msg += f"\nFail to access embedding model({mdl_nm})." + str(e)
        case LLMType.CHAT.value:
            assert factory in ChatModel, f"Chat model from {factory} is not supported yet."
            mdl = ChatModel[factory](
                key=model_api_key,
                model_name=mdl_nm,
                base_url=model_base_url,
                **extra,
            )
            try:
                verified = False
                async with asyncio.timeout(timeout_seconds):
                    async for chunk in mdl.async_chat_streamly(
                        "",
                        [{"role": "user", "content": "Hi"}],
                        {"temperature": 0.9},
                    ):
                        if chunk and isinstance(chunk, str) and chunk.find("**ERROR**:") < 0:
                            verified = True
                            break
                    if not verified:
                        raise Exception("No valid response received")
            except Exception as e:
                msg += f"\nFail to access model({factory}/{mdl_nm})." + str(e)

        case LLMType.RERANK.value:
            assert factory in RerankModel, f"RE-rank model from {factory} is not supported yet."
            try:
                mdl = RerankModel[factory](key=model_api_key, model_name=mdl_nm, base_url=model_base_url)
                arr, tc = await asyncio.wait_for(
                    thread_pool_exec(mdl.similarity, "Hello~ MultiRAGer!", ["Hi, there!", "Ohh, my friend!"]),
                    timeout=timeout_seconds,
                )
                if len(arr) == 0:
                    raise Exception("Not known.")
            except KeyError:
                msg += f"{factory} dose not support this model({factory}/{mdl_nm})"
            except Exception as e:
                msg += f"\nFail to access model({factory}/{mdl_nm})." + str(e)

        case LLMType.IMAGE2TEXT.value:
            assert factory in CvModel, f"Image to text model from {factory} is not supported yet."
            mdl = CvModel[factory](key=model_api_key, model_name=mdl_nm, base_url=model_base_url)
            try:
                image_data = test_image
                m, tc = await asyncio.wait_for(
                    thread_pool_exec(mdl.describe, image_data),
                    timeout=timeout_seconds,
                )
                if not tc and m.find("**ERROR**:") >= 0:
                    raise Exception(m)
            except Exception as e:
                msg += f"\nFail to access model({factory}/{mdl_nm})." + str(e)
        case LLMType.TTS.value:
            assert factory in TTSModel, f"TTS model from {factory} is not supported yet."
            mdl = TTSModel[factory](key=model_api_key, model_name=mdl_nm, base_url=model_base_url)
            try:
                def drain_tts():
                    for _ in mdl.tts("Hello~ MultiRAGer!"):
                        pass

                await asyncio.wait_for(
                    thread_pool_exec(drain_tts),
                    timeout=timeout_seconds,
                )
            except RuntimeError as e:
                msg += f"\nFail to access model({factory}/{mdl_nm})." + str(e)
        case LLMType.OCR.value:
            assert factory in OcrModel, f"OCR model from {factory} is not supported yet."
            try:
                mdl = OcrModel[factory](key=model_api_key, model_name=mdl_nm, base_url=model_base_url)
                ok, reason = await asyncio.wait_for(
                    thread_pool_exec(mdl.check_available),
                    timeout=timeout_seconds,
                )
                if not ok:
                    raise RuntimeError(reason or "Model not available")
            except Exception as e:
                msg += f"\nFail to access model({factory}/{mdl_nm})." + str(e)
        case LLMType.SPEECH2TEXT.value:
            assert factory in Seq2txtModel, f"Speech model from {factory} is not supported yet."
            try:
                mdl = Seq2txtModel[factory](key=model_api_key, model_name=mdl_nm, base_url=model_base_url)
                # TODO: check the availability
            except Exception as e:
                msg += f"\nFail to access model({factory}/{mdl_nm})." + str(e)
        case _:
            raise RuntimeError(f"Unknown model type: {model_type}")

    if req.get("verify", False):
        return get_json_result(data={"message": msg, "success": len(msg.strip()) == 0})

    if msg:
        raise HTTPException(status_code=400, detail=msg)

    try:
        if not TenantLLMService.filter_update(db, [TenantLLM.tenant_id == user.id, TenantLLM.llm_factory == factory,
                                                   TenantLLM.llm_name == llm["llm_name"]], llm):
            TenantLLMService.save(db, **llm)
    except IntegrityError as e:
        raise HTTPException(status_code=400, detail="LLM already exists for this tenant, factory, and name.")

    return get_json_result(data=True)


@router.post('/delete_llm', summary="删除模型", response_description="成功删除该模型")
def delete_llm(request: DeleteLLMRequest, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        req = request.model_dump()
        llm = TenantLLMService.query(db, tenant_id=user.id, llm_factory=req["llm_factory"], llm_name=req["llm_name"])
        if not llm:
            raise HTTPException(status_code=404, detail="LLM not found")

        TenantLLMService.filter_delete(db, [TenantLLM.tenant_id == user.id, TenantLLM.llm_factory == req["llm_factory"], TenantLLM.llm_name == req["llm_name"]])
        return get_json_result(data=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post('/enable_llm', summary="启用/禁用模型", response_description="成功更新模型状态")
def enable_llm(request: EnableLLMRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req = request.model_dump()
    TenantLLMService.filter_update(
        db,
        [TenantLLM.tenant_id == user.id, TenantLLM.llm_factory == req["llm_factory"], TenantLLM.llm_name == req["llm_name"]],
        {"status": req["status"]},
    )
    return get_json_result(data=True)


@router.post('/delete_factory', summary="删除模型厂商", response_description="成功删除模型厂商")
def delete_factory(request: DeleteFactoryRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req = request.model_dump()
    TenantLLMService.filter_delete(
        db, [TenantLLM.tenant_id == user.id, TenantLLM.llm_factory == req["llm_factory"]])
    return get_json_result(data=True)


@router.get('/my_llms', summary="获取用户的所有模型", response_description="成功获取到用户的所有模型")
def my_llms(include_details: bool = False, db: Session = Depends(get_db), user=Depends(manager), request: Request = None):
    try:
        TenantLLMService.ensure_mineru_from_env(db, user.id)

        if include_details:
            res = {}
            objs = TenantLLMService.query(db, tenant_id=user.id)
            factories = LLMFactoriesService.query(db, status=StatusEnum.VALID.value)

            for o in objs:
                o_dict = o.to_dict()
                factory_tags = None
                for f in factories:
                    if f.name == o_dict["llm_factory"]:
                        factory_tags = f.tags
                        break

                if o_dict["llm_factory"] not in res:
                    res[o_dict["llm_factory"]] = {
                        "tags": factory_tags,
                        "llm": []
                    }

                res[o_dict["llm_factory"]]["llm"].append({
                    "id": o_dict["id"],
                    "type": o_dict["mdl_type"],
                    "name": o_dict["llm_name"],
                    "used_token": o_dict["used_tokens"],
                    "api_base": o_dict["api_base"] or "",
                    "max_tokens": o_dict["max_tokens"] or 8192,
                    "status": o_dict["status"] or "1"
                })
        else:
            res = {}
            for o in TenantLLMService.get_my_llms(db, user.id):
                if o.llm_factory not in res:
                    res[o.llm_factory] = {
                        "tags": o.tags,
                        "llm": []
                    }
                res[o.llm_factory]["llm"].append({
                    "id": o.id,
                    "type": o.mdl_type,
                    "name": o.llm_name,
                    "used_token": o.used_tokens,
                    "status": o.status
                })
        return get_json_result(data=res)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Embeddings 文件上传辅助函数 ====================

# 支持的媒体文件扩展名
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "tiff", "tif", "webp", "svg", "ico", "avif", "heic"}
VIDEO_EXTENSIONS = {"mp4", "avi", "mov", "mkv", "webm", "wmv", "flv"}


def get_media_type_from_ext(filename: str) -> Literal["image_url", "video_url"] | None:
    """根据文件扩展名判断媒体类型"""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in IMAGE_EXTENSIONS:
        return "image_url"
    elif ext in VIDEO_EXTENSIONS:
        return "video_url"
    return None


async def upload_file_to_presigned_url(file: UploadFile) -> tuple[str, str | None]:
    """
    上传文件到对象存储并返回预签名URL

    Args:
        file: FastAPI UploadFile 对象

    Returns:
        (url, media_type): 预签名URL和媒体类型
    """
    content = await file.read()
    if not content:
        raise ValueError("Empty file content")

    bucket = (settings.OSS.get("bucket") or
              settings.MINIO.get("bucket") or
              "multimodal-temp")

    ext = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else "bin"
    unique_filename = f"embeddings_upload/{get_uuid()}.{ext}"
    content_type = CONTENT_TYPE_MAP.get(ext, "application/octet-stream")

    try:
        settings.STORAGE_IMPL.put(bucket, unique_filename, content, content_type=content_type)
    except TypeError:
        # Fallback for storage backends that don't support content_type
        settings.STORAGE_IMPL.put(bucket, unique_filename, content)

    url = settings.STORAGE_IMPL.get_presigned_url(bucket, unique_filename, expires=3600)
    if not url:
        raise Exception("Failed to generate presigned URL")

    media_type = get_media_type_from_ext(file.filename) if file.filename else None
    return url, media_type


# ==================== Embeddings 接口 ====================

@router.post('/embeddings', summary="文本/多模态向量化（2025标准）", response_description="返回OpenAI风格的embedding结果")
async def embeddings_api(
    request_body: EmbeddingsMultiModalRequest | None = Body(None),
    request_form: str | None = Form(None, alias="request", description="请求JSON（multipart时使用）"),
    files: list[UploadFile] | None = File(None, description="上传的媒体文件列表"),
    merge_mode_form: str | None = Form(None, alias="merge_mode", description="多文件处理模式: independent|merged"),
    raw_req: Request = None,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### POST `/v1/llm/embeddings` 文本向量化服务（2025标准）

**功能描述**:
此接口提供标准化的文本与多模态向量化服务。支持按租户默认嵌入模型或显式指定模型进行编码，可同时提交纯文本、图片 URL、视频 URL 组合内容，并返回与 OpenAI Embeddings 接口一致的响应结构（object=list, data=[...], model, usage）。

**支持两种请求模式**:
1. **JSON Body 模式**：Content-Type: application/json，直接传递 JSON 请求体
2. **Multipart 模式**：Content-Type: multipart/form-data，支持文件上传

---

### 请求参数

#### JSON Body 模式

| 字段              | 类型                  | 必填 | 默认值     | 描述                                                                 |
|-------------------|-----------------------|------|-----------|----------------------------------------------------------------------|
| `model`           | `string`              | 否   | 租户默认   | 嵌入模型名称；不填则使用当前租户的默认嵌入模型。 |
| `input`           | `string or string[]`  | 否   | -         | 待向量化文本；支持单条或批量，纯多模态场景可为空。                   |
| `media`           | `object[]`            | 否   | -         | 多模态输入列表，元素支持 `type=text|image_url|video_url`。 |
| `input_type`      | `string`              | 否   | `document`| `document` 或 `query`；部分模型会对查询向量做专项优化。              |
| `encoding_format` | `string`              | 否   | `float`   | `float` 返回浮点数组；`base64` 返回 float32 打包后的 base64 字符串。  |
| `merge_mode`      | `string`              | 否   | `independent` | `independent` 每个输入独立返回向量（默认）；`merged` 融合为单向量。 |
| `user`            | `string`              | 否   | -         | 可选的用户标识，用于审计或配额统计。                                  |

#### Multipart 模式（支持文件上传）

| 字段              | 类型                  | 必填 | 描述                                                                 |
|-------------------|-----------------------|------|----------------------------------------------------------------------|
| `request`         | `string`              | 否   | JSON 格式的请求体（同上述 JSON Body 模式的内容）                     |
| `files`           | `file[]`              | 否   | 上传的媒体文件列表（图片、视频），将自动转换为临时URL                |
| `merge_mode`      | `string`              | 否   | 多文件处理模式，覆盖 request 中的 merge_mode                         |

---

### 请求示例

#### 批量文档向量（JSON Body）
```json
{
  "model": "text-embedding-3-small",
  "input": ["hello world", "multirag"],
  "input_type": "document",
  "encoding_format": "float"
}
```

#### 上传文件（Multipart）
```bash
curl -X POST "/v1/llm/embeddings" \\
  -F 'request={"model":"doubao-embedding-vision-250615"}' \\
  -F "files=@/path/to/image.png" \\
  -F "merge_mode=merged"
```

#### 多文件独立向量（Multipart）
```bash
curl -X POST "/v1/llm/embeddings" \\
  -F 'request={"model":"doubao-embedding-vision-250615"}' \\
  -F "files=@/path/to/image1.png" \\
  -F "files=@/path/to/image2.jpg" \\
  -F "merge_mode=independent"
```

---

### 成功响应 (Response 200)

#### 融合模式（merge_mode=merged）
```json
{
  "object": "list",
  "data": [
    { "object": "embedding", "index": 0, "embedding": [0.0123, -0.0456, 0.0789] }
  ],
  "model": "doubao-embedding-vision-250615",
  "usage": { "prompt_tokens": 42, "total_tokens": 42 }
}
```

#### 独立模式（merge_mode=independent）
```json
{
  "object": "list",
  "data": [
    { "object": "embedding", "index": 0, "embedding": [...] },
    { "object": "embedding", "index": 1, "embedding": [...] }
  ],
  "model": "doubao-embedding-vision-250615",
  "usage": { "prompt_tokens": 84, "total_tokens": 84 }
}
```

---

### 错误响应

- **400: Invalid JSON**
- **404: Tenant not found**
- **404: Embedding model not available**
- **500: Embedding generation failed**
- **500: File upload failed**
    """
    # ==================== 双模式请求解析 ====================
    if request_form is not None:
        # Multipart 模式：从 form 字段解析 JSON
        try:
            request = EmbeddingsMultiModalRequest.model_validate_json(request_form)
        except ValidationError as e:
            raise HTTPException(status_code=400, detail=f"Invalid request JSON: {str(e)}")
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    elif request_body is not None:
        # JSON Body 模式
        request = request_body
    else:
        # 兜底：从原始请求解析
        try:
            data = await raw_req.json()
            request = EmbeddingsMultiModalRequest.model_validate(data)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
        except ValidationError as e:
            raise HTTPException(status_code=400, detail=f"Invalid request: {str(e)}")
        except Exception:
            request = EmbeddingsMultiModalRequest()

    # ==================== 处理上传的文件 ====================
    # 获取合并模式（优先 Form 参数，其次 JSON 中的参数）
    actual_merge_mode = merge_mode_form or request.merge_mode or "independent"

    # 处理上传的文件，转换为临时URL
    uploaded_media: list[VolcEmbeddingMedia] = []
    if files:
        for file in files:
            if file.size and file.size > 0:
                try:
                    url, media_type = await upload_file_to_presigned_url(file)
                    if media_type == "image_url":
                        uploaded_media.append(VolcEmbeddingMedia(
                            type="image_url",
                            image_url={"url": url}
                        ))
                    elif media_type == "video_url":
                        uploaded_media.append(VolcEmbeddingMedia(
                            type="video_url",
                            video_url={"url": url}
                        ))
                    else:
                        logging.warning(f"Unsupported media type for file: {file.filename}, skipping")
                except Exception as e:
                    logging.exception(f"Failed to upload file: {file.filename}")
                    raise HTTPException(status_code=500, detail=f"File upload failed: {str(e)}")

    req = request.model_dump()

    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")
    tenant_id = tenants[0]["tenant_id"]

    raw_input = req.get("input", [])
    if raw_input is None:
        inputs = []
    else:
        inputs = raw_input if isinstance(raw_input, list) else [raw_input]
        inputs = [item for item in inputs if isinstance(item, str) and item]
    # 合并 JSON 中的 media 和上传的文件
    media_items: list[VolcEmbeddingMedia] = (request.media or []) + uploaded_media
    model_name = req.get("model")
    input_type = (req.get("input_type") or "document").lower()
    encoding_format = (req.get("encoding_format") or "float").lower()

    try:
        embd_config = get_model_config_by_type_and_name(db, tenant_id, LLMType.EMBEDDING.value, model_name)
        emb_bundle = LLMBundle(db, tenant_id, embd_config)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Embedding model not available: {str(e)}")

    try:
        vectors: list[Any]
        used_tokens: int
        import numpy as np

        # 检查是否是视觉模型（强制多模态）
        is_vision_model = model_name and any(k in model_name.lower() for k in ["vision", "multimodal", "vl"])

        # 检查模型是否支持多模态输入
        supports_multimodal = getattr(emb_bundle.mdl, "_supports_multimodal", False) or is_vision_model
        if media_items and not supports_multimodal:
            model_display_name = emb_bundle.llm_name or getattr(emb_bundle.mdl, "model_name", "Unknown")
            raise HTTPException(
                status_code=400,
                detail=f"The embedding model '{model_display_name}' does not support multimodal inputs (images, videos). "
                       "Please use a multimodal embedding model (e.g., VolcEngine) for media content, or remove media items from the request."
            )

        if media_items or is_vision_model:
            # ==================== 多模态向量化 ====================
            if actual_merge_mode == "independent":
                # 独立模式（默认）：每个输入独立生成向量
                vectors = []
                total_tokens = 0

                # 处理文本输入
                for text in inputs:
                    if input_type == "query":
                        vec, tk = emb_bundle.encode_queries(text)
                    else:
                        vecs, tk = emb_bundle.encode([text])
                        vec = vecs[0] if len(vecs) > 0 else vecs
                    vectors.append(vec)
                    total_tokens += tk

                # 处理媒体输入（每个独立调用）
                for media in media_items:
                    media_payload = {
                        "model": req.get("model") or emb_bundle.llm_name or getattr(emb_bundle.mdl, "model_name", None),
                        "input": [media.model_dump(by_alias=True)]
                    }
                    embedding, tk = emb_bundle.encode([media_payload])
                    if isinstance(embedding, (list, tuple, np.ndarray)) and len(embedding) > 0:
                        vectors.append(embedding[0])
                    else:
                        vectors.append(embedding)
                    total_tokens += tk

                used_tokens = total_tokens
            else:
                # 融合模式：所有输入合并为单个向量
                normalized_media = [media.model_dump(by_alias=True) if isinstance(media, VolcEmbeddingMedia) else media for media in media_items]
                combined_inputs: list[Any] = [{"type": "text", "text": text} for text in inputs] + normalized_media
                payload = {
                    "model": req.get("model") or emb_bundle.llm_name or getattr(emb_bundle.mdl, "model_name", None),
                    "input": combined_inputs,
                }
                # 如果是 vision 模型，必须通过 payload 形式传递，并在底层触发多模态路由
                embedding, used_tokens = emb_bundle.encode([payload])

                # 修复：正确处理 numpy array 和 list，提取第一条向量
                if isinstance(embedding, (list, tuple, np.ndarray)) and len(embedding) > 0:
                    vectors = [embedding[0]]
                else:
                    vectors = [embedding]
        elif input_type == "query":
            if len(inputs) == 1:
                vec, used_tokens = emb_bundle.encode_queries(inputs[0])
                vectors = [vec]
            else:
                vectors = []
                total_tokens = 0
                for q in inputs:
                    v, tk = emb_bundle.encode_queries(q)
                    vectors.append(v)
                    total_tokens += tk
                used_tokens = total_tokens
        else:
            vectors, used_tokens = emb_bundle.encode(inputs)
    except Exception as e:
        logging.exception("Embedding generation failed")
        raise HTTPException(status_code=500, detail=f"Embedding generation failed: {str(e)}")

    def to_base64(v) -> str:
        try:
            seq = v.tolist()
        except AttributeError:
            seq = list(v)
        arr = array('f', [float(x) for x in seq])
        return base64.b64encode(arr.tobytes()).decode('ascii')

    data_items = []
    for idx, v in enumerate(vectors):
        if encoding_format == "base64":
            embedding_value = to_base64(v)
        else:
            try:
                embedding_value = v.tolist()
            except AttributeError:
                embedding_value = [float(x) for x in v]
        data_items.append({
            "object": "embedding",
            "index": idx,
            "embedding": embedding_value
        })

    try:
        used = int(used_tokens)
    except Exception:
        used = used_tokens

    result = {
        "object": "list",
        "data": data_items,
        "model": getattr(emb_bundle, "llm_name", model_name) or "",
        "usage": {"prompt_tokens": used, "total_tokens": used}
    }

    return get_json_result(data=result)


@router.get('/list', summary="列出所有模型", response_description="成功列出所有模型")
def list_app(mdl_type: str | None = None, db: Session = Depends(get_db), user=Depends(manager)):
    self_deployed = ["Youdao", "FastEmbed", "BAAI", "Ollama", "Xinference", "LocalAI", "LM-Studio", "GPUStack"]
    weighted = ["Youdao", "FastEmbed", "BAAI"] if settings.LIGHTEN != 0 else []
    tenant_id = user.id
    try:
        TenantLLMService.ensure_mineru_from_env(db, tenant_id)
        objs = TenantLLMService.query(db, tenant_id=tenant_id)
        facts = set(o.llm_factory for o in objs if o.api_key and o.status==StatusEnum.VALID.value)
        tenant_llm_mapping = {f"{o.llm_name}@{o.llm_factory}": o for o in objs}
        status = {(o.llm_name + "@" + o.llm_factory) for o in objs if o.status == StatusEnum.VALID.value}
        llms = LLMService.get_all(db)
        llms = [m.to_dict() for m in llms if m.status == StatusEnum.VALID.value and m.fid not in weighted and (m.fid == 'Builtin' or (m.llm_name + "@" + m.fid) in status)]

        for m in llms:
            tenant_llm = tenant_llm_mapping.get(m["llm_name"] + "@" + m["fid"])
            m["id"] = tenant_llm.id if tenant_llm else None
            m["available"] = m["fid"] in facts or m["llm_name"].lower() == "flag-embedding" or m["fid"] in self_deployed
            if "tei-" in os.getenv("COMPOSE_PROFILES", "") and m["model_type"]==LLMType.EMBEDDING and m["fid"]=="Builtin" and m["llm_name"]==os.getenv('TEI_MODEL', ''):
                m["available"] = True

        llm_set = set([m["llm_name"] + "@" + m["fid"] for m in llms])
        for o in objs:
            if o.llm_name + "@" + o.llm_factory in llm_set:
                continue
            llms.append({"id": o.id, "llm_name": o.llm_name, "mdl_type": o.mdl_type, "fid": o.llm_factory, "available": True, "status": StatusEnum.VALID.value})

        res = {}
        for m in llms:
            if mdl_type and m["mdl_type"].find(mdl_type) < 0:
                continue
            if m["fid"] not in res:
                res[m["fid"]] = []
            res[m["fid"]].append(m)

        return get_json_result(data=res)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post('/chat_service', summary="模型对话服务", response_description="成功调用对话模型")
async def chat_service(request: LLMServiceRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req = request.model_dump()
    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")

    my_llms = TenantLLMService.get_my_llms(db, tenants[0]["tenant_id"])

    def get_llm_type(model_name, my_llms):
        for row in my_llms:
            if row.llm_name == model_name:
                return row.mdl_type
        return None  # 如果找不到，返回 None

    llm_type = get_llm_type(req["llm_name"], my_llms)
    if llm_type:
        logging.debug(f"The llm_type for model {req['llm_name']} is: {llm_type}")
    else:
        raise HTTPException(status_code=404, detail=f"Model {req['llm_name']} not found in the list.")

    mdl_config = get_model_config_by_type_and_name(db, tenants[0]["tenant_id"], llm_type, req["llm_name"])
    chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], mdl_config)
    # 构建调用参数
    call_params = {
        "system": req["prompt"],
        "history": req["messages"],
        "gen_conf": req["gen_conf"]
    }

    # 如果llm_type为image2text，添加image参数
    if llm_type == 'image2text':
        call_params["image"] = req["image"]

    # 根据是否流式调用选择合适的方法
    if req["stream"]:
        data = []
        if req["delta_stream"]:
            async for chunk in chat_mdl.async_chat_streamly_delta(**call_params):
                if isinstance(chunk, int):
                    break
                data.append(chunk)
        else:
            async for chunk in chat_mdl.async_chat_streamly(**call_params):
                data.append(chunk)
    else:
        data = await chat_mdl.async_chat(**call_params)

    return get_json_result(data=data)


@router.post('/chat_service_sse', summary="模型对话服务", response_description="成功调用对话模型")
async def chat_service_sse(request: LLMServiceRequest, req: Request, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/v1/llm/chat_service` 模型对话服务

**功能描述**:
此接口用于调用对话模型，基于用户提供的输入生成对应的响应内容。支持文本生成、图像到文本转换、消息处理等多种模型类型，接口根据请求体中的配置，选择适当的模型及生成方式，提供流式和非流式响应模式。

---

### 请求体 (Request Body)

| 字段         | 类型                | 必填 | 描述                                                                                  |
|--------------|---------------------|------|---------------------------------------------------------------------------------------|
| `prompt`     | `string`           | 否   | 用户提供的提示内容，用于引导对话模型生成响应。                                        |
| `messages`   | `list[dict]`       | 是   | 对话消息列表，包含用户与模型之间的对话历史，格式为 `{ "role": "user/assistant", "content": "..." }`。|
| `llm_name`   | `string`           | 是   | 模型名称，用于指定所调用的语言模型。                                                 |
| `stream`     | `boolean`          | 是   | 指定是否使用流式响应，`true` 表示流式响应，`false` 表示非流式响应。                   |
| `gen_conf`   | `object`           | 否   | 生成配置，控制对话生成行为，例如温度值、生成长度等（具体配置视模型能力而定）。         |
| `image`      | `string (Base64)`  | 否   | Base64 编码的图像数据，适用于图像到文本的转换模型。                                   |

---

### 响应 (Response)

#### 成功响应 (200)

- **流式响应**:
    - **`Content-Type: text/event-stream`**
    - 数据按块流式返回，每条消息以 `data:` 开头，并以两个换行符 `\\n\\n` 结束。

    **示例**:

    ```plaintext
    data: {"retcode": 0, "retmsg": "", "data": "你好"}

    data: {"retcode": 0, "retmsg": "", "data": "你好👋！"}

    data: {"retcode": 0, "retmsg": "", "data": "你好👋！我是人工智能助手"}

    data: {"retcode": 0, "retmsg": "", "data": true}
    ```

- **非流式响应**:
    - **`Content-Type: application/json`**
    - **示例**:
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": "你好👋！我是人工智能助手，很高兴见到你！欢迎问我任何问题。"
    }
    ```

---

### 错误响应

#### **404: Tenant not found**
- **描述**: 当根据用户ID查找租户信息失败时，返回此错误。
- **示例**:
    ```json
    {
        "detail": "Tenant not found!"
    }
    ```

#### **404: Model not found**
- **描述**: 当指定的模型名称在用户租户可用模型列表中未找到时，返回此错误。
- **示例**:
    ```json
    {
        "detail": "Model glm-4-airx not found in the list."
    }
    ```

#### **500: 内部错误**
- **描述**: 当发生意外错误时，返回此错误。
- **示例**:
    - **流式响应错误**:
        ```plaintext
        data: {"retcode": 500, "retmsg": "Internal server error", "data": {"answer": "**ERROR**: Internal error"}}
        ```
    - **非流式响应错误**:
        ```json
        {
            "retcode": 500,
            "retmsg": "Internal server error",
            "data": {"answer": "**ERROR**: Internal error"}
        }
        ```

---

### 主要流程

1. 从请求中提取用户输入的内容、模型名称和配置信息。
2. 通过用户信息获取租户信息，确保用户的租户身份；如果未找到租户信息，返回404错误。
3. 获取用户租户关联的模型列表，确定模型类型 (`llm_type`)。
4. 根据 `llm_type` 判断是否需要传入 `image` 参数，构建生成请求。
5. 根据 `stream` 参数选择流式或非流式的生成方法，调用模型获取对话响应内容。
6. 返回生成的对话结果。

---

### 注意事项

- **模型选择**:
    - 仅当 `llm_type` 为 `image2text` 时传递 `image` 参数，以确保在需要图像到文本转换时能处理Base64编码的图像数据。
    - 支持多种模型类型 (如文本生成、图像到文本、消息对话)，请根据需求选择适当的 `llm_name` 和 `llm_type`。
- **流式调用**:
    - 若 `stream` 参数为 `True`，将返回流式响应，用于实时数据生成；若为 `False`，返回完整的响应数据。
- **数据格式**:
    - 返回数据格式可能因模型及请求内容不同而有所变化；默认返回JSON格式的结构化数据或文本响应。
- **流式响应结束标记**:
    - 流式响应的最后一条消息为:
      ```plaintext
      data: {"retcode": 0, "retmsg": "", "data": true}
      ```

    """
    req_dict = request.model_dump()

    # 检查是否有敏感词过滤结果
    if hasattr(req, 'state') and hasattr(req.state, 'sensitive_filter_result'):
        filter_result = req.state.sensitive_filter_result
        logging.info(f"[SSE接口] 检测到敏感词过滤结果: {filter_result.get('is_sensitive')}")

        if filter_result.get('is_sensitive') and filter_result.get('action') == 'filter':
            # 使用过滤后的内容替换原始内容
            filtered_content = filter_result.get('filtered_content', '')
            matched_words = filter_result.get('matched_words', [])

            # 处理messages中的敏感词
            if 'messages' in req_dict and isinstance(req_dict['messages'], list):
                for msg in req_dict['messages']:
                    if isinstance(msg, dict) and 'content' in msg:
                        # 查找并替换匹配的敏感词
                        content = msg['content']
                        for word_info in matched_words:
                            word = word_info.get('word', '')
                            replacement = word_info.get('replacement', '***')
                            if word in content:
                                content = content.replace(word, replacement)
                        msg['content'] = content

            # 处理prompt中的敏感词
            if 'prompt' in req_dict and req_dict['prompt']:
                prompt = req_dict['prompt']
                for word_info in matched_words:
                    word = word_info.get('word', '')
                    replacement = word_info.get('replacement', '***')
                    if word in prompt:
                        prompt = prompt.replace(word, replacement)
                req_dict['prompt'] = prompt

            logging.info(f"[SSE接口] 已应用敏感词过滤，替换了 {len(matched_words)} 个敏感词")

    # 使用可能已过滤的数据
    req = req_dict

    # 将操作封装在异步函数中
    async def process_non_streaming_request():
        # 获取租户信息
        tenants = TenantService.get_info_by(db, user.id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        my_llms = TenantLLMService.get_my_llms(db, tenants[0]["tenant_id"])

        def get_llm_type(model_name, my_llms):
            for row in my_llms:
                if row.llm_name == model_name:
                    return row.mdl_type
            return None

        llm_type = get_llm_type(req["llm_name"], my_llms)
        if llm_type:
            logging.debug(f"The llm_type for model {req['llm_name']} is: {llm_type}")
        else:
            raise HTTPException(status_code=404, detail=f"Model {req['llm_name']} not found in the list.")

        mdl_config = get_model_config_by_type_and_name(db, tenants[0]["tenant_id"], llm_type, req["llm_name"])
        chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], mdl_config)
        # 构建调用参数
        call_params = {
            "system": req["prompt"],
            "history": req["messages"],
            "gen_conf": req["gen_conf"]
        }

        # 如果llm_type为image2text，添加image参数
        if llm_type == 'image2text':
            call_params["images"] = req["image"]

        # 非流式调用，直接返回完整响应
        data = await chat_mdl.async_chat(**call_params)
        return data

    # 获取初始设置的同步函数
    def get_initial_setup():
        tenants = TenantService.get_info_by(db, user.id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        my_llms = TenantLLMService.get_my_llms(db, tenants[0]["tenant_id"])

        def get_llm_type(model_name, my_llms):
            for row in my_llms:
                if row.llm_name == model_name:
                    return row.mdl_type
            return None

        llm_type = get_llm_type(req["llm_name"], my_llms)
        if not llm_type:
            raise HTTPException(status_code=404, detail=f"Model {req['llm_name']} not found in the list.")

        mdl_config = get_model_config_by_type_and_name(db, tenants[0]["tenant_id"], llm_type, req["llm_name"])
        chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], mdl_config)

        # 构建调用参数
        call_params = {
            "system": req["prompt"],
            "history": req["messages"],
            "gen_conf": req["gen_conf"]
        }

        # # 如果llm_type为image2text，添加image参数
        # if llm_type == 'image2text':
        #     call_params["image"] = req["image"]

        if llm_type == "image2text":
            llm_model_config = get_model_config_by_type_and_name(
                db, tenants[0]["tenant_id"], LLMType.IMAGE2TEXT.value, req["llm_name"]
            )
            call_params["images"] = req["image"]
        else:
            llm_model_config = get_model_config_by_type_and_name(
                db, tenants[0]["tenant_id"], LLMType.CHAT.value, req["llm_name"]
            )

        max_tokens = llm_model_config.get("max_tokens", 8192)
        kbinfos = {"total": 0, "chunks": [], "doc_aggs": []}
        questions = [m["content"] for m in req["messages"] if m["role"] == "user"]
        if req["tavily_api_key"]:
            tav = Tavily(req["tavily_api_key"])
            tav_res = tav.retrieve_chunks(" ".join(questions))
            kbinfos["chunks"].extend(tav_res["chunks"])
            kbinfos["doc_aggs"].extend(tav_res["doc_aggs"])
            kbinfos["total"] = len(kbinfos["chunks"])
        knowledges = kb_prompt(kbinfos, max_tokens)
        knowledges = "\n------\n" + "\n\n------\n\n".join(knowledges)
        call_params["system"] = "\n------\n" + call_params["system"] + knowledges
        return chat_mdl, call_params

    # 处理流式响应
    async def stream_response():
        try:
            # 获取初始设置
            chat_mdl, call_params = get_initial_setup()

            stream_iter = chat_mdl.async_chat_streamly_delta(**call_params)
            accumulated = ""
            async for kind, value, state in _stream_with_think_delta(stream_iter):
                if kind == "marker":
                    flags = {"start_to_think": True} if value == "<think>" else {"end_to_think": True}
                    sse_data = json.dumps({"retcode": 0, "retmsg": "", "data": "", **flags}, ensure_ascii=False)
                else:
                    if req["delta_stream"]:
                        text_data = value
                    else:
                        accumulated += value
                        text_data = accumulated
                    sse_data = json.dumps({"retcode": 0, "retmsg": "", "data": text_data}, ensure_ascii=False)
                yield f"data: {sse_data}\n\n"

            # 流结束
            end_message = json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False)
            yield f"data: {end_message}\n\n"

        except Exception as e:
            error_message = json.dumps(
                {"retcode": 500, "retmsg": str(e), "data": {"answer": f"**ERROR**: {str(e)}"}},
                ensure_ascii=False
            )
            yield f"data: {error_message}\n\n"
            # 流结束标记
            end_message = json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False)
            yield f"data: {end_message}\n\n"

    # 根据是否流式调用选择合适的方法
    if req["stream"]:
        return StreamingResponse(stream_response(), media_type="text/event-stream")
    else:
        # 直接调用异步函数
        data = await process_non_streaming_request()
        return get_json_result(data=data)


@router.post('/fine_prompt', summary="优化提示词", response_description="返回优化后的提示词")
async def fine_prompt(request: FinePromptRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    **功能描述**:
    此接口用于根据用户输入的任务描述或现有提示词，优化生成详细的系统提示词，以便更好地引导语言模型完成任务。该接口结合预定义的优化提示模板 (META_PROMPT)，确保模型输出的提示词更加清晰、具体，并且能合理规划任务的完成步骤。

    ### 请求体 (Request Body):
    - **model_dump (dict)**: 包含以下字段：
        - `prompt` (str): 用户提供的任务描述或现有提示词，将根据此内容进行优化。
        - `llm_name` (str): 大语言模型名称，用于选择特定的模型来生成优化的提示词。
        - `gen_conf` (dict, 可选): 生成配置，控制提示词生成的行为。

    ### 响应 (Response):
    - **成功响应 (200)**:
        - `data` (dict): 返回包含优化后的提示词。格式可能包括简单的字符串或结构化的JSON，具体取决于模型输出的要求。

    ### 错误响应:
    - **404: Tenant not found**:
        - 当根据用户ID查找租户信息失败时，返回此错误。

    ### 主要流程:
    1. 从请求中提取用户输入的提示词及相关配置信息。
    2. 通过用户信息检索相关租户信息。如果租户信息未找到，抛出404错误。
    3. 根据预定义的META_PROMPT，结合用户输入的任务描述，使用指定的LLM模型生成优化后的系统提示词。
    4. 将生成的提示词返回给用户，格式可能为JSON或简单文本。

    ### 注意事项:
    - **优化策略**:
        - 系统优先保留用户提供的内容，对于简单提示词进行微调，对于复杂提示词则在不改变原始结构的前提下增强清晰度。
        - 系统会根据内容需要添加示例和步骤，确保输出提示词具有高可读性和清晰性。
    - **常量与格式**:
        - 提示词中应包括不易受到提示注入影响的常量，例如规则、评分标准等。
        - 对于任务输出明确的结构化数据(如JSON)会更倾向于返回JSON格式，但不会使用代码块包装（除非明确要求）。
    """
    req = request.model_dump()
    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")
    META_PROMPT = """
    Given a task description or existing prompt, produce a detailed system prompt to guide a language model in completing the task effectively.

    # Guidelines

    - Understand the Task: Grasp the main objective, goals, requirements, constraints, and expected output.
    - Minimal Changes: If an existing prompt is provided, improve it only if it's simple. For complex prompts, enhance clarity and add missing elements without altering the original structure.
    - Reasoning Before Conclusions**: Encourage reasoning steps before any conclusions are reached. ATTENTION! If the user provides examples where the reasoning happens afterward, REVERSE the order! NEVER START EXAMPLES WITH CONCLUSIONS!
        - Reasoning Order: Call out reasoning portions of the prompt and conclusion parts (specific fields by name). For each, determine the ORDER in which this is done, and whether it needs to be reversed.
        - Conclusion, classifications, or results should ALWAYS appear last.
    - Examples: Include high-quality examples if helpful, using placeholders [in brackets] for complex elements.
       - What kinds of examples may need to be included, how many, and whether they are complex enough to benefit from placeholders.
    - Clarity and Conciseness: Use clear, specific language. Avoid unnecessary instructions or bland statements.
    - Formatting: Use markdown features for readability. DO NOT USE ``` CODE BLOCKS UNLESS SPECIFICALLY REQUESTED.
    - Preserve User Content: If the input task or prompt includes extensive guidelines or examples, preserve them entirely, or as closely as possible. If they are vague, consider breaking down into sub-steps. Keep any details, guidelines, examples, variables, or placeholders provided by the user.
    - Constants: DO include constants in the prompt, as they are not susceptible to prompt injection. Such as guides, rubrics, and examples.
    - Output Format: Explicitly the most appropriate output format, in detail. This should include length and syntax (e.g. short sentence, paragraph, JSON, etc.)
        - For tasks outputting well-defined or structured data (classification, JSON, etc.) bias toward outputting a JSON.
        - JSON should never be wrapped in code blocks (```) unless explicitly requested.

    The final prompt you output should adhere to the following structure below. Do not include any additional commentary, only output the completed system prompt. SPECIFICALLY, do not include any additional messages at the start or end of the prompt. (e.g. no "---")

    [Concise instruction describing the task - this should be the first line in the prompt, no section header]

    [Additional details as needed.]

    [Optional sections with headings or bullet points for detailed steps.]

    # Steps [optional]

    [optional: a detailed breakdown of the steps necessary to accomplish the task]

    # Output Format

    [Specifically call out how the output should be formatted, be it response length, structure e.g. JSON, markdown, etc]

    # Examples [optional]

    [Optional: 1-3 well-defined examples with placeholders if necessary. Clearly mark where examples start and end, and what the input and output are. User placeholders as necessary.]
    [If the examples are shorter than what a realistic example is expected to be, make a reference with () explaining how real examples should be longer / shorter / different. AND USE PLACEHOLDERS! ]

    # Notes [optional]

    [optional: edge cases, details, and an area to call or repeat out specific important considerations]
    """.strip()
    chat_config = get_model_config_by_type_and_name(db, tenants[0]["tenant_id"], LLMType.CHAT.value, req["llm_name"])
    chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], chat_config)

    data = await chat_mdl.async_chat(META_PROMPT, [{"role": "user", "content": "Task, Goal, or Current Prompt:\n" + req["prompt"]}],
                         req["gen_conf"])

    return get_json_result(data=data)


@router.post('/generate_suggestions', summary="生成用户输入建议", response_description="成功调用大模型生成建议")
async def generate_suggestions(request: SuggestionRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/v1/suggestions/generate_suggestions` 生成用户输入建议

**功能描述**
此接口用于调用大模型根据当前对话上下文及智能体的配置信息，生成用户下一轮输入建议。生成的建议应紧密相关、多样性高、避免重复，并与用户的角色匹配。

---

## 请求体 (Request Body)

| 字段            | 类型            | 必填 | 描述                                                                                     |
|-----------------|-----------------|------|------------------------------------------------------------------------------------------|
| `llm_name`      | `string`       | 是   | 模型名称，指定用于生成建议的大模型。                                                    |
| `last_response` | `string`       | 是   | 模型在对话中的最后一轮回复内容。                                                        |
| `messages`      | `list[dict]`   | 是   | 对话消息列表，包括用户与模型之间的上下文历史，格式为 `{ "role": "user/assistant", "content": "..." }`。 |
| `gen_conf`      | `object`       | 否   | 大模型的生成配置，用于控制生成行为，例如温度值、生成长度等。                              |
| `num`           | `integer`      | 否   | 返回的建议条数，默认为3。                                                               |

---

## 响应 (Response)

### 成功响应 (200)

- **`Content-Type: application/json`**
- **示例**:
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": [
            "建议 1",
            "建议 2",
            "建议 3"
        ]
    }
    ```

---

## 错误响应

### 404: Tenant not found
- **描述**
  当根据用户ID查找租户信息失败时，返回此错误。
- **示例**
    ```json
    {
        "detail": "Tenant not found!"
    }
    ```

### 500: JSON 解析错误
- **描述**
  模型返回数据无法解析为有效的JSON格式。
- **示例**
    ```json
    {
        "detail": "模型返回数据无法解析为 JSON: Expecting value: line 1 column 1 (char 0)"
    }
    ```

### 500: 无效的建议列表
- **描述**
  模型返回的建议列表为空或格式不正确。
- **示例**
    ```json
    {
        "detail": "模型未返回有效的建议列表"
    }
    ```

### 500: 解析模型返回数据时发生未知错误
- **描述**
  发生未知错误导致无法解析模型返回的数据。
- **示例**
    ```json
    {
        "detail": "解析模型返回数据时发生未知错误: list index out of range"
    }
    ```

---

## 主要流程

1. 从请求中提取 `llm_name`、`last_response`、`messages`、`gen_conf` 和 `num` 等字段。
2. 获取用户对应的租户信息；如果未找到，返回404错误。
3. 构造 `system_prompt`，用于明确生成建议的任务及格式。
4. 调用大模型生成建议，并解析模型返回的 JSON 数据。
5. 校验建议列表的有效性，确保返回符合要求的结果。
6. 返回生成的建议列表。

---

## 注意事项

- **紧密相关性**
  建议内容必须与最后一轮模型回复和对话上下文相关，避免偏离主题。

- **多样性**
  确保生成的建议从不同方向或角度切入，不重复也不雷同。

- **角色适配性**
  根据用户的身份和对话场景定制建议内容，确保更具针对性。

- **JSON 格式输出**
  大模型返回数据必须是纯 JSON 格式，不应包含多余标记或格式化代码块。

- **异常处理**
  当模型返回数据无法解析或格式不符合预期时，记录日志并返回错误响应。

    """
    req = request.model_dump()

    system_prompt = f"""
    你是一个智能对话助手，当前任务是根据用户对话上下文和智能体的配置信息，为用户生成 {req["num"]} 条下一轮输入建议。生成的建议需满足以下要求：

    1. **紧密相关**：建议内容应与最后一轮的模型回复紧密相关。
    2. **避免重复**：建议的输入内容不能与上下文中用户已提问或模型已回答的内容重复。
    3. **角色匹配**：建议内容应与用户当前的角色及对话类型匹配。例如，如果用户是技术开发人员，建议可以是具体的技术问题；如果是普通用户，则建议应更加简洁和实用。
    4. **多样性**：生成的 {req["num"]} 条建议应具有一定的多样性，涵盖不同的方向或角度。

    ### 输入格式

    以下是输入数据的格式：
    - 最后一轮模型回复（`last_response`）：{json.dumps(req["last_response"], ensure_ascii=False)}
    - 对话上下文（`messages`）：{json.dumps(req["messages"], ensure_ascii=False)}

    ### 输出格式

    请仅输出以下格式的纯 JSON 数据，不要添加任何其他标记：
    ```json
    {{
        "suggestions": [
            "建议 1",
            "建议 2",
            "建议 3"
        ]
    }}
    """
    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")

    chat_config = get_model_config_by_type_and_name(db, tenants[0]["tenant_id"], LLMType.CHAT.value, req["llm_name"])
    chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], chat_config)

    # 调用大模型
    response = await chat_mdl.async_chat(
        system=system_prompt,
        history=[{"role": "user", "content": "请按照要求输出"}],
        gen_conf=req["gen_conf"].get("gen_conf", {})
    )

    try:
        # 检查模型返回数据
        logging.info("模型返回原始数据: %s", response)

        if response.startswith("```json"):
            logging.warning("检测到带格式的代码块标记，正在移除")
            response = response.replace("```json", "").replace("```", "").strip()
        elif response.startswith("```"):
            logging.warning("检测到代码块标记，正在移除")
            response = response.strip("```").strip()

        # 解析 JSON
        response_data = json.loads(response)
        suggestions = response_data.get("suggestions", [])
    except json.JSONDecodeError as e:
        logging.error("JSON 解析错误: %s", str(e))
        logging.debug("模型返回数据: %s", response)
        raise HTTPException(
            status_code=500,
            detail=f"模型返回数据无法解析为 JSON: {str(e)}"
        )
    except Exception as e:
        logging.error("解析模型返回数据时发生未知错误: %s", str(e))
        raise HTTPException(
            status_code=500,
            detail=f"解析模型返回数据时发生未知错误: {str(e)}"
        )

    # 确保 suggestions 是一个非空列表
    if not suggestions or not isinstance(suggestions, list):
        logging.error("模型返回无效建议列表: %s", response_data)
        raise HTTPException(status_code=500, detail="模型未返回有效的建议列表")

    logging.info("生成的用户输入建议: %s", suggestions)

    return get_json_result(data=suggestions)


@router.post(
    "/recognize_intent",
    summary="表单意图识别",
    response_description="返回匹配到的表单 ID 及置信度",
    response_model=RecognizeIntentResponse
)
async def recognize_intent(
    request: RecognizeIntentRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### 功能
    - 根据 `user_text` 在 `candidate_forms` 中找到**最合适**的表单。
    - 仅返回 `intent_id`（表单 ID）和 `confidence`。

    ### 请求示例
    ```json
    {
      "user_text": "我想明天上午请半天病假",
      "candidate_forms": [
        { "form_id": 1, "name": "请假申请表", "description": "员工请假使用" },
        { "form_id": 2, "name": "报销单",   "description": "差旅费报销" }
      ],
      "llm_name": "gpt-4o-mini",
      "gen_conf": { "temperature": 0 }
    }
    ```
    """
    req = request.model_dump()

    # 1) 获取租户 & 模型信息（与你现有 chat_service 相同逻辑）
    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")

    my_llms = TenantLLMService.get_my_llms(db, tenants[0]["tenant_id"])

    def get_llm_type(model_name: str, rows):
        for r in rows:
            if r.llm_name == model_name:
                return r.mdl_type
        return None

    llm_type = get_llm_type(req["llm_name"], my_llms)
    if llm_type != LLMType.CHAT.value:
        raise HTTPException(status_code=400, detail="指定模型不是对话模型，或未找到")

    mdl_config = get_model_config_by_type_and_name(db, tenants[0]["tenant_id"], llm_type, req["llm_name"])
    chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], mdl_config)

    # 2) 组 Prompt（few-shot + 指令，完全内存化）
    forms: list[dict] = req["candidate_forms"]
    form_lines = [
        f"{f['form_id']} | {f['name']} — {f.get('description', '')}" for f in forms
    ]
    prompt = (
        "Role: 你是企业工作流中的 **表单意图识别助手**。\n"
        "Task: 从候选表单中挑选最符合用户需求的一张，**只输出对应的 form_id 数字**，不要包含多余文字。\n"
        "Candidates:\n"
        + "\n".join(form_lines)
        + "\nUSER: "
        + req["user_text"]
        + "\nAnswer:"
    )

    # 3) 调 LLM
    answer = await chat_mdl.async_chat(
        system=prompt,
        history=[{"role": "user", "content": "请根据要求返回规定的格式"}],  # 空 user 消息 → 模型直接输出结果
        gen_conf=req["gen_conf"]
    )

    # 4) 解析结果（取首个数字；无则兜底为候选列表第 0 个）
    m = re.search(r"\d+", answer)
    if m:
        intent_id = int(m.group())
        confidence = 1.0
    else:
        intent_id = forms[0]["form_id"]
        confidence = 0.5

    return get_json_result(
        data={"intent_id": intent_id, "confidence": confidence}
    )


@router.post(
    "/fill_fields",
    summary="表单字段填充",
    response_model=FillFieldsResponse
)
async def fill_fields(
    req: FillFieldsRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    1) 用 LLM 粗填 JSON
    2) 后端解析 & 校验
    3) 如缺失必填且 retry=True，则内部追问一次
    4) 返回 field_values + missing + invalid
    """
    # —— 租户 & 模型准备（同 chat_service）
    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")
    my_llms = TenantLLMService.get_my_llms(db, tenants[0]["tenant_id"])
    llm_type = next((r.mdl_type for r in my_llms if r.llm_name == req.llm_name), None)
    if llm_type != LLMType.CHAT.value:
        raise HTTPException(status_code=400, detail="指定模型不是对话模型，或未找到")
    mdl_config = get_model_config_by_type_and_name(db, tenants[0]["tenant_id"], llm_type, req.llm_name)
    chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], mdl_config)

    async def call_llm(prompt: str) -> str:
        return await chat_mdl.async_chat(
            system=prompt,
            history=[{"role": "user", "content": "请按照要求输出"}],
            gen_conf=req.gen_conf
        )

    def build_prompt(user_text: str, fields: list[FieldMeta]) -> str:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = []
        for f in fields:
            desc = f.description or ""
            # 如果是 datetime 且描述中有"默认填当前时间"，将"当前时间"替换为具体值
            if f.type == "datetime" and "默认填当前时间" in desc:
                # 将描述里"当前时间"或"默认填当前时间"替换为 now_str
                desc = desc.replace("默认填当前时间", f"默认填当前时间（即 {now_str}）")
            lines.append(f"{f.name} ({f.type})：{desc}")
        prompt = (
                "请根据下列字段含义，从用户输入中抽取对应的值，未提及的必填字段请按提示填充。\n\n"
                + "\n".join(lines)
                + f"\n\n用户输入：{user_text}\n"
                  '请只输出 JSON，例如 {"字段1":"值1","字段2":"值2",…}'
        )
        return prompt

    def parse_and_validate(raw: str, fields: list[FieldMeta]) -> dict[str, Any]:
        # 解析 JSON
        try:
            # 尝试提取首个 {...} 结构
            m = re.search(r"\{.*\}", raw, re.S)
            obj = json.loads(m.group()) if m else {}
        except Exception:
            obj = {}
        values: dict[str, Any] = {}
        missing = []
        invalid: dict[str, str] = {}

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        for f in fields:
            v = obj.get(f.name)
            # 缺失
            if v is None:
                if f.required:
                    if f.type == "datetime" and "当前时间" in (f.description or ""):
                        values[f.name] = now
                    else:
                        missing.append(f.name)
                        continue
                else:
                    values[f.name] = "" if f.type == "text" else None
                    continue
            # 校验类型
            if f.type == "enum":
                if f.options and v not in f.options:
                    invalid[f.name] = "不在可选项内"
                else:
                    values[f.name] = v
            elif f.type == "datetime":
                if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", str(v)):
                    invalid[f.name] = "格式错误，需 YYYY-MM-DD HH:mm"
                else:
                    values[f.name] = v
            else:  # text or others
                values[f.name] = v
        return {"values": values, "missing": missing, "invalid": invalid}

    # —— 阶段一
    prompt1 = build_prompt(req.user_text, req.fields)
    raw1 = await call_llm(prompt1)
    res1 = parse_and_validate(raw1, req.fields)

    # —— 阶段二：内部追问（仅一次）
    if res1["missing"] and req.retry:
        # 构造追问 Prompt
        missed = ",".join(res1["missing"])
        prompt2 = (
            f"请补全字段：{missed}，按之前格式再输出完整 JSON，不要多余文字。"
        )
        raw2 = await call_llm(prompt2)
        res2 = parse_and_validate(raw2, req.fields)
        final = res2
    else:
        final = res1

    return get_json_result(data={
        "field_values": final["values"],
        "missing": final["missing"],
        "invalid": final["invalid"]
    })


@router.post('/enhanced_chat_sse')
async def enhanced_chat_service_sse(
        request: ChatRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):

    mcp_sessions = []

    try:
        # 获取租户信息
        try:
            tenants = TenantService.get_info_by(db, user.id)
            if not tenants:
                raise HTTPException(status_code=404, detail="Tenant not found!")

            tenant_id = tenants[0]["tenant_id"]
        except Exception as e:
            logging.error(f"Failed to get tenant info: {e}")
            raise HTTPException(status_code=500, detail="Failed to get tenant information")

        # 验证模型
        try:
            my_llms = TenantLLMService.get_my_llms(db, tenant_id)
        except Exception as e:
            logging.error(f"Failed to get LLMs: {e}")
            raise HTTPException(status_code=500, detail="Failed to get available models")

        llm_type = None
        for row in my_llms:
            if row.llm_name == request.llm_name:
                llm_type = row.mdl_type
                break

        if not llm_type:
            raise HTTPException(status_code=404, detail=f"Model {request.llm_name} not found")

        # 准备知识上下文（复用现有的Tavily集成）
        knowledge_context = prepare_knowledge_context(db, request.messages, request.tavily_api_key, tenant_id, request.llm_name)

        # 创建对话Agent适配器，直接复用Agent类
        chat_agent = ChatAgentAdapter(
            tenant_id=tenant_id,
            llm_name=request.llm_name,
            system_prompt=request.prompt,
            mcp_ids=request.mcp_ids
        )

        if not request.stream:
            # 非流式响应 - 使用纯异步方法，避免阻塞事件循环
            try:
                result_content = await chat_agent.chat_async(
                    query=request.messages[-1]["content"] if request.messages else "",
                    messages=request.messages[:-1] if request.messages else [],
                    knowledge_context=knowledge_context,
                    files=request.files
                )
                return {"retcode": 0, "retmsg": "success", "data": {"answer": result_content}}
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Chat error: {str(e)}")

        # 流式响应 - 使用原生异步方法，减少线程开销
        async def sse_stream():
            try:
                if request.structured_output:
                    # 结构化输出模式 - 使用异步方法
                    accumulated_text = ""
                    async for message in chat_agent.chat_with_tools_stream_structured_async(
                        query=request.messages[-1]["content"] if request.messages else "",
                        messages=request.messages[:-1] if request.messages else [],
                        knowledge_context=knowledge_context,
                        files=request.files
                    ):
                        if "start_to_think" in message or "end_to_think" in message:
                            # think 标记提升到 SSE 顶层，与其他端点格式一致
                            key = "start_to_think" if "start_to_think" in message else "end_to_think"
                            yield f"data: {json.dumps({'retcode': 0, 'retmsg': '', 'data': '', key: True}, ensure_ascii=False)}\n\n"
                        elif isinstance(message, dict) and message.get("type") == "text":
                            if request.delta_stream:
                                out = message
                            else:
                                accumulated_text += message["content"]
                                out = {**message, "content": accumulated_text}
                            yield f"data: {json.dumps({'retcode': 0, 'retmsg': '', 'data': out}, ensure_ascii=False)}\n\n"
                        else:
                            yield f"data: {json.dumps({'retcode': 0, 'retmsg': '', 'data': message}, ensure_ascii=False)}\n\n"

                    # 发送完成消息
                    end_data = {
                        "retcode": 0,
                        "retmsg": "Stream completed",
                        "data": True
                    }
                    yield f"data: {json.dumps(end_data, ensure_ascii=False)}\n\n"

                else:
                    # 非结构化输出模式 - 使用异步方法，带 think 块处理
                    stream_iter = chat_agent.chat_with_tools_stream_async(
                        query=request.messages[-1]["content"] if request.messages else "",
                        messages=request.messages[:-1] if request.messages else [],
                        knowledge_context=knowledge_context,
                        files=request.files
                    )
                    accumulated_content = ""
                    async for kind, value, state in _stream_with_think_delta(stream_iter):
                        if kind == "marker":
                            flags = {"start_to_think": True} if value == "<think>" else {"end_to_think": True}
                            yield f"data: {json.dumps({'retcode': 0, 'retmsg': '', 'data': '', **flags}, ensure_ascii=False)}\n\n"
                        else:
                            if request.delta_stream:
                                text_data = value
                            else:
                                accumulated_content += value
                                text_data = accumulated_content
                            yield f"data: {json.dumps({'retcode': 0, 'retmsg': '', 'data': text_data}, ensure_ascii=False)}\n\n"

                    # 添加工具使用摘要（如果启用）
                    if request.verbose_tool_use and chat_agent.agent.tools:
                        use_tools = getattr(chat_agent.agent, '_last_use_tools', [])
                        if use_tools:
                            tools_summary = f"\n\n📊 **本次对话使用了 {len(use_tools)} 个工具调用**"
                            yield f"data: {json.dumps({'retcode': 0, 'retmsg': '', 'data': tools_summary}, ensure_ascii=False)}\n\n"

                    # 发送完成消息
                    end_data = {
                        "retcode": 0,
                        "retmsg": "Stream completed",
                        "data": True
                    }
                    yield f"data: {json.dumps(end_data, ensure_ascii=False)}\n\n"

            except asyncio.CancelledError:
                logging.info("SSE stream was cancelled by client")
                raise
            except Exception as e:
                logging.exception(f"Stream error: {e}")
                error_data = {
                    "retcode": 500,
                    "retmsg": str(e),
                    "data": {"answer": f"**ERROR**: {str(e)}"}
                }
                yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"
                end_data = {
                    "retcode": 0,
                    "retmsg": "",
                    "data": True
                }
                yield f"data: {json.dumps(end_data, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            sse_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "Content-Type": "text/event-stream; charset=utf-8",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Cache-Control",
                "Access-Control-Expose-Headers": "X-Accel-Buffering"
            }
        )

    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logging.exception(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # 清理MCP会话
        if mcp_sessions:
            try:
                close_multiple_mcp_toolcall_sessions(mcp_sessions)
            except Exception as e:
                logging.warning(f"Error closing MCP sessions: {e}")

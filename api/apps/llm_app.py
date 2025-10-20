# coding=utf-8
"""
@project: multirag
@Author：龙
@file： llm_app.py
@date：2024/7/11 14:30
@desc:
"""
import asyncio
import threading
import logging
import json
import re
from datetime import datetime
from typing import Any, Literal
import base64
from array import array

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from api.apps import manager, executor
from agent.component.agent_with_tools import Agent, AgentParam
# from api.db.services.llm_service import LLMFactoriesService, TenantLLMService, LLMService, LLMBundle
from api.db.services.tenant_llm_service import LLMFactoriesService, TenantLLMService
from api.db.services.llm_service import LLMService, LLMBundle
from api.db.services.user_service import TenantService
from api import settings
from api.utils.api_utils import get_json_result, server_error_response, get_data_error_result
from api.db import StatusEnum, LLMType
from api.db.db_models import TenantLLM, get_db, db_connection
from api.utils.base64_image import test_image
from core.llm import EmbeddingModel, ChatModel, CvModel, RerankModel, TTSModel

from core.prompts.prompts import kb_prompt
from core.utils.tavily_conn import Tavily
from api.db.services.mcp_server_service import MCPServerService
from core.utils.mcp_tool_call_conn import close_multiple_mcp_toolcall_sessions


class ChatAgentAdapter:
    """对话Agent适配器，直接复用Agent类但适配对话场景"""

    def __init__(self, tenant_id: str, llm_name: str, system_prompt: str = "", mcp_ids: list[str] = None):
        self.tenant_id = tenant_id
        self.llm_name = llm_name
        self.system_prompt = system_prompt
        self.mcp_ids = mcp_ids or []

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
                logging.debug(f"Tool callback: {component_id}")

            def get_variable_value(self, var_name):
                return self.globals.get(var_name, "")

            def set_variable_value(self, var_name, value):
                self.globals[var_name] = value

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

    def chat_with_tools_stream(self, query: str, messages: list[dict] = None,
                               knowledge_context: str = "", files: list[str] = None):
        """使用工具进行流式对话，直接复用Agent的流式能力"""

        # 准备历史记录 - 保持字典格式，但添加当前查询
        history = []
        
        if messages:
            # 保持字典格式的消息
            history = messages.copy()
        
        # 添加当前查询到历史记录末尾（因为_prepare_prompt_variables会用[:-1]移除它）
        if query:
            history.append({"role": "user", "content": query})
        
        self.canvas_mock.history = history

        # 准备输入变量
        self.canvas_mock.globals["sys.query"] = query
        if files:
            self.canvas_mock.globals["sys.files"] = files

        # 如果有知识上下文，临时修改系统提示词
        original_prompt = self.agent._param.sys_prompt
        if knowledge_context:
            enhanced_prompt = original_prompt + "\n\n" + knowledge_context
            self.agent._param.sys_prompt = enhanced_prompt

        try:
            # 准备Agent调用参数
            kwargs = {
                "user_prompt": query,
                "reasoning": "Direct chat request",
                "context": "Chat conversation context"
            }

            # 检查Agent是否有工具
            if self.agent.tools:
                # 有工具时，使用Agent的完整流式工具调用能力
                # 直接复用Agent的stream_output_with_tools方法
                prompt, msg = self.agent._prepare_prompt_variables()
                
                # 重要：像 _invoke 方法一样，将 system 消息添加到 msg 中
                # 这样 _react_with_tools_streamly 中的 hist 才会包含 system 消息
                from core.prompts.prompts import message_fit_in
                _, msg = message_fit_in([{"role": "system", "content": prompt}, *msg], int(self.agent.chat_mdl.max_length * 0.97))

                # 创建用于收集工具使用历史的列表
                use_tools = []

                # 添加工具调用开始提示
                yield "🔧 Starting tool analysis...\n"

                # 直接调用Agent的_react_with_tools_streamly方法，监控工具调用
                previous_tool_count = 0
                for delta_ans, _ in self.agent._react_with_tools_streamly(prompt, msg, use_tools):
                    # 检查是否有新的工具调用
                    if len(use_tools) > previous_tool_count:
                        # 显示新的工具调用
                        new_tools = use_tools[previous_tool_count:]
                        for tool_call in new_tools:
                            tool_name = tool_call.get('name', 'Unknown')
                            tool_args = tool_call.get('arguments', {})

                            # 简化参数显示
                            args_preview = ""
                            if isinstance(tool_args, dict) and tool_args:
                                key_args = []
                                # for k, v in list(tool_args.items())[:2]:  # 只显示前2个关键参数
                                for k, v in list(tool_args.items()):
                                    # if isinstance(v, str) and len(v) > 30:
                                    #     v = v[:30] + "..."
                                    key_args.append(f"{k}={v}")
                                args_preview = f"({', '.join(key_args)})"

                            yield f"\n🔧 **工具调用**: {tool_name}{args_preview}\n"

                            # 显示结果（如果已有结果）
                            tool_results = tool_call.get('results', '')
                            if tool_results:
                                results_preview = str(tool_results)
                                # if len(results_preview) > 200:
                                #     results_preview = results_preview[:200] + "..."
                                yield f"📋 **结果**: {results_preview}\n\n"

                        previous_tool_count = len(use_tools)
                    if delta_ans:
                        yield delta_ans

                # 保存工具使用历史，供verbose模式使用
                self.agent._last_use_tools = use_tools
                # 同时设置为Agent的标准输出格式
                if use_tools:
                    self.agent.set_output("use_tools", use_tools)

            else:
                # 没有工具时，直接使用LLM流式输出
                # 调用Agent的invoke方法获取流式生成器
                self.agent._param.prompts = [{"role": "user", "content": query}]
                prompt, msg = self.agent._prepare_prompt_variables()

                # 直接使用Agent的_stream_output方法
                for delta in self.agent._stream_output(prompt, msg):
                    yield delta

        finally:
            # 恢复原始系统提示词
            if knowledge_context:
                self.agent._param.sys_prompt = original_prompt

    def chat_with_tools_stream_structured(self, query: str, messages: list[dict] = None,
                                         knowledge_context: str = "", files: list[str] = None):
        """
        带工具的流式对话 - 结构化输出版本
        返回结构化的SSE消息，每个文本消息都包含累积内容
        """
        # 处理历史消息
        messages = messages or []
        history = []
        
        for msg in messages:
            history.append({
                "role": msg.get("role", "user"),
                "content": msg.get("content", "")
            })
        
        if query:
            history.append({"role": "user", "content": query})
        
        # 合并知识上下文到系统提示词
        original_prompt = self.agent._param.sys_prompt
        if knowledge_context:
            self.agent._param.sys_prompt = self.agent._param.sys_prompt + "\n" + knowledge_context
        
        try:
            # 更新 canvas_mock 的历史记录
            self.canvas_mock.history = history
            
            # 准备提示词
            self.agent._param.prompts = history
            prompt, msg = self.agent._prepare_prompt_variables()
            
            # 累积文本内容（重要：与原实现保持一致）
            accumulated_text = ""
            
            # 检查是否有工具可用
            if self.agent.tools:
                # 重要：像 _invoke 方法一样，将 system 消息添加到 msg 中
                from core.prompts.prompts import message_fit_in
                _, msg = message_fit_in([{"role": "system", "content": prompt}, *msg], int(self.agent.chat_mdl.max_length * 0.97))
                
                use_tools = []
                
                # 发送工具分析开始消息
                yield {"type": "tool_start", "content": "Starting tool analysis..."}
                
                # 调用Agent的_react_with_tools_streamly方法
                previous_tool_count = 0
                call_id_counter = 0
                
                for delta_ans, _ in self.agent._react_with_tools_streamly(prompt, msg, use_tools):
                    # 检查是否有新的工具调用
                    if len(use_tools) > previous_tool_count:
                        new_tools = use_tools[previous_tool_count:]
                        for tool_call in new_tools:
                            call_id_counter += 1
                            call_id = f"call_{call_id_counter}"
                            
                            tool_name = tool_call.get('name', 'Unknown')
                            tool_args = tool_call.get('arguments', {})
                            
                            # 发送工具调用消息
                            yield {
                                "type": "tool_call",
                                "content": {
                                    "tool_name": tool_name,
                                    "arguments": tool_args,
                                    "call_id": call_id
                                }
                            }
                            
                            # 如果有结果，发送工具结果消息
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
                        
                        previous_tool_count = len(use_tools)
                    
                    # 处理文本增量并输出累积内容
                    if delta_ans:
                        accumulated_text += delta_ans  # 累积增量
                        # 输出累积内容（与原实现一致）
                        yield {
                            "type": "text",
                            "content": accumulated_text  # 直接输出累积内容
                        }
                
                # 发送工具分析结束消息
                if use_tools:
                    yield {
                        "type": "tool_end",
                        "content": {
                            "total_calls": len(use_tools),
                            "summary": f"Used {len(use_tools)} tool(s)"
                        }
                    }
                
                # 保存工具使用历史
                self.agent._last_use_tools = use_tools
                if use_tools:
                    self.agent.set_output("use_tools", use_tools)
            
            else:
                # 没有工具时，直接使用LLM流式输出
                self.agent._param.prompts = [{"role": "user", "content": query}]
                prompt, msg = self.agent._prepare_prompt_variables()
                
                for delta in self.agent._stream_output(prompt, msg):
                    if delta:
                        accumulated_text += delta  # 累积增量
                        # 输出累积内容（与原实现一致）
                        yield {
                            "type": "text",
                            "content": accumulated_text  # 直接输出累积内容
                        }
            
            # 不再发送完成消息，由外层处理
        
        except Exception as e:
            # 发送错误消息
            yield {
                "type": "error",
                "content": {
                    "error": str(e),
                    "code": 500
                }
            }
        
        finally:
            # 恢复原始系统提示词
            if knowledge_context:
                self.agent._param.sys_prompt = original_prompt



def prepare_knowledge_context(db: Session, messages: list[dict], tavily_api_key: str, tenant_id: str, llm_name: str) -> str:
    """准备知识上下文，复用现有的Tavily集成"""
    knowledge_context = ""

    if tavily_api_key and messages:
        try:
            llm_model_config = TenantLLMService.get_model_config(db, tenant_id, LLMType.CHAT, llm_name)
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


class AddLLMRequest(BaseModel):
    llm_factory: str
    llm_name: str
    mdl_type: str
    api_key: str = None
    api_base: str | None = None
    ark_api_key: str | None = None
    endpoint_id: str | None = None
    bedrock_ak: str | None = None
    bedrock_sk: str | None = None
    bedrock_region: str | None = None
    fish_audio_ak: str | None = None
    fish_audio_refid: str | None = None
    hunyuan_sid: str | None = None
    hunyuan_sk: str | None = None
    tencent_cloud_sid: str | None = None
    tencent_cloud_sk: str | None = None
    spark_api_password: str | None = None
    spark_app_id: str | None = None
    spark_api_secret: str | None = None
    spark_api_key: str | None = None
    google_project_id: str | None = None
    google_region: str | None = None
    google_service_account_key: str | None = None


class DeleteLLMRequest(BaseModel):
    llm_factory: str
    llm_name: str


class ListLLMRequest(BaseModel):
    mdl_type: str | None = None


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
    """
    此异步函数用于获取所有模型供应商的信息，排除特定供应商，并将结果以JSON格式返回。
    摘要: 获取模型供应商信息
    响应描述: 成功获取到所有模型供应商信息

    返回:
    - dict: 包含模型供应商信息的JSON结果，数据部分是一个字典列表，每个字典代表一个供应商的信息。

    功能:
    1. 查询数据库中所有的模型供应商信息。
    2. 排除名为"Youdao"、"FastEmbed"和"BAAI"的供应商信息。
    3. 将筛选后的供应商信息转换为字典列表。
    4. 将结果封装为JSON格式的字典并返回。

    流程:
    1. 使用LLMFactoriesService从数据库中获取所有供应商信息。
    2. 遍历获取的供应商信息，排除特定名称的供应商。
    3. 将筛选后的供应商信息转换为字典列表。
    4. 返回封装后的JSON结果。

    异常处理:
    - 如果在执行数据库操作或数据处理过程中发生异常，将捕获异常并调用server_error_response函数返回服务器错误响应。

    注意:
    - 被排除的供应商名称"Youdao"、"FastEmbed"和"BAAI"可能是系统默认供应商或特殊供应商，具体原因需根据实际业务逻辑确定。
    """

    try:
        fac = LLMFactoriesService.get_all(db)
        fac = [f.to_dict() for f in fac if f.name not in ["Youdao", "FastEmbed", "BAAI"]]
        llms = LLMService.get_all(db)
        mdl_types = {}
        for m in llms:
            if m.status != StatusEnum.VALID.value:
                continue
            if m.fid not in mdl_types:
                mdl_types[m.fid] = set([])
            mdl_types[m.fid].add(m.mdl_type)
        for f in fac:
            f["model_types"] = list(mdl_types.get(f["name"], [LLMType.CHAT, LLMType.EMBEDDING, LLMType.RERANK,
                                                              LLMType.IMAGE2TEXT, LLMType.SPEECH2TEXT, LLMType.TTS]))
        return get_json_result(data=fac)
    except Exception as e:
        return server_error_response(e)


@router.post('/set_api_key', summary="新增模型厂商api key", response_description="成功保存该模型服务厂商的api key")
def set_api_key(request: SetAPIKeyRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    此异步函数用于设置模型制造商的API密钥，并验证其是否能正确访问特定类型的模型。
    摘要: 新增模型厂商api key
    响应描述: 成功保存该模型服务厂商的api key

    参数:
    - request (SetAPIKeyRequest): 一个依赖注入的请求对象，包含模型工厂ID、API密钥等信息。

    返回:
    - dict: 成功时返回一个表示操作成功的JSON结果；失败时返回一个包含错误信息的JSON结果。

    功能:
    1. 验证API密钥是否能够成功访问聊天模型（Chat），并尝试访问嵌入（Embedding）和重排序（Rerank）模型（注：后两者当前未实现）。
    2. 如果API密钥无法访问任何模型，函数将返回一个错误结果。
    3. 更新或创建租户的模型配置，包括API密钥、基础URL等信息。
    4. 如果在更新或创建过程中遇到完整性错误（例如，API密钥已存在），则抛出HTTP异常。

    流程:
    1. 解析请求体并初始化变量。
    2. 遍历所有属于指定模型工厂的模型，尝试使用API密钥访问它们。
    3. 如果访问失败，收集错误信息。
    4. 如果有错误信息，返回错误结果。
    5. 否则，更新或创建租户的模型配置。
    6. 返回操作成功的JSON结果。

    注意:
    - 目前仅实现了对聊天模型的访问测试。
    - 未来可能扩展到嵌入和重排序模型的测试。
    - 在更新或创建租户模型配置时，会检查API密钥是否已存在，以避免重复。
    """
    req = request.model_dump()
    chat_passed, embd_passed, rerank_passed = False, False, False
    factory = req["llm_factory"]
    extra = {"provider": factory}
    msg = ""
    for llm in LLMService.query(db, fid=factory):
        if not embd_passed and llm.mdl_type == LLMType.EMBEDDING.value:
            assert factory in EmbeddingModel, f"Embedding model from {factory} is not supported yet."
            mdl = EmbeddingModel[factory](req["api_key"], llm.llm_name, base_url=req.get("base_url"))
            try:
                arr, tc = mdl.encode(["Test if the api key is available"])
                if len(arr[0]) == 0:
                    raise Exception("Fail")
                embd_passed = True
            except Exception as e:
                msg += f"\nFail to access embedding model({llm.llm_name}) using this api key." + str(e)
        elif not chat_passed and llm.mdl_type == LLMType.CHAT.value:
            assert factory in ChatModel, f"Chat model from {factory} is not supported yet."
            mdl = ChatModel[factory](req["api_key"], llm.llm_name, base_url=req.get("base_url"), **extra)
            try:
                m, tc = mdl.chat("", [{"role": "user", "content": "Hello! How are you doing!"}],
                                 {"temperature": 0.9, 'max_tokens': 50})
                print(m)
                if m.find("**ERROR**") >= 0:
                    raise Exception(m)
            except Exception as e:
                msg += f"\nFail to access model({llm.fid}/{llm.llm_name}) this api key." + str(e)
            chat_passed = True
        elif not rerank_passed and llm.mdl_type == LLMType.RERANK:
            assert factory in RerankModel, f"Re-rank model from {factory} is not supported yet."
            mdl = RerankModel[factory](req["api_key"], llm.llm_name, base_url=req.get("base_url"))
            try:
                arr, tc = mdl.similarity("What's the weather?", ["Is it sunny today?"])
                if len(arr) == 0 or tc == 0:
                    raise Exception("Fail")
                rerank_passed = True
                logging.debug(f'passed model rerank {llm.llm_name}')
            except Exception as e:
                msg += f"\nFail to access model({llm.fid}/{llm.llm_name}) using this api key." + str(e)

    if msg:
        return get_data_error_result(retmsg=msg)

    llm_config = {
        "api_key": req["api_key"],
        "api_base": req.get("base_url", "")
    }
    for n in ["mdl_type", "llm_name"]:
        if n in req:
            llm_config[n] = req[n]

    for llm in LLMService.query(db, fid=factory):
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
def add_llm(request: AddLLMRequest, db: Session = Depends(get_db), user=Depends(manager)):
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

    def apikey_json(keys):
        nonlocal req
        return json.dumps({k: req.get(k, "") for k in keys})

    if factory == "VolcEngine":
        # For VolcEngine, due to its special authentication method
        # Assemble ark_api_key endpoint_id into api_key
        api_key = apikey_json(["ark_api_key", "endpoint_id"])

    elif factory == "Tencent Hunyuan":
        req["api_key"] = apikey_json(["hunyuan_sid", "hunyuan_sk"])
        return set_api_key(SetAPIKeyRequest(**req), db, user)

    elif factory == "Tencent Cloud":
        req["api_key"] = apikey_json(["tencent_cloud_sid", "tencent_cloud_sk"])
        return set_api_key(SetAPIKeyRequest(**req), db, user)

    elif factory == "Bedrock":
        api_key = apikey_json(["bedrock_ak", "bedrock_sk", "bedrock_region"])

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

    llm = {
        "tenant_id": user.id,
        "llm_factory": factory,
        "mdl_type": req["mdl_type"],
        "llm_name": llm_name,
        "api_base": req.get("api_base", ""),
        "api_key": api_key
    }

    msg = ""
    mdl_nm = llm["llm_name"].split("___")[0]
    extra = {"provider": factory}
    if llm["mdl_type"] == LLMType.EMBEDDING.value:
        assert factory in EmbeddingModel, f"Embedding model from {factory} is not supported yet."
        mdl = EmbeddingModel[factory](
            key=llm['api_key'],
            model_name=mdl_nm,
            base_url=llm["api_base"])
        try:
            arr, tc = mdl.encode(["Test if the api key is available"])
            if len(arr[0]) == 0:
                raise Exception("Fail")
        except Exception as e:
            msg += f"\nFail to access embedding model({mdl_nm})." + str(e)
    elif llm["mdl_type"] == LLMType.CHAT.value:
        assert factory in ChatModel, f"Chat model from {factory} is not supported yet."
        mdl = ChatModel[factory](
            key=llm['api_key'],
            model_name=mdl_nm,
            base_url=llm["api_base"],
            **extra,
        )
        try:
            m, tc = mdl.chat("", [{"role": "user", "content": "Hello! How are you doing!"}],
                             {"temperature": 0.9, 'max_tokens': 500})
            if not tc and m.find("**ERROR**:") >= 0:
                raise Exception(m)
        except Exception as e:
            msg += f"\nFail to access model({factory}/{mdl_nm})." + str(e)
    elif llm["mdl_type"] == LLMType.RERANK:
        assert factory in RerankModel, f"RE-rank model from {factory} is not supported yet."
        try:
            mdl = RerankModel[factory](
                key=llm["api_key"],
                model_name=mdl_nm,
                base_url=llm["api_base"]
            )
            arr, tc = mdl.similarity("Hello~ MultiRAGer!", ["Hi, there!", "Ohh, my friend!"])
            if len(arr) == 0:
                raise Exception("Not known.")
        except KeyError:
            msg += f"{factory} dose not support this model({factory}/{mdl_nm})"
        except Exception as e:
            msg += f"\nFail to access model({factory}/{mdl_nm})." + str(e)
    elif llm["mdl_type"] == LLMType.IMAGE2TEXT.value:
        assert factory in CvModel, f"Image to text model from {factory} is not supported yet."
        mdl = CvModel[factory](
            key=llm["api_key"],
            model_name=mdl_nm,
            base_url=llm["api_base"]
        )
        try:
            image_data = test_image
            m, tc = mdl.describe(image_data)
            if not m and not tc:
                raise Exception(m)
        except Exception as e:
            msg += f"\nFail to access model({factory}/{mdl_nm})." + str(e)
    elif llm["mdl_type"] == LLMType.TTS:
        assert factory in TTSModel, f"TTS model from {factory} is not supported yet."
        mdl = TTSModel[factory](
            key=llm["api_key"], model_name=mdl_nm, base_url=llm["api_base"]
        )
        try:
            for resp in mdl.tts("Hello~ MultiRAGer!"):
                pass
        except RuntimeError as e:
            msg += f"\nFail to access model({factory}/{mdl_nm})." + str(e)
    else:
        # TODO: check other type of models
        pass

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
    """
    此异步函数用于删除指定的语言模型（LLM）。
    摘要: 删除模型
    响应描述: 成功删除该模型

    参数:
    - request (DeleteLLMRequest): 一个依赖注入的请求对象，包含模型供应商和模型名称的信息。

    返回:
    - dict: 成功时返回一个表示操作成功的JSON结果。

    功能:
    1. 删除指定供应商和名称的模型。

    流程:
    1. 解析请求体，获取模型供应商和模型名称。
    2. 检查指定的模型是否存在。
    3. 从数据库中删除指定的模型。
    4. 返回操作成功的JSON结果。

    异常处理:
    - 如果在删除模型的过程中发生异常，将捕获异常并返回服务器错误响应。
    """
    try:
        req = request.model_dump()
        llm = TenantLLMService.query(db, tenant_id=user.id, llm_factory=req["llm_factory"], llm_name=req["llm_name"])
        if not llm:
            raise HTTPException(status_code=404, detail="LLM not found")

        TenantLLMService.filter_delete(db, [TenantLLM.tenant_id == user.id, TenantLLM.llm_factory == req["llm_factory"],
                                            TenantLLM.llm_name == req["llm_name"]])
        return get_json_result(data=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post('/delete_factory', summary="删除模型厂商", response_description="成功删除模型厂商")
def delete_factory(request: DeleteFactoryRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req = request.model_dump()
    TenantLLMService.filter_delete(
        db, [TenantLLM.tenant_id == user.id, TenantLLM.llm_factory == req["llm_factory"]])
    return get_json_result(data=True)


@router.get('/my_llms', summary="获取用户的所有模型", response_description="成功获取到用户的所有模型")
def my_llms(include_details: bool = False, db: Session = Depends(get_db), user=Depends(manager), request: Request = None):
    """
    ### GET `/v1/llm/my_llms` 获取用户的所有模型

**功能描述**:
此接口用于获取当前登录用户的所有可用语言模型列表，支持按模型厂商分组显示，并可选择是否包含详细信息如token使用量、API基址、最大token数等。接口返回用户配置的所有模型的结构化信息，便于前端展示和管理。

---

### 查询参数 (Query Parameters)

| 字段             | 类型      | 必填 | 描述                                                                                    |
|------------------|-----------|------|---------------------------------------------------------------------------------------|
| `include_details`| `boolean` | 否   | 是否包含详细信息，`true` 表示返回详细信息（包含已使用token数、API基址、最大token数），`false` 表示返回基本信息。默认值为 `false`。|

---

### 响应 (Response)

#### 成功响应 (200)

- **`Content-Type: application/json`**

- **基本信息响应 (`include_details=false`)**:
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "OpenAI": {
                "tags": ["CHAT", "EMBEDDING"],
                "llm": [
                    {
                        "type": "chat",
                        "name": "gpt-4",
                        "used_token": 15420
                    },
                    {
                        "type": "embedding",
                        "name": "text-embedding-ada-002",
                        "used_token": 8950
                    }
                ]
            },
            "Anthropic": {
                "tags": ["CHAT"],
                "llm": [
                    {
                        "type": "chat",
                        "name": "claude-3-opus",
                        "used_token": 12300
                    }
                ]
            }
        }
    }
    ```

- **详细信息响应 (`include_details=true`)**:
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "OpenAI": {
                "tags": ["CHAT", "EMBEDDING"],
                "llm": [
                    {
                        "type": "chat",
                        "name": "gpt-4",
                        "used_token": 15420,
                        "api_base": "https://api.openai.com/v1",
                        "max_tokens": 8192
                    },
                    {
                        "type": "embedding",
                        "name": "text-embedding-ada-002",
                        "used_token": 8950,
                        "api_base": "https://api.openai.com/v1",
                        "max_tokens": 8192
                    }
                ]
            }
        }
    }
    ```

---

### 错误响应

#### **500: 内部错误**
- **描述**: 当发生意外错误时，返回此错误。
- **示例**:
    ```json
    {
        "detail": "数据库连接失败或其他系统错误信息"
    }
    ```

---

### 返回数据结构说明

- **外层结构**: 按模型厂商（如 "OpenAI"、"Anthropic" 等）分组
- **厂商信息**:
    - `tags`: 该厂商支持的模型类型标签数组
    - `llm`: 该厂商下的具体模型列表
- **模型信息**:
    - `type`: 模型类型（如 "chat"、"embedding"、"rerank"、"image2text"、"tts" 等）
    - `name`: 模型名称
    - `used_token`: 已使用的token数量
    - `api_base`: API基础地址（仅在 `include_details=true` 时返回）
    - `max_tokens`: 最大token限制（仅在 `include_details=true` 时返回，默认8192）

---

### 主要流程

1. 根据查询参数 `include_details` 确定返回详细程度。
2. 查询当前用户租户下的所有已配置模型。
3. 如果需要详细信息，则额外查询模型厂商的标签信息并包含API基址、最大token数等。
4. 按模型厂商分组整理数据，每个厂商包含支持的模型类型和具体模型列表。
5. 返回结构化的模型信息数据。

---

### 注意事项

- **数据分组**: 返回数据按模型厂商进行分组，便于前端按厂商展示模型列表。
- **可选详情**: 通过 `include_details` 参数控制是否返回详细信息，基本模式下只返回核心字段以减少数据传输量。
- **Token统计**: `used_token` 字段显示该模型的累计使用量，可用于使用情况分析。
- **厂商标签**: `tags` 字段标识该厂商支持的模型类型，帮助前端做功能分类展示。
- **API配置**: 详细模式下会返回 `api_base` 和 `max_tokens`，用于模型配置管理。

    """
    try:

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
                    "type": o_dict["mdl_type"],
                    "name": o_dict["llm_name"],
                    "used_token": o_dict["used_tokens"],
                    "api_base": o_dict["api_base"] or "",
                    "max_tokens": o_dict["max_tokens"] or 8192
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
                    "type": o.mdl_type,
                    "name": o.llm_name,
                    "used_token": o.used_tokens
                })
        return get_json_result(data=res)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post('/embeddings', summary="文本/多模态向量化（2025标准）", response_description="返回OpenAI风格的embedding结果")
def embeddings_api(request: EmbeddingsMultiModalRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/v1/llm/embeddings` 文本向量化服务（2025标准）

**功能描述**:
此接口提供标准化的文本与多模态向量化服务。支持按租户默认嵌入模型或显式指定模型进行编码，可同时提交纯文本、图片 URL、视频 URL 组合内容，并返回与 OpenAI Embeddings 接口一致的响应结构（object=list, data=[...], model, usage）。

---

### 请求体 (Request Body)

| 字段              | 类型                  | 必填 | 默认值     | 描述                                                                 |
|-------------------|-----------------------|------|-----------|----------------------------------------------------------------------|
| `model`           | `string`              | 否   | 租户默认   | 嵌入模型名称；不填则使用当前租户的默认嵌入模型（`Tenant.embd_id`）。 |
| `input`           | `string or string[]`  | 否   | -         | 待向量化文本；支持单条或批量，纯多模态场景可为空。                   |
| `media`           | `object[]`            | 否   | -         | 多模态输入列表，元素支持 `type=text|image_url|video_url`。若与 `input` 同时出现，将融合为单个向量。 |
| `input_type`      | `string`              | 否   | `document`| `document` 或 `query`；部分模型会对查询向量做专项优化。              |
| `encoding_format` | `string`              | 否   | `float`   | `float` 返回浮点数组；`base64` 返回 float32 打包后的 base64 字符串。  |
| `user`            | `string`              | 否   | -         | 可选的用户标识，用于审计或配额统计。                                  |

---

### 请求示例

#### 批量文档向量（返回 float 数组）
```json
{
  "model": "text-embedding-3-small",
  "input": ["hello world", "multirag"],
  "input_type": "document",
  "encoding_format": "float"
}
```

#### 单条查询向量（返回 base64 编码）
```json
{
  "model": "text-embedding-3-small",
  "input": "what is multirag?",
  "input_type": "query",
  "encoding_format": "base64"
}
```

#### 同时包含文本 + 图片 + 视频的多模态向量
```json
{
  "model": "doubao-embedding-vision-250615",
  "input": ["这是一段辅助描述"],
  "media": [
    {
      "type": "image_url",
      "image_url": {
        "url": "https://ark-project.tos-cn-beijing.volces.com/doc_image/tower.png"
      }
    },
    {
      "type": "video_url",
      "video_url": {
        "url": "https://ark-project.tos-cn-beijing.volces.com/doc_video/ark_vlm_video_input.mp4"
      }
    },
    {
      "type": "text",
      "text": "视频和图片里有什么?"
    }
  ]
}
```

---

### 成功响应 (Response 200)

#### 返回 float 数组
```json
{
  "object": "list",
  "data": [
    { "object": "embedding", "index": 0, "embedding": [0.0123, -0.0456, 0.0789] },
    { "object": "embedding", "index": 1, "embedding": [0.0021, -0.0345, 0.0678] }
  ],
  "model": "text-embedding-3-small",
  "usage": { "prompt_tokens": 42, "total_tokens": 42 }
}
```

#### 返回 base64 字符串
```json
{
  "object": "list",
  "data": [
    { "object": "embedding", "index": 0, "embedding": "AAABP0AAAD8..." }
  ],
  "model": "text-embedding-3-small",
  "usage": { "prompt_tokens": 21, "total_tokens": 21 }
}
```

---

### 错误响应

- **404: Tenant not found**
  ```json
  { "detail": "Tenant not found!" }
  ```

- **404: Embedding model not available**（模型不可用或未授权）
  ```json
  { "detail": "Embedding model not available: <reason>" }
  ```

- **500: Internal Server Error**（生成向量失败或其他内部错误）
  ```json
  { "detail": "Embedding generation failed: <error>" }
  ```

---

### 主要流程

1. 解析请求体，确定 `model`、`input`、`input_type`、`encoding_format`。
2. 若未显式指定 `model`，使用当前租户配置的默认嵌入模型。
3. 若存在 `media`，会与 `input` 文本合并后调用多模态接口，**整个提交仅返回一个融合向量**。
4. 无 `media` 时根据 `input_type`：
   - `document` 调用批量 `encode(inputs)`。
   - `query` 单条调用 `encode_queries(text)`；多条查询按条调用以保留模型的查询优化路径。
5. 根据 `encoding_format` 将向量以 `float` 或 `base64(float32)` 的形式返回。
6. 统一返回 OpenAI 风格响应，包含 `data`、`model` 与 `usage`。

---

### 注意事项

- 若 `input` 为字符串则自动转为单元素数组处理。
- 多模态请求会将 `input` 与 `media` 合并为单个输入列表，返回一个融合后的向量。
- `usage.prompt_tokens` 与 `usage.total_tokens` 返回底层模型统计的已用 token 数。
- `base64` 编码采用 float32 打包后再进行 base64 编码，便于网络传输和前端存储。
    """
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
    media_items: list[VolcEmbeddingMedia] = request.media or []
    model_name = req.get("model")
    input_type = (req.get("input_type") or "document").lower()
    encoding_format = (req.get("encoding_format") or "float").lower()

    try:
        emb_bundle = LLMBundle(db, tenant_id, LLMType.EMBEDDING.value, model_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Embedding model not available: {str(e)}")

    try:
        vectors: list[Any]
        used_tokens: int

        if media_items:
            normalized_media = [media.model_dump(by_alias=True) if isinstance(media, VolcEmbeddingMedia) else media for media in media_items]
            combined_inputs: list[Any] = [ {"type": "text", "text": text} for text in inputs ] + normalized_media
            payload = {
                "model": req.get("model") or emb_bundle.llm_name or getattr(emb_bundle.mdl, "model_name", None),
                "input": combined_inputs,
            }
            embedding, used_tokens = emb_bundle.encode([payload])
            vectors = [embedding[0] if isinstance(embedding, (list, tuple)) else embedding]
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
    """
    列出所有模型，支持按模型类型筛选。
    摘要: 列出所有模型
    响应描述: 成功列出所有模型

    参数:
    - mdl_type (Optional[str]): 可选的模型类型，用于筛选模型。

    返回:
    - dict: 包含所有模型信息的JSON结果，数据部分是一个字典，其中每个键代表一个模型工厂，值是该工厂下的模型信息列表。

    功能:
    1. 查询所有有效的模型信息。
    2. 根据模型类型筛选模型信息。
    3. 按模型工厂分类整理模型信息。
    4. 将整理后的模型信息封装为JSON格式的字典并返回。

    流程:
    1. 使用TenantLLMService从数据库中获取当前用户的所有模型信息。
    2. 使用LLMService获取所有有效的模型信息。
    3. 遍历获取的模型信息，按模型工厂分类整理模型信息。
    4. 将整理后的模型信息封装为JSON格式的字典并返回。

    异常处理:
    - 如果在执行数据库操作或数据处理过程中发生异常，将捕获异常并抛出HTTP异常，返回服务器错误响应。

    注意:
    - 模型信息按模型工厂分类，每个工厂下包含多个模型信息。
    - 可选的模型类型参数用于筛选模型信息，只返回指定类型的模型。
    """
    self_deployed = ["Youdao", "FastEmbed", "BAAI", "Ollama", "Xinference", "LocalAI", "LM-Studio", "GPUStack"]
    weighted = ["Youdao", "FastEmbed", "BAAI"] if settings.LIGHTEN != 0 else []
    try:
        objs = TenantLLMService.query(db, tenant_id=user.id)
        facts = set(o.llm_factory for o in objs if o.api_key)
        llms = LLMService.get_all(db)
        llms = [m.to_dict() for m in llms if m.status == StatusEnum.VALID.value and m.fid not in weighted]

        for m in llms:
            m["available"] = m["fid"] in facts or m["llm_name"].lower() == "flag-embedding" or m["fid"] in self_deployed

        llm_set = set([m["llm_name"] + "@" + m["fid"] for m in llms])
        for o in objs:
            if o.llm_name + "@" + o.llm_factory in llm_set:
                continue
            llms.append({"llm_name": o.llm_name, "mdl_type": o.mdl_type, "fid": o.llm_factory, "available": True})

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
def chat_service(request: LLMServiceRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    **功能描述**:
    此接口用于调用对话模型，基于用户提供的输入生成对应的响应内容。支持文本生成、图像到文本转换、消息处理等多种模型类型。接口根据请求体中的配置，选择适当的模型及生成方式，提供流式和非流式响应模式。

    ### 请求体 (Request Body):
    - **model_dump (dict)**: 包含以下字段：
        - `prompt` (str, 可选): 用户提供的提示内容，用于引导对话模型生成响应。
        - `messages` (list[dict]): 对话消息列表，包含用户与模型之间的对话历史。
        - `llm_name` (str): 模型名称，用于指定所调用的语言模型。
        - `stream` (bool): 指定是否使用流式响应。
        - `gen_conf` (dict, 可选): 生成配置，控制对话生成行为。
        - `image` (str, 可选): Base64编码的图像数据，适用于图像到文本的转换模型。

    ### 响应 (Response):
    - **成功响应 (200)**:
        - `data` (dict): 返回包含模型生成的响应内容，格式可能包括文本、结构化数据或基于图像的文本输出，具体取决于模型类型和请求内容。

    ### 错误响应:
    - **404: Tenant not found**:
        - 当根据用户ID查找租户信息失败时，返回此错误，表示该用户无对应的租户记录。
    - **404: Model not found**:
        - 当指定的模型名称在用户租户可用模型列表中未找到时，返回此错误。

    ### 主要流程:
    1. 从请求中提取用户输入的内容、模型名称和配置信息。
    2. 通过用户信息获取租户信息，确保用户的租户身份；如果未找到租户信息，返回404错误。
    3. 获取用户租户关联的模型列表，确定模型类型 (`llm_type`)。
    4. 根据 `llm_type` 判断是否需要传入 `image` 参数，构建生成请求。
    5. 根据 `stream` 参数选择流式或非流式的生成方法，调用模型获取对话响应内容。
    6. 返回生成的对话结果。

    ### 注意事项:
    - **模型选择**:
        - 仅当 `llm_type` 为 `image2text` 时传递 `image` 参数，以确保在需要图像到文本转换时能处理Base64编码的图像数据。
        - 支持多种模型类型 (如文本生成、图像到文本、消息对话)，请根据需求选择适当的 `llm_name` 和 `llm_type`。
    - **流式调用**:
        - 若 `stream` 参数为 `True`，将返回流式响应，用于实时数据生成；若为 `False`，返回完整的响应数据。
    - **数据格式**:
        - 返回数据格式可能因模型及请求内容不同而有所变化；默认返回JSON格式的结构化数据或文本响应。
    """
    req = request.model_dump()
    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")

    my_llms = TenantLLMService.get_my_llms(db, tenants[0]["tenant_id"])

    def get_llm_type(model_name, my_llms):
        for row in my_llms:
            if row[4] == model_name:  # 这里的第5个元素是 model_name
                return row[-3]  # 倒数第三个元素是 llm_type
        return None  # 如果找不到，返回 None

    llm_type = get_llm_type(req["llm_name"], my_llms)
    if llm_type:
        logging.debug(f"The llm_type for model {req['llm_name']} is: {llm_type}")
    else:
        raise HTTPException(status_code=404, detail=f"Model {req['llm_name']} not found in the list.")

    chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], llm_type, req["llm_name"])
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
        data = chat_mdl.chat_streamly(**call_params)
    else:
        data = chat_mdl.chat(**call_params)

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

    # 将同步操作封装在函数中
    def process_non_streaming_request():
        # 获取租户信息
        tenants = TenantService.get_info_by(db, user.id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        my_llms = TenantLLMService.get_my_llms(db, tenants[0]["tenant_id"])

        def get_llm_type(model_name, my_llms):
            for row in my_llms:
                if row[4] == model_name:
                    return row[-3]
            return None

        llm_type = get_llm_type(req["llm_name"], my_llms)
        if llm_type:
            logging.debug(f"The llm_type for model {req['llm_name']} is: {llm_type}")
        else:
            raise HTTPException(status_code=404, detail=f"Model {req['llm_name']} not found in the list.")

        chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], llm_type, req["llm_name"])
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
        data = chat_mdl.chat(**call_params)
        return data

    # 获取初始设置的同步函数
    def get_initial_setup():
        tenants = TenantService.get_info_by(db, user.id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        my_llms = TenantLLMService.get_my_llms(db, tenants[0]["tenant_id"])

        def get_llm_type(model_name, my_llms):
            for row in my_llms:
                if row[4] == model_name:
                    return row[-3]
            return None

        llm_type = get_llm_type(req["llm_name"], my_llms)
        if not llm_type:
            raise HTTPException(status_code=404, detail=f"Model {req['llm_name']} not found in the list.")

        chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], llm_type, req["llm_name"])

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
            llm_model_config = TenantLLMService.get_model_config(db, tenants[0]["tenant_id"], LLMType.IMAGE2TEXT,
                                                                 req["llm_name"])
            call_params["images"] = req["image"]
        else:
            llm_model_config = TenantLLMService.get_model_config(db, tenants[0]["tenant_id"], LLMType.CHAT,
                                                                 req["llm_name"])

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
            # 在线程池中获取初始设置
            loop = asyncio.get_running_loop()
            chat_mdl, call_params = await loop.run_in_executor(executor, get_initial_setup)

            # 在线程池中执行流式生成并迭代结果
            def generate_stream():
                try:
                    # 调用模型的流式生成方法
                    result_generator = chat_mdl.chat_streamly(**call_params)
                    # 返回生成器对象
                    return result_generator
                except Exception as e:
                    # 捕获异常并返回
                    raise e

            # 获取生成器
            generator = await loop.run_in_executor(executor, generate_stream)

            # 保持与原代码相同的累加逻辑
            last_ans = ""  # 初始化累加变量

            # 使用线程池处理每次迭代，但保持原始累加逻辑
            def get_next_chunk(generator):
                try:
                    return next(generator), False  # 返回下一个结果和未完成标志
                except StopIteration:
                    return None, True  # 返回None和完成标志
                except Exception as e:
                    raise e

            is_complete = False
            while not is_complete:
                # 在线程池中获取下一个响应
                chunk, is_complete = await loop.run_in_executor(executor, get_next_chunk, generator)

                if is_complete:
                    # 流结束
                    end_message = json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False)
                    yield f"data: {end_message}\n\n"
                    break

                # 计算增量，与原代码保持一致
                delta_ans = chunk[len(last_ans):]  # 计算增量
                if not delta_ans:  # 如果没有新内容，跳过
                    continue

                last_ans = chunk  # 更新累加内容
                sse_data = json.dumps({"retcode": 0, "retmsg": "", "data": last_ans}, ensure_ascii=False)
                yield f"data: {sse_data}\n\n"  # 注意这里返回的是累加的内容，与原代码一致

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
        # 在线程池中运行同步代码
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(executor, process_non_streaming_request)
        return get_json_result(data=data)


@router.post('/fine_prompt', summary="优化提示词", response_description="返回优化后的提示词")
def fine_prompt(request: FinePromptRequest, db: Session = Depends(get_db), user=Depends(manager)):
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
    chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, req["llm_name"])

    data = chat_mdl.chat(META_PROMPT, [{"role": "user", "content": "Task, Goal, or Current Prompt:\n" + req["prompt"]}],
                         req["gen_conf"])

    return get_json_result(data=data)


@router.post('/generate_suggestions', summary="生成用户输入建议", response_description="成功调用大模型生成建议")
def generate_suggestions(request: SuggestionRequest, db: Session = Depends(get_db), user=Depends(manager)):
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

    chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, req["llm_name"])

    # 调用大模型
    response = chat_mdl.chat(
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
def recognize_intent(
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
            if r[4] == model_name:
                return r[-3]  # 倒数第三个元素是 llm_type
        return None

    llm_type = get_llm_type(req["llm_name"], my_llms)
    if llm_type != LLMType.CHAT.value:
        raise HTTPException(status_code=400, detail="指定模型不是对话模型，或未找到")

    chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], llm_type, req["llm_name"])

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
    answer = chat_mdl.chat(
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
def fill_fields(
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
    llm_type = next((r[-3] for r in my_llms if r[4] == req.llm_name), None)
    if llm_type != LLMType.CHAT.value:
        raise HTTPException(status_code=400, detail="指定模型不是对话模型，或未找到")
    chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], llm_type, req.llm_name)

    def call_llm(prompt: str) -> str:
        return chat_mdl.chat(
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
    raw1 = call_llm(prompt1)
    res1 = parse_and_validate(raw1, req.fields)

    # —— 阶段二：内部追问（仅一次）
    if res1["missing"] and req.retry:
        # 构造追问 Prompt
        missed = ",".join(res1["missing"])
        prompt2 = (
            f"请补全字段：{missed}，按之前格式再输出完整 JSON，不要多余文字。"
        )
        raw2 = call_llm(prompt2)
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
            if row[4] == request.llm_name:
                llm_type = row[-3]
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
            # 非流式响应
            result_content = ""
            try:
                for delta in chat_agent.chat_with_tools_stream(
                        query=request.messages[-1]["content"] if request.messages else "",
                        messages=request.messages[:-1] if request.messages else [],
                        knowledge_context=knowledge_context,
                        files=request.files
                ):
                    result_content += delta

                return {"retcode": 0, "retmsg": "success", "data": {"answer": result_content}}
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Chat error: {str(e)}")

        # 流式响应
        async def sse_stream():
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[str | None] = asyncio.Queue()
            stop_event = threading.Event()

            def safe_put(value: str | None) -> None:
                if stop_event.is_set():
                    return
                future = asyncio.run_coroutine_threadsafe(queue.put(value), loop)
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 - 捕获线程间通信异常
                    stop_event.set()
                    logging.debug(f"Failed to enqueue SSE payload: {exc}")

            def stream_structured() -> None:
                try:
                    stream_generator = chat_agent.chat_with_tools_stream_structured(
                        query=request.messages[-1]["content"] if request.messages else "",
                        messages=request.messages[:-1] if request.messages else [],
                        knowledge_context=knowledge_context,
                        files=request.files
                    )

                    for message in stream_generator:
                        if stop_event.is_set():
                            break
                        wrapped_message = {
                            "retcode": 0,
                            "retmsg": "",
                            "data": message
                        }
                        safe_put(f"data: {json.dumps(wrapped_message, ensure_ascii=False)}\n\n")

                    if not stop_event.is_set():
                        end_data = {
                            "retcode": 0,
                            "retmsg": "Stream completed",
                            "data": True
                        }
                        safe_put(f"data: {json.dumps(end_data, ensure_ascii=False)}\n\n")
                except Exception as e:
                    logging.exception(f"Stream error (structured): {e}")
                    error_data = {
                        "retcode": 500,
                        "retmsg": str(e),
                        "data": {"answer": f"**ERROR**: {str(e)}"}
                    }
                    safe_put(f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n")
                    end_data = {
                        "retcode": 0,
                        "retmsg": "",
                        "data": True
                    }
                    safe_put(f"data: {json.dumps(end_data, ensure_ascii=False)}\n\n")
                finally:
                    safe_put(None)

            def stream_unstructured() -> None:
                accumulated_content = ""
                try:
                    start_data = {
                        "retcode": 0,
                        "retmsg": "Chat started",
                        "data": ""
                    }
                    safe_put(f"data: {json.dumps(start_data, ensure_ascii=False)}\n\n")

                    stream_generator = chat_agent.chat_with_tools_stream(
                        query=request.messages[-1]["content"] if request.messages else "",
                        messages=request.messages[:-1] if request.messages else [],
                        knowledge_context=knowledge_context,
                        files=request.files
                    )

                    for delta in stream_generator:
                        if stop_event.is_set():
                            break
                        if delta:
                            accumulated_content += delta
                            response_data = {
                                "retcode": 0,
                                "retmsg": "",
                                "data": accumulated_content
                            }
                            safe_put(f"data: {json.dumps(response_data, ensure_ascii=False)}\n\n")

                    if (
                        not stop_event.is_set()
                        and request.verbose_tool_use
                        and chat_agent.agent.tools
                    ):
                        use_tools = getattr(chat_agent.agent, '_last_use_tools', [])
                        if use_tools:
                            tools_summary = f"\n\n📊 **本次对话使用了 {len(use_tools)} 个工具调用**"
                            accumulated_content += tools_summary
                            final_data = {
                                "retcode": 0,
                                "retmsg": "",
                                "data": accumulated_content
                            }
                            safe_put(f"data: {json.dumps(final_data, ensure_ascii=False)}\n\n")

                    if not stop_event.is_set():
                        end_data = {
                            "retcode": 0,
                            "retmsg": "Stream completed",
                            "data": True
                        }
                        safe_put(f"data: {json.dumps(end_data, ensure_ascii=False)}\n\n")
                except Exception as e:
                    logging.exception(f"Stream error: {e}")
                    error_data = {
                        "retcode": 500,
                        "retmsg": str(e),
                        "data": {"answer": f"**ERROR**: {str(e)}"}
                    }
                    safe_put(f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n")
                    end_data = {
                        "retcode": 0,
                        "retmsg": "",
                        "data": True
                    }
                    safe_put(f"data: {json.dumps(end_data, ensure_ascii=False)}\n\n")
                finally:
                    safe_put(None)

            producer = loop.run_in_executor(
                executor,
                stream_structured if request.structured_output else stream_unstructured
            )

            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield item
            except asyncio.CancelledError:
                stop_event.set()
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
                raise
            finally:
                stop_event.set()
                if not producer.done():
                    producer.cancel()
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

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
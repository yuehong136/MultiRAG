# coding=utf-8
"""
@project: multirag
@Author：龙
@file： conversation_app.py
@date：2024/7/16 18:00
@desc: 会话管理接口
"""
import json
import os
import re
import logging
from copy import deepcopy
# import trio
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, model_validator, Discriminator, field_validator
from typing import Generator, Literal, Annotated, Any

from api.db.db_models import APIToken, get_db
from api.db.services.conversation_service import ConversationService, structure_answer
from api.db.services.dialog_service import DialogService, async_chat, async_ask, gen_mindmap
# from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.db.services.search_service import SearchService
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import TenantService, UserTenantService
from common.constants import LLMType
from common import settings
from api.utils.api_utils import server_error_response, get_data_error_result
from common.misc_utils import get_uuid
from common.constants import RetCode
from api.utils.api_utils import get_json_result
from api.apps import manager
# from graphrag.general.mind_map_extractor import MindMapExtractor
# from core.app.tag import label_question
from core.prompts.template import load_prompt
from core.prompts.generator import chunks_format


class SetConversationRequest(BaseModel):
    conversation_id: str | None = None
    """会话的唯一标识符，如果为空则表示创建新会话。"""

    dialog_id: str | None = None
    """对话的唯一标识符。"""

    name: str | None = "New conversation"
    """会话的名称。"""

    is_new: bool | None = None
    """是否为新建会话，可选参数。如果未提供，将根据conversation_id是否存在来判断。"""

    @field_validator('conversation_id', 'dialog_id', mode='before')
    def blank_str_to_none(cls, v):
        """把 '' 或 '   ' 这种空白字符串统一转成 None"""
        if isinstance(v, str) and v.strip() == '':
            return None
        return v

    @model_validator(mode='after')
    def validate_conversation_logic(self) -> 'SetConversationRequest':
        """验证会话创建/更新逻辑"""
        # 如果没有指定is_new，根据conversation_id来判断
        if self.is_new is None:
            self.is_new = self.conversation_id is None

        # 如果是新建会话，必须提供dialog_id
        if self.is_new and not self.dialog_id:
            raise ValueError("dialog_id is required when creating a new conversation")

        # 如果是更新会话，必须提供conversation_id
        if not self.is_new and not self.conversation_id:
            raise ValueError("conversation_id is required when updating an existing conversation")

        # 限制名称长度
        if self.name and len(self.name) > 255:
            self.name = self.name[:255]

        return self


class CompletionRequest(BaseModel):
    conversation_id: str
    """会话的唯一标识符。"""

    messages: list[dict]
    """消息列表，每个消息包含角色和内容。"""

    stream: bool | None = True
    """是否使用流式响应，默认值为 True。"""

    filter_condition: str | None = ""
    """过滤条件，可以根据实际需求自定义结构。"""

    llm_id: str | None = None
    """大语言模型的ID，如果不指定则使用默认模型。"""

    temperature: float | None = None
    """温度参数，控制生成文本的随机性。"""

    top_p: float | None = None
    """Top-p采样参数，控制生成文本的多样性。"""

    frequency_penalty: float | None = None
    """频率惩罚参数，降低重复内容的可能性。"""

    presence_penalty: float | None = None
    """存在惩罚参数，鼓励生成新内容。"""

    max_tokens: int | None = None
    """最大生成token数。"""


class RemoveConversationRequest(BaseModel):
    conversation_ids: list[str]
    """要删除的会话ID列表。"""


class DeleteMsgRequest(BaseModel):
    conversation_id: str
    """会话的唯一标识符。"""

    message_id: str
    """消息ID"""


class ThumbupRequest(BaseModel):
    conversation_id: str
    """会话的唯一标识符。"""

    message_id: str
    """消息ID"""

    thumbup: bool | None = None
    """点赞状态"""

    feedback: str
    """反馈"""


class TTSRequest(BaseModel):
    text: str
    """文本内容"""

    llm_name: str | None = None
    """TTS模型名称，可选参数，如果提供则优先使用此模型，否则使用租户默认TTS模型"""


class ASRRequest(BaseModel):
    audio_file_path: str
    """MP3音频文件的地址"""

    llm_name: str | None = None
    """ASR模型名称，可选参数，如果提供则优先使用此模型，否则使用租户默认ASR模型"""


class AskAboutRequest(BaseModel):
    question: str
    """用户提出的问题"""

    kb_ids: list[str]
    """知识库ID列表"""

class SparseSearchMode(BaseModel):
    type: Literal["sparse"] = "sparse"


class DenseSearchMode(BaseModel):
    type: Literal["dense"] = "dense"


class HybridSearchMode(BaseModel):
    type: Literal["hybrid"] = "hybrid"
    weight_dense: float = Field(default=0.7, ge=0.0, le=1.0)
    weight_sparse: float = Field(default=0.3, ge=0.0, le=1.0)

    @model_validator(mode='after')
    def validate_weights(self) -> 'HybridSearchMode':
        """确保权重和为1，如果不是则自动调整"""
        total = self.weight_dense + self.weight_sparse
        if abs(total - 1.0) > 0.001:
            # 自动归一化权重
            self.weight_dense = self.weight_dense / total
            self.weight_sparse = self.weight_sparse / total
        return self


class FusionSearchMode(BaseModel):
    type: Literal["fusion"] = "fusion"
    weights: str = Field(default="0.05,0.95")

    @model_validator(mode='after')
    def validate_weights_format(self) -> 'FusionSearchMode':
        """验证weights格式"""
        try:
            parts = self.weights.split(',')
            if len(parts) != 2:
                raise ValueError("weights must contain exactly two comma-separated values")
            float(parts[0].strip())
            float(parts[1].strip())
        except (ValueError, AttributeError):
            raise ValueError("weights must be in format 'float,float' (e.g., '0.05,0.95')")
        return self


# 使用 Discriminator 的高效版本
SearchModeType = Annotated[
    SparseSearchMode | DenseSearchMode | HybridSearchMode | FusionSearchMode,
    Discriminator('type')
]

class MindmapRequest(BaseModel):
    question: str
    """用户提出的问题"""

    kb_ids: list[str]
    """知识库ID列表"""

    search_mode: SearchModeType | None = None
    """检索模式"""

    def get_search_mode_dict(self) -> dict[str, Any] | None:
        """将搜索模式转换为字典格式供底层函数使用"""
        if self.search_mode is None:
            return None

        mode_data = self.search_mode.model_dump()
        mode_type = mode_data.pop('type')
        return {mode_type: mode_data}

class RelatedQuestionsRequest(BaseModel):
    question: str
    """用户提出的关键词"""


router = APIRouter()


@router.post('/set', summary="设置会话", response_description="成功设置会话")
def set_conversation(request: SetConversationRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    设置会话

    该接口用于创建或更新会话信息。

    参数:
    - request: SetConversationRequest对象，包含会话的配置信息
        - conversation_id: 会话的唯一标识符，如果为空则表示创建新会话
        - dialog_id: 对话的唯一标识符
        - name: 会话的名称
        - is_new: 是否为新建会话

    返回:
    - 成功时返回包含会话信息的JSON结果
    - 失败时返回错误信息
    """
    req = request.model_dump()
    conv_id = req.get("conversation_id")
    is_new = req.get("is_new")
    name = req.get("name", "New conversation")

    if len(name) > 255:
        name = name[0:255]

    # 添加用户ID到请求数据中
    req["user_id"] = user.id

    if not is_new:
        # 更新现有会话
        del req["conversation_id"]
        del req["is_new"]
        try:
            if not ConversationService.update_by_id(db, conv_id, req):
                return get_data_error_result(retmsg="Conversation not found!")
            conv = ConversationService.get_by_id(db, conv_id)
            if not conv:
                return get_data_error_result(retmsg="Fail to update a conversation!")
            conv = conv.to_dict()
            return get_json_result(data=conv)
        except Exception as e:
            return server_error_response(e)

    # 创建新会话
    try:
        dia = DialogService.get_by_id(db, req["dialog_id"])
        if not dia:
            return get_data_error_result(retmsg="Dialog not found")
        
        conv = {
            "id": conv_id or get_uuid(),
            "dialog_id": req["dialog_id"],
            "name": name,
            "message": [{"role": "assistant", "content": dia.prompt_config["prologue"]}],
            "user_id": user.id,
            "reference": [],
        }
        ConversationService.save(db, **conv)
        
        # 重新获取保存后的完整数据，确保向后兼容
        saved_conv = ConversationService.get_by_id(db, conv["id"])
        if not saved_conv:
            return get_data_error_result(retmsg="Fail to create a conversation!")
        
        return get_json_result(data=saved_conv.to_dict())
    except Exception as e:
        return server_error_response(e)


@router.get('/get', summary="获取会话", response_description="成功获取会话")
def get(conversation_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取会话

    该接口用于获取指定会话的信息。

    参数:
    - conversation_id: str 会话的唯一标识符

    返回:
    - 成功时返回包含会话信息的JSON结果
    - 失败时返回错误信息
    """
    try:

        conv = ConversationService.get_by_id(db, conversation_id)
        if not conv:
            return get_data_error_result(retmsg="Conversation not found!")
        tenants = UserTenantService.query(db, user_id=user.id)
        for tenant in tenants:
            dialog = DialogService.query(db, tenant_id=tenant.tenant_id, id=conv.dialog_id)
            if dialog and len(dialog) > 0:
                avatar = dialog[0].icon
                break
        else:
            return get_json_result(data=False, retmsg=f'Only owner of conversation authorized for this operation.', retcode=RetCode.OPERATING_ERROR)

        for ref in conv.reference:
            if isinstance(ref, list):
                continue
            ref["chunks"] = chunks_format(ref)

        conv = conv.to_dict()
        conv["avatar"] = avatar
        return get_json_result(data=conv)
    except Exception as e:
        return server_error_response(e)


@router.get('/getsse/{dialog_id}', summary="获取对话信息（支持SSE）", response_description="成功获取对话信息")
def getsse(dialog_id: str, db: Session = Depends(get_db), request: Request = None):
    """
    获取对话信息（支持SSE）

    该接口用于根据对话ID获取对话信息，并校验Authorization Token。

    参数:
    - dialog_id: str 对话的唯一标识符
    - request: Request 请求对象，用于获取Authorization头部信息

    返回:
    - 成功时返回包含对话信息的JSON结果
    - 失败时返回错误信息
    """
    token_header = request.headers.get('Authorization')
    if not token_header:
        return get_data_error_result(retmsg="Authorization header is missing!")

    token_parts = token_header.split()
    if len(token_parts) != 2 or token_parts[0].lower() != "bearer":
        return get_data_error_result(retmsg="Authorization is not valid!")

    token = token_parts[1]
    objs = APIToken.query(beta=token)
    if not objs:
        return get_data_error_result(retmsg="Token is not valid!")

    try:
        dialog = DialogService.get_by_id(db, dialog_id)
        if not dialog:
            return get_data_error_result(retmsg="Dialog not found!")

        dialog_dict = dialog.to_dict()
        dialog_dict["avatar"] = dialog_dict.pop("icon", None)

        return get_json_result(data=dialog_dict)
    except Exception as e:
        return server_error_response(e)

@router.post('/rm', summary="删除会话", response_description="成功删除会话")
def rm(request: RemoveConversationRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    删除会话

    该接口用于删除指定的会话。

    参数:
    - request: RemoveConversationRequest对象，包含要删除的会话ID列表
        - conversation_ids: List[str] 要删除的会话ID列表

    返回:
    - 成功时返回成功删除的JSON结果
    - 失败时返回错误信息
    """
    try:
        for cid in request.conversation_ids:
            conv = ConversationService.get_by_id(db, cid)
            if not conv:
                return get_data_error_result(retmsg="Conversation not found!")
            tenants = UserTenantService.query(db, user_id=user.id)
            for tenant in tenants:
                if DialogService.query(db, tenant_id=tenant.tenant_id, id=conv.dialog_id):
                    break
            else:
                return get_json_result(
                    data=False, retmsg=f'Only owner of conversation authorized for this operation.',
                    retcode=RetCode.OPERATING_ERROR)
            ConversationService.delete_by_id(db, cid)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.get('/list', summary="列出会话", response_description="成功列出会话")
def list_conversation(dialog_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    列出会话

    该接口用于列出指定对话的所有会话。

    参数:
    - dialog_id: str 对话所说应用id

    返回:
    - 成功时返回包含会话列表的JSON结果
    - 失败时返回错误信息
    """
    try:
        if not DialogService.query(db, tenant_id=user.id, id=dialog_id):
            return get_json_result(
                data=False, retmsg=f'Only owner of dialog authorized for this operation.',
                retcode=RetCode.OPERATING_ERROR)
        convs = ConversationService.query(
            db,
            dialog_id=dialog_id,
            order_by="create_time",
            reverse=True)
        convs = [d.to_dict() for d in convs]
        return get_json_result(data=convs)
    except Exception as e:
        return server_error_response(e)


@router.post('/completion', summary="生成对话", response_description="成功生成对话")
async def completion(request: CompletionRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    # 生成对话响应

    该接口用于在指定会话中生成 AI 对话回复，支持流式和非流式输出，可自定义 LLM 模型及其参数。

    ## 请求参数

    ### 基础参数
    - **conversation_id** `string` *required*
      - 会话的唯一标识符
      - 用于关联历史对话记录

    - **messages** `array` *required*
      - 消息列表，每个消息包含以下字段：
        - `role`: 消息角色，可选值：`user`, `assistant`, `system`
        - `content`: 消息内容
        - `id`: 消息唯一标识符（可选）
      - 示例：
        ```json
        [
          {"role": "user", "content": "你好"},
          {"role": "assistant", "content": "您好，有什么可以帮您的？"}
        ]
        ```

    ### 输出配置
    - **stream** `boolean` *optional*
      - 是否使用流式响应（Server-Sent Events）
      - 默认值：`true`
      - `true`: 实时流式返回，适合长文本生成
      - `false`: 一次性返回完整结果

    - **filter_condition** `string` *optional*
      - 自定义过滤条件
      - 默认值：`""`

    ### 模型配置
    - **llm_id** `string` *optional*
      - 指定使用的大语言模型 ID
      - 不指定则使用对话配置的默认模型
      - 示例：`"gpt-4"`, `"claude-3-sonnet"`

    - **temperature** `float` *optional*
      - 温度参数，控制生成文本的随机性
      - 取值范围：`0.0 ~ 2.0`
      - 较低值（如 0.2）：更确定、保守的输出
      - 较高值（如 0.8）：更有创造性、多样化的输出

    - **top_p** `float` *optional*
      - 核采样参数（nucleus sampling）
      - 取值范围：`0.0 ~ 1.0`
      - 控制生成文本的多样性
      - 建议与 temperature 二选一使用

    - **frequency_penalty** `float` *optional*
      - 频率惩罚系数
      - 取值范围：`-2.0 ~ 2.0`
      - 正值：降低重复词汇出现的频率
      - 负值：增加重复词汇出现的频率

    - **presence_penalty** `float` *optional*
      - 存在惩罚系数
      - 取值范围：`-2.0 ~ 2.0`
      - 正值：鼓励模型探讨新主题
      - 负值：鼓励模型深入当前主题

    - **max_tokens** `integer` *optional*
      - 生成文本的最大 token 数量
      - 限制回复的最大长度

    ## 响应格式

    ### 流式响应 (stream=true)
    返回 `text/event-stream` 格式的 SSE 流：
    ```
    data: {"retcode": 0, "retmsg": "", "data": {"answer": "部分回答...", "reference": [...]}}

    data: {"retcode": 0, "retmsg": "", "data": {"answer": "继续回答...", "reference": [...]}}

    data: {"retcode": 0, "retmsg": "", "data": true}
    ```

    ### 非流式响应 (stream=false)
    ```json
    {
      "retcode": 0,
      "retmsg": "",
      "data": {
        "answer": "完整的回答内容",
        "reference": [
          {
            "chunks": [...],
            "doc_aggs": [...]
          }
        ],
        "id": "message_id"
      }
    }
    ```

    ## 使用示例

    ### 基础调用
    ```json
    {
      "conversation_id": "conv_123456",
      "messages": [
        {"role": "user", "content": "介绍一下人工智能"}
      ],
      "stream": true
    }
    ```

    ### 自定义模型参数
    ```json
    {
      "conversation_id": "conv_123456",
      "messages": [
        {"role": "user", "content": "写一首关于春天的诗"}
      ],
      "llm_id": "gpt-4",
      "temperature": 0.8,
      "max_tokens": 500,
      "presence_penalty": 0.6,
      "stream": false
    }
    ```

    ## 错误码

    | 错误码 | 说明 |
    |-------|------|
    | 0 | 成功 |
    | 400 | 参数错误（缺少必需参数或参数格式不正确） |
    | 404 | 会话或对话不存在 |
    | 500 | 服务器内部错误 |

    ## 注意事项

    1. **消息处理逻辑**
       - 系统消息（`role: system`）会被自动过滤
       - 连续的助手消息会被合并处理

    2. **模型切换**
       - 使用 `llm_id` 参数可以临时切换模型
       - 需确保指定的模型已在租户配置中设置 API Key

    3. **流式响应**
       - 推荐用于长文本生成场景
       - 客户端需要支持 SSE (Server-Sent Events)

    4. **性能优化**
       - 合理设置 `max_tokens` 避免过长响应
       - 根据场景调整 `temperature` 平衡质量和多样性
    """
    req = request.model_dump()
    if not req.get("conversation_id") or not req.get("messages"):
        return get_data_error_result(retmsg="Missing conversation_id or messages!")
    is_stream = req.get("stream", True)
    stream = is_stream
    msg = []
    for m in req["messages"]:
        if m["role"] == "system":
            continue
        if m["role"] == "assistant" and not msg:
            continue
        msg.append(m)
    if not msg[-1].get("id"):
        msg[-1]["id"] = get_uuid()
    message_id = msg[-1].get("id")

    if not msg:
        return get_data_error_result(retmsg="No valid messages found!")

    chat_model_id = req.get("llm_id", "")
    req.pop("llm_id", None)

    chat_model_config = {}
    for model_config in [
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "max_tokens",
    ]:
        config = req.get(model_config)
        if config:
            chat_model_config[model_config] = config

    try:
        conv = ConversationService.get_by_id(db, req["conversation_id"])
        if not conv:
            return get_data_error_result(retmsg="Conversation not found!")
        if len(req["messages"]) != 1:
            conv.message = deepcopy(req["messages"])  # re-generate for conversation
        else:
            conv.message.append(msg[0])
        dia = DialogService.get_by_id(db, conv.dialog_id)
        if not dia:
            return get_data_error_result(retmsg="Dialog not found!")
        del req["conversation_id"]
        del req["messages"]

        if not conv.reference:
            conv.reference = []
        conv.reference = [r for r in conv.reference if r]
        conv.reference.append({"chunks": [], "doc_aggs": []})

        if chat_model_id:
            if not TenantLLMService.get_api_key(db, tenant_id=dia.tenant_id, model_name=chat_model_id):
                req.pop("chat_model_id", None)
                req.pop("chat_model_config", None)
                return get_data_error_result(retmsg=f"Cannot use specified model {chat_model_id}.")
            dia.llm_id = chat_model_id
            dia.llm_setting = chat_model_config

        is_embedded = bool(chat_model_id)

        async def stream_response():
            nonlocal dia, msg, db, req, conv
            try:
                async for ans in async_chat(dia, msg, db, **req):
                    ans = structure_answer(conv, ans, message_id, conv.id)
                    yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": ans}, ensure_ascii=False) + "\n\n"
                ConversationService.update_by_id(db, conv.id, conv.to_dict())
            except Exception as e:
                logging.exception(e)
                yield "data:" + json.dumps({"retcode": 500, "retmsg": str(e),
                                            "data": {"answer": "**ERROR**: " + str(e), "reference": []}},
                                           ensure_ascii=False) + "\n\n"
            yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False) + "\n\n"

        if stream:
            return StreamingResponse(stream_response(), media_type="text/event-stream")
        else:
            answer = None
            conv = deepcopy(conv)  # 深拷贝 conv，否则会导致后续更新数据时，无法更新引用
            async for ans in async_chat(dia, msg, db, **req):
                answer = structure_answer(conv, ans, message_id, conv.id)
                if not is_embedded:
                    ConversationService.update_by_id(db, conv.id, conv.to_dict())
                break
            return get_json_result(data=answer)
    except Exception as e:
        return server_error_response(e)


@router.post('/tts', summary="文本转语音", response_description="成功文本转语音")
def tts(request: TTSRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    文本转语音

    该接口用于将输入的文本内容转换为语音，并以音频流的方式返回。

    参数:
    - request: TTSRequest对象，包含要转换的文本内容和可选的TTS模型名称
        - text: str 要转换为语音的文本内容
        - llm_name: Optional[str] TTS模型名称，如果提供则优先使用此模型

    返回:
    - 成功时返回包含音频流的响应
    - 失败时返回错误信息

    逻辑说明:
    - 首先，根据用户ID获取租户信息。如果租户信息不存在，返回404错误。
    - 优先使用请求中的llm_name参数作为TTS模型，如果没有提供则使用租户默认TTS模型ID。
    - 使用该模型将文本逐段转化为语音流，并逐段返回。
    - 若在转换过程中出现错误，则返回错误信息。

    注意事项:
    - 文本内容不应为空。
    - 如果没有提供llm_name且租户未设置默认TTS模型，将返回错误。
    - 返回结果为MPEG音频流，可供下载或直接播放。
    """
    req = request.model_dump()
    text = req.get("text")
    llm_name = req.get("llm_name")

    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")

    # 优先使用传递的llm_name，如果没有则使用租户默认的tts_id
    if llm_name:
        tts_model_name = llm_name
    else:
        tts_model_name = tenants[0].get("tts_id")
        if not tts_model_name:
            raise HTTPException(status_code=400, detail="No default TTS model is set and no llm_name provided")

    tts_mdl = LLMBundle(db, tenants[0]["tenant_id"], LLMType.TTS, tts_model_name)

    def stream_audio() -> Generator[bytes, None, None]:
        try:
            # Split the text and filter out empty strings
            for txt in filter(None, re.split(r"[，。/《》？；：！\n\r:;]+", text)):
                # Proceed only if txt is not empty after stripping whitespace
                if txt.strip():
                    # Add logging to see the text segments being processed
                    # print(f"Processing text segment: {txt}")
                    for chunk in tts_mdl.tts(txt):
                        # Add logging to check the size of each chunk
                        # print(f"Yielding chunk of size: {len(chunk)} bytes")
                        yield chunk
        except Exception as e:
            error_message = json.dumps({
                "retcode": 500,
                "retmsg": str(e),
                "data": {"answer": "**ERROR**: " + str(e)}
            }, ensure_ascii=False).encode('utf-8')
            yield error_message

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "Content-Type": "audio/mpeg",
        "Content-Disposition": 'attachment; filename="tts_output.mp3"'
    }
    return StreamingResponse(stream_audio(), media_type="audio/mpeg", headers=headers)


@router.post('/sequence2txt', summary="语音转文字（流式）", response_description="成功识别语音")
def sequence2txt(
    file: UploadFile = File(...),
    stream: str = "false",
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    语音转文字（支持流式）

    该接口用于上传音频文件并将其转换为文本，支持流式和非流式输出。

    参数:
    - file: UploadFile 上传的音频文件
    - stream: str 是否使用流式响应，"true" 或 "false"，默认 "false"

    返回:
    - 非流式：返回包含识别文本的JSON结果
    - 流式：返回 SSE 流，逐步返回识别结果

    支持的音频格式:
    - wav, mp3, m4a, aac, flac, ogg, webm, opus, wma
    """
    stream_mode = stream.lower() == "true"

    ALLOWED_EXTS = {
        ".wav", ".mp3", ".m4a", ".aac",
        ".flac", ".ogg", ".webm",
        ".opus", ".wma"
    }

    filename = file.filename or ""
    suffix = os.path.splitext(filename)[-1].lower()
    if suffix not in ALLOWED_EXTS:
        return get_data_error_result(
            retmsg=f"Unsupported audio format: {suffix}. Allowed: {', '.join(sorted(ALLOWED_EXTS))}"
        )

    # 保存上传的音频文件到临时路径
    import tempfile as tf
    fd, temp_audio_path = tf.mkstemp(suffix=suffix)
    os.close(fd)
    with open(temp_audio_path, "wb") as f:
        f.write(file.file.read())

    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        try:
            os.remove(temp_audio_path)
        except Exception:
            pass
        return get_data_error_result(retmsg="Tenant not found!")

    asr_id = tenants[0].get("asr_id")
    if not asr_id:
        try:
            os.remove(temp_audio_path)
        except Exception:
            pass
        return get_data_error_result(retmsg="No default ASR model is set")

    asr_mdl = LLMBundle(db, tenants[0]["tenant_id"], LLMType.SPEECH2TEXT, asr_id)

    if not stream_mode:
        text = asr_mdl.transcription(temp_audio_path)
        try:
            os.remove(temp_audio_path)
        except Exception as e:
            logging.error(f"Failed to remove temp audio file: {str(e)}")
        return get_json_result(data={"text": text})

    def event_stream():
        try:
            for evt in asr_mdl.stream_transcription(temp_audio_path):
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
        except Exception as e:
            err = {"event": "error", "text": str(e)}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        finally:
            try:
                os.remove(temp_audio_path)
            except Exception as e:
                logging.error(f"Failed to remove temp audio file: {str(e)}")

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post('/asr', summary="语音识别", response_description="成功识别语音")
def asr(request: ASRRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    语音识别

    该接口用于将输入的音频文件转换为文本。

    参数:
    - request: ASRRequest对象，包含音频文件的路径和可选的ASR模型名称
        - audio_file_path: str 音频文件的路径
        - llm_name: Optional[str] ASR模型名称，如果提供则优先使用此模型

    返回:
    - 成功时返回包含识别文本的JSON结果
    - 失败时返回错误信息

    逻辑说明:
    - 根据用户ID获取租户信息，确保租户存在。
    - 优先使用请求中的llm_name参数作为ASR模型，如果没有提供则使用租户默认ASR模型ID。
    - 使用该ASR模型处理音频文件，返回识别的文本。
    - 若在处理过程中出现错误，则返回错误信息。

    注意事项:
    - 音频文件路径应为有效路径。
    - 如果没有提供llm_name且租户未设置默认ASR模型，将返回错误。
    """
    req = request.model_dump()
    audio_file_path = req.get("audio_file_path")
    llm_name = req.get("llm_name")

    # 获取用户信息和语音识别模型的信息
    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")

    # 优先使用传递的llm_name，如果没有则使用租户默认的asr_id
    if llm_name:
        asr_model_name = llm_name
    else:
        asr_model_name = tenants[0].get("asr_id")
        if not asr_model_name:
            raise HTTPException(status_code=400, detail="No default ASR model is set and no llm_name provided")

    asr_mdl = LLMBundle(db, tenants[0]["tenant_id"], LLMType.SPEECH2TEXT, asr_model_name)

    # 调用 ASR 语音识别函数
    transcription_result = asr_mdl.transcription(audio=audio_file_path)

    if "**ERROR**" in transcription_result:
        raise HTTPException(status_code=500, detail=transcription_result)
    return get_json_result(data=transcription_result)


@router.post('/asr_upload', summary="语音识别上传", response_description="成功识别语音")
def asr_upload(file: UploadFile = File(...), llm_name: str | None = None, db: Session = Depends(get_db), user=Depends(manager)):
    """
    语音识别上传

    该接口用于上传音频文件并将其转换为文本。

    参数:
    - file: UploadFile 上传的音频文件
    - llm_name: Optional[str] ASR模型名称，如果提供则优先使用此模型（作为查询参数传递）

    返回:
    - 成功时返回包含识别文本的JSON结果
    - 失败时返回错误信息

    逻辑说明:
    - 将上传的音频文件保存到临时路径，并验证用户和模型信息。
    - 优先使用传递的llm_name参数作为ASR模型，如果没有提供则使用租户默认ASR模型ID。
    - 使用该ASR模型识别音频文件内容，并将识别结果返回。
    - 若在处理过程中出现错误，则返回错误信息。

    注意事项:
    - 确保上传文件格式为有效的音频格式（如mp3）。
    - 如果没有提供llm_name且租户未设置默认ASR模型，将返回错误。
    """
    # 将上传的 MP3 文件保存到临时文件
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_audio_file:
        temp_audio_file.write(file.file.read())
        audio_file_path = temp_audio_file.name

    # 获取用户信息和语音识别模型的信息
    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")

    # 优先使用传递的llm_name，如果没有则使用租户默认的asr_id
    if llm_name:
        asr_model_name = llm_name
    else:
        asr_model_name = tenants[0].get("asr_id")
        if not asr_model_name:
            raise HTTPException(status_code=400, detail="No default ASR model is set and no llm_name provided")

    asr_mdl = LLMBundle(db, tenants[0]["tenant_id"], LLMType.SPEECH2TEXT, asr_model_name)

    # 调用 ASR 语音识别函数
    transcription_result = asr_mdl.transcription(audio=audio_file_path)

    if "**ERROR**" in transcription_result:
        raise HTTPException(status_code=500, detail=transcription_result)

    return get_json_result(data=transcription_result)


@router.post('/delete_msg', summary="删除信息", response_description="成功删除信息")
def delete_msg(request: DeleteMsgRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    删除消息

    该接口用于删除指定会话中的某条消息及其关联的参考信息。

    参数:
    - request: DeleteMsgRequest对象，包含会话ID和消息ID
        - conversation_id: str 会话的唯一标识符
        - message_id: List[dict] 消息ID列表

    返回:
    - 成功时返回更新后的会话信息的JSON结果
    - 失败时返回错误信息

    逻辑说明:
    - 首先，根据会话ID查找会话。如果会话不存在，返回错误信息。
    - 如果找到会话，遍历会话中的消息列表，找到与给定消息ID匹配的消息。
    - 删除匹配的消息及其后续消息和对应的参考信息。
    - 更新会话信息，并将结果返回。
    """
    req = request.model_dump()
    conv = ConversationService.get_by_id(db, req["conversation_id"])
    if not conv:
        return get_data_error_result(retmsg="Conversation not found!")

    conv = conv.to_dict()
    for i, msg in enumerate(conv["message"]):
        # 如果当前消息ID与请求的消息ID不匹配，则继续检查下一个消息
        if req["message_id"] != msg.get("id", ""):
            continue
        # 确保不会超出范围
        if i + 1 < len(conv["message"]):
            assert conv["message"][i + 1]["id"] == req["message_id"]
        conv["message"].pop(i)
        if i < len(conv["message"]):
            conv["message"].pop(i)  # 因为前面 pop 了一次，后面的索引需要调整
        if i < len(conv["reference"]):
            conv["reference"].pop(i)  # 同样对 reference 做相应的 pop 操作
        break
        # assert conv["message"][i + 1]["id"] == req["message_id"]
        # conv["message"].pop(i)
        # conv["message"].pop(i)
        # conv["reference"].pop(max(0, i//2-1))
        # break
    ConversationService.update_by_id(db, conv["id"], conv)
    return get_json_result(data=conv)


@router.post('/thumbup', summary="点赞", response_description="成功点赞")
def thumbup(request: ThumbupRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    点赞接口

    该接口用于为特定会话中的消息点赞或取消点赞，并添加反馈信息。

    参数:
    - request: ThumbupRequest对象，包含点赞的详细信息
        - conversation_id: str 会话的唯一标识符
        - message_id: str 消息的唯一标识符
        - set: Optional[bool] 点赞状态（True表示点赞，False表示取消点赞）
        - feedback: str 反馈信息，当取消点赞时可以提供反馈内容

    返回:
    - 成功时返回更新后的会话信息的JSON结果
    - 失败时返回错误信息

    逻辑说明:
    - 根据会话ID查找指定会话，如果未找到则返回错误。
    - 在会话的消息列表中查找匹配的消息ID，并根据请求中的点赞状态设置相应的字段。
    - 若取消点赞且提供了反馈信息，则在消息中记录该反馈。
    - 更新会话信息并将结果返回。

    注意事项:
    - 仅支持为"assistant"角色的消息点赞。
    - 若请求中未设置点赞状态，默认为取消点赞。
    """
    req = request.model_dump()
    conv = ConversationService.get_by_id(db, req["conversation_id"])
    if not conv:
        return get_data_error_result(retmsg="Conversation not found!")
    up_down = req.get("thumbup")
    feedback = req.get("feedback", "")
    conv = conv.to_dict()
    for i, msg in enumerate(conv["message"]):
        if req["message_id"] == msg.get("id", "") and msg.get("role", "") == "assistant":
            if up_down:
                msg["thumbup"] = True
                if "feedback" in msg: del msg["feedback"]
            else:
                msg["thumbup"] = False
                if feedback:
                    msg["feedback"] = feedback
            break

    ConversationService.update_by_id(db, conv["id"], conv)
    return get_json_result(data=conv)


@router.post('/ask', summary="问答接口", response_description="返回答案")
def ask_about(request: AskAboutRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    问答接口

    该接口用于根据用户的问题从知识库中获取答案。

    参数:
    - question: 用户提出的问题
    - kb_ids: 知识库ID列表
    - user: 当前用户对象

    返回:
    - 实时流式返回答案数据
    
    注意: 虽然此函数定义为同步，但返回 StreamingResponse，FastAPI 会正确处理。
    """
    req = request.model_dump()
    uid = user.id

    search_id = req.get("search_id", "")
    search_app = None
    search_config = {}
    if search_id:
        search_app = SearchService.get_detail(db, search_id)
    if search_app:
        search_config = search_app.get("search_config", {})

    async def stream():
        try:
            async for ans in async_ask(db, req["question"], req["kb_ids"], uid, search_config=search_config):
                yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": ans}, ensure_ascii=False) + "\n\n"
        except Exception as e:
            yield "data:" + json.dumps({"retcode": 500, "retmsg": str(e), "data": {"answer": "**ERROR**: " + str(e), "reference": []}}, ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False) + "\n\n"

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no"
    }
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


# 定义 mindmap 接口
@router.post('/mindmap', summary="生成思维导图", response_description="返回思维导图")
def mindmap(request: MindmapRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    生成思维导图

    根据知识库内容生成思维导图。

    参数:
    - question: 用户提出的问题
    - kb_ids: 知识库ID列表
    - user: 当前用户对象

    返回:
    - 思维导图数据
    """
    req = request.model_dump()

    search_id = req.get("search_id", "")

    search_app = SearchService.get_detail(db, search_id) if search_id else {}
    search_config = search_app.get("search_config", {}) if search_app else {}
    kb_ids = search_config.get("kb_ids", [])
    kb_ids.extend(req["kb_ids"])
    kb_ids = list(set(kb_ids))

    mind_map = gen_mindmap(db, req["question"], kb_ids, search_app.get("tenant_id", user.id), search_config)

    # search_app = None
    # search_config = {}
    # if search_id:
    #     search_app = SearchService.get_detail(db, search_id)
    # if search_app:
    #     search_config = search_app.get("search_config", {})
    #
    # kb_ids = req["kb_ids"]
    # if search_config.get("kb_ids", []):
    #     kb_ids = search_config.get("kb_ids", [])
    # kb = KnowledgebaseService.get_by_id(db, kb_ids[0])
    # if not kb:
    #     return get_data_error_result(retmsg="Knowledgebase not found!")
    #
    # chat_id = ""
    # similarity_threshold = 0.3,
    # vector_similarity_weight = 0.3,
    # top = 1024,
    # doc_ids = []
    # rerank_id = ""
    # rerank_mdl = None
    #
    # if search_config:
    #     if search_config.get("chat_id", ""):
    #         chat_id = search_config.get("chat_id", "")
    #     if search_config.get("similarity_threshold", 0.2):
    #         similarity_threshold = search_config.get("similarity_threshold", 0.2)
    #     if search_config.get("vector_similarity_weight", 0.3):
    #         vector_similarity_weight = search_config.get("vector_similarity_weight", 0.3)
    #     if search_config.get("top_k", 1024):
    #         top = search_config.get("top_k", 1024)
    #     if search_config.get("doc_ids", []):
    #         doc_ids = search_config.get("doc_ids", [])
    #     if search_config.get("rerank_id", ""):
    #         rerank_id = search_config.get("rerank_id", "")
    #
    # tenant_id = kb.tenant_id
    # if search_app and search_app.get("tenant_id", ""):
    #     tenant_id = search_app.get("tenant_id", "")
    #
    # embd_mdl = LLMBundle(db, tenant_id, LLMType.EMBEDDING, llm_name=kb.embd_id)
    # chat_mdl = LLMBundle(db, tenant_id, LLMType.CHAT, llm_name=chat_id)
    # if rerank_id:
    #     rerank_mdl = LLMBundle(db, tenant_id, LLMType.RERANK, rerank_id)
    # filter_exp = ""  # todo 暂时不提供权限过滤的查询，如果需要这边需要完善
    # kb_names = list([kb.name])
    #
    # search_mode_dict = request.get_search_mode_dict()
    #
    # ranks = settings.retriever.retrieval(
    #     question=req["question"],
    #     filter_exp=filter_exp,
    #     embd_mdl=embd_mdl,
    #     tenant_id=tenant_id,
    #     kb_names=kb_names,
    #     page=1,
    #     page_size=12,
    #     similarity_threshold=similarity_threshold,
    #     vector_similarity_weight=vector_similarity_weight,
    #     top=top,
    #     doc_ids=doc_ids,
    #     aggs=False,
    #     rerank_mdl=rerank_mdl,
    #     rank_feature=label_question(db, req["question"], [kb]),
    #     search_mode=search_mode_dict
    # )
    # mindmap = MindMapExtractor(chat_mdl)
    # mind_map = trio.run(mindmap, [c["text"] for c in ranks["chunks"]])
    # mind_map = mind_map.output
    if "error" in mind_map:
        return server_error_response(Exception(mind_map["error"]))
    return get_json_result(data=mind_map)


@router.post('/related_questions', summary="生成相关问题", response_description="返回相关问题")
async def related_questions(request: RelatedQuestionsRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    生成相关问题

    根据用户的关键词生成相关搜索问题。

    参数:
    - question: 用户提出的关键词
    - user: 当前用户对象

    返回:
    - 相关搜索问题列表
    """
    req = request.model_dump()

    search_id = req.get("search_id", "")
    search_config = {}
    if search_id:
        if search_app := SearchService.get_detail(db, search_id):
            search_config = search_app.get("search_config", {})

    question = req["question"]

    chat_id = search_config.get("chat_id", "")
    chat_mdl = LLMBundle(db, user.id, LLMType.CHAT, chat_id)

    gen_conf = search_config.get("llm_setting", {"temperature": 0.9})
    if "parameter" in gen_conf:
        del gen_conf["parameter"]
    prompt = load_prompt("related_question")
    ans = await chat_mdl.async_chat(
        prompt,
        [
            {
                "role": "user",
                "content": f"""
    Keywords: {question}
    Related search terms:
        """,
            }
        ],
        gen_conf,
    )

    related_terms = [re.sub(r"^[0-9]\. ", "", a) for a in ans.split("\n") if re.match(r"^[0-9]\. ", a)]

    return get_json_result(data=related_terms)

import asyncio
import json
import logging
import os
import re
from collections.abc import Generator
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Discriminator, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.apps import manager
from api.apps.restful_apis.chat_api import apply_feedback_to_session_payload
from api.db.db_models import APIToken, get_async_db, get_db
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name, get_tenant_default_model_by_type
from api.db.services.conversation_service import ConversationService, structure_answer
from api.db.services.dialog_service import DialogService, async_ask, async_chat, gen_mindmap
from api.db.services.llm_service import LLMBundle
from api.db.services.search_service import SearchService
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import Principal, async_current_user, get_data_error_result, get_json_result, server_error_response
from common.constants import LLMType, RetCode
from common.misc_utils import get_uuid
from core.prompts.generator import chunks_format
from core.prompts.template import load_prompt

# Deprecated compatibility routes for production clients that still call
# /v1/conversation/*. New integrations should use /api/v1/chats/*.


@dataclass
class FullAnswerStreamTransformer:
    # TODO: Transitional compatibility shim for legacy cumulative SSE.
    # Remove this class and `stream_output="full"` after the application frontend
    # fully migrates from full-answer chunks to delta streaming.
    full_answer: str = ""

    def transform(self, answer: dict[str, Any]) -> dict[str, Any] | None:
        payload = {key: value for key, value in answer.items() if key not in {"final", "start_to_think", "end_to_think"}}
        is_final = answer.get("final", True)

        if answer.get("start_to_think"):
            self.full_answer += "<think>"
            payload["answer"] = self.full_answer
            payload["reference"] = {}
            return payload

        if answer.get("end_to_think"):
            self.full_answer += "</think>"
            payload["answer"] = self.full_answer
            payload["reference"] = {}
            return payload

        answer_text = answer.get("answer") or ""
        if is_final:
            if answer_text:
                if answer_text.startswith(self.full_answer):
                    self.full_answer = answer_text
                else:
                    self.full_answer += answer_text
            payload["answer"] = self.full_answer
            return payload

        if not answer_text:
            return None

        self.full_answer += answer_text
        payload["answer"] = self.full_answer
        payload["reference"] = {}
        return payload


class SetConversationRequest(BaseModel):
    conversation_id: str | None = None
    """会话的唯一标识符，如果为空则表示创建新会话。"""

    dialog_id: str | None = None
    """对话的唯一标识符。"""

    name: str | None = "New conversation"
    """会话的名称。"""

    is_new: bool | None = None
    """是否为新建会话，可选参数。如果未提供，将根据conversation_id是否存在来判断。"""

    @field_validator("conversation_id", "dialog_id", mode="before")
    def blank_str_to_none(cls, v):
        """把 '' 或 '   ' 这种空白字符串统一转成 None"""
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @model_validator(mode="after")
    def validate_conversation_logic(self) -> "SetConversationRequest":
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

    # TODO: Transitional compatibility field for the application
    # frontend. Remove together with `FullAnswerStreamTransformer` after the
    # frontend fully migrates to delta streaming.
    stream_output: Literal["delta", "full"] | None = "full"
    """流式输出格式：delta 为增量，full 为兼容旧前端的累计全文。"""

    reasoning: bool | None = None
    """是否启用推理模式。"""

    internet: bool | None = None
    """是否启用联网检索。"""

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

    search_id: str | None = ""
    """搜索应用ID，用于读取搜索配置"""


class SparseSearchMode(BaseModel):
    type: Literal["sparse"] = "sparse"


class DenseSearchMode(BaseModel):
    type: Literal["dense"] = "dense"


class HybridSearchMode(BaseModel):
    type: Literal["hybrid"] = "hybrid"
    weight_dense: float = Field(default=0.7, ge=0.0, le=1.0)
    weight_sparse: float = Field(default=0.3, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_weights(self) -> "HybridSearchMode":
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

    @model_validator(mode="after")
    def validate_weights_format(self) -> "FusionSearchMode":
        """验证weights格式"""
        try:
            parts = self.weights.split(",")
            if len(parts) != 2:
                raise ValueError("weights must contain exactly two comma-separated values")
            float(parts[0].strip())
            float(parts[1].strip())
        except (ValueError, AttributeError):
            raise ValueError("weights must be in format 'float,float' (e.g., '0.05,0.95')")
        return self


# 使用 Discriminator 的高效版本
SearchModeType = Annotated[SparseSearchMode | DenseSearchMode | HybridSearchMode | FusionSearchMode, Discriminator("type")]


class MindmapRequest(BaseModel):
    question: str
    """用户提出的问题"""

    kb_ids: list[str]
    """知识库ID列表"""

    search_id: str | None = ""
    """搜索应用ID，用于读取搜索配置"""

    search_mode: SearchModeType | None = None
    """检索模式"""

    def get_search_mode_dict(self) -> dict[str, Any] | None:
        """将搜索模式转换为字典格式供底层函数使用"""
        if self.search_mode is None:
            return None

        mode_data = self.search_mode.model_dump()
        mode_type = mode_data.pop("type")
        return {mode_type: mode_data}


class RelatedQuestionsRequest(BaseModel):
    question: str
    """用户提出的关键词"""

    search_id: str | None = ""
    """搜索应用ID，用于读取搜索配置"""


router = APIRouter()


@router.post("/set", summary="[Deprecated] 设置会话", response_description="成功设置会话", deprecated=True)
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


@router.get("/get", summary="[Deprecated] 获取会话", response_description="成功获取会话", deprecated=True)
def get(conversation_id: str, db: Session = Depends(get_db), user=Depends(manager)):
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
            return get_json_result(data=False, retmsg="Only owner of conversation authorized for this operation.", retcode=RetCode.OPERATING_ERROR)

        for ref in conv.reference:
            if isinstance(ref, list):
                continue
            ref["chunks"] = chunks_format(ref)

        conv = conv.to_dict()
        conv["avatar"] = avatar
        return get_json_result(data=conv)
    except Exception as e:
        return server_error_response(e)


@router.get("/getsse/{dialog_id}", summary="[Deprecated] 获取对话信息（支持SSE）", response_description="成功获取对话信息", deprecated=True)
def getsse(dialog_id: str, db: Session = Depends(get_db), request: Request = None):
    token_header = request.headers.get("Authorization")
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


@router.post("/rm", summary="[Deprecated] 删除会话", response_description="成功删除会话", deprecated=True)
def rm(request: RemoveConversationRequest, db: Session = Depends(get_db), user=Depends(manager)):
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
                return get_json_result(data=False, retmsg="Only owner of conversation authorized for this operation.", retcode=RetCode.OPERATING_ERROR)
            ConversationService.delete_by_id(db, cid)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.get("/list", summary="[Deprecated] 列出会话", response_description="成功列出会话", deprecated=True)
def list_conversation(dialog_id: str, db: Session = Depends(get_db), user=Depends(manager)):

    try:
        if not DialogService.query(db, tenant_id=user.id, id=dialog_id):
            return get_json_result(data=False, retmsg="Only owner of dialog authorized for this operation.", retcode=RetCode.OPERATING_ERROR)
        convs = ConversationService.query(db, dialog_id=dialog_id, order_by="create_time", reverse=True)
        convs = [d.to_dict() for d in convs]
        return get_json_result(data=convs)
    except Exception as e:
        return server_error_response(e)


@router.post("/completion", summary="[Deprecated] 生成对话", response_description="成功生成对话", deprecated=True)
async def completion(request: CompletionRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)):
    req = request.model_dump()
    if not req.get("conversation_id") or not req.get("messages"):
        return get_data_error_result(retmsg="Missing conversation_id or messages!")
    # Remove stream from req to avoid duplicate argument error
    stream = req.pop("stream", True)
    # TODO(multirag): Transitional compatibility switch for legacy cumulative
    # SSE consumers. Remove after the application frontend no longer sends it.
    stream_output = req.pop("stream_output", "delta")
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
        conv = await db.run_sync(lambda s: ConversationService.get_by_id(s, req["conversation_id"]))  # TODO(async-phase4)
        if not conv:
            return get_data_error_result(retmsg="Conversation not found!")
        if len(req["messages"]) != 1:
            conv.message = deepcopy(req["messages"])  # re-generate for conversation
        else:
            conv.message.append(msg[0])
        dia = await db.run_sync(lambda s: DialogService.get_by_id(s, conv.dialog_id))  # TODO(async-phase4)
        if not dia:
            return get_data_error_result(retmsg="Dialog not found!")
        del req["conversation_id"]
        del req["messages"]

        if not conv.reference:
            conv.reference = []
        conv.reference = [r for r in conv.reference if r]
        conv.reference.append({"chunks": [], "doc_aggs": []})

        if chat_model_id:
            try:
                override_model_type = await asyncio.to_thread(TenantLLMService.llm_id2llm_type, chat_model_id)  # TODO(async-phase4): DB 兜底自开同步连接
                if override_model_type == "image2text":
                    model_type_value = LLMType.IMAGE2TEXT.value
                else:
                    model_type_value = LLMType.CHAT.value
                override_model_config = await db.run_sync(lambda s: get_model_config_by_type_and_name(s, dia.tenant_id, model_type_value, chat_model_id))  # TODO(async-phase4)
            except Exception:
                req.pop("chat_model_id", None)
                req.pop("chat_model_config", None)
                return get_data_error_result(retmsg=f"Cannot use specified model {chat_model_id}.")
            dia.llm_id = chat_model_id
            dia.tenant_llm_id = override_model_config.get("id")
            dia.llm_setting = chat_model_config

        is_embedded = bool(chat_model_id)

        async def stream_response():
            nonlocal dia, msg, db, req, conv
            # TODO: Transitional compatibility adapter for the
            # application frontend. Remove after all `/conversation/completion`
            # consumers handle delta streaming natively.
            transformer = FullAnswerStreamTransformer() if stream_output == "full" else None
            try:
                async for ans in async_chat(dia, msg, db, **req):
                    ans = structure_answer(conv, ans, message_id, conv.id)
                    if transformer is not None:
                        # TODO: Transitional legacy SSE path.
                        ans = transformer.transform(ans)
                        if ans is None:
                            continue
                    yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": ans}, ensure_ascii=False) + "\n\n"
                await db.run_sync(lambda s: ConversationService.update_by_id(s, conv.id, conv.to_dict()))  # TODO(async-phase4)
            except Exception as e:
                logging.exception(e)
                yield "data:" + json.dumps({"retcode": 500, "retmsg": str(e), "data": {"answer": "**ERROR**: " + str(e), "reference": []}}, ensure_ascii=False) + "\n\n"
            yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False) + "\n\n"

        if stream:
            return StreamingResponse(stream_response(), media_type="text/event-stream")
        else:
            answer = None
            conv = deepcopy(conv)  # 深拷贝 conv，否则会导致后续更新数据时，无法更新引用
            async for ans in async_chat(dia, msg, db, **req):
                answer = structure_answer(conv, ans, message_id, conv.id)
                if not is_embedded:
                    await db.run_sync(lambda s: ConversationService.update_by_id(s, conv.id, conv.to_dict()))  # TODO(async-phase4)
                break
            return get_json_result(data=answer)
    except Exception as e:
        return server_error_response(e)


@router.post("/tts", summary="[Deprecated] 文本转语音", response_description="成功文本转语音", deprecated=True)
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

    try:
        if llm_name:
            tts_config = get_model_config_by_type_and_name(db, user.id, LLMType.TTS.value, llm_name)
        else:
            tts_config = get_tenant_default_model_by_type(db, user.id, LLMType.TTS)
    except Exception as e:
        return get_data_error_result(retmsg=str(e))

    tts_mdl = LLMBundle(db, user.id, tts_config)

    def stream_audio() -> Generator[bytes, None, None]:
        try:
            for txt in filter(None, re.split(r"[，。/《》？；：！\n\r:;]+", text)):
                if txt.strip():
                    yield from tts_mdl.tts(txt)
        except Exception as e:
            error_message = json.dumps({"retcode": 500, "retmsg": str(e), "data": {"answer": "**ERROR**: " + str(e)}}, ensure_ascii=False).encode("utf-8")
            yield error_message

    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no", "Content-Type": "audio/mpeg", "Content-Disposition": 'attachment; filename="tts_output.mp3"'}
    return StreamingResponse(stream_audio(), media_type="audio/mpeg", headers=headers)


@router.post("/sequence2txt", summary="[Deprecated] 语音转文字（流式）", response_description="成功识别语音", deprecated=True)
def sequence2txt(file: UploadFile = File(...), stream: str = "false", db: Session = Depends(get_db), user=Depends(manager)):
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

    ALLOWED_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".webm", ".opus", ".wma"}

    filename = file.filename or ""
    suffix = os.path.splitext(filename)[-1].lower()
    if suffix not in ALLOWED_EXTS:
        return get_data_error_result(retmsg=f"Unsupported audio format: {suffix}. Allowed: {', '.join(sorted(ALLOWED_EXTS))}")

    # 保存上传的音频文件到临时路径
    import tempfile as tf

    fd, temp_audio_path = tf.mkstemp(suffix=suffix)
    os.close(fd)
    with open(temp_audio_path, "wb") as f:
        f.write(file.file.read())

    try:
        asr_config = get_tenant_default_model_by_type(db, user.id, LLMType.SPEECH2TEXT)
    except Exception as e:
        try:
            os.remove(temp_audio_path)
        except Exception:
            pass
        return get_data_error_result(retmsg=str(e))

    asr_mdl = LLMBundle(db, user.id, asr_config)

    if not stream_mode:
        text = asr_mdl.transcription(temp_audio_path)
        try:
            os.remove(temp_audio_path)
        except Exception as e:
            logging.error(f"Failed to remove temp audio file: {e!s}")
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
                logging.error(f"Failed to remove temp audio file: {e!s}")

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/asr", summary="[Deprecated] 语音识别", response_description="成功识别语音", deprecated=True)
def asr(request: ASRRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req = request.model_dump()
    audio_file_path = req.get("audio_file_path")
    llm_name = req.get("llm_name")

    try:
        if llm_name:
            asr_config = get_model_config_by_type_and_name(db, user.id, LLMType.SPEECH2TEXT.value, llm_name)
        else:
            asr_config = get_tenant_default_model_by_type(db, user.id, LLMType.SPEECH2TEXT)
    except Exception as e:
        return get_data_error_result(retmsg=str(e))

    asr_mdl = LLMBundle(db, user.id, asr_config)

    transcription_result = asr_mdl.transcription(audio=audio_file_path)

    if "**ERROR**" in transcription_result:
        raise HTTPException(status_code=500, detail=transcription_result)
    return get_json_result(data=transcription_result)


@router.post("/asr_upload", summary="[Deprecated] 语音识别上传", response_description="成功识别语音", deprecated=True)
def asr_upload(file: UploadFile = File(...), llm_name: str | None = None, db: Session = Depends(get_db), user=Depends(manager)):

    import tempfile

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_audio_file:
        temp_audio_file.write(file.file.read())
        audio_file_path = temp_audio_file.name

    try:
        if llm_name:
            asr_config = get_model_config_by_type_and_name(db, user.id, LLMType.SPEECH2TEXT.value, llm_name)
        else:
            asr_config = get_tenant_default_model_by_type(db, user.id, LLMType.SPEECH2TEXT)
    except Exception as e:
        return get_data_error_result(retmsg=str(e))

    asr_mdl = LLMBundle(db, user.id, asr_config)

    # 调用 ASR 语音识别函数
    transcription_result = asr_mdl.transcription(audio=audio_file_path)

    if "**ERROR**" in transcription_result:
        raise HTTPException(status_code=500, detail=transcription_result)

    return get_json_result(data=transcription_result)


@router.post("/delete_msg", summary="[Deprecated] 删除信息", response_description="成功删除信息", deprecated=True)
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

    ConversationService.update_by_id(db, conv["id"], conv)
    return get_json_result(data=conv)


@router.post("/thumbup", summary="[Deprecated] 点赞", response_description="成功点赞", deprecated=True)
def thumbup(request: ThumbupRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req = request.model_dump()
    conv = ConversationService.get_by_id(db, req["conversation_id"])
    if not conv:
        return get_data_error_result(retmsg="Conversation not found!")
    thumbup = req.get("thumbup")
    if not isinstance(thumbup, bool):
        return get_data_error_result(retmsg="thumbup must be a boolean")
    feedback = req.get("feedback", "")
    conv = conv.to_dict()
    apply_feedback_to_session_payload(user.id, conv, req["message_id"], thumbup, feedback)

    ConversationService.update_by_id(db, conv["id"], conv)
    return get_json_result(data=conv)


@router.post("/ask", summary="[Deprecated] 问答接口", response_description="返回答案", deprecated=True)
def ask_about(request: AskAboutRequest, db: Session = Depends(get_db), user=Depends(manager)):
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

    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


@router.post("/mindmap", summary="[Deprecated] 生成思维导图", response_description="返回思维导图", deprecated=True)
async def mindmap(request: MindmapRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req = request.model_dump()

    search_id = req.get("search_id", "")

    search_app = SearchService.get_detail(db, search_id) if search_id else {}
    search_config = search_app.get("search_config", {}) if search_app else {}
    kb_ids = search_config.get("kb_ids", [])
    kb_ids.extend(req["kb_ids"])
    kb_ids = list(set(kb_ids))

    mind_map = await gen_mindmap(db, req["question"], kb_ids, search_app.get("tenant_id", user.id), search_config)
    if "error" in mind_map:
        return server_error_response(Exception(mind_map["error"]))
    return get_json_result(data=mind_map)


@router.post("/related_questions", summary="[Deprecated] 生成相关问题", response_description="返回相关问题", deprecated=True)
async def related_questions(request: RelatedQuestionsRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)):
    req = request.model_dump()

    search_id = req.get("search_id", "")
    search_config = {}
    if search_id:
        if search_app := await db.run_sync(lambda s: SearchService.get_detail(s, search_id)):  # TODO(async-phase4)
            search_config = search_app.get("search_config", {})

    question = req["question"]

    chat_id = search_config.get("chat_id", "")
    if chat_id:
        chat_config = await db.run_sync(lambda s: get_model_config_by_type_and_name(s, user.id, LLMType.CHAT.value, chat_id))  # TODO(async-phase4)
    else:
        chat_config = await db.run_sync(lambda s: get_tenant_default_model_by_type(s, user.id, LLMType.CHAT))  # TODO(async-phase4)
    chat_mdl = await db.run_sync(lambda s: LLMBundle(s, user.id, chat_config))  # TODO(async-phase4)
    chat_mdl.db = None  # run_sync 的 facade 不得逸出 greenlet（AGENTS.md 规约）

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

# coding=utf-8
"""
@project: multirag
@Author：龙
@file： conversation_app.py
@date：2024/7/16 18:00
@desc: 会话管理接口
"""
import json
import re
from copy import deepcopy
import trio
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Generator

from api.db.db_models import APIToken, get_db
from api.db.services.conversation_service import ConversationService, structure_answer
from api.db.services.dialog_service import DialogService, chat, ask
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle, TenantService
from api.db import LLMType
from api.db.services.user_service import UserTenantService
from api import settings
from api.utils.api_utils import server_error_response, get_data_error_result
from api.utils import get_uuid
from api.utils.api_utils import get_json_result
# from api.db.database import get_db
from api.apps import manager
from graphrag.general.mind_map_extractor import MindMapExtractor
from core.app.tag import label_question


class SetConversationRequest(BaseModel):
    conversation_id: str | None = None
    """会话的唯一标识符，如果为空则表示创建新会话。"""

    dialog_id: str | None = None
    """对话的唯一标识符。"""

    name: str | None = "New conversation"
    """会话的名称。"""

    # 其他可能的字段


class CompletionRequest(BaseModel):
    conversation_id: str
    """会话的唯一标识符。"""

    messages: list[dict]
    """消息列表，每个消息包含角色和内容。"""

    quote: bool | None = False
    """是否引用，默认值为 False。"""

    stream: bool | None = True
    """是否使用流式响应，默认值为 True。"""

    filter_condition: str | None = ""
    """过滤条件，可以根据实际需求自定义结构。"""


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


class ASRRequest(BaseModel):
    audio_file_path: str
    """MP3音频文件的地址"""


class AskAboutRequest(BaseModel):
    question: str
    """用户提出的问题"""

    kb_ids: list[str]
    """知识库ID列表"""


class MindmapRequest(BaseModel):
    question: str
    """用户提出的问题"""

    kb_ids: list[str]
    """知识库ID列表"""


class RelatedQuestionsRequest(BaseModel):
    question: str
    """用户提出的关键词"""


router = APIRouter()


@router.post('/set', summary="设置会话", response_description="成功设置会话")
async def set_conversation(request: SetConversationRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    设置会话

    该接口用于创建或更新会话信息。

    参数:
    - request: SetConversationRequest对象，包含会话的配置信息
        - conversation_id: 会话的唯一标识符，如果为空则表示创建新会话
        - dialog_id: 对话的唯一标识符
        - name: 会话的名称

    返回:
    - 成功时返回包含会话信息的JSON结果
    - 失败时返回错误信息
    """
    req = request.model_dump()
    conv_id = req.get("conversation_id")
    if conv_id:
        del req["conversation_id"]
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

    try:
        dia = DialogService.get_by_id(db, req["dialog_id"])
        if not dia:
            return get_data_error_result(retmsg="Dialog not found")
        conv = {
            "id": get_uuid(),
            "dialog_id": req["dialog_id"],
            "name": req.get("name", "New conversation"),
            "message": [{"role": "assistant", "content": dia.prompt_config["prologue"]}]
        }
        ConversationService.save(db, **conv)
        conv = ConversationService.get_by_id(db, conv["id"])
        if not conv:
            return get_data_error_result(retmsg="Fail to new a conversation!")
        conv = conv.to_dict()
        return get_json_result(data=conv)
    except Exception as e:
        return server_error_response(e)


@router.get('/get', summary="获取会话", response_description="成功获取会话")
async def get(conversation_id: str, db: Session = Depends(get_db), user=Depends(manager)):
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
        avatar = None
        for tenant in tenants:
            dialog = DialogService.query(db, tenant_id=tenant.tenant_id, id=conv.dialog_id)
            if dialog and len(dialog) > 0:
                avatar = dialog[0].icon
                break
        else:
            return get_json_result(
                data=False, retmsg=f'Only owner of conversation authorized for this operation.',
                retcode=settings.RetCode.OPERATING_ERROR)

        def get_value(d, k1, k2):
            return d.get(k1, d.get(k2))

        for ref in conv.reference:
            ref["chunks"] = [{
                "id": get_value(ck, "chunk_id", "id"),
                "content": get_value(ck, "content", "text"),
                "document_id": get_value(ck, "doc_id", "document_id"),
                "document_name": get_value(ck, "docnm_kwd", "document_name"),
                "dataset_id": get_value(ck, "kb_id", "dataset_id"),
                "image_id": get_value(ck, "image_id", "img_id"),
                "positions": get_value(ck, "positions", "position_int"),
            } for ck in ref.get("chunks", [])]

        conv = conv.to_dict()
        conv["avatar"] = avatar
        return get_json_result(data=conv)
    except Exception as e:
        return server_error_response(e)


@router.get('/getsse/{dialog_id}', summary="获取对话信息（支持SSE）", response_description="成功获取对话信息")
async def getsse(dialog_id: str, db: Session = Depends(get_db), request: Request = None):
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
async def rm(request: RemoveConversationRequest, db: Session = Depends(get_db), user=Depends(manager)):
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
                    retcode=settings.RetCode.OPERATING_ERROR)
            ConversationService.delete_by_id(db, cid)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.get('/list', summary="列出会话", response_description="成功列出会话")
async def list_conversation(dialog_id: str, db: Session = Depends(get_db), user=Depends(manager)):
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
                retcode=settings.RetCode.OPERATING_ERROR)
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
def completion(request: CompletionRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
        完成会话

        该接口用于完成指定会话，生成对话内容。

        参数:
        - request: CompletionRequest对象，包含会话的详细信息
            - conversation_id: str 会话的唯一标识符
            - messages: List[dict] 消息列表，每个消息包含角色和内容
            - quote: Optional[bool] 是否引用，默认值为 False
            - stream: Optional[bool] 是否使用流式响应，默认值为 True
            - filter_condition: Optional[dict] 过滤条件

        返回:
        - 成功时返回生成的对话内容
        - 失败时返回错误信息
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
        else:
            def get_value(d, k1, k2):
                return d.get(k1, d.get(k2))

            for ref in conv.reference:
                if isinstance(ref, list):
                    continue
                ref["chunks"] = [{
                    "id": get_value(ck, "chunk_id", "id"),
                    "content": get_value(ck, "content", "text"),
                    "document_id": get_value(ck, "doc_id", "document_id"),
                    "document_name": get_value(ck, "docnm_kwd", "document_name"),
                    "dataset_id": get_value(ck, "kb_id", "dataset_id"),
                    "image_id": get_value(ck, "image_id", "img_id"),
                    "positions": get_value(ck, "positions", "position_int"),
                } for ck in ref.get("chunks", [])]

        if not conv.reference:
            conv.reference = []
        conv.reference.append({"chunks": [], "doc_aggs": []})

        def stream_response():
            nonlocal dia, msg, db, req, conv
            try:
                for ans in chat(dia, msg, db, **req):
                    ans = structure_answer(conv, ans, message_id, conv.id)
                    yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": ans}, ensure_ascii=False) + "\n\n"
                ConversationService.update_by_id(db, conv.id, conv.to_dict())
            except Exception as e:
                yield "data:" + json.dumps({"retcode": 500, "retmsg": str(e),
                                            "data": {"answer": "**ERROR**: " + str(e), "reference": []}},
                                           ensure_ascii=False) + "\n\n"
            yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False) + "\n\n"

        if stream:
            return StreamingResponse(stream_response(), media_type="text/event-stream")
        else:
            answer = None
            conv = deepcopy(conv)  # 深拷贝 conv，否则会导致后续更新数据时，无法更新引用
            for ans in chat(dia, msg, db, **req):
                answer = structure_answer(conv, ans, message_id, conv.id)
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
    - request: TTSRequest对象，包含要转换的文本内容
        - text: str 要转换为语音的文本内容

    返回:
    - 成功时返回包含音频流的响应
    - 失败时返回错误信息

    逻辑说明:
    - 首先，根据用户ID获取租户信息。如果租户信息不存在，返回404错误。
    - 获取用户的默认TTS模型ID，并验证其是否存在。
    - 使用该模型将文本逐段转化为语音流，并逐段返回。
    - 若在转换过程中出现错误，则返回错误信息。

    注意事项:
    - 文本内容不应为空。
    - 返回结果为MPEG音频流，可供下载或直接播放。
    """
    req = request.model_dump()
    text = req.get("text")

    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")

    tts_id = tenants[0].get("tts_id")
    if not tts_id:
        raise HTTPException(status_code=400, detail="No default TTS model is set")

    tts_mdl = LLMBundle(db, tenants[0]["tenant_id"], LLMType.TTS, tts_id)

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


@router.post('/asr', summary="语音识别", response_description="成功识别语音")
def asr(request: ASRRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    语音识别

    该接口用于将输入的音频文件转换为文本。

    参数:
    - request: ASRRequest对象，包含音频文件的路径
        - audio_file_path: str 音频文件的路径

    返回:
    - 成功时返回包含识别文本的JSON结果
    - 失败时返回错误信息

    逻辑说明:
    - 根据用户ID获取租户信息，确保租户存在并获取默认ASR模型ID。
    - 使用该ASR模型处理音频文件，返回识别的文本。
    - 若在处理过程中出现错误，则返回错误信息。

    注意事项:
    - 音频文件路径应为有效路径。
    - 确保租户已配置默认ASR模型，否则将返回错误信息。
    """
    req = request.model_dump()
    audio_file_path = req.get("audio_file_path")

    # 获取用户信息和语音识别模型的信息
    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")

    asr_id = tenants[0].get("asr_id")
    if not asr_id:
        raise HTTPException(status_code=400, detail="No default ASR model is set")

    asr_mdl = LLMBundle(db, tenants[0]["tenant_id"], LLMType.SPEECH2TEXT, asr_id)

    # 调用 ASR 语音识别函数
    transcription_result = asr_mdl.transcription(audio=audio_file_path)

    if "**ERROR**" in transcription_result:
        raise HTTPException(status_code=500, detail=transcription_result)
    return get_json_result(data=transcription_result)


@router.post('/asr_upload', summary="语音识别上传", response_description="成功识别语音")
def asr_upload(file: UploadFile = File(...), db: Session = Depends(get_db), user=Depends(manager)):
    """
    语音识别上传

    该接口用于上传音频文件并将其转换为文本。

    参数:
    - file: UploadFile 上传的音频文件

    返回:
    - 成功时返回包含识别文本的JSON结果
    - 失败时返回错误信息

    逻辑说明:
    - 将上传的音频文件保存到临时路径，并验证用户和模型信息。
    - 使用默认ASR模型识别音频文件内容，并将识别结果返回。
    - 若在处理过程中出现错误，则返回错误信息。

    注意事项:
    - 确保上传文件格式为有效的音频格式（如mp3）。
    - 确保租户已配置默认ASR模型，否则将返回错误信息。
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

    asr_id = tenants[0].get("asr_id")
    if not asr_id:
        raise HTTPException(status_code=400, detail="No default ASR model is set")

    asr_mdl = LLMBundle(db, tenants[0]["tenant_id"], LLMType.SPEECH2TEXT, asr_id)

    # 调用 ASR 语音识别函数
    transcription_result = asr_mdl.transcription(audio=audio_file_path)

    if "**ERROR**" in transcription_result:
        raise HTTPException(status_code=500, detail=transcription_result)

    return get_json_result(data=transcription_result)


@router.post('/delete_msg', summary="删除信息", response_description="成功删除信息")
async def delete_msg(request: DeleteMsgRequest, db: Session = Depends(get_db), user=Depends(manager)):
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
async def thumbup(request: ThumbupRequest, db: Session = Depends(get_db), user=Depends(manager)):
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
async def ask_about(request: AskAboutRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    问答接口

    该接口用于根据用户的问题从知识库中获取答案。

    参数:
    - question: 用户提出的问题
    - kb_ids: 知识库ID列表
    - user: 当前用户对象

    返回:
    - 实时流式返回答案数据
    """
    req = request.model_dump()
    uid = user.id

    def stream():
        try:
            for ans in ask(db, req["question"], req["kb_ids"], uid):
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
    kb_ids = req["kb_ids"]
    kb = KnowledgebaseService.get_by_id(db, kb_ids[0])
    if not kb:
        return get_data_error_result(retmsg="Knowledgebase not found!")

    embd_mdl = LLMBundle(db, kb.tenant_id, LLMType.EMBEDDING, llm_name=kb.embd_id)
    chat_mdl = LLMBundle(db, user.id, LLMType.CHAT)
    filter_exp = ""  # todo 暂时不提供权限过滤的查询，如果需要这边需要完善
    kb_names = list([kb.name])
    ranks = settings.retrievaler.retrieval(req["question"], filter_exp, embd_mdl, kb.tenant_id, kb_names, 1, 12, 0.3, 0.3, aggs=False, rank_feature=label_question(db, req["question"], [kb]))
    mindmap = MindMapExtractor(chat_mdl)
    mind_map = trio.run(mindmap, [c["text"] for c in ranks["chunks"]])
    mind_map = mind_map.output
    if "error" in mind_map:
        return server_error_response(Exception(mind_map["error"]))
    return get_json_result(data=mind_map)


# 定义 related_questions 接口
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
    question = req["question"]
    chat_mdl = LLMBundle(db, user.id, LLMType.CHAT)
    prompt = """
Role: You are an AI language model assistant tasked with generating 5-10 related questions based on a user’s original query. These questions should help expand the search query scope and improve search relevance.

Instructions:
	Input: You are provided with a user’s question.
	Output: Generate 5-10 alternative questions that are related to the original user question. These alternatives should help retrieve a broader range of relevant documents from a vector database.
	Context: Focus on rephrasing the original question in different ways, making sure the alternative questions are diverse but still connected to the topic of the original query. Do not create overly obscure, irrelevant, or unrelated questions.
	Fallback: If you cannot generate any relevant alternatives, do not return any questions.
	Guidance:
	1. Each alternative should be unique but still relevant to the original query.
	2. Keep the phrasing clear, concise, and easy to understand.
	3. Avoid overly technical jargon or specialized terms unless directly relevant.
	4. Ensure that each question contributes towards improving search results by broadening the search angle, not narrowing it.

Example:
Original Question: What are the benefits of electric vehicles?

Alternative Questions:
	1. How do electric vehicles impact the environment?
	2. What are the advantages of owning an electric car?
	3. What is the cost-effectiveness of electric vehicles?
	4. How do electric vehicles compare to traditional cars in terms of fuel efficiency?
	5. What are the environmental benefits of switching to electric cars?
	6. How do electric vehicles help reduce carbon emissions?
	7. Why are electric vehicles becoming more popular?
	8. What are the long-term savings of using electric vehicles?
	9. How do electric vehicles contribute to sustainability?
	10. What are the key benefits of electric vehicles for consumers?

Reason:
	Rephrasing the original query into multiple alternative questions helps the user explore different aspects of their search topic, improving the quality of search results.
	These questions guide the search engine to provide a more comprehensive set of relevant documents.
"""

    ans = chat_mdl.chat(
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
        {"temperature": 0.9},
    )

    related_terms = [re.sub(r"^[0-9]\. ", "", a) for a in ans.split("\n") if re.match(r"^[0-9]\. ", a)]

    return get_json_result(data=related_terms)

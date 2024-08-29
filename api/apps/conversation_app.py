# coding=utf-8
"""
@project: multirag
@Author：龙
@file： conversation_app.py
@date：2024/7/16 18:00
@desc: 会话管理接口
"""
import json
from copy import deepcopy
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional, Generator
from api.db.services.dialog_service import DialogService, ConversationService, chat
from api.db.services.llm_service import LLMBundle, TenantService
from api.db import LLMType
from api.utils.api_utils import server_error_response, get_data_error_result, get_json_result
from api.utils import get_uuid
from api.utils.api_utils import get_json_result
from api.db.database import get_db
from api.apps import manager


class SetConversationRequest(BaseModel):
    conversation_id: Optional[str] = None
    """会话的唯一标识符，如果为空则表示创建新会话。"""

    dialog_id: Optional[str] = None
    """对话的唯一标识符。"""

    name: Optional[str] = None
    """会话的名称。"""

    # 其他可能的字段


class CompletionRequest(BaseModel):
    conversation_id: str
    """会话的唯一标识符。"""

    messages: List[dict]
    """消息列表，每个消息包含角色和内容。"""

    quote: Optional[bool] = False
    """是否引用，默认值为 False。"""

    stream: Optional[bool] = True
    """是否使用流式响应，默认值为 True。"""

    filter_condition: Optional[str] = ""
    """过滤条件，可以根据实际需求自定义结构。"""


class RemoveConversationRequest(BaseModel):
    conversation_ids: List[str]
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

    set: Optional[bool] = None
    """点赞状态"""

    feedback: str
    """反馈"""

class TTSRequest(BaseModel):
    text: str
    """文本内容"""

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
    - db: Session 数据库会话对象
    - user: 当前用户对象

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
    - db: Session 数据库会话对象
    - user: 当前用户对象

    返回:
    - 成功时返回包含会话信息的JSON结果
    - 失败时返回错误信息
    """
    try:
        conv = ConversationService.get_by_id(db, conversation_id)
        if not conv:
            return get_data_error_result(retmsg="Conversation not found!")
        conv = conv.to_dict()
        return get_json_result(data=conv)
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
    - db: Session 数据库会话对象
    - user: 当前用户对象

    返回:
    - 成功时返回成功删除的JSON结果
    - 失败时返回错误信息
    """
    try:
        for cid in request.conversation_ids:
            ConversationService.delete_by_id(db, cid)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.get('/list', summary="列出会话", response_description="成功列出会话")
async def list_conversation(conversation_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    列出会话

    该接口用于列出指定对话的所有会话。

    参数:
    - conversation_id: str 对话的唯一标识符
    - db: Session 数据库会话对象
    - user: 当前用户对象

    返回:
    - 成功时返回包含会话列表的JSON结果
    - 失败时返回错误信息
    """
    try:
        convs = ConversationService.query(
            db,
            id=conversation_id,
            # order_by=ConversationService.model.create_time,
            order_by="create_time",
            reverse=True)
        convs = [d.to_dict() for d in convs]
        return get_json_result(data=convs)
    except Exception as e:
        return server_error_response(e)


@router.post('/completion', summary="生成对话", response_description="成功生成对话")
async def completion(request: CompletionRequest, db: Session = Depends(get_db), user=Depends(manager)):
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
        - db: Session 数据库会话对象
        - user: 当前用户对象

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
        # msg.append({"role": m["role"], "content": m["content"]})
        # if "doc_ids" in m:
        #     msg[-1]["doc_ids"] = m["doc_ids"]
        msg.append(m)
    if not msg[-1].get("id"): msg[-1]["id"] = get_uuid()
    message_id = msg[-1].get("id")

    if not msg:
        return get_data_error_result(retmsg="No valid messages found!")

    try:
        conv = ConversationService.get_by_id(db, req["conversation_id"])
        if not conv:
            return get_data_error_result(retmsg="Conversation not found!")
        conv.message.append(deepcopy(msg[-1]))
        dia = DialogService.get_by_id(db, conv.dialog_id)
        if not dia:
            return get_data_error_result(retmsg="Dialog not found!")
        del req["conversation_id"]
        del req["messages"]

        if not conv.reference:
            conv.reference = []
        # conv.message.append({"role": "assistant", "content": ""})
        conv.message.append({"role": "assistant", "content": "", "id": message_id})
        conv.reference.append({"chunks": [], "doc_aggs": []})

        def fillin_conv(ans):
            nonlocal conv, message_id
            if not conv.reference:
                conv.reference.append(ans["reference"])
            else:
                conv.reference[-1] = ans["reference"]
            # conv.message[-1] = {"role": "assistant", "content": ans["answer"]}
            conv.message[-1] = {"role": "assistant", "content": ans["answer"],
                                "id": message_id, "prompt": ans.get("prompt", "")}
            ans["id"] = message_id

        def stream_response():
            nonlocal dia, msg, db, req, conv
            try:
                for ans in chat(dia, msg, db, **req):
                    fillin_conv(ans)
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
            for ans in chat(dia, msg, db, **req):
                answer = ans
                fillin_conv(ans)
                ConversationService.update_by_id(db, conv.id, conv.to_dict())
                break
            return get_json_result(data=answer)
    except Exception as e:
        return server_error_response(e)


@router.post('/tts', summary="文本转语音", response_description="成功文本转语音")
async def tts(request: TTSRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req = request.model_dump()
    text = req.get("text")

    tenants = TenantService.get_by_user_id(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")

    tts_id = tenants[0].get("tts_id")
    if not tts_id:
        raise HTTPException(status_code=400, detail="No default TTS model is set")

    tts_mdl = LLMBundle(db, tenants[0]["tenant_id"], "TTS", tts_id)

    def stream_audio() -> Generator[bytes, None, None]:
        try:
            for chunk in tts_mdl(text):
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
        "X-Accel-Buffering": "no"
    }
    return StreamingResponse(stream_audio(), media_type="audio/mpeg", headers=headers)


@router.post('/delete_msg', summary="删除信息", response_description="成功删除信息")
async def delete_msg(request: DeleteMsgRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    删除消息

    该接口用于删除指定会话中的某条消息及其关联的参考信息。

    参数:
    - request: DeleteMsgRequest对象，包含会话ID和消息ID
        - conversation_id: str 会话的唯一标识符
        - message_id: List[dict] 消息ID列表
    - db: Session 数据库会话对象
    - user: 当前用户对象

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
        # todo 待解决：目前无法完全符合预期删除reference
        if i < len(conv["reference"]):
            conv["reference"].pop(i)  # 同样对 reference 做相应的 pop 操作
        break
        # assert conv["message"][i + 1]["id"] == req["message_id"]
        # conv["message"].pop(i)
        # conv["message"].pop(i)
        # conv["reference"].pop(i)
        # break
    ConversationService.update_by_id(db, conv["id"], conv)
    return get_json_result(data=conv)



@router.post('/thumbup', summary="点赞", response_description="成功点赞")
async def thumbup(request: ThumbupRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req = request.model_dump()
    conv = ConversationService.get_by_id(db, req["conversation_id"])
    if not conv:
        return get_data_error_result(retmsg="Conversation not found!")
    up_down = req.get("set")
    feedback = req.get("feedback", "")
    conv = conv.to_dict()
    for i, msg in enumerate(conv["message"]):
        if req["message_id"] == msg.get("id", "") and msg.get("role", "") == "assistant":
            if up_down:
                msg["thumbup"] = True
                if "feedback" in msg: del msg["feedback"]
            else:
                msg["thumbup"] = False
                if feedback: msg["feedback"] = feedback
            break

    ConversationService.update_by_id(db, conv["id"], conv)
    return get_json_result(data=conv)
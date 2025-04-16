import time
import json
from uuid import uuid4

from sqlalchemy import asc
from sqlalchemy.orm import Session

from api.db import StatusEnum
from api.db.db_models import Conversation
from api.db.services.api_service import API4ConversationService
from api.db.services.common_service import CommonService
from api.db.services.dialog_service import DialogService, chat
from api.utils import get_uuid

from core.prompts import chunks_format

class ConversationService(CommonService):
    model = Conversation

    @classmethod
    def get_list(cls, db: Session, dialog_id, page_number, items_per_page, orderby, desc, id=None, name=None):
        # 使用 SQLAlchemy 的 query 方法开始查询
        query = db.query(cls.model).filter(cls.model.dialog_id == dialog_id)

        # 添加条件过滤器
        if id:
            query = query.filter(cls.model.id == id)
        if name:
            query = query.filter(cls.model.name == name)

        # 根据 desc 参数确定排序方式
        order_clause = getattr(cls.model, orderby)
        if desc:
            query = query.order_by(desc(order_clause))
        else:
            query = query.order_by(asc(order_clause))

        # 应用分页
        query = query.offset((page_number - 1) * items_per_page).limit(items_per_page)

        # 执行查询并返回结果
        results = query.all()
        # 将结果转换为字典形式返回
        return [item.__dict__ for item in results]


def structure_answer(conv, ans, message_id, session_id):
    reference = ans["reference"]
    if not isinstance(reference, dict):
        reference = {}
        ans["reference"] = {}

    chunk_list = chunks_format(reference)

    reference["chunks"] = chunk_list
    ans["id"] = message_id
    ans["session_id"] = session_id

    if not conv:
        return ans

    if not conv.message:
        conv.message = []
    if not conv.message or conv.message[-1].get("role", "") != "assistant":
        conv.message.append({"role": "assistant", "content": ans["answer"], "created_at": time.time(), "id": message_id})
    else:
        conv.message[-1] = {"role": "assistant", "content": ans["answer"], "created_at": time.time(), "id": message_id}
    if conv.reference:
        conv.reference[-1] = reference
        # conv.reference.append(reference)
    return ans


def completion(db, tenant_id, chat_id, question, name="New session", session_id=None, stream=True, **kwargs):
    assert name, "`name` can not be empty."
    dia = DialogService.query(db, id=chat_id, tenant_id=tenant_id, status=StatusEnum.VALID.value)
    assert dia, "You do not own the chat."

    if not session_id:
        session_id = get_uuid()
        conv = {
            "id":session_id ,
            "dialog_id": chat_id,
            "name": name,
            "message": [{"role": "assistant", "content": dia[0].prompt_config.get("prologue"), "created_at": time.time()}],
        }
        ConversationService.save(**conv)
        yield "data:" + json.dumps({"code": 0, "message": "",
                                    "data": {
                                        "answer": conv["message"][0]["content"],
                                        "reference": {},
                                        "audio_binary": None,
                                        "id": None,
                                        "session_id": session_id
                                    }},
                                   ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"
        return

    conv = ConversationService.query(db, id=session_id, dialog_id=chat_id)
    if not conv:
        raise LookupError("Session does not exist")

    conv = conv[0]
    msg = []
    question = {
        "content": question,
        "role": "user",
        "id": str(uuid4())
    }
    conv.message.append(question)
    for m in conv.message:
        if m["role"] == "system":
            continue
        if m["role"] == "assistant" and not msg:
            continue
        msg.append(m)
    message_id = msg[-1].get("id")
    e, dia = DialogService.get_by_id(db, conv.dialog_id)

    if not conv.reference:
        conv.reference = []
    conv.message.append({"role": "assistant", "content": "", "id": message_id})
    conv.reference.append({"chunks": [], "doc_aggs": []})

    if stream:
        try:
            for ans in chat(dia, msg, db, True, **kwargs):
                ans = structure_answer(conv, ans, message_id, session_id)
                yield "data:" + json.dumps({"code": 0, "data": ans}, ensure_ascii=False) + "\n\n"
            ConversationService.update_by_id(db, conv.id, conv.to_dict())
        except Exception as e:
            yield "data:" + json.dumps({"code": 500, "message": str(e),
                                        "data": {"answer": "**ERROR**: " + str(e), "reference": []}},
                                       ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"code": 0, "data": True}, ensure_ascii=False) + "\n\n"

    else:
        answer = None
        for ans in chat(dia, msg, db, False, **kwargs):
            answer = structure_answer(conv, ans, message_id, session_id)
            ConversationService.update_by_id(db, conv.id, conv.to_dict())
            break
        yield answer


def iframe_completion(db, dialog_id, question, session_id=None, stream=True, **kwargs):
    dia = DialogService.get_by_id(db, dialog_id)
    assert dia, "Dialog not found"
    if not session_id:
        session_id = get_uuid()
        conv = {
            "id": session_id,
            "dialog_id": dialog_id,
            "user_id": kwargs.get("user_id", ""),
            "message": [{"role": "assistant", "content": dia.prompt_config["prologue"], "created_at": time.time()}]
        }
        API4ConversationService.save(db, **conv)
        yield "data:" + json.dumps({"code": 0, "message": "",
                                    "data": {
                                        "answer": conv["message"][0]["content"],
                                        "reference": {},
                                        "audio_binary": None,
                                        "id": None,
                                        "session_id": session_id
                                    }},
                                   ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"
        return
    else:
        session_id = session_id
        e, conv = API4ConversationService.get_by_id(db, session_id)
        assert e, "Session not found!"

    if not conv.message:
        conv.message = []
    messages = conv.message
    question = {
        "role": "user",
        "content": question,
        "id": str(uuid4())
    }
    messages.append(question)

    msg = []
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "assistant" and not msg:
            continue
        msg.append(m)
    if not msg[-1].get("id"):
        msg[-1]["id"] = get_uuid()
    message_id = msg[-1]["id"]

    if not conv.reference:
        conv.reference = []
    conv.reference.append({"chunks": [], "doc_aggs": []})

    if stream:
        try:
            for ans in chat(dia, msg, db, True, **kwargs):
                ans = structure_answer(conv, ans, message_id, session_id)
                yield "data:" + json.dumps({"code": 0, "message": "", "data": ans},
                                           ensure_ascii=False) + "\n\n"
            API4ConversationService.append_message(db, conv.id, conv.to_dict())
        except Exception as e:
            yield "data:" + json.dumps({"code": 500, "message": str(e),
                                        "data": {"answer": "**ERROR**: " + str(e), "reference": []}},
                                       ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"

    else:
        answer = None
        for ans in chat(dia, msg, db, False, **kwargs):
            answer = structure_answer(conv, ans, message_id, session_id)
            API4ConversationService.append_message(db, conv.id, conv.to_dict())
            break
        yield answer


import json
import logging
import time
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db.db_models import Conversation
from api.db.services.api_service import API4ConversationService
from api.db.services.common_service import CommonService
from api.db.services.dialog_service import DialogService, async_chat, chat
from common.constants import StatusEnum
from common.misc_utils import get_uuid
from core.prompts.generator import chunks_format


def _conversation_to_dict(conversation: Any) -> dict[str, Any]:
    if hasattr(conversation, "to_dict"):
        return conversation.to_dict()
    return {
        "id": conversation.id,
        "dialog_id": conversation.dialog_id,
        "name": conversation.name,
        "message": conversation.message,
        "reference": conversation.reference,
        "user_id": conversation.user_id,
        "create_time": conversation.create_time,
        "create_date": conversation.create_date,
        "update_time": conversation.update_time,
        "update_date": conversation.update_date,
    }


class ConversationService(CommonService):
    model = Conversation

    @classmethod
    def get_list(cls, db: Session, dialog_id, page_number, items_per_page, orderby, is_desc, id=None, name=None, user_id=None):
        stmt = select(cls.model).where(cls.model.dialog_id == dialog_id)

        if id:
            stmt = stmt.where(cls.model.id == id)
        if name:
            stmt = stmt.where(cls.model.name == name)
        if user_id:
            stmt = stmt.where(cls.model.user_id == user_id)
        if not hasattr(cls.model, orderby):
            raise ValueError(f"'{orderby}' is not a valid attribute of '{cls.model.__name__}'")
        order_col = getattr(cls.model, orderby)
        stmt = stmt.order_by(order_col.desc() if is_desc else order_col.asc())

        # page_size=0 follows the REST API convention of disabling pagination.
        if items_per_page > 0:
            stmt = stmt.offset((page_number - 1) * items_per_page).limit(items_per_page)

        results = db.scalars(stmt).all()
        return [_conversation_to_dict(item) for item in results]

    @classmethod
    def get_all_conversation_by_dialog_ids(cls, db: Session, dialog_ids: list[str]) -> list[dict]:
        """根据对话ID列表批量查询所有会话记录，使用分页避免内存溢出"""
        if not dialog_ids:
            return []

        stmt = select(cls.model).where(cls.model.dialog_id.in_(dialog_ids)).order_by(cls.model.create_time.asc())

        offset, limit = 0, 100
        res = []

        while True:
            try:
                s_batch = db.scalars(stmt.offset(offset).limit(limit)).all()

                if not s_batch:
                    break

                res.extend(_conversation_to_dict(session) for session in s_batch)
                offset += limit
            except Exception:
                logging.exception("Failed to get conversations for dialog_ids at offset %d", offset)
                break

        return res


def structure_answer(conv, ans, message_id, session_id):
    reference = ans["reference"]
    if not isinstance(reference, dict):
        reference = {}
        ans["reference"] = {}
    is_final = ans.get("final", True)

    chunk_list = chunks_format(reference)

    reference["chunks"] = chunk_list
    ans["id"] = message_id
    ans["session_id"] = session_id

    if not conv:
        return ans

    content = ans["answer"]
    if ans.get("start_to_think"):
        content = "<think>"
    elif ans.get("end_to_think"):
        content = "</think>"

    if not conv.message:
        conv.message = []
    if not conv.message or conv.message[-1].get("role", "") != "assistant":
        conv.message.append({"role": "assistant", "content": content, "created_at": time.time(), "id": message_id})
    else:
        if is_final:
            if ans.get("answer"):
                conv.message[-1] = {"role": "assistant", "content": ans["answer"], "created_at": time.time(), "id": message_id}
            else:
                conv.message[-1]["created_at"] = time.time()
                conv.message[-1]["id"] = message_id
        else:
            conv.message[-1]["content"] = (conv.message[-1].get("content") or "") + content
            conv.message[-1]["created_at"] = time.time()
            conv.message[-1]["id"] = message_id
    if conv.reference:
        should_update_reference = is_final or bool(reference.get("chunks")) or bool(reference.get("doc_aggs"))
        if should_update_reference:
            conv.reference[-1] = reference
    return ans


def completion(db, tenant_id, chat_id, question, name="New session", session_id=None, stream=True, **kwargs):
    assert name, "`name` can not be empty."
    dia = DialogService.query(db, id=chat_id, tenant_id=tenant_id, status=StatusEnum.VALID.value)
    assert dia, "You do not own the chat."

    if not session_id:
        session_id = get_uuid()
        conv = {
            "id": session_id,
            "dialog_id": chat_id,
            "name": name,
            "message": [{"role": "assistant", "content": dia[0].prompt_config.get("prologue"), "created_at": time.time()}],
            "user_id": kwargs.get("user_id", ""),
        }
        ConversationService.save(db, **conv)
        if stream:
            yield (
                "data:"
                + json.dumps(
                    {"code": 0, "message": "", "data": {"answer": conv["message"][0]["content"], "reference": {}, "audio_binary": None, "id": None, "session_id": session_id}}, ensure_ascii=False
                )
                + "\n\n"
            )
            yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"
            return
        else:
            answer = {"answer": conv["message"][0]["content"], "reference": {}, "audio_binary": None, "id": None, "session_id": session_id}
            yield answer
            return

    conv = ConversationService.query(db, id=session_id, dialog_id=chat_id)
    if not conv:
        raise LookupError("Session does not exist")

    conv = conv[0]
    msg = []
    question = {"content": question, "role": "user", "id": str(uuid4())}

    # Propagate runtime attachments so downstream chat flow can resolve file content.
    if isinstance(kwargs.get("files"), list) and kwargs["files"]:
        question["files"] = kwargs["files"]

    conv.message.append(question)
    for m in conv.message:
        if m["role"] == "system":
            continue
        if m["role"] == "assistant" and not msg:
            continue
        msg.append(m)
    message_id = msg[-1].get("id")
    dia = DialogService.get_by_id(db, conv.dialog_id)

    kb_ids = kwargs.get("kb_ids", [])
    dia.kb_ids = list(set(dia.kb_ids + kb_ids))
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
            yield "data:" + json.dumps({"code": 500, "message": str(e), "data": {"answer": "**ERROR**: " + str(e), "reference": []}}, ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"code": 0, "data": True}, ensure_ascii=False) + "\n\n"

    else:
        answer = None
        for ans in chat(dia, msg, db, False, **kwargs):
            answer = structure_answer(conv, ans, message_id, session_id)
            ConversationService.update_by_id(db, conv.id, conv.to_dict())
            break
        yield answer


async def async_completion(db, tenant_id, chat_id, question, name="New session", session_id=None, stream=True, **kwargs):
    """异步版本的 completion"""
    assert name, "`name` can not be empty."
    dia = DialogService.query(db, id=chat_id, tenant_id=tenant_id, status=StatusEnum.VALID.value)
    assert dia, "You do not own the chat."

    if not session_id:
        session_id = get_uuid()
        conv = {
            "id": session_id,
            "dialog_id": chat_id,
            "name": name,
            "message": [{"role": "assistant", "content": dia[0].prompt_config.get("prologue"), "created_at": time.time()}],
            "user_id": kwargs.get("user_id", ""),
        }
        ConversationService.save(db, **conv)
        if stream:
            yield (
                "data:"
                + json.dumps(
                    {"code": 0, "message": "", "data": {"answer": conv["message"][0]["content"], "reference": {}, "audio_binary": None, "id": None, "session_id": session_id}}, ensure_ascii=False
                )
                + "\n\n"
            )
            yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"
            return
        else:
            answer = {"answer": conv["message"][0]["content"], "reference": {}, "audio_binary": None, "id": None, "session_id": session_id}
            yield answer
            return

    conv = ConversationService.query(db, id=session_id, dialog_id=chat_id)
    if not conv:
        raise LookupError("Session does not exist")

    conv = conv[0]
    msg = []
    question = {"content": question, "role": "user", "id": str(uuid4())}

    # Propagate runtime attachments so downstream chat flow can resolve file content.
    if isinstance(kwargs.get("files"), list) and kwargs["files"]:
        question["files"] = kwargs["files"]

    conv.message.append(question)
    for m in conv.message:
        if m["role"] == "system":
            continue
        if m["role"] == "assistant" and not msg:
            continue
        msg.append(m)
    message_id = msg[-1].get("id")
    dia = DialogService.get_by_id(db, conv.dialog_id)

    kb_ids = kwargs.get("kb_ids", [])
    dia.kb_ids = list(set(dia.kb_ids + kb_ids))
    if not conv.reference:
        conv.reference = []
    conv.message.append({"role": "assistant", "content": "", "id": message_id})
    conv.reference.append({"chunks": [], "doc_aggs": []})

    if stream:
        try:
            async for ans in async_chat(dia, msg, db, True, **kwargs):
                ans = structure_answer(conv, ans, message_id, session_id)
                yield "data:" + json.dumps({"code": 0, "data": ans}, ensure_ascii=False) + "\n\n"
            ConversationService.update_by_id(db, conv.id, conv.to_dict())
        except Exception as e:
            yield "data:" + json.dumps({"code": 500, "message": str(e), "data": {"answer": "**ERROR**: " + str(e), "reference": []}}, ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"code": 0, "data": True}, ensure_ascii=False) + "\n\n"

    else:
        answer = None
        async for ans in async_chat(dia, msg, db, False, **kwargs):
            answer = structure_answer(conv, ans, message_id, session_id)
            ConversationService.update_by_id(db, conv.id, conv.to_dict())
            break
        yield answer


def iframe_completion(db, dialog_id, question, session_id=None, stream=True, **kwargs):
    dia = DialogService.get_by_id(db, dialog_id)
    assert dia, "Dialog not found"
    if not session_id:
        session_id = get_uuid()
        conv = {"id": session_id, "dialog_id": dialog_id, "user_id": kwargs.get("user_id", ""), "message": [{"role": "assistant", "content": dia.prompt_config["prologue"], "created_at": time.time()}]}
        API4ConversationService.save(db, **conv)
        yield (
            "data:"
            + json.dumps({"code": 0, "message": "", "data": {"answer": conv["message"][0]["content"], "reference": {}, "audio_binary": None, "id": None, "session_id": session_id}}, ensure_ascii=False)
            + "\n\n"
        )
        yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"
        return
    else:
        session_id = session_id
        conv = API4ConversationService.get_by_id(db, session_id)
        assert conv, "Session not found!"

    if not conv.message:
        conv.message = []
    messages = conv.message
    question = {"role": "user", "content": question, "id": str(uuid4())}

    # Propagate runtime attachments so downstream chat flow can resolve file content.
    if isinstance(kwargs.get("files"), list) and kwargs["files"]:
        question["files"] = kwargs["files"]

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
                yield "data:" + json.dumps({"code": 0, "message": "", "data": ans}, ensure_ascii=False) + "\n\n"
            API4ConversationService.append_message(db, conv.id, conv.to_dict())
        except Exception as e:
            yield "data:" + json.dumps({"code": 500, "message": str(e), "data": {"answer": "**ERROR**: " + str(e), "reference": []}}, ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"

    else:
        answer = None
        for ans in chat(dia, msg, db, False, **kwargs):
            answer = structure_answer(conv, ans, message_id, session_id)
            API4ConversationService.append_message(db, conv.id, conv.to_dict())
            break
        yield answer


async def async_iframe_completion(db, dialog_id, question, session_id=None, stream=True, **kwargs):
    """异步版本的 iframe_completion"""
    dia = DialogService.get_by_id(db, dialog_id)
    assert dia, "Dialog not found"
    if not session_id:
        session_id = get_uuid()
        conv = {"id": session_id, "dialog_id": dialog_id, "user_id": kwargs.get("user_id", ""), "message": [{"role": "assistant", "content": dia.prompt_config["prologue"], "created_at": time.time()}]}
        API4ConversationService.save(db, **conv)
        yield (
            "data:"
            + json.dumps({"code": 0, "message": "", "data": {"answer": conv["message"][0]["content"], "reference": {}, "audio_binary": None, "id": None, "session_id": session_id}}, ensure_ascii=False)
            + "\n\n"
        )
        yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"
        return
    else:
        session_id = session_id
        conv = API4ConversationService.get_by_id(db, session_id)
        assert conv, "Session not found!"

    if not conv.message:
        conv.message = []
    messages = conv.message
    question = {"role": "user", "content": question, "id": str(uuid4())}

    # Propagate runtime attachments so downstream chat flow can resolve file content.
    if isinstance(kwargs.get("files"), list) and kwargs["files"]:
        question["files"] = kwargs["files"]

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
            async for ans in async_chat(dia, msg, db, True, **kwargs):
                ans = structure_answer(conv, ans, message_id, session_id)
                yield "data:" + json.dumps({"code": 0, "message": "", "data": ans}, ensure_ascii=False) + "\n\n"
            API4ConversationService.append_message(db, conv.id, conv.to_dict())
        except Exception as e:
            yield "data:" + json.dumps({"code": 500, "message": str(e), "data": {"answer": "**ERROR**: " + str(e), "reference": []}}, ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"

    else:
        answer = None
        async for ans in async_chat(dia, msg, db, False, **kwargs):
            answer = structure_answer(conv, ans, message_id, session_id)
            API4ConversationService.append_message(db, conv.id, conv.to_dict())
            break
        yield answer

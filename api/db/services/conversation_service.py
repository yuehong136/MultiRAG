import json
import logging
import time
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import Conversation
from api.db.services.api_service import API4ConversationService
from api.db.services.common_service import CommonService
from api.db.services.dialog_service import DialogService, async_chat, chat
from common.constants import StatusEnum
from common.misc_utils import get_uuid
from common.reasoning import strip_reasoning
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


def _rewind_latest_conversation_turn(
    messages: list[dict[str, Any]],
    references: list[dict[str, Any]],
    question: str,
) -> None:
    """Remove exactly the latest completed turn before an explicit regenerate."""

    if len(messages) < 2:
        raise LookupError("No completed turn is available for regeneration")
    user_message, assistant_message = messages[-2:]
    if user_message.get("role") != "user" or assistant_message.get("role") != "assistant" or user_message.get("content") != question:
        raise LookupError("Only the latest matching turn can be regenerated")
    user_id = user_message.get("id")
    assistant_id = assistant_message.get("id")
    if user_id and assistant_id and user_id != assistant_id:
        raise LookupError("The latest conversation turn is inconsistent")
    del messages[-2:]
    if references:
        references.pop()


def _sanitize_assistant_messages(messages: list[dict[str, Any]]) -> None:
    """Remove private reasoning from prompt and persisted assistant history."""

    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = strip_reasoning(content)


def _has_visible_latest_assistant(messages: list[dict[str, Any]]) -> bool:
    if not messages or messages[-1].get("role") != "assistant":
        return False
    content = messages[-1].get("content")
    return isinstance(content, str) and bool(strip_reasoning(content))


def structure_answer(
    conv: Any,
    ans: dict[str, Any],
    message_id: str,
    session_id: str,
    *,
    persist_reasoning: bool = True,
) -> dict[str, Any]:
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
                final_content = ans["answer"] if persist_reasoning else strip_reasoning(ans["answer"])
                conv.message[-1] = {"role": "assistant", "content": final_content, "created_at": time.time(), "id": message_id}
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


async def async_completion(
    db: AsyncSession,
    tenant_id: str,
    chat_id: str,
    question: str,
    name: str = "New session",
    session_id: str | None = None,
    stream: bool = True,
    **kwargs: Any,
) -> AsyncIterator[str | dict[str, Any] | None]:
    """异步版本的 completion(AsyncSession;遗留同步 service 经 run_sync 桥接)"""
    assert name, "`name` can not be empty."
    regenerate = kwargs.pop("regenerate", False) is True
    persist_reasoning = kwargs.pop("persist_reasoning", True) is not False
    require_visible_answer = kwargs.pop("require_visible_answer", False) is True
    if regenerate and not session_id:
        raise LookupError("A session is required for regeneration")
    if regenerate:
        persist_reasoning = False
        require_visible_answer = True
    dia = await db.run_sync(lambda s: DialogService.query(s, id=chat_id, tenant_id=tenant_id, status=StatusEnum.VALID.value))  # TODO(async-phase4)
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
        await db.run_sync(lambda s: ConversationService.save(s, **conv))  # TODO(async-phase4)
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

    conv = await db.run_sync(lambda s: ConversationService.query(s, id=session_id, dialog_id=chat_id))  # TODO(async-phase4)
    if not conv:
        raise LookupError("Session does not exist")

    conv = conv[0]
    messages = deepcopy(conv.message or [])
    references = deepcopy(conv.reference or [])
    if regenerate:
        _rewind_latest_conversation_turn(messages, references, question)
    if not persist_reasoning:
        _sanitize_assistant_messages(messages)
    conv.message = messages
    conv.reference = references
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
    dia = await db.run_sync(lambda s: DialogService.get_by_id(s, conv.dialog_id))  # TODO(async-phase4)

    kb_ids = kwargs.get("kb_ids", [])
    dia.kb_ids = list(set(dia.kb_ids + kb_ids))
    if not conv.reference:
        conv.reference = []
    conv.message.append({"role": "assistant", "content": "", "id": message_id})
    conv.reference.append({"chunks": [], "doc_aggs": []})

    if stream:
        try:
            async for ans in async_chat(dia, msg, db, True, **kwargs):
                ans = structure_answer(
                    conv,
                    ans,
                    message_id,
                    session_id,
                    persist_reasoning=persist_reasoning,
                )
                yield "data:" + json.dumps({"code": 0, "data": ans}, ensure_ascii=False) + "\n\n"
            if require_visible_answer and not _has_visible_latest_assistant(conv.message):
                await db.rollback()
                yield "data:" + json.dumps({"code": 500, "message": "No visible answer", "data": False}, ensure_ascii=False) + "\n\n"
                return
            if not persist_reasoning:
                _sanitize_assistant_messages(conv.message)
            await db.run_sync(lambda s: ConversationService.update_by_id(s, conv.id, conv.to_dict()))  # TODO(async-phase4)
        except Exception as e:
            await db.rollback()
            yield "data:" + json.dumps({"code": 500, "message": str(e), "data": {"answer": "**ERROR**: " + str(e), "reference": []}}, ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"code": 0, "data": True}, ensure_ascii=False) + "\n\n"

    else:
        answer = None
        async for ans in async_chat(dia, msg, db, False, **kwargs):
            answer = structure_answer(
                conv,
                ans,
                message_id,
                session_id,
                persist_reasoning=persist_reasoning,
            )
            if require_visible_answer and not _has_visible_latest_assistant(conv.message):
                await db.rollback()
                yield None
                return
            if not persist_reasoning:
                _sanitize_assistant_messages(conv.message)
            await db.run_sync(lambda s: ConversationService.update_by_id(s, conv.id, conv.to_dict()))  # TODO(async-phase4)
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


async def async_iframe_completion(db: AsyncSession, dialog_id, question, session_id=None, stream=True, **kwargs):
    """异步版本的 iframe_completion(AsyncSession;遗留同步 service 经 run_sync 桥接)"""
    dia = await db.run_sync(lambda s: DialogService.get_by_id(s, dialog_id))  # TODO(async-phase4)
    assert dia, "Dialog not found"
    if not session_id:
        session_id = get_uuid()
        conv = {"id": session_id, "dialog_id": dialog_id, "user_id": kwargs.get("user_id", ""), "message": [{"role": "assistant", "content": dia.prompt_config["prologue"], "created_at": time.time()}]}
        await db.run_sync(lambda s: API4ConversationService.save(s, **conv))  # TODO(async-phase4)
        yield (
            "data:"
            + json.dumps({"code": 0, "message": "", "data": {"answer": conv["message"][0]["content"], "reference": {}, "audio_binary": None, "id": None, "session_id": session_id}}, ensure_ascii=False)
            + "\n\n"
        )
        yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"
        return
    else:
        session_id = session_id
        conv = await db.run_sync(lambda s: API4ConversationService.get_by_id(s, session_id))  # TODO(async-phase4)
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
            await db.run_sync(lambda s: API4ConversationService.append_message(s, conv.id, conv.to_dict()))  # TODO(async-phase4)
        except Exception as e:
            yield "data:" + json.dumps({"code": 500, "message": str(e), "data": {"answer": "**ERROR**: " + str(e), "reference": []}}, ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"

    else:
        answer = None
        async for ans in async_chat(dia, msg, db, False, **kwargs):
            answer = structure_answer(conv, ans, message_id, session_id)
            await db.run_sync(lambda s: API4ConversationService.append_message(s, conv.id, conv.to_dict()))  # TODO(async-phase4)
            break
        yield answer

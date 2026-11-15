#
#  Copyright 2025 The MultiRAG Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import base64
import binascii
import hashlib
import json
import sys
from typing import Any, TypedDict

from common import settings
from common.constants import MemoryType
from common.doc_store.doc_store_base import MatchExpr, OrderByExpr
from memory.revisions import gate
from memory.revisions.domain import RevisionConflict

MAX_RECENT_MESSAGES_SCAN = 10_000
MAX_RECENT_MESSAGES_PAGE_SIZE = 100
MAX_SOURCE_MESSAGES_BATCH_SIZE = 100

_MESSAGE_FIELDS = [
    "message_id",
    "message_type",
    "source_id",
    "memory_id",
    "user_id",
    "agent_id",
    "session_id",
    "valid_at",
    "invalid_at",
    "forget_at",
    "status",
    "content",
]


class RecentMessageReadLimitError(ValueError):
    """A complete candidate set cannot be read within the bounded scan."""


class RecentMessageCursorStaleError(ValueError):
    """The cursor's checked messages changed since the preceding page was read."""


class SourceMessageBindingError(ValueError):
    """A stored message does not match the requested memory/message identity."""


class RecentMessagePage(TypedDict):
    messages: list[dict[str, Any]]
    next_cursor: str | None


def _recent_message_condition(
    agent_id: str,
    session_id: str,
    *,
    message_type: str | None,
    user_id: str | None,
    status: bool | None,
) -> dict[str, Any]:
    condition: dict[str, Any] = {"agent_id": agent_id, "session_id": session_id}
    if message_type is not None:
        if message_type not in {kind.name.lower() for kind in MemoryType}:
            raise ValueError("Unsupported message type")
        condition["message_type"] = message_type
    if user_id is not None:
        # Lists also preserve an explicit empty attribution on stores that skip falsy scalars.
        condition["user_id"] = [user_id]
    if status is not None:
        if not isinstance(status, bool):
            raise ValueError("Message status must be a boolean")
        # Several connectors skip scalar zero, whereas the nonempty IN condition is retained.
        condition["status"] = [int(status)]
    return condition


def _message_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SourceMessageBindingError("Stored message has an invalid message ID")
    try:
        message_id = int(value)
    except ValueError as exc:
        raise SourceMessageBindingError("Stored message has an invalid message ID") from exc
    if message_id <= 0:
        raise SourceMessageBindingError("Stored message has an invalid message ID")
    return message_id


def _message_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decode_recent_cursor(cursor: str, query_digest: str) -> tuple[int, str]:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
        raise ValueError("Invalid recent-message cursor")
    try:
        payload = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("Invalid recent-message cursor") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"v", "query", "messages", "offset"}
        or type(payload["v"]) is not int
        or payload["v"] != 1
        or payload["query"] != query_digest
        or not isinstance(payload["messages"], str)
        or len(payload["messages"]) != 64
        or any(character not in "0123456789abcdef" for character in payload["messages"])
        or type(payload["offset"]) is not int
        or not 0 < payload["offset"] < MAX_RECENT_MESSAGES_SCAN
    ):
        raise ValueError("Invalid recent-message cursor")
    return payload["offset"], payload["messages"]


def _encode_recent_cursor(query_digest: str, messages_digest: str, offset: int) -> str:
    payload = {"v": 1, "query": query_digest, "messages": messages_digest, "offset": offset}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _recent_anchor(message: dict[str, Any]) -> dict[str, Any]:
    valid_at = message.get("valid_at")
    if not isinstance(valid_at, str) or len(valid_at) > 64:
        raise SourceMessageBindingError("Stored message has an invalid sort timestamp")
    message_id = _message_id(message.get("message_id"))
    if message_id > 2**63 - 1:
        raise SourceMessageBindingError("Stored message has an invalid message ID")
    return {"id": message_id, "time": valid_at, "digest": _message_digest(message)}


def _decode_seek_cursor(cursor: str, query_digest: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 1024:
        raise ValueError("Invalid recent-message cursor")
    try:
        payload = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("Invalid recent-message cursor") from exc
    if not isinstance(payload, dict) or set(payload) != {"v", "query", "first", "last"} or type(payload["v"]) is not int or payload["v"] != 2 or payload["query"] != query_digest:
        raise ValueError("Invalid recent-message cursor")
    for name in ("first", "last"):
        anchor = payload[name]
        if (
            not isinstance(anchor, dict)
            or set(anchor) != {"id", "time", "digest"}
            or type(anchor["id"]) is not int
            or not 0 < anchor["id"] <= 2**63 - 1
            or not isinstance(anchor["time"], str)
            or len(anchor["time"]) > 64
            or not isinstance(anchor["digest"], str)
            or len(anchor["digest"]) != 64
            or any(character not in "0123456789abcdef" for character in anchor["digest"])
        ):
            raise ValueError("Invalid recent-message cursor")
    if (payload["first"]["time"], payload["first"]["id"]) < (payload["last"]["time"], payload["last"]["id"]):
        raise ValueError("Invalid recent-message cursor")
    return payload["first"], payload["last"]


def _encode_seek_cursor(query_digest: str, first: dict[str, Any], last: dict[str, Any]) -> str:
    payload = {"v": 2, "query": query_digest, "first": first, "last": last}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _matches_recent_condition(message: dict[str, Any], condition: dict[str, Any]) -> bool:
    for field, value in condition.items():
        if field in {"agent_id", "session_id"} and not value:
            continue
        if field == "status":
            if message.get(field) not in ((1, True, "1") if value == [1] else (0, False, "0")):
                return False
        elif isinstance(value, list):
            if message.get(field) not in value:
                return False
        elif message.get(field) != value:
            return False
    return message.get("forget_at") in (None, "", "-")


def index_name(uid: str) -> str:
    return f"memory_{uid}"


def _search_normal_messages(tenant_ids: list[str], bank_ids: list[str], *, strict: bool = False, **kwargs: Any) -> tuple[Any, int]:
    eligibility = gate.managed_query_exclusions(tenant_ids, bank_ids)
    search = settings.msgStoreConn.search
    if eligibility:
        search = getattr(settings.msgStoreConn, "search_managed", None)
        if not callable(search):
            raise RevisionConflict("managed_query_backend_unsupported")
        kwargs["eligibility_by_memory"] = eligibility
    elif strict:
        strict_search = getattr(settings.msgStoreConn, "search_strict", None)
        if callable(strict_search):
            search = strict_search
    return search(**kwargs)


def _supply_messages(tenant_ids: list[str], bank_ids: list[str], messages: list[dict[str, Any]], *, reject_changed: bool = False) -> list[dict[str, Any]]:
    served = gate.filter_messages(tenant_ids, bank_ids, messages)
    if reject_changed and len(served) != len(messages):
        raise RevisionConflict("managed_candidates_changed_during_read")
    return served


class MessageService:
    @classmethod
    def get_recent_read_capabilities(cls, *, tenant_id: str | None = None, memory_id: str | None = None) -> dict[str, Any]:
        """Describe the active connector's recent-page semantics without reading messages."""
        from api.db.joint_services.memory_write_runtime import supports_coordinated_writes

        supports_native = getattr(settings.msgStoreConn, "supports_recent_message_query", None)
        managed = bool(tenant_id and memory_id and gate.is_managed(tenant_id, memory_id))
        guarded = bool(tenant_id and memory_id and gate.requires_guarded_read(tenant_id, memory_id))
        native = not guarded and callable(supports_native) and supports_native() is True
        supported = all(callable(getattr(settings.msgStoreConn, name, None)) for name in ("query_source_rows", "query_management_rows", "search_managed"))
        coordinated = supported and supports_coordinated_writes()
        return {
            "pagination": "live_seek_cursor" if native else "bounded_scan_cursor",
            "max_page_size": MAX_RECENT_MESSAGES_PAGE_SIZE,
            "max_scan_messages": None if native else MAX_RECENT_MESSAGES_SCAN,
            "change_detection": "first_last_boundary" if native else "full_candidate_digest",
            "snapshot_isolation": False,
            "managed": managed,
            "activation_scope": "memory_bank",
            "legacy_writes_supported": not managed,
            "managed_revisions_supported": supported,
            "coordinated_writes_supported": coordinated,
            "managed_delete_supported": coordinated,
            "managed_delete_blocked_reason": None if coordinated else "backend_missing_coordinated_writer_capabilities",
        }

    @classmethod
    def has_index(cls, uid: str, memory_id: str):
        index = index_name(uid)
        return settings.msgStoreConn.index_exist(index, memory_id)

    @classmethod
    def create_index(cls, uid: str, memory_id: str, vector_size: int):
        gate.assert_legacy_write_allowed(uid, memory_id)
        index = index_name(uid)
        return settings.msgStoreConn.create_idx(index, memory_id, vector_size)

    @classmethod
    def delete_index(cls, uid: str, memory_id: str):
        gate.assert_legacy_write_allowed(uid, memory_id)
        index = index_name(uid)
        return settings.msgStoreConn.delete_idx(index, memory_id)

    @classmethod
    def insert_message(cls, messages: list[dict], uid: str, memory_id: str):
        gate.assert_legacy_write_allowed(uid, memory_id)
        index = index_name(uid)
        [m.update({"id": f"{memory_id}_{m['message_id']}", "status": 1 if m["status"] else 0}) for m in messages]
        return settings.msgStoreConn.insert(messages, index, memory_id)

    @classmethod
    def update_message(cls, condition: dict, update_dict: dict, uid: str, memory_id: str):
        gate.assert_legacy_write_allowed(uid, memory_id)
        index = index_name(uid)
        if "status" in update_dict:
            update_dict["status"] = 1 if update_dict["status"] else 0
        return settings.msgStoreConn.update(condition, update_dict, index, memory_id)

    @classmethod
    def delete_message(cls, condition: dict, uid: str, memory_id: str):
        gate.assert_legacy_write_allowed(uid, memory_id)
        index = index_name(uid)
        return settings.msgStoreConn.delete(condition, index, memory_id)

    @classmethod
    def list_message(
        cls,
        uid: str,
        memory_id: str,
        agent_ids: list[str] | None = None,
        keywords: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ):
        """
        List messages from memory with pagination.

        Returns raw messages with their extracted content attached.
        """
        index = index_name(uid)
        managed = gate.is_managed(uid, memory_id)
        management_query = getattr(settings.msgStoreConn, "search_strict", None) if managed else settings.msgStoreConn.search
        if not callable(management_query):
            raise RevisionConflict("managed_management_read_unsupported")
        filter_dict: dict[str, Any] = {}
        if agent_ids:
            filter_dict["agent_id"] = agent_ids
        if keywords:
            filter_dict["session_id"] = keywords

        select_fields = [
            "message_id",
            "message_type",
            "source_id",
            "memory_id",
            "user_id",
            "agent_id",
            "session_id",
            "valid_at",
            "invalid_at",
            "forget_at",
            "status",
        ]
        extract_select_fields = [*select_fields, "content"]
        order_by = OrderByExpr()
        order_by.desc("valid_at")

        # Query raw messages only (message_type == "raw")
        res, total_count = management_query(
            select_fields=select_fields,
            highlight_fields=[],
            condition={**filter_dict, "message_type": MemoryType.RAW.name.lower()},
            match_expressions=[],
            order_by=order_by,
            offset=(page - 1) * page_size,
            limit=page_size,
            index_names=index,
            memory_ids=[memory_id],
            agg_fields=[],
            hide_forgotten=False,
        )
        if not total_count:
            return {"message_list": [], "total_count": 0}

        raw_msg_mapping = settings.msgStoreConn.get_fields(res, select_fields)
        raw_messages = gate.filter_messages([uid], [memory_id], list(raw_msg_mapping.values()), management_metadata=True)
        if not raw_messages:
            return {"message_list": [], "total_count": 0, "total_count_is_exact": False}

        # Query extracted messages for the raw messages
        extract_filter = {"source_id": [r["message_id"] for r in raw_messages]}
        extract_res, extract_count = management_query(
            select_fields=extract_select_fields,
            highlight_fields=[],
            condition=extract_filter,
            match_expressions=[],
            order_by=order_by,
            offset=0,
            limit=MAX_RECENT_MESSAGES_SCAN if managed else 512,
            index_names=index,
            memory_ids=[memory_id],
            agg_fields=[],
            hide_forgotten=False,
        )
        extract_msg = settings.msgStoreConn.get_fields(extract_res, extract_select_fields)
        if managed and (len(extract_msg) >= MAX_RECENT_MESSAGES_SCAN or extract_count > len(extract_msg)):
            raise RecentMessageReadLimitError("Management extract scan is incomplete; narrow the source selection")

        # Group extracted messages by source_id
        grouped_extract_msg: dict[int, list[dict[str, Any]]] = {}
        for msg in gate.filter_messages([uid], [memory_id], list(extract_msg.values()), management_body=True):
            source_id = msg.get("source_id")
            if source_id:
                if source_id not in grouped_extract_msg:
                    grouped_extract_msg[source_id] = []
                grouped_extract_msg[source_id].append(msg)

        # Attach extracted messages to raw messages
        for raw_msg in raw_messages:
            raw_msg["extract"] = grouped_extract_msg.get(raw_msg["message_id"], [])

        return {"message_list": raw_messages, "total_count": total_count}

    @classmethod
    def get_recent_messages(
        cls,
        uid_list: list[str],
        memory_ids: list[str],
        agent_id: str,
        session_id: str,
        limit: int,
        *,
        message_type: str | None = None,
        user_id: str | None = None,
        status: bool | None = None,
    ) -> list[dict[str, Any]]:
        index_names = [index_name(uid) for uid in uid_list]
        condition_dict = _recent_message_condition(agent_id, session_id, message_type=message_type, user_id=user_id, status=status)
        order_by = OrderByExpr()
        order_by.desc("valid_at")
        res, total_count = _search_normal_messages(
            uid_list,
            memory_ids,
            select_fields=[
                "message_id",
                "message_type",
                "source_id",
                "memory_id",
                "user_id",
                "agent_id",
                "session_id",
                "valid_at",
                "invalid_at",
                "forget_at",
                "status",
                "content",
            ],
            highlight_fields=[],
            condition=condition_dict,
            match_expressions=[],
            order_by=order_by,
            offset=0,
            limit=limit,
            index_names=index_names,
            memory_ids=memory_ids,
            agg_fields=[],
        )
        if not total_count:
            return []

        doc_mapping = settings.msgStoreConn.get_fields(
            res,
            [
                "message_id",
                "message_type",
                "source_id",
                "memory_id",
                "user_id",
                "agent_id",
                "session_id",
                "valid_at",
                "invalid_at",
                "forget_at",
                "status",
                "content",
            ],
        )
        return _supply_messages(uid_list, memory_ids, list(doc_mapping.values()), reject_changed=True)

    @classmethod
    def get_recent_page(
        cls,
        uid: str,
        memory_id: str,
        agent_id: str,
        session_id: str,
        limit: int,
        *,
        message_type: str | None = None,
        user_id: str | None = None,
        status: bool | None = None,
        cursor: str | None = None,
    ) -> RecentMessagePage:
        """Page one authorized memory using native live seek or a bounded legacy read.

        Native pages check first/last boundary anchors; the legacy scan checks the full
        candidate digest. Neither mode retains a cross-request snapshot.
        """
        if not uid or not memory_id:
            raise ValueError("A tenant and memory are required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RECENT_MESSAGES_PAGE_SIZE:
            raise ValueError(f"Page size must be between 1 and {MAX_RECENT_MESSAGES_PAGE_SIZE}")
        condition = _recent_message_condition(agent_id, session_id, message_type=message_type, user_id=user_id, status=status)
        query_digest = _message_digest({"tenant": uid, "memory": memory_id, "condition": condition, "limit": limit})
        if cls.get_recent_read_capabilities(tenant_id=uid, memory_id=memory_id)["pagination"] == "live_seek_cursor":
            return cls._get_recent_seek_page(uid, memory_id, condition, limit, cursor, query_digest)
        offset, expected_digest = _decode_recent_cursor(cursor, query_digest) if cursor is not None else (0, None)
        order_by = OrderByExpr().desc("valid_at").desc("message_id")
        candidate_query = getattr(settings.msgStoreConn, "query_recent_candidates", None)
        if callable(candidate_query):
            result = candidate_query(select_fields=_MESSAGE_FIELDS.copy(), condition=condition, index_name=index_name(uid), memory_id=memory_id, limit=MAX_RECENT_MESSAGES_SCAN)
            total_count = len(result)
        else:
            result, total_count = settings.msgStoreConn.search(
                select_fields=_MESSAGE_FIELDS.copy(),
                highlight_fields=[],
                condition=condition,
                match_expressions=[],
                order_by=order_by,
                offset=0,
                limit=MAX_RECENT_MESSAGES_SCAN,
                index_names=[index_name(uid)],
                memory_ids=[memory_id],
                agg_fields=[],
            )
        messages = list(settings.msgStoreConn.get_fields(result, _MESSAGE_FIELDS.copy()).values()) if result is not None else []
        if len(messages) >= MAX_RECENT_MESSAGES_SCAN or total_count > len(messages):
            raise RecentMessageReadLimitError("Recent-message scan is incomplete; narrow the memory, agent, session, user, or message-type scope")
        seen_ids: set[int] = set()
        for message in messages:
            message_id = _message_id(message.get("message_id"))
            if message.get("memory_id") != memory_id or message_id in seen_ids:
                raise SourceMessageBindingError("Stored recent messages do not match the requested memory identity")
            seen_ids.add(message_id)
        # Some connectors use substring session matching. The complete bounded read
        # lets the new page contract apply exact selectors without changing those adapters.
        messages = [
            message
            for message in messages
            if (not agent_id or message.get("agent_id") == agent_id)
            and (not session_id or message.get("session_id") == session_id)
            and (message_type is None or message.get("message_type") == message_type)
            and (user_id is None or message.get("user_id") == user_id)
            and (status is None or message.get("status") in ((1, True, "1") if status else (0, False, "0")))
        ]
        messages = _supply_messages([uid], [memory_id], messages)
        messages.sort(key=lambda message: (str(message.get("valid_at") or ""), _message_id(message["message_id"])), reverse=True)
        messages_digest = _message_digest(messages)
        if expected_digest is not None and expected_digest != messages_digest:
            raise RecentMessageCursorStaleError("Recent messages changed; restart pagination")
        selected = messages[offset : offset + limit]
        next_offset = offset + len(selected)
        next_cursor = _encode_recent_cursor(query_digest, messages_digest, next_offset) if next_offset < len(messages) else None
        return {"messages": selected, "next_cursor": next_cursor}

    @classmethod
    def _get_recent_seek_page(cls, uid: str, memory_id: str, condition: dict[str, Any], limit: int, cursor: str | None, query_digest: str) -> RecentMessagePage:
        first, last = _decode_seek_cursor(cursor, query_digest) if cursor is not None else (None, None)

        def read_rows(filters: dict[str, Any], count: int, before: tuple[str, int] | None = None) -> list[dict[str, Any]]:
            result = settings.msgStoreConn.query_recent_rows(select_fields=_MESSAGE_FIELDS.copy(), condition=filters, index_name=index_name(uid), memory_id=memory_id, limit=count, before=before)
            rows = list(settings.msgStoreConn.get_fields(result, _MESSAGE_FIELDS.copy()).values())
            if len(rows) > count:
                raise SourceMessageBindingError("Stored recent messages exceed the requested page")
            seen_ids: set[int] = set()
            keys: list[tuple[str, int]] = []
            for row in rows:
                anchor = _recent_anchor(row)
                if row.get("memory_id") != memory_id or anchor["id"] in seen_ids or not _matches_recent_condition(row, filters):
                    raise SourceMessageBindingError("Stored recent messages do not match the requested memory scope")
                seen_ids.add(anchor["id"])
                key = (anchor["time"], anchor["id"])
                if before is not None and key >= before:
                    raise SourceMessageBindingError("Stored recent message is outside the requested page")
                keys.append(key)
            if keys != sorted(keys, reverse=True):
                raise SourceMessageBindingError("Stored recent messages are not in the requested order")
            return _supply_messages([uid], [memory_id], rows, reject_changed=True)

        if first is not None and last is not None:
            anchor_ids = list(dict.fromkeys([first["id"], last["id"]]))
            anchors = read_rows({**condition, "message_id": anchor_ids}, len(anchor_ids))
            actual = {_message_id(row["message_id"]): _recent_anchor(row) for row in anchors}
            if actual.get(first["id"]) != first or actual.get(last["id"]) != last:
                raise RecentMessageCursorStaleError("Recent-message boundary changed; restart pagination")
        before = (last["time"], last["id"]) if last is not None else None
        rows = read_rows(condition, limit + 1, before)
        selected = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = _encode_seek_cursor(query_digest, first or _recent_anchor(selected[0]), _recent_anchor(selected[-1]))
        return {"messages": selected, "next_cursor": next_cursor}

    @classmethod
    def get_source_messages(cls, uid: str, memory_id: str, message_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Read bounded source candidates; the caller checks source type and subject scope.

        Missing rows are omitted. Identity mismatches and storage failures propagate,
        so callers cannot misrepresent a failed or foreign read as an absent source.
        """
        if not uid or not memory_id:
            raise ValueError("A tenant and memory are required")
        if len(message_ids) > MAX_SOURCE_MESSAGES_BATCH_SIZE:
            raise ValueError(f"At most {MAX_SOURCE_MESSAGES_BATCH_SIZE} source messages may be read")
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in message_ids):
            raise ValueError("Source message IDs must be positive integers")
        unique_ids = list(dict.fromkeys(message_ids))
        if not unique_ids:
            return {}
        sources: dict[int, dict[str, Any]] = {}
        source_query = getattr(settings.msgStoreConn, "query_source_rows", None)
        if callable(source_query):
            result = source_query(select_fields=_MESSAGE_FIELDS.copy(), message_ids=unique_ids, index_name=index_name(uid), memory_id=memory_id)
            messages = list(settings.msgStoreConn.get_fields(result, _MESSAGE_FIELDS.copy()).values())
            for message in messages:
                message_id = _message_id(message.get("message_id"))
                if message.get("memory_id") != memory_id or message_id not in unique_ids or message_id in sources:
                    raise SourceMessageBindingError("Stored source does not match the requested memory/message identity")
                sources[message_id] = {field: message[field] for field in _MESSAGE_FIELDS if field in message}
            return {int(message["message_id"]): message for message in _supply_messages([uid], [memory_id], list(sources.values()))}
        for message_id in unique_ids:
            message = cls.get_by_message_id(memory_id, message_id, uid)
            if not message:
                continue
            if message.get("memory_id") != memory_id or _message_id(message.get("message_id")) != message_id:
                raise SourceMessageBindingError("Stored source does not match the requested memory/message identity")
            sources[message_id] = {field: message[field] for field in _MESSAGE_FIELDS if field in message}
        return {int(message["message_id"]): message for message in _supply_messages([uid], [memory_id], list(sources.values()))}

    @classmethod
    def search_message(
        cls,
        memory_ids: list[str],
        condition_dict: dict[str, Any],
        uid_list: list[str],
        match_expressions: list[MatchExpr],
        top_n: int,
        *,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        index_names = [index_name(uid) for uid in uid_list]
        # filter only valid messages by default
        if "status" not in condition_dict:
            condition_dict["status"] = 1

        order_by = OrderByExpr()
        order_by.desc("valid_at")
        res, total_count = _search_normal_messages(
            uid_list,
            memory_ids,
            strict=strict,
            select_fields=[
                "message_id",
                "message_type",
                "source_id",
                "memory_id",
                "user_id",
                "agent_id",
                "session_id",
                "valid_at",
                "invalid_at",
                "forget_at",
                "status",
                "content",
            ],
            highlight_fields=[],
            condition=condition_dict,
            match_expressions=match_expressions,
            order_by=order_by,
            offset=0,
            limit=top_n,
            index_names=index_names,
            memory_ids=memory_ids,
            agg_fields=[],
        )
        if not total_count:
            return []

        docs = settings.msgStoreConn.get_fields(
            res,
            [
                "message_id",
                "message_type",
                "source_id",
                "memory_id",
                "user_id",
                "agent_id",
                "session_id",
                "valid_at",
                "invalid_at",
                "forget_at",
                "status",
                "content",
            ],
        )
        return _supply_messages(uid_list, memory_ids, list(docs.values()), reject_changed=True)

    @staticmethod
    def calculate_message_size(message: dict):
        content = message.get("content") or ""
        content_embed = message.get("content_embed")
        if content_embed is None:
            content_embed = []
        elif hasattr(content_embed, "tolist"):
            content_embed = content_embed.tolist()
        if not content_embed:
            return sys.getsizeof(content)
        return sys.getsizeof(content) + sys.getsizeof(content_embed[0]) * len(content_embed)

    @classmethod
    def calculate_memory_size(cls, memory_ids: list[str], uid_list: list[str]):
        index_names = [index_name(uid) for uid in uid_list]
        order_by = OrderByExpr()
        order_by.desc("valid_at")

        res, count = settings.msgStoreConn.search(
            select_fields=["memory_id", "content", "content_embed"],
            highlight_fields=[],
            condition={},
            match_expressions=[],
            order_by=order_by,
            offset=0,
            limit=2048 * len(memory_ids),
            index_names=index_names,
            memory_ids=memory_ids,
            agg_fields=[],
            hide_forgotten=False,
        )
        if count == 0:
            return {}

        docs = settings.msgStoreConn.get_fields(res, ["memory_id", "content", "content_embed"])
        size_dict: dict[str, int] = {}
        for doc in docs.values():
            if size_dict.get(doc["memory_id"]):
                size_dict[doc["memory_id"]] += cls.calculate_message_size(doc)
            else:
                size_dict[doc["memory_id"]] = cls.calculate_message_size(doc)
        return size_dict

    @classmethod
    def pick_messages_to_delete_by_fifo(cls, memory_id: str, uid: str, size_to_delete: int):
        gate.assert_legacy_write_allowed(uid, memory_id)
        select_fields = ["message_id", "content", "content_embed"]
        _index_name = index_name(uid)

        # Get forgotten messages first using get_forgotten_messages
        res = settings.msgStoreConn.get_forgotten_messages(select_fields, _index_name, memory_id)
        current_size = 0
        ids_to_remove = []
        if res:
            message_list = settings.msgStoreConn.get_fields(res, select_fields)
            for message in message_list.values():
                if current_size < size_to_delete:
                    current_size += cls.calculate_message_size(message)
                    ids_to_remove.append(message["message_id"])
                else:
                    return ids_to_remove, current_size
            if current_size >= size_to_delete:
                return ids_to_remove, current_size

        order_by = OrderByExpr()
        order_by.asc("valid_at")
        res, total_count = settings.msgStoreConn.search(
            select_fields=select_fields,
            highlight_fields=[],
            condition={},
            match_expressions=[],
            order_by=order_by,
            offset=0,
            limit=512,
            index_names=[_index_name],
            memory_ids=[memory_id],
            agg_fields=[],
        )
        docs = settings.msgStoreConn.get_fields(res, select_fields)
        for doc in docs.values():
            if current_size < size_to_delete:
                current_size += cls.calculate_message_size(doc)
                ids_to_remove.append(doc["message_id"])
            else:
                return ids_to_remove, current_size
        return ids_to_remove, current_size

    @classmethod
    def get_missing_field_messages(cls, memory_id: str, uid: str, field_name: str):
        gate.assert_legacy_write_allowed(uid, memory_id)
        select_fields = ["message_id", "content"]
        _index_name = index_name(uid)
        res = settings.msgStoreConn.get_missing_field_message(select_fields=select_fields, index_name=_index_name, memory_id=memory_id, field_name=field_name)
        if not res:
            return []
        docs = settings.msgStoreConn.get_fields(res, select_fields)
        return list(docs.values())

    @classmethod
    def get_by_message_id(cls, memory_id: str, message_id: int, uid: str):
        if gate.is_managed(uid, memory_id):
            messages = cls._read_managed_physical(memory_id, message_id, uid)
            served = _supply_messages([uid], [memory_id], messages)
            return served[0] if served else None
        index = index_name(uid)
        doc_id = f"{memory_id}_{message_id}"
        message = settings.msgStoreConn.get(doc_id, index, [memory_id])
        messages = _supply_messages([uid], [memory_id], [message]) if message is not None else []
        return messages[0] if messages else None

    @classmethod
    def _read_managed_physical(cls, memory_id: str, message_id: int, uid: str) -> list[dict[str, Any]]:
        query = getattr(settings.msgStoreConn, "query_source_rows", None)
        if not callable(query):
            raise RevisionConflict("managed_read_backend_unsupported")
        fields = [*_MESSAGE_FIELDS, "content_embed", "id"]
        rows = list(settings.msgStoreConn.get_fields(query(fields, [message_id], index_name(uid), memory_id), fields).values())
        if any(row.get("memory_id") != memory_id or _message_id(row.get("message_id")) != message_id for row in rows) or len(rows) > 1:
            raise SourceMessageBindingError("Managed physical read returned an unrequested message")
        return rows

    @classmethod
    def get_management_message(cls, memory_id: str, message_id: int, uid: str) -> dict[str, Any] | None:
        """Caller-authorized management view; permanent source withdrawal still hides text."""
        if not gate.is_managed(uid, memory_id):
            return cls.get_by_message_id(memory_id, message_id, uid)
        rows = gate.filter_messages([uid], [memory_id], cls._read_managed_physical(memory_id, message_id, uid), management_body=True)
        return rows[0] if rows else None

    @classmethod
    def get_max_message_id(cls, uid_list: list[str], memory_ids: list[str]):
        order_by = OrderByExpr()
        order_by.desc("message_id")
        index_names = [index_name(uid) for uid in uid_list]
        res, total_count = settings.msgStoreConn.search(
            select_fields=["message_id"],
            highlight_fields=[],
            condition={},
            match_expressions=[],
            order_by=order_by,
            offset=0,
            limit=1,
            index_names=index_names,
            memory_ids=memory_ids,
            agg_fields=[],
            hide_forgotten=False,
        )
        if not total_count:
            return 1

        docs = settings.msgStoreConn.get_fields(res, ["message_id"])
        if not docs:
            return 1
        else:
            latest_msg = next(iter(docs.values()))
            return int(latest_msg["message_id"])

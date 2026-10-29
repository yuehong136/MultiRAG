"""
@project: multirag
@Author：龙
@file： memory_api_service.py
@date：2026/03/18
@desc: Memory API 业务逻辑层 - 从gateway层解耦的业务处理
"""

import asyncio
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.constants import MEMORY_NAME_LIMIT, MEMORY_SIZE_LIMIT
from api.db.db_models import Memory, db_connection
from api.db.joint_services.memory_message_service import (
    get_memory_size_cache,
    judge_system_prompt_is_default,
    query_message,
    queue_save_to_memory_task,
)
from api.db.services.canvas_service import UserCanvasService
from api.db.services.memory_service import MemoryService
from api.db.services.task_service import TaskService
from api.utils.memory_utils import format_ret_data_from_memory, get_memory_type_human
from api.utils.tenant_utils import ensure_tenant_model_id_for_params
from common.constants import ForgettingPolicy, MemoryType, TenantPermission
from common.exceptions import ArgumentException, NotFoundException
from common.time_utils import current_timestamp, timestamp_to_date
from memory.services.messages import MessageService
from memory.utils.prompt_util import PromptAssembler


def _split_filter_values(values: str | list[str] | None) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    return list(dict.fromkeys(part.strip() for value in values for part in value.split(",") if part.strip()))


def _accessible_memories(db: Session, user_id: str, memory_ids: list[str]) -> list[Memory]:
    if not memory_ids:
        return []
    tenant_ids = MemoryService.accessible_tenant_ids(db, user_id)
    return [memory for memory in MemoryService.get_by_ids(db, memory_ids) if memory.tenant_id == user_id or (memory.permissions == TenantPermission.TEAM.value and memory.tenant_id in tenant_ids)]


def _require_memory_access(db: Session, user_id: str, memory_id: str) -> Memory:
    memories = _accessible_memories(db, user_id, [memory_id])
    if not memories:
        raise NotFoundException(f"Memory '{memory_id}' not found.")
    return memories[0]


def create_memory(db: Session, tenant_id: str, memory_info: dict) -> tuple[bool, Any]:
    """
    创建Memory

    :param db: 数据库会话
    :param tenant_id: 租户ID
    :param memory_info: {
        "name": str,
        "memory_type": list[str],
        "embd_id": str,
        "llm_id": str,
        "tenant_embd_id": str,
        "tenant_llm_id": str
    }
    """
    name = memory_info["name"].strip()
    if len(name) == 0:
        raise ArgumentException("Memory name cannot be empty or whitespace.")
    if len(name) > MEMORY_NAME_LIMIT:
        raise ArgumentException(f"Memory name '{name}' exceeds limit of {MEMORY_NAME_LIMIT}.")

    if not isinstance(memory_info["memory_type"], list):
        raise ArgumentException("Memory type must be a list.")
    memory_type_set = set(memory_info["memory_type"])
    valid_types = {e.name.lower() for e in MemoryType}
    invalid_types = memory_type_set - valid_types
    if invalid_types:
        raise ArgumentException(f"Memory type '{invalid_types}' is not supported.")

    memory_info = ensure_tenant_model_id_for_params(db, tenant_id, dict(memory_info))

    success, result = MemoryService.create_memory(
        db=db,
        tenant_id=tenant_id,
        name=name,
        memory_type=list(memory_type_set),
        embd_id=memory_info["embd_id"],
        llm_id=memory_info["llm_id"],
        tenant_embd_id=memory_info.get("tenant_embd_id"),
        tenant_llm_id=memory_info.get("tenant_llm_id"),
    )

    if success:
        return True, format_ret_data_from_memory(result)
    else:
        return False, result


def update_memory(db: Session, memory_id: str, new_settings: dict, *, user_id: str) -> tuple[bool, Any]:
    """
    更新Memory配置

    :param db: 数据库会话
    :param memory_id: Memory ID
    :param new_settings: 可更新字段字典
    """
    current_memory = _require_memory_access(db, user_id, memory_id)

    new_settings = dict(new_settings)
    for model_key in ("llm_id", "embd_id"):
        tenant_key = f"tenant_{model_key}"
        if tenant_key in new_settings:
            if not new_settings.get(model_key):
                raise ArgumentException(f"Use {model_key} to change the memory model.")
            # Resolve from the memory owner's tenant; never trust supplied row IDs.
            new_settings.pop(tenant_key)
    update_dict = {}

    if "name" in new_settings:
        name = new_settings["name"].strip()
        if len(name) == 0:
            raise ArgumentException("Memory name cannot be empty or whitespace.")
        if len(name) > MEMORY_NAME_LIMIT:
            raise ArgumentException(f"Memory name '{name}' exceeds limit of {MEMORY_NAME_LIMIT}.")
        update_dict["name"] = name

    if new_settings.get("permissions"):
        if new_settings["permissions"] not in [e.value for e in TenantPermission]:
            raise ArgumentException(f"Unknown permission '{new_settings['permissions']}'.")
        update_dict["permissions"] = new_settings["permissions"]

    if new_settings.get("embd_id"):
        update_dict["embd_id"] = new_settings["embd_id"].strip()
        if not update_dict["embd_id"]:
            raise ArgumentException("Embedding model reference cannot be blank.")

    if new_settings.get("memory_type"):
        if not isinstance(new_settings["memory_type"], list) or not all(isinstance(value, str) for value in new_settings["memory_type"]):
            raise ArgumentException("Memory type must be a list of strings.")
        memory_type_set = {value.strip().lower() for value in new_settings["memory_type"]}
        valid_types = {e.name.lower() for e in MemoryType}
        invalid_types = memory_type_set - valid_types
        if invalid_types:
            raise ArgumentException(f"Memory type '{invalid_types}' is not supported.")
        update_dict["memory_type"] = sorted(memory_type_set)

    if new_settings.get("llm_id"):
        update_dict["llm_id"] = new_settings["llm_id"].strip()
        if not update_dict["llm_id"]:
            raise ArgumentException("Chat model reference cannot be blank.")

    if new_settings.get("memory_size"):
        if not 0 < int(new_settings["memory_size"]) <= MEMORY_SIZE_LIMIT:
            raise ArgumentException(f"Memory size should be in range (0, {MEMORY_SIZE_LIMIT}] Bytes.")
        update_dict["memory_size"] = new_settings["memory_size"]

    if new_settings.get("forgetting_policy"):
        if new_settings["forgetting_policy"] not in [e.value for e in ForgettingPolicy]:
            raise ArgumentException(f"Forgetting policy '{new_settings['forgetting_policy']}' is not supported.")
        update_dict["forgetting_policy"] = new_settings["forgetting_policy"]

    if "temperature" in new_settings:
        temperature = float(new_settings["temperature"])
        if not 0 <= temperature <= 1:
            raise ArgumentException("Temperature should be in range [0, 1].")
        update_dict["temperature"] = temperature

    for field in ["avatar", "description", "system_prompt", "user_prompt"]:
        if field in new_settings:
            update_dict[field] = new_settings[field]

    memory_dict = current_memory.to_dict()
    memory_dict["memory_type"] = get_memory_type_human(current_memory.memory_type)

    update_dict = ensure_tenant_model_id_for_params(db, current_memory.tenant_id, update_dict, strict=True)
    for model_key in ("llm_id", "embd_id"):
        if model_key not in update_dict:
            continue
        tenant_key = f"tenant_{model_key}"
        current_id = memory_dict.get(tenant_key)
        if current_id is None and memory_dict.get(model_key):
            try:
                current_id = ensure_tenant_model_id_for_params(db, current_memory.tenant_id, {model_key: memory_dict[model_key]}).get(tenant_key)
            except (LookupError, ValueError):
                # An unusable legacy reference is a change, never a no-op.
                current_id = None
        if current_id is not None and current_id == update_dict[tenant_key]:
            update_dict.pop(model_key)
            update_dict.pop(tenant_key)

    to_update = {}
    for k, v in update_dict.items():
        if k == "memory_type":
            if set(memory_dict[k]) != set(v):
                to_update[k] = v
        elif memory_dict.get(k) != v:
            to_update[k] = v

    if not to_update:
        return True, memory_dict

    memory_size = get_memory_size_cache(current_memory.tenant_id, memory_id)
    not_allowed_update = [f for f in ["tenant_embd_id", "embd_id", "memory_type"] if f in to_update and memory_size > 0]
    if not_allowed_update:
        raise ArgumentException(f"Can't update {not_allowed_update} when memory isn't empty.")

    if "memory_type" in to_update:
        if "system_prompt" not in to_update and judge_system_prompt_is_default(current_memory.system_prompt, current_memory.memory_type):
            to_update["system_prompt"] = PromptAssembler.assemble_system_prompt({"memory_type": to_update["memory_type"]})

    MemoryService.update_memory(db, current_memory.tenant_id, memory_id, to_update)
    updated_memory = MemoryService.get_by_memory_id(db, memory_id)
    return True, format_ret_data_from_memory(updated_memory)


def delete_memory(db: Session, memory_id: str, *, user_id: str) -> bool:
    """删除Memory"""
    memory = _require_memory_access(db, user_id, memory_id)
    MemoryService.delete_memory(db, memory_id)
    if MessageService.has_index(memory.tenant_id, memory_id):
        MessageService.delete_message({"memory_id": memory_id}, memory.tenant_id, memory_id)
    return True


def list_memory(db: Session, user_id: str, filter_params: dict, keywords: str, page: int = 1, page_size: int = 50) -> dict:
    """获取Memory列表"""
    filter_dict: dict = {"storage_type": filter_params.get("storage_type")}
    allowed_tenant_ids = MemoryService.accessible_tenant_ids(db, user_id)
    # Both filters are constraints, including when explicitly empty.
    for key in ("tenant_id", "owner_ids"):
        if key in filter_params:
            allowed_tenant_ids &= set(_split_filter_values(filter_params[key]))
    if not allowed_tenant_ids:
        return {"memory_list": [], "total_count": 0}
    filter_dict["tenant_id"] = list(allowed_tenant_ids)
    filter_dict["memory_type"] = _split_filter_values(filter_params.get("memory_type"))
    memory_list, count = MemoryService.get_by_filter(db, filter_dict, keywords, page, page_size, user_id=user_id)
    for memory in memory_list:
        memory["memory_type"] = get_memory_type_human(memory["memory_type"])

    return {"memory_list": memory_list, "total_count": count}


def get_memory_config(db: Session, memory_id: str, *, user_id: str) -> dict:
    """获取Memory配置"""
    _require_memory_access(db, user_id, memory_id)
    memory = MemoryService.get_with_owner_name_by_id(db, memory_id)
    if not memory:
        raise NotFoundException(f"Memory '{memory_id}' not found.")
    return format_ret_data_from_memory(memory)


def get_memory_messages(db: Session, memory_id: str, agent_ids: list[str], keywords: str, page: int = 1, page_size: int = 50, *, user_id: str) -> dict:
    """获取Memory消息列表"""
    memory = _require_memory_access(db, user_id, memory_id)

    messages = MessageService.list_message(memory.tenant_id, memory_id, agent_ids, keywords, page, page_size)

    agent_name_mapping = {}
    extract_task_mapping = {}
    if messages.get("message_list"):
        agent_id_list = [msg.get("agent_id") for msg in messages["message_list"] if msg.get("agent_id")]
        agent_list = UserCanvasService.get_basic_info_by_canvas_ids(db, agent_id_list)
        agent_name_mapping = {agent["id"]: agent.get("title", "Unknown") for agent in agent_list}
        task_list = TaskService.get_tasks_progress_by_doc_ids(db, [memory_id])
        if task_list:
            task_list.sort(key=lambda t: t["create_time"])
            for task in task_list:
                extract_task_mapping.update({int(task["digest"]): task})

    for message in messages.get("message_list", []):
        message["agent_name"] = agent_name_mapping.get(message.get("agent_id"), "Unknown")
        message["task"] = extract_task_mapping.get(message.get("message_id"), {})
        for extract_msg in message.get("extract", []):
            extract_msg["agent_name"] = agent_name_mapping.get(extract_msg.get("agent_id"), "Unknown")

    return {"messages": messages, "storage_type": memory.storage_type}


async def add_message(db: AsyncSession, memory_ids: list[str], message_dict: dict, *, user_id: str) -> tuple[bool, str]:
    """
    添加消息到Memory，并投递后台任务提取结构化记忆。

    :param db: 数据库会话
    :param memory_ids: Memory ID列表
    :param message_dict: {
        "user_id": str,
        "agent_id": str,
        "session_id": str,
        "user_input": str,
        "agent_response": str,
    }
    """
    memory_ids = _split_filter_values(memory_ids)
    accessible_ids = await db.run_sync(lambda s: [m.id for m in _accessible_memories(s, user_id, memory_ids)])  # TODO(async-phase4)
    if not memory_ids or set(accessible_ids) != set(memory_ids):
        return False, "Memory not found."
    return await queue_save_to_memory_task(db, memory_ids, message_dict)


def forget_message(db: Session, memory_id: str, message_id: int, *, user_id: str) -> bool:
    """忘记（软删除）消息"""
    memory = _require_memory_access(db, user_id, memory_id)

    forget_time = timestamp_to_date(current_timestamp())
    update_succeed = MessageService.update_message({"memory_id": memory_id, "message_id": int(message_id)}, {"forget_at": forget_time}, memory.tenant_id, memory_id)
    if update_succeed:
        return True
    raise Exception(f"Failed to forget message '{message_id}' in memory '{memory_id}'.")


def update_message_status(db: Session, memory_id: str, message_id: int, status: bool, *, user_id: str) -> bool:
    """更新消息状态"""
    memory = _require_memory_access(db, user_id, memory_id)

    update_succeed = MessageService.update_message({"memory_id": memory_id, "message_id": int(message_id)}, {"status": status}, memory.tenant_id, memory_id)
    if update_succeed:
        return True
    raise Exception(f"Failed to set status for message '{message_id}' in memory '{memory_id}'.")


def search_message(db: Session, filter_dict: dict, params: dict, *, user_id: str) -> list[dict]:
    """
    搜索记忆中的消息

    :param db: 数据库会话
    :param filter_dict: {
        "memory_id": list[str],
        "agent_id": str,
        "session_id": str,
        "user_id": str
    }
    :param params: {
        "query": str,
        "similarity_threshold": float,
        "keywords_similarity_weight": float,
        "top_n": int
    }
    """
    memory_ids = _split_filter_values(filter_dict.get("memory_id"))
    accessible_ids = [m.id for m in _accessible_memories(db, user_id, memory_ids)]
    if not accessible_ids:
        return []
    return query_message(db, {**filter_dict, "memory_id": accessible_ids}, params)


def get_messages(db: Session, memory_ids: list[str], agent_id: str = "", session_id: str = "", limit: int = 10, *, user_id: str) -> list[dict]:
    """获取最近的消息"""
    memory_list = _accessible_memories(db, user_id, _split_filter_values(memory_ids))
    if not memory_list:
        return []
    memory_ids = [memory.id for memory in memory_list]
    uids = [memory.tenant_id for memory in memory_list]
    return MessageService.get_recent_messages(uids, memory_ids, agent_id, session_id, limit)


def get_message_content(db: Session, memory_id: str, message_id: int, *, user_id: str) -> dict:
    """获取消息内容"""
    memory = _require_memory_access(db, user_id, memory_id)

    res = MessageService.get_by_message_id(memory_id, message_id, memory.tenant_id)
    if res:
        return res
    raise NotFoundException(f"Message '{message_id}' in memory '{memory_id}' not found.")


# ---------------------------------------------------------------------------
# 混轨函数的异步入口（§11.12 混轨块判例形态）：MessageService(msg_store)/Redis size
# cache/query_message(embedding) 与 DB 读在函数内交错，run_sync 只桥 session 自身的
# IO——整块进工作线程 + 自开短会话。终态（Phase 3/4 拆面）前的过渡形态。
# ---------------------------------------------------------------------------


async def _run_with_own_session(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    def _run() -> Any:
        with db_connection() as s:
            return fn(s, *args, **kwargs)

    return await asyncio.to_thread(_run)


async def update_memory_async(memory_id: str, new_settings: dict, *, user_id: str) -> tuple[bool, Any]:
    """update_memory 的异步入口（Redis size cache + msg_store 空判混轨）。"""
    return await _run_with_own_session(update_memory, memory_id, new_settings, user_id=user_id)


async def delete_memory_async(memory_id: str, *, user_id: str) -> bool:
    """delete_memory 的异步入口（msg_store 索引删除混轨）。"""
    return await _run_with_own_session(delete_memory, memory_id, user_id=user_id)


async def get_memory_messages_async(memory_id: str, agent_ids: list[str], keywords: str, page: int = 1, page_size: int = 50, *, user_id: str) -> dict:
    """get_memory_messages 的异步入口（msg_store 列表与 DB 富化交错）。"""
    return await _run_with_own_session(get_memory_messages, memory_id, agent_ids, keywords, page, page_size, user_id=user_id)


async def forget_message_async(memory_id: str, message_id: int, *, user_id: str) -> bool:
    """forget_message 的异步入口（DB 读 + msg_store 更新混轨）。"""
    return await _run_with_own_session(forget_message, memory_id, message_id, user_id=user_id)


async def update_message_status_async(memory_id: str, message_id: int, status: bool, *, user_id: str) -> bool:
    """update_message_status 的异步入口（DB 读 + msg_store 更新混轨）。"""
    return await _run_with_own_session(update_message_status, memory_id, message_id, status, user_id=user_id)


async def search_message_async(filter_dict: dict, params: dict, *, user_id: str) -> Any:
    """search_message 的异步入口（query_message：DB + embedding HTTP + msg_store 检索混轨）。"""
    return await _run_with_own_session(search_message, filter_dict, params, user_id=user_id)


async def get_messages_async(memory_ids: list[str], agent_id: str = "", session_id: str = "", limit: int = 10, *, user_id: str) -> Any:
    """get_messages 的异步入口（DB 读 + msg_store 最近消息混轨）。"""
    return await _run_with_own_session(get_messages, memory_ids, agent_id, session_id, limit, user_id=user_id)


async def get_message_content_async(memory_id: str, message_id: int, *, user_id: str) -> Any:
    """get_message_content 的异步入口（DB 读 + msg_store 取内容混轨）。"""
    return await _run_with_own_session(get_message_content, memory_id, message_id, user_id=user_id)

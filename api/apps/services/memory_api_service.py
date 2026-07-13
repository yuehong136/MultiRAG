"""
@project: multirag
@Author：龙
@file： memory_api_service.py
@date：2026/03/18
@desc: Memory API 业务逻辑层 - 从gateway层解耦的业务处理
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.constants import MEMORY_NAME_LIMIT, MEMORY_SIZE_LIMIT
from api.db.joint_services.memory_message_service import (
    get_memory_size_cache,
    judge_system_prompt_is_default,
    query_message,
    queue_save_to_memory_task,
)
from api.db.services.canvas_service import UserCanvasService
from api.db.services.memory_service import MemoryService
from api.db.services.task_service import TaskService
from api.db.services.user_service import UserTenantService
from api.utils.memory_utils import format_ret_data_from_memory, get_memory_type_human
from api.utils.tenant_utils import ensure_tenant_model_id_for_params
from common.constants import ForgettingPolicy, MemoryType, TenantPermission
from common.exceptions import ArgumentException, NotFoundException
from common.time_utils import current_timestamp, timestamp_to_date
from memory.services.messages import MessageService
from memory.utils.prompt_util import PromptAssembler


def create_memory(db: Session, tenant_id: str, memory_info: dict):
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


def update_memory(db: Session, memory_id: str, new_settings: dict):
    """
    更新Memory配置

    :param db: 数据库会话
    :param memory_id: Memory ID
    :param new_settings: 可更新字段字典
    """
    current_memory = MemoryService.get_by_memory_id(db, memory_id)
    if not current_memory:
        raise NotFoundException(f"Memory '{memory_id}' not found.")

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
        update_dict["embd_id"] = new_settings["embd_id"]

    if new_settings.get("memory_type"):
        memory_type_set = set(new_settings["memory_type"])
        valid_types = {e.name.lower() for e in MemoryType}
        invalid_types = memory_type_set - valid_types
        if invalid_types:
            raise ArgumentException(f"Memory type '{invalid_types}' is not supported.")
        update_dict["memory_type"] = list(memory_type_set)

    if new_settings.get("llm_id"):
        update_dict["llm_id"] = new_settings["llm_id"]

    if new_settings.get("tenant_llm_id"):
        update_dict["tenant_llm_id"] = new_settings["tenant_llm_id"]
    if new_settings.get("tenant_embd_id"):
        update_dict["tenant_embd_id"] = new_settings["tenant_embd_id"]

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

    to_update = {}
    for k, v in update_dict.items():
        if isinstance(v, list) and set(memory_dict.get(k, [])) != set(v):
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

    to_update = ensure_tenant_model_id_for_params(db, current_memory.tenant_id, to_update)
    MemoryService.update_memory(db, current_memory.tenant_id, memory_id, to_update)
    updated_memory = MemoryService.get_by_memory_id(db, memory_id)
    return True, format_ret_data_from_memory(updated_memory)


def delete_memory(db: Session, memory_id: str):
    """删除Memory"""
    memory = MemoryService.get_by_memory_id(db, memory_id)
    if not memory:
        raise NotFoundException(f"Memory '{memory_id}' not found.")
    MemoryService.delete_memory(db, memory_id)
    if MessageService.has_index(memory.tenant_id, memory_id):
        MessageService.delete_message({"memory_id": memory_id}, memory.tenant_id, memory_id)
    return True


def list_memory(db: Session, user_id: str, filter_params: dict, keywords: str, page: int = 1, page_size: int = 50):
    """获取Memory列表"""
    filter_dict: dict = {"storage_type": filter_params.get("storage_type")}

    tenant_ids = filter_params.get("tenant_id")
    if not tenant_ids:
        user_tenants = UserTenantService.get_user_tenant_relation_by_user_id(db, user_id)
        filter_dict["tenant_id"] = [tenant["tenant_id"] for tenant in user_tenants]
    else:
        if len(tenant_ids) == 1 and "," in tenant_ids[0]:
            tenant_ids = tenant_ids[0].split(",")
        filter_dict["tenant_id"] = tenant_ids

    memory_types = filter_params.get("memory_type")
    if memory_types and len(memory_types) == 1 and "," in memory_types[0]:
        memory_types = memory_types[0].split(",")
    filter_dict["memory_type"] = memory_types

    memory_list, count = MemoryService.get_by_filter(db, filter_dict, keywords, page, page_size)
    for memory in memory_list:
        memory["memory_type"] = get_memory_type_human(memory["memory_type"])

    return {"memory_list": memory_list, "total_count": count}


def get_memory_config(db: Session, memory_id: str):
    """获取Memory配置"""
    memory = MemoryService.get_with_owner_name_by_id(db, memory_id)
    if not memory:
        raise NotFoundException(f"Memory '{memory_id}' not found.")
    return format_ret_data_from_memory(memory)


def get_memory_messages(db: Session, memory_id: str, agent_ids: list[str], keywords: str, page: int = 1, page_size: int = 50):
    """获取Memory消息列表"""
    memory = MemoryService.get_by_memory_id(db, memory_id)
    if not memory:
        raise NotFoundException(f"Memory '{memory_id}' not found.")

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


async def add_message(db: AsyncSession, memory_ids: list[str], message_dict: dict) -> tuple[bool, str]:
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
    return await queue_save_to_memory_task(db, memory_ids, message_dict)


def forget_message(db: Session, memory_id: str, message_id: int):
    """忘记（软删除）消息"""
    memory = MemoryService.get_by_memory_id(db, memory_id)
    if not memory:
        raise NotFoundException(f"Memory '{memory_id}' not found.")

    forget_time = timestamp_to_date(current_timestamp())
    update_succeed = MessageService.update_message({"memory_id": memory_id, "message_id": int(message_id)}, {"forget_at": forget_time}, memory.tenant_id, memory_id)
    if update_succeed:
        return True
    raise Exception(f"Failed to forget message '{message_id}' in memory '{memory_id}'.")


def update_message_status(db: Session, memory_id: str, message_id: int, status: bool):
    """更新消息状态"""
    memory = MemoryService.get_by_memory_id(db, memory_id)
    if not memory:
        raise NotFoundException(f"Memory '{memory_id}' not found.")

    update_succeed = MessageService.update_message({"memory_id": memory_id, "message_id": int(message_id)}, {"status": status}, memory.tenant_id, memory_id)
    if update_succeed:
        return True
    raise Exception(f"Failed to set status for message '{message_id}' in memory '{memory_id}'.")


def search_message(db: Session, filter_dict: dict, params: dict):
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
    return query_message(db, filter_dict, params)


def get_messages(db: Session, memory_ids: list[str], agent_id: str = "", session_id: str = "", limit: int = 10):
    """获取最近的消息"""
    memory_list = MemoryService.get_by_ids(db, memory_ids)
    uids = [memory.tenant_id for memory in memory_list]
    return MessageService.get_recent_messages(uids, memory_ids, agent_id, session_id, limit)


def get_message_content(db: Session, memory_id: str, message_id: int):
    """获取消息内容"""
    memory = MemoryService.get_by_memory_id(db, memory_id)
    if not memory:
        raise NotFoundException(f"Memory '{memory_id}' not found.")

    res = MessageService.get_by_message_id(memory_id, message_id, memory.tenant_id)
    if res:
        return res
    raise NotFoundException(f"Message '{message_id}' in memory '{memory_id}' not found.")

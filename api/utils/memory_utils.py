# coding=utf-8
"""
@project: multirag
@Author：龙
@file： memory_utils.py
@date：2025/01/14
@desc: Memory相关工具函数
"""
from typing import Any

from common.constants import MemoryType


def format_ret_data_from_memory(memory: Any) -> dict:
    """
    格式化Memory对象为API返回数据

    Args:
        memory: Memory对象或包含Memory字段的Row对象

    Returns:
        dict: 格式化后的Memory数据字典
    """
    return {
        "id": memory.id,
        "name": memory.name,
        "avatar": memory.avatar,
        "tenant_id": memory.tenant_id,
        "owner_name": memory.owner_name if hasattr(memory, "owner_name") else None,
        "memory_type": get_memory_type_human(memory.memory_type),
        "storage_type": memory.storage_type,
        "embd_id": memory.embd_id,
        "llm_id": memory.llm_id,
        "permissions": memory.permissions,
        "description": memory.description,
        "memory_size": memory.memory_size,
        "forgetting_policy": memory.forgetting_policy,
        "temperature": memory.temperature,
        "system_prompt": memory.system_prompt,
        "user_prompt": memory.user_prompt,
        "create_time": memory.create_time,
        "create_date": str(memory.create_date) if memory.create_date else None,
        "update_time": memory.update_time,
        "update_date": str(memory.update_date) if memory.update_date else None
    }


def get_memory_type_human(memory_type: int) -> list[str]:
    """
    将Memory类型位标志转换为人类可读的类型名称列表

    Args:
        memory_type: 位标志整数，如5表示raw(1)+episodic(4)

    Returns:
        list[str]: 类型名称列表，如["raw", "episodic"]

    Examples:
        >>> get_memory_type_human(1)
        ['raw']
        >>> get_memory_type_human(5)
        ['raw', 'episodic']
        >>> get_memory_type_human(15)
        ['raw', 'semantic', 'episodic', 'procedural']
    """
    return [mem_type.name.lower() for mem_type in MemoryType if memory_type & mem_type.value]


def calculate_memory_type(memory_type_name_list: list[str]) -> int:
    """
    将Memory类型名称列表转换为位标志整数

    Args:
        memory_type_name_list: 类型名称列表，如["raw", "semantic"]

    Returns:
        int: 位标志整数，如3表示raw(1)+semantic(2)

    Examples:
        >>> calculate_memory_type(["raw"])
        1
        >>> calculate_memory_type(["raw", "semantic"])
        3
        >>> calculate_memory_type(["raw", "episodic", "procedural"])
        13
    """
    memory_type = 0
    type_value_map = {mem_type.name.lower(): mem_type.value for mem_type in MemoryType}
    for mem_type in memory_type_name_list:
        mem_type_lower = mem_type.lower()
        if mem_type_lower in type_value_map:
            memory_type |= type_value_map[mem_type_lower]
    return memory_type

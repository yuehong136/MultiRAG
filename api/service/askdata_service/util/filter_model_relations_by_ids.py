from typing import Any

from api.service.askdata_service.util.askdata_logger import get_askdata_logger

logger = get_askdata_logger()
def filter_model_relations_by_ids(
        model_ids: list[str],
        model_relations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    根据给定的model_ids列表，过滤模型关系列表。

    保留一个关系(relation)的条件是：该关系的 sourceModelId 或 targetModelId
    至少有一个存在于 model_ids 列表中。如果两者都不在，则该关系将被移除。

    Args:
        model_ids (List[str]): 经过筛选后保留的有效模型ID列表。
        model_relations (List[Dict[str, Any]]): 原始的、完整的模型关系列表。

    Returns:
        List[Dict[str, Any]]: 过滤后只包含有效关系的新列表。
    """
    if not model_ids or not model_relations:
        # 如果任一输入为空，则不可能存在有效关系，直接返回空列表
        return []

    # 1. 为了提高查找效率 (O(1) 平均时间复杂度)，将列表转换为集合。
    #    这对于大量的 model_ids 尤为重要。
    valid_model_ids_set: set[str] = set(model_ids)

    # 2. 使用列表推导式进行过滤，代码更简洁、高效。
    #    .get() 方法可以安全地处理字典中可能不存在键的情况。
    filtered_relations = [
        relation for relation in model_relations
        if (relation.get("sourceModelId") in valid_model_ids_set or
            relation.get("targetModelId") in valid_model_ids_set)
    ]

    # 打印统计信息，便于调试
    original_count = len(model_relations)
    filtered_count = len(filtered_relations)
    logger.info(f"原始关系数量: {original_count}, 过滤后剩余关系数量: {filtered_count}. "
          f"移除了 {original_count - filtered_count} 条无效关系。")

    return filtered_relations

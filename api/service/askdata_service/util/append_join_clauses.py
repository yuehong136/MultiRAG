from typing import Any


def append_join_clauses(from_sentence: str, relationships: list[dict[str, Any]], model_table_alias_mapping_list: list[dict[str, Any]], existing_tables: list[str]) -> str:
    """
    根据关系信息追加 JOIN 子句到 FROM 语句

    Args:
        from_sentence: 原始 FROM 子句
        relationships: 关系列表
        model_table_alias_mapping_list: 模型表别名映射
        existing_tables: 已存在的表列表

    Returns:
        更新后的 FROM 子句
    """
    # 创建模型ID到别名的映射
    model_to_alias = {m["modelId"]: m["alias"] for m in model_table_alias_mapping_list}
    model_to_table = {m["modelId"]: m["table"] for m in model_table_alias_mapping_list}

    join_clauses = []

    for rel in relationships:
        join_type = rel.get("joinType", "LEFT")  # 默认使用 LEFT JOIN
        source_model_id = rel["sourceModelId"]
        target_model_id = rel["targetModelId"]
        source_table = rel["source_dataobject"]
        target_table = rel["target_dataobject"]
        source_field = rel["sourceField"]
        target_field = rel["targetField"]

        # 判断哪个是新增的表
        if source_table not in existing_tables and target_table in existing_tables:
            # source 是新表，需要 JOIN
            new_table = source_table
            new_alias = model_to_alias.get(source_model_id, "")
            existing_alias = model_to_alias.get(target_model_id, "")
            join_clause = f" {join_type} JOIN {new_table} {new_alias} ON {existing_alias}.{target_field} = {new_alias}.{source_field}"
            join_clauses.append(join_clause)

        elif target_table not in existing_tables and source_table in existing_tables:
            # target 是新表，需要 JOIN
            new_table = target_table
            new_alias = model_to_alias.get(target_model_id, "")
            existing_alias = model_to_alias.get(source_model_id, "")
            join_clause = f" {join_type} JOIN {new_table} {new_alias} ON {existing_alias}.{source_field} = {new_alias}.{target_field}"
            join_clauses.append(join_clause)

    # 追加所有 JOIN 子句
    updated_from_sentence = from_sentence + "".join(join_clauses)

    return updated_from_sentence

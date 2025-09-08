import json
from typing import List, Dict, Any


def filter_dimension_values_by_segmented_words(dimension_values: List[Dict[str, Any]], segmented_words: List[str]) -> \
List[Dict[str, Any]]:
    """
    根据分词列表对维度值进行模糊匹配过滤

    Args:
        dimension_values: 维度值列表，包含 value 和可能的 synonyms
        segmented_words: 分词列表

    Returns:
        过滤后的维度值列表
    """
    if not segmented_words:
        # 如果没有分词，返回原始数据
        return dimension_values

    filtered_values = []

    for dim_value in dimension_values:
        value = dim_value.get('value', '')
        synonyms = dim_value.get('synonyms', [])

        # 检查 value 是否与任何分词匹配
        value_matched = any(word in value or value in word for word in segmented_words)

        # 检查 synonyms 是否与任何分词匹配
        synonyms_matched = False
        if synonyms:
            for synonym in synonyms:
                if any(word in synonym or synonym in word for word in segmented_words):
                    synonyms_matched = True
                    break

        # 如果 value 或 synonyms 中任何一个匹配，则保留该维度值
        if value_matched or synonyms_matched:
            filtered_values.append(dim_value)

    return filtered_values


def process_data_models(
        models: List[Dict[str, Any]],
        user_semantic_permissions: Dict[str, Any],
        dimensions: List[Dict[str, Any]],
        metrics: List[Dict[str, Any]],
        model_relations: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    处理数据模型，只保留被维度、指标、关联关系和行级权限引用的字段
    """
    data_models_output = []
    if not models:
        return data_models_output

    # 构建权限映射字典，以 modelId 为键
    permissions_map = {}
    if user_semantic_permissions and user_semantic_permissions.get("dataPermissions"):
        data_permissions = user_semantic_permissions["dataPermissions"]
        if data_permissions.get("models"):
            for permission_model in data_permissions["models"]:
                model_id = permission_model.get("modelId")
                if model_id:
                    permissions_map[model_id] = permission_model

    # 构建每个模型需要保留的字段集合
    model_used_fields = {}  # {modelId: set(fieldNames)}

    # 1. 收集维度使用的字段
    for dimension in dimensions:
        model_id = dimension.get("modelId")
        field_name = dimension.get("dimensionEnName")
        if model_id and field_name:
            if model_id not in model_used_fields:
                model_used_fields[model_id] = set()
            model_used_fields[model_id].add(field_name)

    # 2. 收集指标表达式中使用的字段
    for metric in metrics:
        model_id = metric.get("modelId")
        expression = metric.get("expression", "")
        if model_id and expression:
            if model_id not in model_used_fields:
                model_used_fields[model_id] = set()
            # 从表达式中提取字段名
            # 这里需要找出所有可能的字段名
            for model_data in models:
                if model_data.get("modelId") == model_id:
                    if model_data.get("fields"):
                        for field_data in model_data["fields"]:
                            field_name = field_data.get("fieldName")
                            if field_name and field_name in expression:
                                model_used_fields[model_id].add(field_name)
                    break

    # 3. 收集关联关系使用的字段
    for relation in model_relations:
        # 处理源模型字段
        source_model_name = relation.get("sourceModelName")
        source_field = relation.get("sourceField")
        if source_model_name and source_field:
            # 找到对应的 modelId
            for model_data in models:
                if model_data.get("modelName") == source_model_name:
                    model_id = model_data.get("modelId")
                    if model_id:
                        if model_id not in model_used_fields:
                            model_used_fields[model_id] = set()
                        model_used_fields[model_id].add(source_field)
                    break

        # 处理目标模型字段
        target_model_name = relation.get("targetModelName")
        target_field = relation.get("targetField")
        if target_model_name and target_field:
            # 找到对应的 modelId
            for model_data in models:
                if model_data.get("modelName") == target_model_name:
                    model_id = model_data.get("modelId")
                    if model_id:
                        if model_id not in model_used_fields:
                            model_used_fields[model_id] = set()
                        model_used_fields[model_id].add(target_field)
                    break

    # 4. 收集行级权限中使用的字段
    for model_id, permission_model in permissions_map.items():
        row_filter = permission_model.get("rowFilter")
        if row_filter and row_filter.get("rules"):
            if model_id not in model_used_fields:
                model_used_fields[model_id] = set()

            for rule in row_filter["rules"]:
                expression = rule.get("expression", "")
                if expression:
                    # 移除 WHERE 前缀
                    if expression.upper().startswith("WHERE "):
                        expression = expression[6:]

                    # 从表达式中提取字段名
                    for model_data in models:
                        if model_data.get("modelId") == model_id:
                            if model_data.get("fields"):
                                for field_data in model_data["fields"]:
                                    field_name = field_data.get("fieldName")
                                    if field_name and field_name in expression:
                                        model_used_fields[model_id].add(field_name)
                            break

    # 处理每个模型
    for model_data in models:
        model_id = model_data.get("modelId")

        # 构建每个 dataModel 的基本结构
        data_model = {
            "modelId": model_id,
            "name": model_data.get("modelName"),
            "table": model_data.get("tableName"),
            "fields": []
        }

        # 获取该模型需要保留的字段集合
        used_fields = model_used_fields.get(model_id, set())

        # 处理 fields - 只保留被使用的字段和主键
        if model_data.get("fields"):
            for field_data in model_data["fields"]:
                field_name = field_data.get("fieldName")
                is_primary = field_data.get("is_pk") == '1'

                # 只保留：1) 被引用的字段 2) 主键字段
                if field_name in used_fields or is_primary:
                    processed_field = {
                        "name": field_name,
                        "type": field_data.get("dataType")
                    }

                    # 添加主键标记
                    if is_primary:
                        processed_field["isPrimary"] = True

                    # 添加 comment 字段（如果存在）
                    if field_data.get("description"):
                        processed_field["comment"] = field_data.get("description")

                    data_model["fields"].append(processed_field)

        # 处理 rowFilter（保持原有逻辑）
        row_filter_value = None

        if model_id and model_id in permissions_map:
            permission_model = permissions_map[model_id]
            row_filter = permission_model.get("rowFilter")

            if row_filter and row_filter.get("rules"):
                processed_row_filter = {
                    "logicalOperator": row_filter.get("logicalOperator", "OR"),
                    "rules": []
                }

                for rule in row_filter["rules"]:
                    processed_rule = {}
                    if rule.get("expression"):
                        expression = rule["expression"]
                        if expression.upper().startswith("WHERE "):
                            expression = expression[6:]
                        processed_rule["expression"] = expression

                    processed_row_filter["rules"].append(processed_rule)

                row_filter_value = processed_row_filter

        data_model["rowFilter"] = row_filter_value
        data_models_output.append(data_model)

    return data_models_output


def process_business_datasets(
        dataset_details,
        dimensions,
        metrics,
        dimension_values,
        segmented_words
) -> List[Dict[str, Any]]:
    business_datasets_output = []
    for dataset_detail in dataset_details:
        business_dataset = {}
        dataset_id = dataset_detail.get("datasetId")
        business_dataset["name"] = dataset_detail.get("datasetName")
        business_dataset["desc"] = dataset_detail.get("description")
        business_dataset["domain"] = dataset_detail.get("domainName")

        dimensions_output = []
        for dimension in dimensions:
            if not dimension.get("dataset_wid"):
                continue
            dimension_id = dimension.get("dimensionId")
            if dataset_id in dimension.get("dataset_wid"):
                dimension_output = {}
                dimension_output["id"] = dimension_id
                dimension_output["name"] = dimension.get("dimensionName")
                dimension_output["dimType"] = dimension.get("dimtype")
                if dimension_output["dimType"].lower() == "time":
                    semanticsformat = dimension.get('semanticsformat', {})
                    if semanticsformat:
                        dimension_output["timeFormat"] = json.loads(semanticsformat).get("timeFormat")
                dimension_output["field"] = dimension.get("dimensionEnName")
                dimension_output["fromModel"] = dimension.get("modelName")
                dimension_output["fromModelId"] = dimension.get("modelId")
                dimension_output["comment"] = dimension.get("description")
                dimension_output["synonyms"] = dimension.get("synonyms")

                # 修改这里：使用分词进行过滤
                if dimension_output["dimType"] == "LC":
                    original_values = dimension_values.get(dimension_id, [])
                    # 使用分词进行模糊匹配过滤
                    filtered_values = filter_dimension_values_by_segmented_words(original_values, segmented_words)
                    # 进一步限制样例值的数量，给大模型提供部分样例即可，太多会影响效果
                    dimension_output["sampleValues"] = filtered_values[:20]
                elif dimension_output["dimType"].lower() == "time":
                    original_values = dimension_values.get(dimension_id, [])
                    dimension_output["sampleValues"] = original_values[:5]
                if not dimension.get("hasPermission", True):
                    dimension_output["hasPermission"] = False
                    dimension_output["permissionDesc"] = "当前用户无权限访问此维度，在生成SQL时不允许使用此维度"
                dimensions_output.append(dimension_output)
        business_dataset["dimensions"] = dimensions_output

        metrics_output = []
        for metric in metrics:
            if not metric.get("dataset_wid"):
                continue
            if dataset_id in metric.get("dataset_wid"):
                metric_output = {}
                metric_output["id"] = metric.get("metricId")
                metric_output["name"] = metric.get("metricName")
                metric_output["comment"] = metric.get("description")
                metric_output["calculationFormula"] = metric.get("expression")
                metric_output["formatting"] = metric.get("formatting")
                metric_output["fromModel"] = metric.get("modelName")
                metric_output["fromModelId"] = metric.get("modelId")
                if not metric.get("hasPermission", True):
                    metric_output["hasPermission"] = False
                    metric_output["permissionDesc"] = "当前用户无权限访问此指标，在生成SQL时不允许使用此指标"
                else:
                    metric_output["enName"] = metric.get("metricEnName")
                    metric_output["synonyms"] = metric.get("synonyms")
                metrics_output.append(metric_output)
        business_dataset["metrics"] = metrics_output

        business_datasets_output.append(business_dataset)

    return business_datasets_output


def process_relationships(model_relations) -> List[Dict[str, Any]]:
    relationships = []
    for relation in model_relations:
        relationship = {}
        relationship["sourceModelName"] = relation.get("sourceModelName")
        relationship["sourceTableName"] = relation.get("source_dataobject")
        relationship["sourceField"] = relation.get("sourceField")
        relationship["targetModelName"] = relation.get("targetModelName")
        relationship["targetTableName"] = relation.get("target_dataobject")
        relationship["targetField"] = relation.get("targetField")
        relationship["joinType"] = relation.get("joinType")
        relationships.append(relationship)
    return relationships


def process_business_terms(business_term_rows) -> List[Dict[str, Any]]:
    business_terms = []
    for term in business_term_rows:
        business_term = {}
        business_term["termName"] = term.get("termName")
        business_term["description"] = term.get("description")
        business_term["synonyms"] = term.get("synonyms")
        business_terms.append(business_term)
    return business_terms


def process_semantic_layer(semantic_layer: Dict[str, Any], user_semantic_permissions: Dict[str, Any],
                           segmented_words: List[str]) -> Dict[str, Any]:
    dataset_details = semantic_layer.get("dataset_details")
    dimensions = semantic_layer.get("dimensions")
    dimension_values = semantic_layer.get("dimension_values")
    metrics = semantic_layer.get("metrics")
    model_details = semantic_layer.get("model_details")
    model_relations = semantic_layer.get("model_relations")
    business_term_rows = semantic_layer.get("business_term_rows")

    semantic_structure = {
        "dataModels": [],
        "businessDatasets": [],
        "relationships": [],
        "businessTerms": []
    }

    semantic_structure["dataModels"] = process_data_models(
        model_details,
        user_semantic_permissions,
        dimensions,
        metrics,
        model_relations
    )

    semantic_structure["businessDatasets"] = process_business_datasets(dataset_details, dimensions, metrics,
                                                                       dimension_values, segmented_words)
    semantic_structure["relationships"] = process_relationships(model_relations)
    semantic_structure["businessTerms"] = process_business_terms(business_term_rows)

    return semantic_structure
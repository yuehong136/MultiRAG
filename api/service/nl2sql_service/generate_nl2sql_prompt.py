import os
from datetime import date

from api.utils.prompt_template_util import PromptTemplateUtil
from typing import Dict, Any, List


def generate_nl2sql_prompt(
        user_question: str,
        query_intents: List[Dict[str, str]],
        semantic_layer: Dict[str, Any]
):
    # 确定模板完整路径
    prompt_dir = os.path.join(os.path.dirname(__file__), "prompt")

    full_template_path = os.path.join(prompt_dir, "nl2sql_with_semantic_template.txt")

    # 加载模板
    template_content = PromptTemplateUtil.load_template_from_file(full_template_path)

    # 准备用于填充模板的数据字典
    template_data = {}
    template_data["user_question"] = user_question

    # 处理查询意图为格式化的字符串
    formatted_intents = []
    for intent in query_intents:
        # 格式化为 "* Intent Label：中文描述"
        formatted_intent = f"* {intent['intent_label']}：{intent['description']}"
        formatted_intents.append(formatted_intent)

    # 将所有格式化的意图连接成一个字符串，每个意图一行
    template_data["query_intents"] = "\n".join(formatted_intents)
    template_data["semantic_layer"] = process_semantic_layer(semantic_layer)
    template_data["current_date"] = date.today()

    # 使用PromptTemplateUtil填充模板
    prompt = PromptTemplateUtil.fill_template(template_content, template_data)
    return prompt, template_data["semantic_layer"]


def process_data_models(models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    data_models_output = []
    if not models:
        return data_models_output

    for model_data in models:
        # 构建每个 dataModel 的基本结构
        data_model = {
            "name": model_data.get("modelName"),
            "table": model_data.get("tableName"),
            "fields": []
        }

        # 处理 fields
        if model_data.get("fields"):
            for field_data in model_data["fields"]:
                processed_field = {
                    "name": field_data.get("fieldName"),
                    "type": field_data.get("dataType")
                }
                # 检查 is_pk 字段并转换为布尔值
                if field_data.get("is_pk") == '1':
                    processed_field["isPrimary"] = True
                else:
                    # 如果 is_pk 不是 '1'，则不包含 isPrimary 字段或设为 False
                    # 根据示例，只有主键才包含 isPrimary: true
                    pass

                # 添加 comment 字段（如果存在）
                if field_data.get("description"):
                    processed_field["comment"] = field_data.get("description")

                data_model["fields"].append(processed_field)

        data_models_output.append(data_model)

    return data_models_output


def process_business_datasets(
        dataset_details,
        dimensions,
        metrics,
        dimension_values
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
            dimension_id = dimension.get("dimensionId")
            if dataset_id in dimension.get("dataset_wid"):
                dimension_output = {}
                dimension_output["id"] = dimension_id
                dimension_output["name"] = dimension.get("dimensionName")
                dimension_output["field"] = dimension.get("dimensionEnName")
                dimension_output["fromModel"] = dimension.get("modelName")
                dimension_output["comment"] = dimension.get("description")
                dimension_output["synonyms"] = dimension.get("synonyms")
                dimension_output["possibleValues"] = dimension_values[dimension_id]
                dimensions_output.append(dimension_output)
        business_dataset["dimensions"] = dimensions_output

        metrics_output = []
        for metric in metrics:
            if dataset_id in metric.get("datasetWid"):
                metric_output = {}
                metric_output["id"] = metric.get("metricId")
                metric_output["name"] = metric.get("metricName")
                metric_output["enName"] = metric.get("metricEnName")
                metric_output["synonyms"] = metric.get("synonyms")
                metric_output["comment"] = metric.get("description")
                metric_output["calculationFormula"] = metric.get("expression")
                metric_output["formatting"] = metric.get("formatting")
                metric_output["fromModel"] = metric.get("modelName")
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


def process_semantic_layer(semantic_layer: Dict[str, Any]) -> Dict[str, Any]:
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

    semantic_structure["dataModels"] = process_data_models(model_details)
    semantic_structure["businessDatasets"] = process_business_datasets(dataset_details, dimensions, metrics,
                                                                       dimension_values)
    semantic_structure["relationships"] = process_relationships(model_relations)
    semantic_structure["businessTerms"] = process_business_terms(business_term_rows)

    return semantic_structure

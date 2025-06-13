import uuid
from typing import Any, List, Dict, Tuple

from api.service.askdata_service.sql_components_parser import SQLComponentsParser
from api.service.nl2sql_service.semantic_api_client import SemanticApiClient


class TableConfigGenerator:
    """
    负责从SQL查询和语义模型生成表格配置。
    """

    def __init__(self, semantic_api_client: SemanticApiClient):
        self.semantic_api_client = semantic_api_client

    async def generate(self, model_ids: List[str], used_models: List[str],
                       sql_components: Dict[str, Any]):
        """
        生成表配置信息，将SQL解析结果映射到语义层。

        Args:
            model_ids: 所有相关的模型ID列表
            used_models: SQL中实际使用的模型名称列表
            sql_components: SQL句子成分

        Returns:
            包含列、过滤器和排序信息的配置字典
        """
        # 1. 解析SQL
        parts = SQLComponentsParser(sql_components).parse_all()

        # 2. 构建使用的模型和表的详情字典
        _, used_table_detail_dict, model_list = await self._build_model_details(
            model_ids, used_models
        )

        model_table_alias_mapping_list = []
        for model in model_list:
            alias = ""
            for alias_and_table in parts["from_tables"]:
                if alias_and_table["table"] == model["tableName"]:
                    alias = alias_and_table["alias"]
                    break
            model_table_alias_mapping_list.append(
                {"modelId": model["modelId"], "table": model["tableName"], "alias": alias})

        # 4. 一次性构建所有语义字段信息
        semantic_fields_info = await self._build_semantic_fields_info(model_list)

        # 5. 处理SELECT列
        selected_columns = self._process_select_columns(parts, used_table_detail_dict)

        # 6. 处理WHERE条件
        filter_conditions = self._process_where_conditions(parts, used_table_detail_dict)

        # 7. 处理ORDER BY字段
        order_by_fields = self._process_order_by_fields(parts, used_table_detail_dict)

        # 8. 构建返回结果
        return model_table_alias_mapping_list, {
            "columns": {
                "selected_columns": selected_columns,
                "available_fields": semantic_fields_info["available_fields"]
            },
            "filters": {
                "filter_conditions": filter_conditions,
                "available_fields": semantic_fields_info["filterable_fields"]
            },
            "order_by": {
                "order_by_fields": order_by_fields,
                "available_fields": semantic_fields_info["sortable_fields"]
            },
            "all_semantic_fields": semantic_fields_info["all_fields"]
        }

    async def _build_model_details(self, model_ids: List[str],
                                   used_models: List[str]) -> Tuple[Dict, Dict, List]:
        """构建模型详情字典"""
        used_model_detail_dict = {}
        used_table_detail_dict = {}
        model_list = []

        model_detail_list = await self.semantic_api_client.get_model_detail_async(model_ids=model_ids)

        for model_detail in model_detail_list:
            if model_detail.get('modelName') in used_models:
                # 获取模型的指标和维度信息
                model_detail[
                    'dimsAndMetrics'] = await self.semantic_api_client.get_model_inds_and_dims_by_model_id_async(
                    model_id=model_detail["modelId"]
                )
                model_list.append(model_detail)
                used_model_detail_dict[model_detail["modelName"]] = model_detail
                used_table_detail_dict[model_detail["tableName"]] = model_detail

        return used_model_detail_dict, used_table_detail_dict, model_list

    async def _build_semantic_fields_info(self, model_list: List[Dict]) -> Dict[str, List]:
        """一次性构建所有语义字段信息，避免重复遍历"""
        available_fields, filterable_fields, sortable_fields, all_fields = [], [], [], []
        all_dimension_ids = [
            dim['dimensionId'] for model in model_list for dim in model["dimsAndMetrics"]["dimensions"]
        ]

        dimensions_value_dict = await self.semantic_api_client.get_dimension_values_async(
            dimension_ids=all_dimension_ids
        ) if all_dimension_ids else {}

        for model in model_list:
            model_id = model["modelId"]
            model_name = model["modelName"]

            for metric in model["dimsAndMetrics"]["metrics"]:
                is_agg = "(" in metric["expression"].lower()
                available_fields.append(
                    {"is_allow_use": not is_agg, "semantic_type": "metric", "id": metric["metricId"],
                     "metric_name": metric["metricName"]})
                if not is_agg:
                    filterable_fields.append(
                        {"semantic_type": "metric", "id": metric["metricId"], "from_model": model_name,
                         "from_model_id": model_id, "metric_name": metric["metricName"]})
                    sortable_fields.append({"is_allow_use": True, "semantic_type": "metric", "id": metric["metricId"],
                                            "from_model": model_id, "metric_name": metric["metricName"]})
                all_fields.append({"semantic_type": "metric", "semantic_field": metric, "id": metric["metricId"],
                                   "from_model": model_name, "from_model_id": model_id})

            for dim in model["dimsAndMetrics"]["dimensions"]:
                dim_id = dim["dimensionId"]
                dim['possibleValues'] = dimensions_value_dict.get(dim_id, [])
                available_fields.append({"is_allow_use": True, "semantic_type": "dimension", "id": dim_id,
                                         "dimension_name": dim["dimensionName"]})
                filterable_fields.append(
                    {"semantic_type": "dimension", "id": dim_id, "from_model": model_name, "from_model_id": model_id,
                     "dimension_name": dim["dimensionName"]})
                sortable_fields.append(
                    {"is_allow_use": True, "semantic_type": "dimension", "id": dim_id, "from_model": model_id,
                     "dimension_name": dim["dimensionName"]})
                all_fields.append(
                    {"semantic_type": "dimension", "semantic_field": dim, "id": dim_id, "from_model": model_name,
                     "from_model_id": model_id})

        return {
            "available_fields": available_fields, "filterable_fields": filterable_fields,
            "sortable_fields": sortable_fields, "all_fields": all_fields
        }

    def _process_select_columns(self, parts: Dict[str, Any], used_table_detail_dict: Dict[str, Any]) -> List[Dict]:
        """处理SELECT列"""
        selected_columns = []
        table_alias_mapping = parts["table_alias_mapping"]
        for col in parts["select_columns"]:
            is_matched_semantic_field = False
            split_col = self._split_column(col)
            if len(split_col) == 2:
                alias, column_name = split_col
                table_name = table_alias_mapping[alias]
                table_detail = used_table_detail_dict[table_name]
                for dim in table_detail['dimsAndMetrics']['dimensions']:
                    if dim['dimensionEnName'].lower() == column_name.lower():
                        selected_columns.append(
                            {"is_semantic_field": True, "semantic_type": "dimension", "id": dim["dimensionId"],
                             "dimension_name": dim["dimensionName"]}
                        )
                        is_matched_semantic_field = True
                        break
                if is_matched_semantic_field:
                    continue
                for metric in table_detail['dimsAndMetrics']['metrics']:
                    if metric['expression'].lower() == column_name.lower():
                        selected_columns.append(
                            {"is_semantic_field": True, "semantic_type": "metric", "id": metric["metricId"],
                             "metric_name": metric["metricName"]}
                        )
                        is_matched_semantic_field = True
                        break
                if is_matched_semantic_field:
                    continue
                selected_columns.append({"is_semantic_field": False, "sql_column": col, "id": str(uuid.uuid4())})
        return selected_columns

    def _process_where_conditions(self, parts: Dict[str, Any], used_table_detail_dict: Dict[str, Any]) -> List[
        Dict[str, Any]]:
        """处理WHERE条件"""
        table_alias_mapping = parts["table_alias_mapping"]
        where_conditions = parts['where_conditions']
        if where_conditions.get('has_or'):
            raw = where_conditions['raw_condition']
            return [{"is_semantic_field": False, "is_complex_condition": True, "raw_condition": raw, "field": raw,
                     "operator": "", "value": "", "from_model": None, "id": str(uuid.uuid4())}]

        filter_columns = []
        for cond in where_conditions.get('parsed_conditions', []):
            is_matched_semantic_field = False
            alias, column_name = self._split_column(cond['field'])
            operator = cond['operator']
            value = cond['value']
            table_name = table_alias_mapping[alias]
            table_detail = used_table_detail_dict[table_name]
            for dim in table_detail['dimsAndMetrics']['dimensions']:
                if dim['dimensionEnName'].lower() == column_name.lower():
                    filter_columns.append(
                        {"is_semantic_field": True, "semantic_type": "dimension", "id": dim["dimensionId"],
                         "dimension_name": dim["dimensionName"], "operator": operator, "value": value}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            for metric in table_detail['dimsAndMetrics']['metrics']:
                if metric['expression'].lower() == column_name.lower():
                    filter_columns.append(
                        {"is_semantic_field": True, "semantic_type": "metric", "id": metric["metricId"],
                         "metric_name": metric["metricName"], "operator": operator, "value": value}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            filter_columns.append({"is_semantic_field": False, "sql_column": cond, "id": str(uuid.uuid4())})

        return filter_columns

    def _process_order_by_fields(self, parts: Dict[str, Any], used_table_detail_dict: Dict[str, Any]) -> List[
        Dict[str, Any]]:
        """处理ORDER BY字段"""
        order_by_columns = []
        table_alias_mapping = parts["table_alias_mapping"]
        order_by_fields = parts['order_by']
        for order_info in order_by_fields:
            is_matched_semantic_field = False
            alias, column_name = self._split_column(order_info['field'])
            direction = order_info['direction']
            table_name = table_alias_mapping[alias]
            table_detail = used_table_detail_dict[table_name]
            for metric in table_detail['dimsAndMetrics']['metrics']:
                if metric['expression'].lower() == column_name.lower():
                    order_by_columns.append(
                        {"is_semantic_field": True, "semantic_type": "metric", "id": metric["metricId"],
                         "metric_name": metric["metricName"], "direction": direction}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            for dim in table_detail['dimsAndMetrics']['dimensions']:
                if dim['dimensionEnName'].lower() == column_name.lower():
                    order_by_columns.append(
                        {"is_semantic_field": True, "semantic_type": "dimension", "id": dim["dimensionId"],
                         "dimension_name": dim["dimensionName"], "direction": direction}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            order_by_columns.append(
                {"is_semantic_field": False, "sql_column": order_info['field'], "id": str(uuid.uuid4()),
                 "direction": direction})

        return order_by_columns

    def _split_column(self, column: str) -> Tuple[str, str]:
        """将 'table.field' 格式的列名分割为 (table, field)"""
        parts = column.split('.')
        return (parts[0], parts[1]) if len(parts) == 2 else ("", column)

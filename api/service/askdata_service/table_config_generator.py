import uuid
from typing import Any, List, Dict, Tuple, Optional, Set

from api.service.askdata_service.sql_components_parser import SQLComponentsParser
from api.service.nl2sql_service.semantic_api_client import SemanticApiClient
from api.work_flow_api import logger


class TableConfigGenerator:
    """
    负责从SQL查询和语义模型生成表格配置。
    """

    def __init__(self, semantic_api_client: SemanticApiClient):
        self.semantic_api_client = semantic_api_client

    async def generate(self, used_table_detail_dict: Dict[str, Dict], model_list: List[Dict],
                       sql_components: Dict[str, Any], recommended_chart: str):
        """
        生成表配置信息，将SQL解析结果映射到语义层。

        Args:
            used_table_detail_dict: 使用的表的详情字典
            model_list: 所有相关的模型信息列表
            sql_components: SQL句子成分
            recommended_chart: 推荐的图表类型

        Returns:
            包含列、过滤器和排序信息的配置字典
        """
        # 1. 解析SQL
        parts = SQLComponentsParser(sql_components).parse_all()

        model_table_alias_mapping_list = []
        for model in model_list:
            alias = ""
            for alias_and_table in parts["from_tables"]:
                if alias_and_table["table"] == model["tableName"]:
                    alias = alias_and_table["alias"]
                    break
            model_table_alias_mapping_list.append(
                {"modelId": model["modelId"], "table": model["tableName"], "alias": alias, "modelName": model["modelName"]})

        if recommended_chart == "明细表":
            # 4. 一次性构建所有语义字段信息
            semantic_fields_info = await self._build_semantic_fields_info(model_list)

            # 5. 处理SELECT列
            selected_columns = self._process_select_columns(parts, used_table_detail_dict)

            # 6. 处理WHERE条件
            filter_conditions = self._process_where_conditions(parts, used_table_detail_dict)

            # 7. 处理ORDER BY字段
            order_by_fields = self._process_order_by_fields(parts, used_table_detail_dict)

            limit = self._process_limit(parts)

            return model_table_alias_mapping_list, {
                "chart_type": "table-row",
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
                "limit": limit,
                "all_semantic_fields": semantic_fields_info["all_fields"]
            }
        elif recommended_chart == "聚合表":
            # 4. 一次性构建所有语义字段信息
            semantic_fields_info = await self._build_semantic_fields_info_for_aggr(model_list)

            # 5. 处理维度，维度是group by中的内容
            selected_dimensions = self._build_selected_dimensions(parts)

            # 6. 处理指标，指标其实就是select_columns字段减去group by字段
            selected_metrics = self._build_selected_metrics(parts)

            order_by_fields = self._process_order_by_fields(parts, used_table_detail_dict)

            limit = self._process_limit(parts)

            return model_table_alias_mapping_list, {
                "chart_type": "table-aggr",
                "dimensions": {
                    "selected_dimensions": selected_dimensions,
                    "available_dimensions": semantic_fields_info["available_dimensions"]
                },
                "metrics": {
                    "selected_metrics": selected_metrics,
                    "available_metrics": semantic_fields_info["available_metrics"]
                },
                "order_by": {
                    "order_by_fields": order_by_fields,
                    "available_fields": semantic_fields_info["sortable_fields"]
                },
                "limit": limit,
                "all_semantic_fields": semantic_fields_info["all_fields"]
            }

    def _build_selected_dimensions(self, parts: Dict[str, Any]) -> List[Dict]:
        """构建group by维度字典"""
        group_by_dimensions = []
        for group_by in parts["group_by"]:
            group_by_dimensions.append({
                "is_semantic_field": False,
                "sql_column": group_by,
                "id": str(uuid.uuid4()),
                "wid": str(uuid.uuid4()),
                "nanoId": str(uuid.uuid4())
            })
        return group_by_dimensions

    def _build_selected_metrics(self, parts: Dict[str, Any]) -> List[Dict]:
        selected_metrics = []
        selected_columns = parts["select_columns"]
        group_by = parts['group_by']
        metrics = list(set(selected_columns) - set(group_by))
        for metric in metrics:
            selected_metrics.append({
                "is_semantic_field": False,
                "sql_column": metric,
                "id": str(uuid.uuid4()),
                "wid": str(uuid.uuid4()),
                "nanoId": str(uuid.uuid4())
            })
        return selected_metrics

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
                # 如果表达式中包含括号，则认为是聚合表达式，由于当前是为明细表服务，不可用于值展示、过滤、排序
                is_agg = "(" in metric["expression"].lower()
                available_fields.append(
                    {"is_allow_use": not is_agg, "semantic_type": "metric", "id": metric["metricId"],
                     "metric_name": metric["metricName"]})
                filterable_fields.append(
                    {"is_allow_use": not is_agg, "semantic_type": "metric", "id": metric["metricId"],
                     "from_model": model_name,
                     "from_model_id": model_id, "metric_name": metric["metricName"]})
                sortable_fields.append({"is_allow_use": not is_agg, "semantic_type": "metric", "id": metric["metricId"],
                                        "from_model": model_id, "metric_name": metric["metricName"]})
                all_fields.append({"semantic_type": "metric", "semantic_field": metric, "id": metric["metricId"],
                                   "from_model": model_name, "from_model_id": model_id})

            for dim in model["dimsAndMetrics"]["dimensions"]:
                dim_id = dim["dimensionId"]
                dim['possibleValues'] = dimensions_value_dict.get(dim_id, [])
                available_fields.append({"is_allow_use": True, "semantic_type": "dimension", "id": dim_id,
                                         "dimension_name": dim["dimensionName"]})
                filterable_fields.append(
                    {"is_allow_use": True, "semantic_type": "dimension", "id": dim_id, "from_model": model_name,
                     "from_model_id": model_id,
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

    async def _build_semantic_fields_info_for_aggr(self, model_list: List[Dict]) -> Dict[str, List]:
        """一次性构建所有语义字段信息，避免重复遍历"""
        available_dimensions, available_metrics, sortable_fields, all_fields = [], [], [], []
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
                available_metrics.append(
                    {"is_allow_use": True, "semantic_type": "metric", "id": metric["metricId"],
                     "from_model": model_name,
                     "from_model_id": model_id, "metric_name": metric["metricName"], "wid": str(uuid.uuid4())})
                available_metrics.append(
                    {"is_allow_use": True, "semantic_type": "measure", "id": metric["metricId"],
                     "from_model": model_name,
                     "from_model_id": model_id, "metric_name": metric["metricName"], "wid": str(uuid.uuid4())})
                sortable_fields.append({"is_allow_use": True, "semantic_type": "metric", "id": metric["metricId"],
                                        "from_model": model_id, "metric_name": metric["metricName"]})
                all_fields.append({"semantic_type": "metric", "semantic_field": metric, "id": metric["metricId"],
                                   "from_model": model_name, "from_model_id": model_id})

            for dim in model["dimsAndMetrics"]["dimensions"]:
                dim_id = dim["dimensionId"]
                dim['possibleValues'] = dimensions_value_dict.get(dim_id, [])
                available_dimensions.append({"is_allow_use": True, "semantic_type": "dimension", "id": dim_id,
                                             "dimension_name": dim["dimensionName"]})
                available_metrics.append(
                    {"is_allow_use": True, "semantic_type": "dimension", "id": dim_id, "from_model": model_name,
                     "from_model_id": model_id,
                     "dimension_name": dim["dimensionName"], "wid": str(uuid.uuid4())})
                sortable_fields.append(
                    {"is_allow_use": True, "semantic_type": "dimension", "id": dim_id, "from_model": model_id,
                     "dimension_name": dim["dimensionName"]})
                all_fields.append(
                    {"semantic_type": "dimension", "semantic_field": dim, "id": dim_id, "from_model": model_name,
                     "from_model_id": model_id})

        return {
            "available_dimensions": available_dimensions, "available_metrics": available_metrics,
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
                             "dimension_name": dim["dimensionName"], "wid": str(uuid.uuid4())}
                        )
                        is_matched_semantic_field = True
                        break
                if is_matched_semantic_field:
                    continue
                for metric in table_detail['dimsAndMetrics']['metrics']:
                    if metric['expression'].lower() == column_name.lower():
                        selected_columns.append(
                            {"is_semantic_field": True, "semantic_type": "metric", "id": metric["metricId"],
                             "metric_name": metric["metricName"], "wid": str(uuid.uuid4())}
                        )
                        is_matched_semantic_field = True
                        break
                if is_matched_semantic_field:
                    continue
                selected_columns.append(
                    {"is_semantic_field": False, "sql_column": col, "id": str(uuid.uuid4()), "wid": str(uuid.uuid4())})
        return selected_columns

    def _process_where_conditions(self, parts: Dict[str, Any], used_table_detail_dict: Dict[str, Any]) -> List[
        Dict[str, Any]]:
        """处理WHERE条件"""
        table_alias_mapping = parts["table_alias_mapping"]
        where_conditions = parts['where_conditions']
        if where_conditions.get('has_or'):
            raw = where_conditions['raw_condition']
            return [{"is_semantic_field": False, "is_complex_condition": True, "raw_condition": raw, "field": raw,
                     "operator": "", "value": "", "from_model": None, "id": str(uuid.uuid4()),
                     "wid": str(uuid.uuid4())}]

        filter_columns = []
        for cond in where_conditions.get('parsed_conditions', []):
            is_matched_semantic_field = False
            if "(" in cond['field']:
                # 如果字段中包含了括号，则认为是复杂条件
                filter_columns.append(
                    {"is_semantic_field": False, "is_complex_condition": True, "raw_condition": cond['field'],
                     "operator": "", "value": "", "from_model": None, "id": str(uuid.uuid4()),
                     "wid": str(uuid.uuid4())})
                continue
            alias, column_name = self._split_column(cond['field'])
            operator = cond['operator']
            value = cond['value']
            table_name = table_alias_mapping[alias]
            table_detail = used_table_detail_dict[table_name]
            for dim in table_detail['dimsAndMetrics']['dimensions']:
                if dim['dimensionEnName'].lower() == column_name.lower():
                    filter_columns.append(
                        {"is_semantic_field": True, "semantic_type": "dimension", "id": dim["dimensionId"],
                         "dimension_name": dim["dimensionName"], "operator": operator, "value": value,
                         "wid": str(uuid.uuid4())}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            for metric in table_detail['dimsAndMetrics']['metrics']:
                if metric['expression'].lower() == column_name.lower():
                    filter_columns.append(
                        {"is_semantic_field": True, "semantic_type": "metric", "id": metric["metricId"],
                         "metric_name": metric["metricName"], "operator": operator, "value": value,
                         "wid": str(uuid.uuid4())}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            filter_columns.append(
                {"is_semantic_field": False, "sql_column": cond["field"], "operator": operator, "value": value,
                 "id": str(uuid.uuid4()), "wid": str(uuid.uuid4())})

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
                         "metric_name": metric["metricName"], "direction": direction, "wid": str(uuid.uuid4())}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            for dim in table_detail['dimsAndMetrics']['dimensions']:
                if dim['dimensionEnName'].lower() == column_name.lower():
                    order_by_columns.append(
                        {"is_semantic_field": True, "semantic_type": "dimension", "id": dim["dimensionId"],
                         "dimension_name": dim["dimensionName"], "direction": direction, "wid": str(uuid.uuid4())}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            order_by_columns.append(
                {"is_semantic_field": False, "sql_column": order_info['field'], "id": str(uuid.uuid4()),
                 "direction": direction, "wid": str(uuid.uuid4())})

        return order_by_columns

    def _process_limit(self, parts: Dict[str, Any]) -> Optional[int]:
        """处理LIMIT字段"""
        limit = parts['limit']
        if not limit:
            return None
        return int(limit)

    def _split_column(self, column: str) -> Tuple[str, str]:
        """将 'table.field' 格式的列名分割为 (table, field)"""
        parts = column.split('.')
        return (parts[0], parts[1]) if len(parts) == 2 else ("", column)

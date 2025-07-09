import logging
import re
import uuid
from tokenize import group
from typing import Any, List, Dict, Tuple, Optional, Set

from api.service.askdata_service.sql_components_parser import SQLComponentsParser
from api.service.askdata_service.util.parse_sql_extract import parse_sql_extract
from api.service.nl2sql_service.semantic_api_client import SemanticApiClient


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
        logging.info(f"used_table_detail_dict: {used_table_detail_dict}")
        logging.info(f"model_list: {model_list}")
        logging.info(f"sql_components: {sql_components}")
        logging.info(f"recommended_chart: {recommended_chart}")

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
                {"modelId": model["modelId"], "table": model["tableName"], "alias": alias,
                 "modelName": model["modelName"]})

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
            selected_dimensions = self._build_selected_dimensions(parts, used_table_detail_dict)

            # 6. 处理指标，指标其实就是select_columns字段减去group by字段
            selected_metrics = self._build_selected_metrics(parts, used_table_detail_dict)

            # 7. 处理where
            where_conditions = self._process_where_conditions(parts, used_table_detail_dict)

            # 8. 处理having
            having_conditions = self._process_having_conditions(parts, used_table_detail_dict)

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
                "where_conditions": {
                    "where_conditions": where_conditions,
                    "available_fields": semantic_fields_info["whereable_fields"]
                },
                "having_conditions": {
                    "having_conditions": having_conditions,
                    "available_fields": semantic_fields_info["havingable_fields"]
                },
                "order_by": {
                    "order_by_fields": order_by_fields,
                    "available_fields": semantic_fields_info["sortable_fields"]
                },
                "limit": limit,
                "all_semantic_fields": semantic_fields_info["all_fields"]
            }

    def _process_having_conditions(self, parts: Dict[str, Any], used_table_detail_dict: Dict[str, Any]) -> List[Dict]:
        """处理having条件"""
        table_alias_mapping = parts["table_alias_mapping"]
        having_conditions = parts['having_conditions']
        if having_conditions.get('has_or'):
            raw = having_conditions['raw_condition']
            return [{"is_semantic_field": False, "is_complex_condition": True, "raw_condition": raw, "field": raw,
                     "operator": "", "value": "", "from_model": None, "id": str(uuid.uuid4()),
                     "wid": str(uuid.uuid4())}]

        filter_columns = []
        for cond in having_conditions.get('parsed_conditions', []):
            is_matched_semantic_field = False
            table_alias, column_name = self._get_table_alias_and_field_by_split_column(cond['field'])
            operator = cond['operator']
            value = cond['value']
            table_name = table_alias_mapping[table_alias]
            table_detail = used_table_detail_dict[table_name]
            for metric in table_detail['dimsAndMetrics']['metrics']:
                if metric['expression'].lower() == column_name.lower():
                    filter_columns.append(
                        {"is_semantic_field": True, "semantic_type": "metric", "id": metric["metricId"],
                         "metric_name": metric["metricName"], "operator": operator, "value": value,
                         "wid": str(uuid.uuid4()), "original_sql_component": cond}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            filter_columns.append(
                {"is_semantic_field": False, "sql_column": cond["field"], "operator": operator, "value": value,
                 "id": str(uuid.uuid4()), "wid": str(uuid.uuid4())})

        return filter_columns

    def _build_selected_dimensions(self, parts: Dict[str, Any], used_table_detail_dict: Dict[str, Any]) -> List[Dict]:
        """构建group by维度字典"""
        try:
            group_by_dimensions = []
            table_alias_mapping = parts["table_alias_mapping"]
            for group_by in parts["group_by"]:
                is_matched_semantic_field = False
                split_col = self._get_table_alias_and_field_by_split_column(group_by)
                if len(split_col) == 2:
                    table_alias, column_name = self._get_table_alias_and_field_by_split_column(group_by)
                    table_name = table_alias_mapping[table_alias]
                    table_detail = used_table_detail_dict[table_name]
                    is_timeseries = False
                    time_unit = None
                    time_source = None
                    if column_name.lower().startswith("extract("):
                        parsed_result = parse_sql_extract(column_name)
                        if parsed_result['unit'] is not None and parsed_result['source'] is not None:
                            time_unit = parsed_result['unit']
                            time_source = parsed_result['source']
                        if time_unit and time_source:
                            is_timeseries = True
                    for dim in table_detail['dimsAndMetrics']['dimensions']:
                        if is_timeseries and dim['dimensionEnName'].lower() == time_source.lower():
                            group_by_dimensions.append(
                                {"is_semantic_field": True, "semantic_type": "dimension(timeseries)", "id": dim["dimensionId"],
                                 "dimension_name": dim["dimensionName"], "wid": str(uuid.uuid4()),
                                 "nanoId": str(uuid.uuid4()), "original_sql_component": group_by, "unit": time_unit}
                            )
                            is_matched_semantic_field = True
                            break
                        if dim['dimensionEnName'].lower() == column_name.lower():
                            group_by_dimensions.append(
                                {"is_semantic_field": True, "semantic_type": "dimension", "id": dim["dimensionId"],
                                 "dimension_name": dim["dimensionName"], "wid": str(uuid.uuid4()),
                                 "nanoId": str(uuid.uuid4()), "original_sql_component": group_by}
                            )
                            is_matched_semantic_field = True
                            break
                    if is_matched_semantic_field:
                        continue

                if not is_matched_semantic_field:
                    group_by_dimensions.append({
                        "is_semantic_field": False,
                        "sql_column": group_by,
                        "id": str(uuid.uuid4()),
                        "wid": str(uuid.uuid4()),
                        "nanoId": str(uuid.uuid4())
                    })
            return group_by_dimensions
        except Exception as e:
            logging.exception(f"parts: {parts}")
            logging.exception(f"used_table_detail_dict: {used_table_detail_dict}")
            return []

    def _build_selected_metrics(self, parts: Dict[str, Any], used_table_detail_dict: Dict[str, Any]) -> List[Dict]:
        selected_metrics = []
        selected_columns = parts["select_columns"]
        group_by = parts['group_by']
        table_alias_mapping = parts["table_alias_mapping"]
        metrics = list(set(selected_columns) - set(group_by))
        for metric in metrics:
            is_matched_semantic_field = False
            split_col = self._get_table_alias_and_field_by_split_column(metric)
            if len(split_col) == 2:
                table_alias, column_name = self._get_table_alias_and_field_by_split_column(metric)
                if column_name.lower() == "count(*)".lower():
                    selected_metrics.append(
                        {"is_semantic_field": False, "sql_column": metric, "id": str(uuid.uuid4()),
                         "wid": str(uuid.uuid4()), "nanoId": str(uuid.uuid4()), "original_sql_component": metric}
                    )
                    continue
                table_name = table_alias_mapping[table_alias]
                table_detail = used_table_detail_dict[table_name]
                for metric in table_detail['dimsAndMetrics']['metrics']:
                    if metric['expression'].lower() == column_name.lower():
                        selected_metrics.append(
                            {"is_semantic_field": True, "semantic_type": "metric", "id": metric["metricId"],
                             "metric_name": metric["metricName"], "wid": str(uuid.uuid4()),
                             "nanoId": str(uuid.uuid4()), "original_sql_component": metric}
                        )
                        is_matched_semantic_field = True
                        break
                if is_matched_semantic_field:
                    continue
            if not is_matched_semantic_field:
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
        available_dimensions, available_metrics, sortable_fields, whereable_fields, havingable_fields, all_fields = [], [], [], [], [], []
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
                havingable_fields.append(
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
                whereable_fields.append(
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
            "sortable_fields": sortable_fields, "whereable_fields": whereable_fields,
            "havingable_fields": havingable_fields, "all_fields": all_fields
        }

    def _process_select_columns(self, parts: Dict[str, Any], used_table_detail_dict: Dict[str, Any]) -> List[Dict]:
        """处理SELECT列"""
        selected_columns = []
        table_alias_mapping = parts["table_alias_mapping"]
        for col in parts["select_columns"]:
            is_matched_semantic_field = False
            split_col = self._get_table_alias_and_field_by_split_column(col)
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
                     "wid": str(uuid.uuid4()), "original_sql_component": cond})
                continue
            table_alias, column_name = self._get_table_alias_and_field_by_split_column(cond['field'])
            operator = cond['operator']
            value = cond['value']
            table_name = table_alias_mapping[table_alias]
            table_detail = used_table_detail_dict[table_name]
            for dim in table_detail['dimsAndMetrics']['dimensions']:
                if dim['dimensionEnName'].lower() == column_name.lower():
                    filter_columns.append(
                        {"is_semantic_field": True, "semantic_type": "dimension", "id": dim["dimensionId"],
                         "dimension_name": dim["dimensionName"], "operator": operator, "value": value,
                         "wid": str(uuid.uuid4()), "original_sql_component": cond}
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
                         "wid": str(uuid.uuid4()), "original_sql_component": cond}
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
            table_alias, column_name = self._get_table_alias_and_field_by_split_column(order_info['field'])
            direction = order_info['direction']
            table_name = table_alias_mapping.get(table_alias, None)
            if not table_name:
                order_by_columns.append(
                    {"is_semantic_field": False, "sql_column": order_info['field'], "id": str(uuid.uuid4()),
                     "direction": direction, "wid": str(uuid.uuid4())})
                continue
            table_detail = used_table_detail_dict[table_name]
            for metric in table_detail['dimsAndMetrics']['metrics']:
                if metric['expression'].lower() == column_name.lower():
                    order_by_columns.append(
                        {"is_semantic_field": True, "semantic_type": "metric", "id": metric["metricId"],
                         "metric_name": metric["metricName"], "direction": direction, "wid": str(uuid.uuid4()),
                         "original_sql_component": order_info}
                    )
                    is_matched_semantic_field = True
                    break
            if is_matched_semantic_field:
                continue
            for dim in table_detail['dimsAndMetrics']['dimensions']:
                if dim['dimensionEnName'].lower() == column_name.lower():
                    order_by_columns.append(
                        {"is_semantic_field": True, "semantic_type": "dimension", "id": dim["dimensionId"],
                         "dimension_name": dim["dimensionName"], "direction": direction, "wid": str(uuid.uuid4()),
                         "original_sql_component": order_info}
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

    def _get_table_alias_and_field_by_split_column(self, column: str) -> Tuple[str, str]:
        """
        将列名分割为 (table, field)
        支持以下格式：
        - 'table.field' -> ('table', 'field')
        - 'COUNT(table.field)' -> ('table', 'COUNT(field)')
        - 'COUNT(table.field) AS alias' -> ('table', 'COUNT(field)')
        - 'EXTRACT(YEAR FROM t1.hire_date)' -> ('t1', 'EXTRACT(YEAR FROM hire_date)')
        - 'SUBSTRING(t1.name FROM 1 FOR 10)' -> ('t1', 'SUBSTRING(name FROM 1 FOR 10)')
        - 'CASE WHEN t1.status = 1 THEN t1.name END' -> ('t1', 'CASE WHEN status = 1 THEN name END')
        - 'field' -> ('', 'field')
        - 'COUNT(field)' -> ('', 'COUNT(field)')
        """
        # 去除前后空格
        column = column.strip()

        # 处理 AS 别名，取 AS 前面的部分
        if ' AS ' in column.upper():
            column = column.split(' AS ')[0].strip()

        # 查找所有表别名引用 (格式: alias.field)
        table_refs = self._find_table_references(column)
        
        if not table_refs:
            # 没有找到表引用，检查是否是简单的聚合函数
            func_pattern = r'^([A-Z_]+)\((.*)\)$'
            match = re.match(func_pattern, column, re.IGNORECASE)
            if match:
                func_name = match.group(1)
                func_arg = match.group(2).strip()
                
                # 处理 DISTINCT 关键字
                distinct_prefix = ""
                if func_arg.upper().startswith('DISTINCT '):
                    distinct_prefix = "DISTINCT "
                    func_arg = func_arg[9:].strip()
                
                # 检查函数参数中是否有表前缀
                if '.' in func_arg and not ' ' in func_arg:
                    # 简单的 table.field 格式
                    parts = func_arg.split('.')
                    table = parts[0].strip()
                    field = parts[1].strip()
                    new_column = f"{func_name}({distinct_prefix}{field})"
                    return (table, new_column)
                else:
                    # 函数参数中没有表前缀或者是复杂表达式
                    if distinct_prefix:
                        new_column = f"{func_name}({distinct_prefix}{func_arg})"
                    else:
                        new_column = column
                    return ("", new_column)
            else:
                # 不是函数，按原逻辑处理
                parts = column.split('.')
                return (parts[0], parts[1]) if len(parts) == 2 else ("", column)
        
        # 有表引用，选择主要的表别名（出现次数最多的）
        main_table = max(table_refs, key=table_refs.count) if table_refs else ""
        
        # 移除主要表别名，生成新的列表达式
        new_column = self._remove_table_prefix(column, main_table)
        
        return (main_table, new_column)
    
    def _find_table_references(self, expression: str) -> List[str]:
        """
        查找表达式中的所有表别名引用
        返回表别名列表
        """
        # 匹配 alias.field 格式，但排除数字开头的（如 1.5）
        pattern = r'\b([a-zA-Z_][a-zA-Z0-9_]*)\.'
        matches = re.findall(pattern, expression)
        
        # 过滤掉SQL关键字和数字
        sql_keywords = {
            'SELECT', 'FROM', 'WHERE', 'JOIN', 'INNER', 'LEFT', 'RIGHT', 'OUTER',
            'ON', 'AND', 'OR', 'NOT', 'IN', 'EXISTS', 'CASE', 'WHEN', 'THEN',
            'ELSE', 'END', 'AS', 'DISTINCT', 'GROUP', 'BY', 'ORDER', 'HAVING',
            'LIMIT', 'OFFSET', 'UNION', 'ALL', 'EXTRACT', 'SUBSTRING', 'CAST',
            'CONVERT', 'DATEPART', 'DATEDIFF', 'YEAR', 'MONTH', 'DAY'
        }
        
        table_refs = []
        for match in matches:
            if match.upper() not in sql_keywords and not match.isdigit():
                table_refs.append(match)
        
        return table_refs
    
    def _remove_table_prefix(self, expression: str, table_alias: str) -> str:
        """
        从表达式中移除指定的表别名前缀
        例如: 'EXTRACT(YEAR FROM t1.hire_date)' -> 'EXTRACT(YEAR FROM hire_date)'
        """
        if not table_alias:
            return expression
        
        # 使用正则表达式匹配 table_alias. 但不匹配在字符串或标识符中间的
        # 确保匹配的是完整的表别名，不是标识符的一部分
        pattern = r'\b' + re.escape(table_alias) + r'\.'
        
        # 替换所有匹配的表前缀
        result = re.sub(pattern, '', expression)
        
        return result

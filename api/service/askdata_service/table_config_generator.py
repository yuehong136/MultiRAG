import uuid
from typing import Any, List, Dict, Tuple

from api.service.askdata_service.field_mapper import FieldMapper
from api.service.askdata_service.sql_parser import SQLParser
from api.service.nl2sql_service.semantic_api_client import SemanticApiClient


class TableConfigGenerator:
    """
    负责从SQL查询和语义模型生成表格配置。
    """

    def __init__(self, semantic_api_client: SemanticApiClient):
        self.semantic_api_client = semantic_api_client

    async def generate(self, sql: str, model_ids: List[str], used_models: List[str]) -> Dict[str, Any]:
        """
        生成表配置信息，将SQL解析结果映射到语义层。

        Args:
            sql: SQL查询语句
            model_ids: 所有相关的模型ID列表
            used_models: SQL中实际使用的模型名称列表

        Returns:
            包含列、过滤器和排序信息的配置字典
        """
        # 1. 解析SQL
        parser = SQLParser(sql)
        parts = parser.parse_all()

        # 2. 构建使用的模型和表的详情字典
        _, used_table_detail_dict, model_list = await self._build_model_details(
            model_ids, used_models
        )

        # 3. 创建字段映射器
        field_mapper = FieldMapper(used_table_detail_dict)

        # 4. 一次性构建所有语义字段信息
        semantic_fields_info = await self._build_semantic_fields_info(model_list)

        # 5. 处理SELECT列
        selected_columns = self._process_select_columns(parts["select_columns_full"], field_mapper)

        # 6. 处理WHERE条件
        filter_conditions = self._process_where_conditions(parts['where_conditions_detailed'], field_mapper)

        # 7. 处理ORDER BY字段
        order_by_fields = self._process_order_by_fields(parts['order_by_fields_detailed'], field_mapper)

        # 8. 构建返回结果
        return {
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
                    {"is_allow_use": not is_agg, "semantic_type": "metric", "id": metric["metricId"]})
                if not is_agg:
                    filterable_fields.append(
                        {"semantic_type": "metric", "id": metric["metricId"], "from_model": model_name,
                         "from_model_id": model_id})
                    sortable_fields.append({"is_allow_use": True, "semantic_type": "metric", "id": metric["metricId"],
                                            "from_model": model_id})
                all_fields.append({"semantic_type": "metric", "semantic_field": metric, "id": metric["metricId"],
                                   "from_model": model_name, "from_model_id": model_id})

            for dim in model["dimsAndMetrics"]["dimensions"]:
                dim_id = dim["dimensionId"]
                dim['possibleValues'] = dimensions_value_dict.get(dim_id, [])
                available_fields.append({"is_allow_use": True, "semantic_type": "dimension", "id": dim_id})
                filterable_fields.append(
                    {"semantic_type": "dimension", "id": dim_id, "from_model": model_name, "from_model_id": model_id})
                sortable_fields.append(
                    {"is_allow_use": True, "semantic_type": "dimension", "id": dim_id, "from_model": model_id})
                all_fields.append(
                    {"semantic_type": "dimension", "semantic_field": dim, "id": dim_id, "from_model": model_name,
                     "from_model_id": model_id})

        return {
            "available_fields": available_fields, "filterable_fields": filterable_fields,
            "sortable_fields": sortable_fields, "all_fields": all_fields
        }

    def _process_select_columns(self, sql_columns: List[str], field_mapper: FieldMapper) -> List[Dict]:
        """处理SELECT列"""
        return [field_mapper.map_to_semantic_field(*self._split_column(col), col) for col in sql_columns]

    def _process_where_conditions(self, where_conditions: Dict, field_mapper: FieldMapper) -> List[Dict[str, Any]]:
        """处理WHERE条件"""
        if where_conditions.get('has_or'):
            raw = where_conditions['raw_condition']
            return [{"is_semantic_field": False, "is_complex_condition": True, "raw_condition": raw, "field": raw,
                     "operator": "", "value": "", "from_model": None, "id": str(uuid.uuid4())}]

        conditions = []
        for cond in where_conditions.get('parsed_conditions', []):
            table, field = self._split_column(cond['full_field'])
            mapped = field_mapper.map_to_filter_condition(table, field, cond['full_field'], cond['operator'],
                                                          cond['value'])
            conditions.append(mapped)
        return conditions

    def _process_order_by_fields(self, order_by_fields: List[Dict], field_mapper: FieldMapper) -> List[Dict[str, Any]]:
        """处理ORDER BY字段"""
        fields = []
        for order_info in order_by_fields:
            table, field = self._split_column(order_info['full_field'])
            mapped = field_mapper.map_to_order_by_field(table, field, order_info['full_field'], order_info['direction'])
            fields.append(mapped)
        return fields

    def _split_column(self, column: str) -> Tuple[str, str]:
        """将 'table.field' 格式的列名分割为 (table, field)"""
        parts = column.split('.')
        return (parts[0], parts[1]) if len(parts) == 2 else ("", column)


import uuid
from typing import Any, Dict


class FieldMapper:
    """字段映射器，负责将SQL字段映射到语义层"""

    def __init__(self, used_table_detail_dict: Dict[str, Any]):
        """
        初始化字段映射器。

        Args:
            used_table_detail_dict: 一个字典，键是表名，值是该表的模型详情，
                                  其中必须包含 'dimsAndMetrics'。
        """
        self.used_table_detail_dict = used_table_detail_dict

    def map_to_semantic_field(self, table: str, field: str, sql_column: str):
        """将表字段映射到语义字段"""
        if table not in self.used_table_detail_dict:
            return {"is_semantic_field": False, "sql_column": sql_column,
                    "id": str(uuid.uuid4())}

        model_detail = self.used_table_detail_dict[table]

        # 先检查指标
        semantic_field = self._find_in_metrics(sql_column, model_detail)
        if semantic_field:
            return {"is_semantic_field": True, "semantic_type": "metric",
                    "id": semantic_field["metricId"]}

        # 再检查维度
        semantic_field = self._find_in_dimensions(field, model_detail)
        if semantic_field:
            return {"is_semantic_field": True, "semantic_type": "dimension",
                    "id": semantic_field["dimensionId"]}

        # 未找到匹配
        return {"is_semantic_field": False, "sql_column": sql_column,
                "id": str(uuid.uuid4())}

    def _find_in_metrics(self, sql_column: str, model_detail: Dict):
        """在指标中查找匹配的字段"""
        for metric in model_detail["dimsAndMetrics"]["metrics"]:
            if metric["expression"].lower() == sql_column.lower():
                return metric
        return None

    def _find_in_dimensions(self, field: str, model_detail: Dict):
        """在维度中查找匹配的字段"""
        for dimension in model_detail["dimsAndMetrics"]["dimensions"]:
            if dimension["dimensionEnName"].lower() == field.lower():
                return dimension
        return None

    def map_to_filter_condition(self, table: str, field: str, full_field: str,
                                operator: str, value: str):
        """将表字段映射到过滤条件，返回更丰富的信息结构"""
        if table not in self.used_table_detail_dict:
            return {
                "is_semantic_field": False,
                "semantic_type": None,
                "field": full_field,
                "operator": operator,
                "value": value,
                "from_model": None,
                "id": str(uuid.uuid4())
            }

        model_detail = self.used_table_detail_dict[table]
        model_id = model_detail['modelId']

        for dimension in model_detail["dimsAndMetrics"]["dimensions"]:
            if dimension["dimensionEnName"].lower() == field.lower():
                return {
                    "is_semantic_field": True,
                    "semantic_type": "dimension",
                    "field": full_field,
                    "operator": operator,
                    "value": value,
                    "from_model": model_id,
                    "id": dimension["dimensionId"]
                }

        for metric in model_detail["dimsAndMetrics"]["metrics"]:
            if metric["expression"].lower() == full_field.lower():
                return {
                    "is_semantic_field": True,
                    "semantic_type": "metric",
                    "field": full_field,
                    "operator": operator,
                    "value": value,
                    "from_model": model_id,
                    "id": metric["metricId"]
                }

        return {
            "is_semantic_field": False,
            "semantic_type": None,
            "field": full_field,
            "operator": operator,
            "value": value,
            "from_model": model_id,
            "id": str(uuid.uuid4())
        }

    def map_to_order_by_field(self, table: str, field: str, full_field: str,
                              direction: str):
        """将表字段映射到排序字段，返回更丰富的信息结构"""
        if table not in self.used_table_detail_dict:
            return {
                "is_semantic_field": False,
                "semantic_type": None,
                "field": full_field,
                "direction": direction,
                "from_model": None,
                "id": str(uuid.uuid4())
            }

        model_detail = self.used_table_detail_dict[table]
        model_id = model_detail['modelId']

        for metric in model_detail["dimsAndMetrics"]["metrics"]:
            if metric["expression"].lower() == field.lower():
                return {
                    "is_semantic_field": True,
                    "semantic_type": "metric",
                    "field": full_field,
                    "direction": direction,
                    "from_model": model_id,
                    "id": metric["metricId"]
                }

        for dimension in model_detail["dimsAndMetrics"]["dimensions"]:
            if dimension["dimensionEnName"].lower() == field.lower():
                return {
                    "is_semantic_field": True,
                    "semantic_type": "dimension",
                    "field": full_field,
                    "direction": direction,
                    "from_model": model_id,
                    "id": dimension["dimensionId"]
                }

        return {
            "is_semantic_field": False,
            "semantic_type": None,
            "field": full_field,
            "direction": direction,
            "from_model": model_id,
            "id": str(uuid.uuid4())
        }

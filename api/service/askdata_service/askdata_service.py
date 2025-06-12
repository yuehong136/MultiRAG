import os
import logging
import uuid
from typing import Any, List, Dict, Optional, Tuple

from fastapi.params import Depends
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.db.services.ask_data_history_service import AskDataHistoryService
from api.service.askdata_service.event.event_utils import send_event
from api.service.askdata_service.llm_sql_query_generator import NLQToInitialSQLGenerator
from api.service.askdata_service.process_semantic_layer import process_semantic_layer
from api.service.askdata_service.sql_parser import SQLParser
from api.service.nl2sql_service.custom_jieba_tokenizer import custom_tokenize_with_semantic_words
from api.service.nl2sql_service.query_intent_analyzer import QueryIntentAnalyzer
from api.service.nl2sql_service.query_rewriter import QueryRewriter
from api.service.nl2sql_service.semantic_api_client import SemanticApiClient

logger = logging.getLogger(__name__)


class FieldMapper:
    """字段映射器，负责将SQL字段映射到语义层"""

    def __init__(self, used_table_detail_dict: Dict[str, Any]):
        self.used_table_detail_dict = used_table_detail_dict

    def map_to_semantic_field(self, table: str, field: str, sql_column: str):
        """将表字段映射到语义字段"""
        if table not in self.used_table_detail_dict:
            return "unknown"

        model_detail = self.used_table_detail_dict[table]
        model_id = model_detail['modelId']

        # 先检查指标
        semantic_field = self._find_in_metrics(field, sql_column, model_detail, model_id)
        if semantic_field:
            return {"is_semantic_field": True, "semantic_type": "metric",
                    "id": semantic_field["metricId"]}

        # 再检查维度
        semantic_field = self._find_in_dimensions(field, sql_column, model_detail, model_id)
        if semantic_field:
            return {"is_semantic_field": True, "semantic_type": "dimension",
                    "id": semantic_field["dimensionId"]}

        # 未找到匹配
        return {"is_semantic_field": False, "sql_column": sql_column,
                "id": str(uuid.uuid4())}

    def _find_in_metrics(self, field: str, sql_column: str, model_detail: Dict, model_id: str):
        """在指标中查找匹配的字段"""
        for metric in model_detail["dimsAndMetrics"]["metrics"]:
            if metric["expression"].lower() == sql_column.lower():
                return metric
        return None

    def _find_in_dimensions(self, field: str, column: str, model_detail: Dict, model_id: str):
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

        # 未找到匹配的语义字段
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

        # 先检查指标
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

        # 再检查维度
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

        # 未找到匹配的语义字段
        return {
            "is_semantic_field": False,
            "semantic_type": None,
            "field": full_field,
            "direction": direction,
            "from_model": model_id,
            "id": str(uuid.uuid4())
        }


class AskdataService:
    """服务类，用于处理自然语言到SQL的转换和查询重写。"""

    def __init__(self, db: Session, user: Any):
        self.db = db
        self.user = user
        self.prompt_dir = os.path.join(os.path.dirname(__file__), "prompt")
        # 初始化查询重写器
        self.query_rewriter = QueryRewriter(db, user.id, self.prompt_dir)
        # 初始化查询意图分析器
        self.query_intent_analyzer = QueryIntentAnalyzer(db, user.id, self.prompt_dir)
        self.nlq_to_initial_sql_generator = NLQToInitialSQLGenerator(db, user.id, self.prompt_dir)

        self.semantic_api_client = SemanticApiClient()
        # 初始化历史记录服务
        self.history_service = AskDataHistoryService()

    async def rewrite_query(self, query_text: str, llm_name: str) -> List[str]:
        """
        使用LLM重写自然语言查询，生成多个变体。

        参数:
            query_text: 原始自然语言查询文本
            llm_name: 用于重写的LLM模型名称

        返回:
            重写后的查询变体列表
        """
        return await self.query_rewriter.rewrite_query(query_text, llm_name)

    async def analyze_query_intent(self, query_text: str, llm_name: str) -> List[Dict[str, str]]:
        """
        使用LLM分析自然语言查询意图
        """
        return await self.query_intent_analyzer.get_query_intents_with_descriptions(query_text, llm_name)

    async def generate_semantic_layer(self, user_query: str, dataset_id_list: List[str],
                                      conversation_id: Optional[str] = None,
                                      event_id: Optional[str] = None):
        await send_event(event_id, {"message": "分词", "action": "start"}, "message")
        segmented_words = await custom_tokenize_with_semantic_words(text=user_query, dataset_id_list=dataset_id_list)
        await send_event(event_id, {"message": "分词", "action": "complete"}, "message")
        await send_event(event_id, {"message": "分词结果", "data": segmented_words}, "data")

        await send_event(event_id, {"message": "获取维度信息", "action": "start"}, "message")
        # 1. 将分词到语义层结构化数据中进行检索得到相关数据
        # 2. 根据分词关键字获得维度列表
        dimensions_by_keyword = await self.semantic_api_client.get_dimension_info_by_keyword_async(
            keyword=segmented_words,
            dataset_ids=dataset_id_list)
        await send_event(event_id, {"message": "获取维度信息", "action": "complete"}, "message")
        # 3. 分词关键字作为维度值关键字获得获得维度列表
        dimensions_by_value = await self.semantic_api_client.get_dimension_by_dimension_value_async(
            keyword=segmented_words,
            dataset_ids=dataset_id_list)
        # 4. 根据dimensionId对dimensions_by_keyword和dimensions_by_value进行维度去重，获得最终维度列表
        unique_dimensions = self._deduplicate_dimensions(dimensions_by_keyword, dimensions_by_value)
        dimension_values = await self.semantic_api_client.get_dimension_values_async(dimension_ids=unique_dimensions)
        dimensions = await self.semantic_api_client.get_dimension_info_by_id_async(dimension_ids=unique_dimensions)
        await send_event(event_id, {"message": "获取维度信息", "action": "complete"}, "message")
        await send_event(event_id, {"message": "维度信息", "data": dimensions}, "data")
        # 5. 根据分词关键字获得指标列表
        await send_event(event_id, {"message": "获取指标信息", "action": "start"}, "message")
        metrics = await self.semantic_api_client.get_metric_info_by_keyword_async(
            keyword=segmented_words,
            dataset_ids=dataset_id_list)
        await send_event(event_id, {"message": "获取指标信息", "action": "complete"}, "message")
        await send_event(event_id, {"message": "指标信息", "data": metrics}, "data")

        # 6. 从维度和指标中提取所有modelId并去重，获得模型ID列表
        model_ids = self._extract_unique_model_ids(dimensions, metrics)

        # 7. 查询模型详情和关联关系
        await send_event(event_id, {"message": "获取模型信息", "action": "start"}, "message")
        model_details = await self.semantic_api_client.get_model_detail_async(model_ids=model_ids)
        await send_event(event_id, {"message": "获取模型信息", "action": "complete"}, "message")
        await send_event(event_id, {"message": "模型信息", "data": model_details}, "data")
        model_relations = await self.semantic_api_client.get_model_relationships_async(model_ids=model_ids)
        await send_event(event_id, {}, "stream_end")
        # 8. 查询业务术语
        dataset_details = await self.semantic_api_client.get_dataset_detail_async(dataset_ids=dataset_id_list)
        domain_ids = self._extract_unique_domain_ids(dataset_details)
        business_term_rows = await self.semantic_api_client.get_business_term_info_async(keyword=segmented_words,
                                                                                         domain_ids=domain_ids)
        semantic_layer_original = dict(dataset_details=dataset_details, dimensions=dimensions,
                                       dimension_values=dimension_values,
                                       metrics=metrics, model_details=model_details,
                                       model_relations=model_relations, business_term_rows=business_term_rows)

        processed_semantic_layer = process_semantic_layer(semantic_layer_original)

        return processed_semantic_layer, model_ids

    async def nlq_to_initial_sql(self, user_query: str, llm_name: str, semantic_layer: Dict[str, Any]):
        result = await self.nlq_to_initial_sql_generator.generate_sql_query_with_models(user_query, semantic_layer,
                                                                                        llm_name)
        if result:
            sql = result['sql']
            used_models = result['usedModels']

        return sql, used_models

    async def generate_table_config(self, sql: str, dataset_id_list: List[str],
                                    model_ids: List[str], used_models: List[str]) -> Dict[str, Any]:
        """
        生成表配置信息，将SQL解析结果映射到语义层

        Args:
            sql: SQL查询语句
            dataset_id_list: 数据集ID列表
            model_ids: 模型ID列表
            used_models: 使用的模型名称列表

        Returns:
            包含列、模型、过滤条件和排序信息的配置字典
        """
        # 1. 解析SQL
        parser = SQLParser(sql)
        parts = parser.parse_all()

        # 2. 构建使用的模型和表的详情字典
        used_model_detail_dict, used_table_detail_dict, model_list = await self._build_model_details(
            model_ids, used_models
        )

        # 3. 创建字段映射器
        field_mapper = FieldMapper(used_table_detail_dict)

        # 4. 一次性构建所有语义字段信息（优化：避免重复遍历）
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

    async def _build_semantic_fields_info(self, model_list: List[Dict]) -> Dict[str, List]:
        """
        一次性构建所有语义字段信息，避免重复遍历

        Returns:
            Dict包含:
            - available_fields: 可选择的字段（用于列选择）
            - filterable_fields: 可过滤的字段
            - sortable_fields: 可排序的字段
            - all_fields: 所有语义字段详细信息
        """
        available_fields = []
        filterable_fields = []
        sortable_fields = []
        all_fields = []

        # 收集所有维度ID以批量获取维度值
        all_dimension_ids = []
        for model in model_list:
            for dimension in model["dimsAndMetrics"]["dimensions"]:
                all_dimension_ids.append(dimension['dimensionId'])

        # 批量获取维度值
        dimensions_value_dict = await self.semantic_api_client.get_dimension_values_async(
            dimension_ids=all_dimension_ids
        ) if all_dimension_ids else {}

        # 一次遍历构建所有字段信息
        for model in model_list:
            model_id = model["modelId"]
            model_name = model["modelName"]

            # 处理指标
            for metric in model["dimsAndMetrics"]["metrics"]:
                metric_id = metric["metricId"]
                is_aggregated = "(" in metric["expression"].lower()

                # 可选择字段
                available_fields.append({
                    "is_allow_use": not is_aggregated,
                    "semantic_type": "metric",
                    "id": metric_id
                })

                # 可过滤和排序字段（仅非聚合指标）
                if not is_aggregated:
                    filterable_fields.append({
                        "semantic_type": "metric",
                        "id": metric_id,
                        "from_model": model_name,
                        "from_model_id": model_id
                    })

                    sortable_fields.append({
                        "is_allow_use": True,
                        "semantic_type": "metric",
                        "id": metric_id,
                        "from_model": model_id
                    })

                # 所有字段详细信息
                all_fields.append({
                    "semantic_type": "metric",
                    "semantic_field": metric,
                    "id": metric_id,
                    "from_model": model_name,
                    "from_model_id": model_id
                })

            # 处理维度
            for dimension in model["dimsAndMetrics"]["dimensions"]:
                dimension_id = dimension["dimensionId"]

                # 添加维度值到维度信息中
                dimension['possibleValues'] = dimensions_value_dict.get(dimension_id, [])

                # 可选择字段
                available_fields.append({
                    "is_allow_use": True,
                    "semantic_type": "dimension",
                    "id": dimension_id
                })

                # 可过滤字段
                filterable_fields.append({
                    "semantic_type": "dimension",
                    "id": dimension_id,
                    "from_model": model_name,
                    "from_model_id": model_id
                })

                # 可排序字段
                sortable_fields.append({
                    "is_allow_use": True,
                    "semantic_type": "dimension",
                    "id": dimension_id,
                    "from_model": model_id
                })

                # 所有字段详细信息
                all_fields.append({
                    "semantic_type": "dimension",
                    "semantic_field": dimension,
                    "id": dimension_id,
                    "from_model": model_name,
                    "from_model_id": model_id
                })

        return {
            "available_fields": available_fields,
            "filterable_fields": filterable_fields,
            "sortable_fields": sortable_fields,
            "all_fields": all_fields
        }

    async def add_ask_data_history(self, conversation_id: str, ask_id: str, data: str):
        """
        添加一条问数历史记录
        """
        return self.history_service.add_history(self.db, conversation_id, ask_id, data, self.user.id)

    async def get_ask_data_history(self, conversation_id: str) -> list[dict]:
        """
        根据对话ID获取问数历史记录
        """
        return self.history_service.get_history_by_conversation_id(self.db, conversation_id)

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

    def _process_select_columns(self, sql_columns: List[str],
                                field_mapper: FieldMapper):
        """处理SELECT列"""
        selected_columns = []

        for sql_column in sql_columns:
            table, field = self._split_column(sql_column)
            selected_columns.append(field_mapper.map_to_semantic_field(table, field, sql_column))

        return selected_columns

    def _process_where_conditions(self, where_conditions: Dict,
                                  field_mapper: FieldMapper) -> List[Dict[str, Any]]:
        """处理WHERE条件，返回详细的条件信息"""
        filter_list = []

        if where_conditions['has_or']:
            # 如果包含OR，返回原始条件
            filter_list.append({
                "is_semantic_field": False,
                "is_complex_condition": True,
                "raw_condition": where_conditions['raw_condition'],
                "field": where_conditions['raw_condition'],
                "operator": "",
                "value": "",
                "from_model": None,
                "id": str(uuid.uuid4())
            })
        else:
            # 处理AND连接的条件
            for condition in where_conditions['parsed_conditions']:
                full_field = condition['full_field']
                table, field = self._split_column(full_field)

                filter_condition = field_mapper.map_to_filter_condition(
                    table, field, full_field,
                    condition['operator'], condition['value']
                )
                filter_list.append(filter_condition)

        return filter_list

    def _process_order_by_fields(self, order_by_fields: List[Dict],
                                 field_mapper: FieldMapper) -> List[Dict[str, Any]]:
        """处理ORDER BY字段，返回详细的排序信息"""
        order_by_list = []

        for order_info in order_by_fields:
            full_field = order_info['full_field']
            table, field = self._split_column(full_field)

            order_by_field = field_mapper.map_to_order_by_field(
                table, field, full_field, order_info['direction']
            )
            order_by_list.append(order_by_field)

        return order_by_list

    def _split_column(self, column: str) -> Tuple[str, str]:
        """分割列名为表名和字段名"""
        parts = column.split('.')
        if len(parts) == 2:
            return parts[0], parts[1]
        return "", column

    def _extract_unique_model_ids(self, dimensions: List[Any], metrics: List[Any]) -> List[str]:
        """
        从维度和指标数据中提取所有modelId并去重

        参数:
            dimensions: 维度列表
            metrics: 指标列表

        返回:
            去重后的模型ID列表
        """
        # 创建一个集合来存储唯一的模型ID
        unique_model_ids = set()

        # 从维度列表中提取modelId
        for dimension in dimensions:
            model_id = dimension.get('modelId')
            if model_id:
                unique_model_ids.add(model_id)

        # 从指标列表中提取modelId
        for metric in metrics:
            model_id = metric.get('modelId')
            if model_id:
                unique_model_ids.add(model_id)

        # 将集合转换为列表并返回
        return list(unique_model_ids)

    def _deduplicate_dimensions(self, dimensions_by_keyword: List[Any], dimensions_by_value: List[Any]) -> List[str]:
        """
        根据dimensionId对两个维度列表进行去重合并，只返回去重后的维度ID列表

        参数:
            dimensions_by_keyword: 通过关键字搜索得到的维度列表
            dimensions_by_value: 通过维度值搜索得到的维度列表

        返回:
            去重后的维度ID列表
        """
        # 创建一个集合来存储唯一的维度ID
        unique_dimension_ids = set()

        # 首先处理关键字搜索结果
        for dimension in dimensions_by_keyword:
            dimension_id = dimension.get('dimensionId')
            if dimension_id:
                unique_dimension_ids.add(dimension_id)

        # 然后处理维度值搜索结果
        for dimension in dimensions_by_value:
            dimension_id = dimension.get('dimensionId')
            if dimension_id:
                unique_dimension_ids.add(dimension_id)

        # 将集合转换为列表并返回
        return list(unique_dimension_ids)

    def _extract_unique_domain_ids(self, dataset_details: List[Any]) -> List[str]:
        """
        从数据集详情列表中提取所有domainId并去重

        参数:
            dataset_details: 数据集详情列表

        返回:
            去重后的领域ID列表
        """
        # 创建一个集合来存储唯一的领域ID
        unique_domain_ids = set()

        # 从数据集详情列表中提取domainId
        for dataset in dataset_details:
            domain_id = dataset.get('domainId')
            if domain_id:
                unique_domain_ids.add(domain_id)

        # 将集合转换为列表并返回
        return list(unique_domain_ids)


def get_askdata_service(db: Session = Depends(get_db), user=Depends(manager)) -> AskdataService:
    """通过依赖注入获取AskdataService实例"""
    return AskdataService(db, user)

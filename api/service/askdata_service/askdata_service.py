import os
import logging
from enum import Enum
from typing import Any, List, Dict, Optional, Tuple

from fastapi.params import Depends
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.db.services.ask_data_history_service import AskDataHistoryService
from api.service.askdata_service.async_llm_service import AsyncLLMService
from api.service.askdata_service.event.event_utils import send_event
from api.service.askdata_service.llm_sql_query_generator import NLQToInitialSQLGenerator
from api.service.askdata_service.process_semantic_layer import process_semantic_layer
from api.service.askdata_service.sql_assembler import FlexibleSQLAssembler, FilterOperator, OrderDirection
from api.service.askdata_service.table_config_generator import TableConfigGenerator
from api.service.nl2sql_service.custom_jieba_tokenizer import custom_tokenize_with_semantic_words
from api.service.nl2sql_service.semantic_api_client import SemanticApiClient
from api.utils.prompt_template_util import PromptTemplateUtil

logger = logging.getLogger(__name__)


class AskdataService:
    def __init__(self, db: Session, user: Any):
        self.db = db
        self.user = user
        self.prompt_dir = os.path.join(os.path.dirname(__file__), "prompt")
        self.nlq_to_initial_sql_generator = NLQToInitialSQLGenerator(db, user.id, self.prompt_dir)
        self.semantic_api_client = SemanticApiClient()
        self.history_service = AskDataHistoryService()
        self.table_config_generator = TableConfigGenerator(self.semantic_api_client)

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

    async def analyze_user_query_stream(
            self, event_id: str, user_query: str, semantic_layer: Dict[str, Any],
            llm_name: Optional[str], tenant_id: str
    ):
        """分析用户问题并流式返回结果。"""
        llm_service = AsyncLLMService(self.db)
        template_path = os.path.join(self.prompt_dir, "analyze_user_query.txt")
        prompt_template = PromptTemplateUtil.load_template_from_file(template_path)
        prompt = PromptTemplateUtil.fill_template(prompt_template,
                                                  {"user_query": user_query, "semantic_layer": semantic_layer})
        history = [{"role": "user", "content": prompt}]
        gen_conf = {"temperature": 0.7, "max_tokens": 2000}

        await llm_service.chat_stream_async(event_id=event_id, tenant_id=tenant_id, history=history, gen_conf=gen_conf,
                                            llm_name=llm_name)

    async def nlq_to_initial_sql(self, user_query: str, llm_name: str, semantic_layer: Dict[str, Any]) -> Optional[
        Dict[str, Any]]:
        """
        从自然语言生成初始SQL，返回包含组件的完整字典。
        """
        logger.info(f"开始为查询 '{user_query}' 生成初始SQL。")
        # 调用更新后的方法
        result = await self.nlq_to_initial_sql_generator.generate_sql_query_with_components(
            user_query, semantic_layer, llm_name
        )

        if not result:
            logger.warning("NLQ to Initial SQL 生成失败，返回 None。")
            return None

        logger.info(f"成功生成SQL: {result.get('sql')}")
        return result

    async def generate_table_config(self, sql: str, dataset_id_list: List[str],
                                    model_ids: List[str], used_models: List[str],
                                    sql_components: Dict[str, Any]):
        """
        生成表配置信息。
        将逻辑委托给 TableConfigGenerator。
        """
        return await self.table_config_generator.generate(
            model_ids=model_ids,
            used_models=used_models,
            sql_components=sql_components
        )

    async def add_ask_data_history(self, conversation_id: str, ask_id: str, data: str):
        """添加一条问数历史记录。"""
        return self.history_service.add_history(self.db, conversation_id, ask_id, data, self.user.id)

    async def get_ask_data_history(self, conversation_id: str) -> list[dict]:
        """根据对话ID获取问数历史记录。"""
        return self.history_service.get_history_by_conversation_id(self.db, conversation_id)

    def _extract_unique_model_ids(self, dimensions: List[Any], metrics: List[Any]) -> List[str]:
        """从维度和指标数据中提取所有modelId并去重。"""
        return list(set(
            [d.get('modelId') for d in dimensions if d.get('modelId')] +
            [m.get('modelId') for m in metrics if m.get('modelId')]
        ))

    def _deduplicate_dimensions(self, dims_by_keyword: List[Any], dims_by_value: List[Any]) -> List[str]:
        """根据dimensionId对两个维度列表进行去重合并。"""
        return list(set(
            [d.get('dimensionId') for d in dims_by_keyword if d.get('dimensionId')] +
            [d.get('dimensionId') for d in dims_by_value if d.get('dimensionId')]
        ))

    def _extract_unique_domain_ids(self, dataset_details: List[Any]) -> List[str]:
        """从数据集详情列表中提取所有domainId并去重。"""
        return list(set(d.get('domainId') for d in dataset_details if d.get('domainId')))

    async def generate_requery_sql(self, chart_type: str, table_config: Dict[str, Any], base_from: str,
                                   model_table_alias_mapping_list: List[Dict[str, Any]]):
        """生成重新查询的SQL语句。"""
        from_sentence = ""
        if base_from.lower().startswith("from"):
            from_sentence = base_from.split("FROM")[1]
        else:
            from_sentence = base_from
        assembler = FlexibleSQLAssembler(from_sentence)
        all_semantic_fields = table_config["all_semantic_fields"]
        for column in table_config["columns"]:
            column_name = ""
            if column["is_semantic_field"]:
                semantic_field = self._find_semantic_field(column["id"], all_semantic_fields)
                table_alias = self._find_table_alias(semantic_field["from_model_id"],
                                                     model_table_alias_mapping_list)
                column_name = f"{table_alias}.{semantic_field['semantic_field_name']}"
            else:
                column_name = column["sql_column"]
            assembler.add_column(column_name)

        for filter in table_config["filters"]:
            if filter["is_semantic_field"]:
                semantic_field = self._find_semantic_field(filter["id"], all_semantic_fields)
                table_alias = self._find_table_alias(semantic_field["from_model_id"],
                                                     model_table_alias_mapping_list)
                column_name = f"{table_alias}.{semantic_field['semantic_field_name']}"
                operator = filter["operator"]
                value = filter["value"]
                assembler.add_filter(column_name, FilterOperator.from_value(operator), value)
            else:
                raw_condition = filter["raw_condition"]
                assembler.add_raw_where(raw_condition)

        for order_by in table_config["order_by"]:
            if order_by["is_semantic_field"]:
                semantic_field = self._find_semantic_field(order_by["id"], all_semantic_fields)
                table_alias = self._find_table_alias(semantic_field["from_model_id"],
                                                     model_table_alias_mapping_list)
                column_name = f"{table_alias}.{semantic_field['semantic_field_name']}"
                direction = order_by["direction"]
                assembler.add_order_by(column_name, OrderDirection.from_value(direction))
            else:
                assembler.add_order_by(order_by["sql_column"], order_by["direction"])

        limit = table_config["limit"]
        if limit:
            assembler.set_limit(limit)

        return assembler.build_sql_for_jdbc()

    def _find_semantic_field(self, semantic_id: str, all_semantic_fields: List[Dict[str, Any]]) -> Optional[
        Dict[str, Any]]:
        """根据语义字段ID查找语义字段信息"""
        for field in all_semantic_fields:
            if field["id"] == semantic_id:
                semantic_name = field['semantic_field']['dimensionEnName'] if field['semantic_type'] == 'dimension' else \
                    field['semantic_field']['expression']
                return {"id": semantic_id, "semantic_type": field["semantic_type"],
                        "semantic_field_name": semantic_name, "from_model_id": field["from_model_id"]}

        return None

    def _find_table_alias(self, model_id: str, table_alias_mapping_list: List[Dict[str, Any]]) -> Optional[str]:
        """根据模型ID查找表别名"""
        for mapping in table_alias_mapping_list:
            if mapping["modelId"] == model_id:
                return mapping["alias"]

        return None


def get_askdata_service(db: Session = Depends(get_db), user=Depends(manager)) -> AskdataService:
    """通过依赖注入获取AskdataService实例。"""
    return AskdataService(db, user)


class ChartType(Enum):
    TABLE_ROW_RECORDS = "table-row"
    TABLE_AGGREGATE = "table-aggregate"
    PIVOT_TABLE = "PivotTable"
    PIE_CHART = "PieChart"
    LINE_CHART = "LineChart"

import asyncio
import os
import logging
from datetime import date
from enum import Enum
from typing import Any, List, Dict, Optional, Tuple, Set
from fastapi.params import Depends
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.db.services.ask_data_history_service import AskDataHistoryService
from api.service.askdata_service.async_llm_service import AsyncLLMService
from api.service.askdata_service.event.event_utils import send_event
from api.service.askdata_service.llm_sql_query_generator import NLQToInitialSQLGenerator
from api.service.askdata_service.process_semantic_layer import process_semantic_layer
from api.service.askdata_service.query_intent import QueryIntentAnalyzer
from api.service.askdata_service.sql_assembler import FlexibleSQLAssembler, FilterOperator, OrderDirection

from api.service.askdata_service.sql_metric_exp_rewriter import SQLFieldAliasProcessor
from api.service.askdata_service.table_config_generator import TableConfigGenerator
from api.service.askdata_service.util.add_table_alias_to_fields import add_table_alias_to_fields
from api.service.askdata_service.util.convert_aggregation_value import convert_aggregation_value
from api.service.askdata_service.util.convert_where_condition_value import process_where_condition
from api.service.askdata_service.util.parse_sql_in_values import parse_sql_in_values
from api.service.askdata_service.util.semantic_permissions_filter import filter_dimensions_by_permissions, \
    filter_metrics_by_permissions
from api.service.nl2sql_service.custom_jieba_tokenizer import custom_tokenize_with_semantic_words
from api.service.nl2sql_service.semantic_api_client import SemanticApiClient
from api.utils.prompt_template_util import PromptTemplateUtil
from api.service.askdata_service.model_dataset_resolver import ModelDatasetResolver


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
        self.query_intent_analyzer = QueryIntentAnalyzer(db, user.id, self.prompt_dir)
        self.model_dataset_resolver = ModelDatasetResolver(self.semantic_api_client)

    async def generate_semantic_layer(self, user_query: str, dataset_id_list: List[str],
                                      userid:str, llm_name: str = None,
                                      event_id: Optional[str] = None, enable_deep_search: bool = False):
        # 并行启动两个任务：语义层检索和图表推荐
        async def semantic_layer_task():
            await send_event(event_id, {"message": "分词", "action": "start"}, "message")
            segmented_words = await custom_tokenize_with_semantic_words(text=user_query,
                                                                        dataset_id_list=dataset_id_list)
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
            involved_dimension_id_list = self._deduplicate_dimensions(dimensions_by_keyword, dimensions_by_value)
            hc_dim_id_list = []
            # 分词关键字作为维度值关键字获得获得维度列表（高基数维度）
            if enable_deep_search:
                hc_dimensions_by_value = await self.semantic_api_client.get_hc_dimension_by_dimension_value_async(
                    keyword_list=segmented_words,
                    dataset_ids=dataset_id_list,
                    exclude_dim_ids=involved_dimension_id_list)
                hc_dim_id_list = [item["dimensionId"] for item in hc_dimensions_by_value if "dimensionId" in item]
            # 4. 根据dimensionId对dimensions_by_keyword和dimensions_by_value进行维度去重，获得最终维度列表
            involved_dimension_id_list.extend(hc_dim_id_list)
            # 根据用户的权限列表，去掉不允许访问的维度
            user_semantic_permissions = await self.semantic_api_client.get_user_semantic_permissions_async(userid, dataset_id_list)
            allowed_dimension_id_list, prohibited_dimension_id_list = filter_dimensions_by_permissions(involved_dimension_id_list, user_semantic_permissions)
            # 使用allowed_dimension_id_list获得允许访问的维度的维度值，被禁止的维度就没必要获取了
            dimension_values = await self.semantic_api_client.get_dimension_values_async(dimension_ids=allowed_dimension_id_list)
            # 维度信息则使用involved_dimension_id_list（所有涉及到的维度）获得
            dimensions = await self.semantic_api_client.get_dimension_info_by_id_async(dimension_ids=involved_dimension_id_list)
            # 清除不允许访问的维度的信息，仅保留必要信息
            for dimension in dimensions:
                if dimension['dimensionId'] in prohibited_dimension_id_list:
                    dimension['hasPermission'] = False

            await send_event(event_id, {"message": "获取维度信息", "action": "complete"}, "message")
            await send_event(event_id, {"message": "维度信息", "data": dimensions}, "data")
            # 5. 根据分词关键字获得指标列表
            await send_event(event_id, {"message": "获取指标信息", "action": "start"}, "message")
            metrics = await self.semantic_api_client.get_metric_info_by_keyword_async(
                keyword=segmented_words,
                dataset_ids=dataset_id_list)
            metric_ids = [metric["metricId"] for metric in metrics]
            allowed_metrics, prohibited_metrics = filter_metrics_by_permissions(metric_ids, user_semantic_permissions)
            for metric in metrics:
                if metric["metricId"] in prohibited_metrics:
                    metric["hasPermission"] = False

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

            # 8. 查询业务术语
            dataset_details = await self.semantic_api_client.get_dataset_detail_async(dataset_ids=dataset_id_list)
            domain_ids = self._extract_unique_domain_ids(dataset_details)
            business_term_rows = await self.semantic_api_client.get_business_term_info_async(keyword=segmented_words,
                                                                                             domain_ids=domain_ids)
            semantic_layer_original = dict(dataset_details=dataset_details, dimensions=dimensions,
                                           dimension_values=dimension_values,
                                           metrics=metrics, model_details=model_details,
                                           model_relations=model_relations, business_term_rows=business_term_rows)

            logger.info(f"semantic_layer_original: {semantic_layer_original}")

            processed_semantic_layer = process_semantic_layer(semantic_layer_original, user_semantic_permissions, segmented_words)

            logger.info(f"processed_semantic_layer: {processed_semantic_layer}")
            return processed_semantic_layer, model_ids

        async def chart_recommendation_task():
            # 使用不依赖语义层的图表推荐方法
            recommended_chart, recommendation_reason = await self.query_intent_analyzer.recommend_chart_without_semantic(
                user_question=user_query,
                supported_charts_list=["明细表", "聚合表"],
                llm_name=llm_name
            )
            return recommended_chart, recommendation_reason

        # 并行执行两个任务
        logger.info("开始并行执行语义层检索和图表推荐...")
        chart_result, semantic_result = await asyncio.gather(
            chart_recommendation_task(),
            semantic_layer_task()
        )

        # 解包结果
        processed_semantic_layer, model_ids = semantic_result
        recommended_chart, recommendation_reason = chart_result

        await send_event(event_id, {}, "stream_end")

        logger.info(f"recommended_chart: {recommended_chart}, recommendation_reason: {recommendation_reason}")

        return processed_semantic_layer, model_ids, recommended_chart, recommendation_reason

    async def analyze_user_query_stream(
            self, event_id: str, user_query: str, semantic_layer: Dict[str, Any],
            llm_name: Optional[str], tenant_id: str, recommended_chart: str, recommendation_reason: str
    ):
        """分析用户问题并流式返回结果。"""
        llm_service = AsyncLLMService(self.db)
        template_path = os.path.join(self.prompt_dir, "analyze_user_query.txt")
        prompt_template = PromptTemplateUtil.load_template_from_file(template_path)
        prompt = PromptTemplateUtil.fill_template(prompt_template,
                                                  {"user_query": user_query, "semantic_layer": semantic_layer,
                                                   "recommended_chart": recommended_chart,
                                                   "recommendation_reason": recommendation_reason,
                                                   "current_date": date.today().strftime("%Y-%m-%d")})
        history = [{"role": "user", "content": prompt}]
        gen_conf = {"temperature": 0.7, "max_tokens": 2048}

        await llm_service.chat_stream_async(event_id=event_id, tenant_id=tenant_id, history=history, gen_conf=gen_conf,
                                            llm_name=llm_name)

    async def nlq_to_initial_sql(self, user_query: str, llm_name: str, semantic_layer: Dict[str, Any],
                                 recommended_chart: str) -> Optional[
        Dict[str, Any]]:
        """
        从自然语言生成初始SQL，返回包含组件的完整字典。
        """
        logger.info(f"开始为查询 '{user_query}' 生成初始SQL。")
        # 调用更新后的方法
        result = await self.nlq_to_initial_sql_generator.generate_sql_query_with_components(
            user_query, semantic_layer, llm_name, recommended_chart
        )

        if not result:
            logger.warning("NLQ to Initial SQL 生成失败，返回 None。")
            return None

        if result.get("status") == "failed":
            return result

        logger.info(f"成功生成SQL: {result.get('sql')}")
        return result

    async def fix_sql_query_with_components(self, original_sql: str, error_message: str,
                                            semantic_layer: Dict[str, Any], llm_name: str) -> Optional[Dict[str, Any]]:
        """
        修复执行失败的SQL查询
        """
        result = await self.nlq_to_initial_sql_generator.fix_sql_query_with_components(
            original_sql, error_message, semantic_layer, llm_name
        )
        if not result:
            logger.warning("NLQ to Initial SQL 修复失败，返回 None。")
            return None
        logger.info(f"成功修复SQL: {result.get('sql')}")
        return result

    async def generate_table_config(self,
                                    used_table_detail_dict: Dict[str, Dict], model_list: List[Dict],
                                    sql_components: Dict[str, Any], recommended_chart: str):
        """
        生成表配置信息。
        将逻辑委托给 TableConfigGenerator。
        """
        return await self.table_config_generator.generate(
            used_table_detail_dict=used_table_detail_dict,
            model_list=model_list,
            sql_components=sql_components,
            recommended_chart=recommended_chart
        )

    async def get_model_details_and_determine_dataset(
        self,
        model_ids: List[str],
        used_models: List[str],
        dataset_id_list: List[str]
    ) -> Tuple[Dict, Dict, List, Set]:
        """
        构建模型详情字典，并确定使用的数据集
        委托给专门的解析器处理
        """
        return await self.model_dataset_resolver.get_model_details_and_determine_dataset(
            model_ids, used_models, dataset_id_list
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

    async def generate_requery_sql(self, chart_type: str, table_config: Dict[str, Any], sql_components: Dict[str, Any],
                                   model_table_alias_mapping_list: List[Dict[str, Any]]):
        """生成重新查询的SQL语句。"""
        base_from = sql_components["from"]
        from_sentence = ""
        if base_from.lower().startswith("from"):
            from_sentence = base_from.split("FROM")[1]
        else:
            from_sentence = base_from
        assembler = FlexibleSQLAssembler(from_sentence)
        all_semantic_fields = table_config["all_semantic_fields"]

        if chart_type == "table-row":
            for column in table_config["columns"]:
                if column["is_semantic_field"]:
                    semantic_field = self._find_semantic_field(column["id"], all_semantic_fields)
                    table_alias = self._find_table_alias(semantic_field["from_model_id"],
                                                         model_table_alias_mapping_list)
                    column_name = f"{table_alias}.{semantic_field['semantic_field_name']}"
                    assembler.add_column(column_name)
                else:
                    column_name = column["sql_column"]
                    assembler.add_raw_column(column_name)

            for filter in table_config["filters"]:
                if filter["is_semantic_field"]:
                    semantic_field = self._find_semantic_field(filter["id"], all_semantic_fields)
                    table_alias = self._find_table_alias(semantic_field["from_model_id"],
                                                         model_table_alias_mapping_list)
                    column_name, operator, converted_value, needs_special_handling, special_sql = process_where_condition(
                        filter, semantic_field, table_alias
                    )

                    if needs_special_handling:
                        # 需要特殊处理的情况（如日期 CAST）
                        assembler.add_parameterized_where(special_sql, [converted_value])
                    else:
                        # 普通情况
                        assembler.add_filter(column_name, FilterOperator.from_value(operator), converted_value)
                else:
                    raw_condition = filter.get("raw_condition", None)
                    if raw_condition:
                        assembler.add_raw_where(raw_condition)
                    else:
                        assembler.add_raw_where(sql_condition=f"{filter['sql_column']} {filter['operator']} {filter['value']}")


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
        elif chart_type == "table-aggr" or chart_type == "bar" or chart_type == "pie" or chart_type == "line" or chart_type == "area" or chart_type == "matrix" or chart_type == "bubble":
            for dimension in table_config["dimensions"]:
                if dimension["is_semantic_field"]:
                    semantic_field = self._find_semantic_field(dimension["id"], all_semantic_fields)
                    table_alias = self._find_table_alias(semantic_field["from_model_id"],
                                                         model_table_alias_mapping_list)
                    column_name = f"{table_alias}.{semantic_field['semantic_field_name']}"
                    assembler.add_column(column_name)
                    assembler.add_group_by(column_name)
                else:
                    assembler.add_raw_column(dimension["sql_column"])
                    assembler.add_raw_group_by(dimension["sql_column"])

            for metric in table_config["metrics"]:
                if metric["is_semantic_field"]:
                    semantic_field = self._find_semantic_field(metric["id"], all_semantic_fields)
                    table_alias = self._find_table_alias(semantic_field["from_model_id"],
                                                         model_table_alias_mapping_list)
                    model_name = self._find_model_name(semantic_field["from_model_id"], model_table_alias_mapping_list)
                    if metric["semantic_type"] == "measure" or metric["semantic_type"] == "dimension":
                        field_name = semantic_field["field_detail"].get("metricEnName", None)
                        semantic_name = semantic_field["field_detail"].get("semanticName", None)
                        if not field_name:
                            field_name = semantic_field["field_detail"].get("dimensionEnName", None)
                            semantic_name = semantic_field["field_detail"].get("dimensionName", None)
                        column_name = f"{table_alias}.{field_name}"
                        aggr_type = metric["type"]
                        if aggr_type == "COUNT_DISTINCT":
                            alias = f"COUNT_DISTINCT_{model_name}_{{semantic_field['field_detail']['metricEnName']}}"
                            assembler.add_raw_column(f"COUNT(DISTINCT {column_name})",
                                                     alias)
                        else:
                            alias = f"{aggr_type}_{model_name}_{semantic_name}"
                            assembler.add_raw_column(f"{aggr_type}({column_name})",
                                                     alias)
                    elif metric["semantic_type"] == "metric":
                        expression = semantic_field["field_detail"]["expression"]
                        processor = SQLFieldAliasProcessor()
                        new_expression = processor.add_table_alias_to_expression(expression, table_alias)
                        assembler.add_raw_column(new_expression)
                else:
                    assembler.add_raw_column(metric["sql_column"])

            for where_condition in table_config.get("where_conditions", []):
                if where_condition["is_semantic_field"]:
                    semantic_field = self._find_semantic_field(where_condition["id"], all_semantic_fields)
                    table_alias = self._find_table_alias(semantic_field["from_model_id"],
                                                         model_table_alias_mapping_list)

                    column_name, operator, converted_value, needs_special_handling, special_sql = process_where_condition(
                        where_condition, semantic_field, table_alias
                    )

                    if needs_special_handling:
                        # 需要特殊处理的情况（如日期 CAST）
                        assembler.add_parameterized_where(special_sql, [converted_value])
                    else:
                        # 普通情况
                        assembler.add_filter(column_name, FilterOperator.from_value(operator), converted_value)
                else:
                    if where_condition.get("sql_column", None):
                        assembler.add_raw_where(where_condition["sql_column"])
                    else:
                        assembler.add_raw_where(where_condition["raw_condition"])

            for having_condition in table_config.get("having_conditions", []):
                if having_condition["is_semantic_field"]:
                    semantic_field = self._find_semantic_field(having_condition["id"], all_semantic_fields)
                    table_alias = self._find_table_alias(semantic_field["from_model_id"],
                                                         model_table_alias_mapping_list)
                    expression = semantic_field["field_detail"]["expression"]
                    column_name = add_table_alias_to_fields(expression, table_alias)
                    operator = having_condition["operator"]
                    value = having_condition["value"]
                    if operator == "IN":
                        value = parse_sql_in_values(value)
                    else:
                        value = convert_aggregation_value(column_name, value)
                    assembler.add_having(column_name, FilterOperator.from_value(operator), value)
                else:
                    assembler.add_raw_having(having_condition["sql_column"])

            for order_by in table_config.get("order_by", []):
                if order_by["is_semantic_field"]:
                    semantic_field = self._find_semantic_field(order_by["id"], all_semantic_fields)
                    table_alias = self._find_table_alias(semantic_field["from_model_id"],
                                                         model_table_alias_mapping_list)
                    column_name = f"{table_alias}.{semantic_field['semantic_field_name']}"
                    direction = order_by["direction"]
                    assembler.add_order_by(column_name, OrderDirection.from_value(direction))
                else:
                    assembler.add_order_by(order_by["sql_column"], order_by["direction"])

            if len(sql_components["limit"]) > 0:
                assembler.set_limit(int(sql_components["limit"]))

            return assembler.build_sql_for_jdbc()

    async def get_hc_dim_values_by_dim_value(
            self,
            keyword: str,
            dimension_id: str,
            page_index: int = 1,
            page_size: int = 20,
            fuzzy_match: bool = True
    ) -> Dict[str, Any]:
        """
        根据关键词在高基数维度中搜索维度值

        Args:
            keyword: 搜索关键词
            dimension_id: 维度ID
            page_index: 页码（从1开始）
            page_size: 每页大小
            fuzzy_match: 是否模糊匹配

        Returns:
            Dict[str, Any]: 完整的API响应结果
        """
        logger.info(
            f"开始搜索高基数维度值: keyword={keyword}, dimension_id={dimension_id}, page_index={page_index}, page_size={page_size}")

        try:
            # 调用API客户端方法
            result = await self.semantic_api_client.get_hc_dim_values_by_dim_value_async(
                keyword=keyword,
                dimension_id=dimension_id,
                page_index=page_index,
                page_size=page_size,
                fuzzy_match=fuzzy_match
            )

            # 获取数据部分
            data_info = result.get("data", {})
            dimension_values = data_info.get("data", [])
            total = data_info.get("total", 0)

            logger.info(f"成功获取到第{page_index}页的 {len(dimension_values)} 条高基数维度值，总计 {total} 条")
            return result

        except Exception as e:
            logger.exception(f"获取高基数维度值失败: {str(e)}")
            raise

    def _find_semantic_field(self, semantic_id: str, all_semantic_fields: List[Dict[str, Any]]) -> Optional[
        Dict[str, Any]]:
        """根据语义字段ID查找语义字段信息"""
        for field in all_semantic_fields:
            if field["id"] == semantic_id:
                semantic_name = field['semantic_field']['dimensionEnName'] if field['semantic_type'] == 'dimension' else \
                    field['semantic_field']['expression']
                return {"id": semantic_id, "semantic_type": field["semantic_type"],
                        "semantic_field_name": semantic_name, "from_model_id": field["from_model_id"],
                        "field_detail": field["semantic_field"]}

        return None

    def _find_table_alias(self, model_id: str, table_alias_mapping_list: List[Dict[str, Any]]) -> Optional[str]:
        """根据模型ID查找表别名"""
        for mapping in table_alias_mapping_list:
            if mapping["modelId"] == model_id:
                return mapping["alias"]

        return None

    def _find_model_name(self, model_id: str, table_alias_mapping_list: List[Dict[str, Any]]) -> Optional[str]:
        """根据模型ID查找模型名称"""
        for mapping in table_alias_mapping_list:
            if mapping["modelId"] == model_id:
                return mapping["modelName"]


def get_askdata_service(db: Session = Depends(get_db), user=Depends(manager)) -> AskdataService:
    """通过依赖注入获取AskdataService实例。"""
    return AskdataService(db, user)


class ChartType(Enum):
    TABLE_ROW_RECORDS = "table-row"
    TABLE_AGGREGATE = "table-aggregate"
    PIVOT_TABLE = "PivotTable"
    PIE_CHART = "PieChart"
    LINE_CHART = "LineChart"

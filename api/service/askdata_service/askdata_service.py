import asyncio
import json
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
from api.service.askdata_service.llm.conversation_query_rewriter import ConversationQueryRewriter
from api.service.askdata_service.llm.semantic_field_extractor import SemanticFieldExtractor
from api.service.askdata_service.llm.semantic_relevance_filter import SemanticRelevanceFilter
from api.service.askdata_service.llm.sql_components_extractor import SQLComponentsExtractor
from api.service.askdata_service.llm.sql_pagination_converter import SQLPaginationConverter
from api.service.askdata_service.llm_sql_query_generator import NLQToInitialSQLGenerator
from api.service.askdata_service.process_semantic_layer import process_semantic_layer
from api.service.askdata_service.query_intent import QueryIntentAnalyzer
from api.service.askdata_service.sql_assembler import FlexibleSQLAssembler, FilterOperator, OrderDirection

from api.service.askdata_service.sql_metric_exp_rewriter import SQLFieldAliasProcessor
from api.service.askdata_service.table_config_generator import TableConfigGenerator
from api.service.askdata_service.util.add_table_alias_to_fields import add_table_alias_to_fields
from api.service.askdata_service.util.convert_aggregation_value import convert_aggregation_value
from api.service.askdata_service.util.convert_where_condition_value import process_where_condition
from api.service.askdata_service.util.filter_model_relations_by_ids import filter_model_relations_by_ids
from api.service.askdata_service.util.merge_dimensions_and_metrics import merge_dimensions_and_metrics
from api.service.askdata_service.util.parse_sql_in_values import parse_sql_in_values
from api.service.askdata_service.util.semantic_filter_processor import apply_semantic_filter
from api.service.askdata_service.util.semantic_permissions_filter import filter_dimensions_by_permissions, \
    filter_metrics_by_permissions
from api.service.askdata_service.util.timer import time_task
from api.service.askdata_service.util.wide_table_sql_generator import WideTableSQLGenerator
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
        self.semantic_field_extractor = SemanticFieldExtractor(db, user.id, self.prompt_dir)
        self.semantic_relevance_filter = SemanticRelevanceFilter(db, user.id, self.prompt_dir)
        self.conversation_query_rewriter = ConversationQueryRewriter(db, user.id, self.prompt_dir)
        self.model_dataset_resolver = ModelDatasetResolver(self.semantic_api_client)
        self.sql_components_extractor = SQLComponentsExtractor(db, user.id, self.prompt_dir)
        self.sql_pagination_converter = SQLPaginationConverter(db, user.id, self.prompt_dir)

    async def rewrite_conversation_question(
            self,
            conversation_history: Optional[List[Dict[str, Any]]],
            new_user_question: str,
            llm_name: str
    ) -> Dict[str, Any]:
        """调用会话查询改写器，对用户追问进行改写。"""
        history_payload = conversation_history or []
        return await self.conversation_query_rewriter.rewrite_question(
            history_payload,
            new_user_question,
            llm_name
        )

    async def prepare_user_query_for_semantic_layer(
            self,
            round_id: Optional[str],
            original_user_query: str,
            llm_name: Optional[str]
    ) -> Tuple[str, Optional[str]]:
        """根据历史对话改写用户提问，返回用于语义层生成的最终问题和改写后的问题。"""
        # 未配置多轮参数或缺少模型名称时，直接返回原始问题
        if not round_id or not llm_name:
            return original_user_query, None

        try:
            chat_history = await self.get_ask_data_history_by_round(round_id)
        except Exception as history_error:
            logger.warning("获取历史对话失败，使用原始问题: %s", history_error)
            return original_user_query, None

        round_chat_list: List[Dict[str, Any]] = []
        for chat in chat_history:
            user_original_question = chat.get("user_original_question")
            rewritten_question = chat.get("rewritten_question") or ""

            sql_info_raw = chat.get("sql_info")
            sql_value = None
            if isinstance(sql_info_raw, str):
                try:
                    sql_info_raw = json.loads(sql_info_raw)
                except Exception as parse_error:
                    logger.debug("解析sql_info失败，忽略该记录: %s", parse_error)
                    sql_info_raw = None
            if isinstance(sql_info_raw, dict):
                sql_value = sql_info_raw.get("sql")

            if sql_value and (user_original_question or rewritten_question):
                # 为改写器组装历史上下文
                round_chat_list.append({
                    "user_query": user_original_question or rewritten_question,
                    "rewritten_question": rewritten_question,
                    "sql": sql_value
                })

        if not round_chat_list:
            return original_user_query, None

        rewrite_result = await self.rewrite_conversation_question(
            round_chat_list,
            original_user_query,
            llm_name
        )

        if rewrite_result.get("is_related") and rewrite_result.get("rewritten_question"):
            rewritten_question = rewrite_result["rewritten_question"]
            logger.info("多轮问题改写成功，原始问题：%s，改写后的问题：%s", original_user_query, rewritten_question)
            return rewritten_question, rewritten_question

        logger.info("多轮问题改写失败或判定不相关，继续使用原始查询")
        return original_user_query, None

    async def generate_semantic_layer(self, user_query: str, dataset_id_list: List[str],
                                      userid: str, llm_name: str = None,
                                      event_id: Optional[str] = None, enable_deep_search: bool = False):
        await send_event(event_id, {"current_step": 0, "total_steps": 13}, "progress_total")

        # 1. 第一批并行任务：获取dataset_details和用户权限（完全独立，可以同时开始）
        dataset_details_task = time_task(
            self.semantic_api_client.get_dataset_detail_async(dataset_ids=dataset_id_list, event_id=event_id),
            name="获取数据集详情"
        )
        user_permissions_task = time_task(
            self.semantic_api_client.get_user_semantic_permissions_async(
                userid, dataset_id_list, event_id=event_id),
            name="获取用户语义权限"
        )

        dataset_details, user_semantic_permissions = await asyncio.gather(
            dataset_details_task,
            user_permissions_task
        )

        # 2. 第二批并行任务：定义三个主要的并行任务（依赖dataset_details）

        async def llm_extraction_task():
            """LLM提取语义字段，静默执行，不发送事件"""
            await send_event(event_id, {"task_name": "LLM提取语义字段", "task_status": "working"}, "task")

            try:
                extracted_fields = await time_task(
                    self.semantic_field_extractor.extract_semantic_fields(
                        user_query=user_query,
                        dataset_info=dataset_details,
                        llm_name=llm_name
                    ),
                    name="LLM提取语义字段"
                )

                logger.info(f"LLM提取到的语义字段：{extracted_fields}")

                llm_dimension_ids = [field["dimension_id"] for field in extracted_fields if "dimension_id" in field]
                llm_metric_ids = [field["metric_id"] for field in extracted_fields if "metric_id" in field]

                await send_event(event_id, {"task_name": "LLM提取语义字段", "task_data": {
                    "dimension_info_list": [field for field in extracted_fields if "dimension_name" in field],
                    "metric_info_list": [field for field in extracted_fields if "metric_name" in field]
                }}, "task_data")
                await send_event(event_id, {"task_name": "LLM提取语义字段", "task_status": "completed"}, "task")
                await send_event(event_id, {}, "progress_up")
                return llm_dimension_ids, llm_metric_ids
            except Exception as e:
                logger.warning(f"LLM extraction failed: {e}")
                return [], []

        async def keyword_search_and_semantic_layer_task():
            """分词检索和语义层构建"""
            # 1. 分词
            await send_event(event_id, {"task_name": "分词", "task_status": "working"}, "task")
            segmented_words = await custom_tokenize_with_semantic_words(
                text=user_query,
                dataset_id_list=dataset_id_list
            )
            await send_event(event_id, {"task_name": "分词", "task_data": {"segmented_words": segmented_words}},
                             "task_data")
            await send_event(event_id, {"task_name": "分词", "task_status": "completed"}, "task")
            await send_event(event_id, {}, "progress_up")

            # 2. 开始获取维度信息
            # 并行获取维度相关信息
            dimensions_by_keyword_task = time_task(
                self.semantic_api_client.get_dimension_info_by_keyword_async(
                    keyword=segmented_words,
                    dataset_ids=dataset_id_list,
                    event_id=event_id
                ),
                name="按关键字获取维度"
            )
            dimensions_by_value_task = time_task(
                self.semantic_api_client.get_dimension_by_dimension_value_async(
                    keyword=segmented_words,
                    dataset_ids=dataset_id_list,
                    event_id=event_id
                ),
                name="按维度值获取维度"
            )

            dimensions_by_keyword, dimensions_by_value = await asyncio.gather(
                dimensions_by_keyword_task,
                dimensions_by_value_task
            )

            keyword_dimension_ids = self._deduplicate_dimensions(dimensions_by_keyword, dimensions_by_value)

            # 高基数维度检索（如果启用）
            hc_dim_ids = []
            if enable_deep_search:
                hc_dimensions_by_value = await time_task(
                    self.semantic_api_client.get_hc_dimension_by_dimension_value_async(
                        keyword_list=segmented_words,
                        dataset_ids=dataset_id_list,
                        exclude_dim_ids=keyword_dimension_ids,
                        event_id=event_id
                    ), name="获取高基数维度信息")
                hc_dim_ids = [item["dimensionId"] for item in hc_dimensions_by_value if "dimensionId" in item]

            keyword_dimension_ids.extend(hc_dim_ids)

            # 3. 获取指标信息
            await send_event(event_id, {"message": "获取指标信息", "action": "start"}, "message")
            keyword_metrics = await time_task(
                self.semantic_api_client.get_metric_info_by_keyword_async(
                    keyword=segmented_words,
                    dataset_ids=dataset_id_list,
                    event_id=event_id
                ),
                name="按关键字获取指标"
            )

            return keyword_dimension_ids, keyword_metrics, segmented_words

        async def chart_recommendation_task():
            """图表推荐，静默执行"""
            recommended_chart, recommendation_reason = await self.query_intent_analyzer.recommend_chart_without_semantic(
                user_question=user_query,
                supported_charts_list=["明细表", "聚合表"],
                llm_name=llm_name
            )
            return recommended_chart, recommendation_reason

        # 3. 并行执行三个任务
        logger.info("开始并行执行：LLM字段提取、关键字检索和图表推荐...")

        (llm_dim_ids, llm_metric_ids), \
            (keyword_dim_ids, keyword_metrics, segmented_words), \
            (recommended_chart, recommendation_reason) = await asyncio.gather(
            llm_extraction_task(),
            keyword_search_and_semantic_layer_task(),
            chart_recommendation_task()
        )

        # 4. 合并LLM提取和关键字检索的结果
        # 合并维度ID（去重）
        all_dimension_ids = list(set(keyword_dim_ids + llm_dim_ids))

        # 处理指标：检查LLM提取的指标是否已在keyword_metrics中
        existing_metric_ids = {metric["metricId"] for metric in keyword_metrics}
        new_metric_ids = [mid for mid in llm_metric_ids if mid not in existing_metric_ids]

        # 如果有新的指标ID，需要单独查询
        all_metrics = keyword_metrics
        if new_metric_ids:
            new_metrics = await time_task(
                self.semantic_api_client.get_metric_info_by_id_async(metric_ids=new_metric_ids, event_id=event_id),
                name="获取LLM提取的指标信息")
            all_metrics.extend(new_metrics)

        all_metric_ids = [metric["metricId"] for metric in all_metrics]

        # 5. 使用已经获取的用户权限进行过滤
        # 过滤权限
        allowed_dimension_ids, prohibited_dimension_ids = filter_dimensions_by_permissions(
            all_dimension_ids, user_semantic_permissions
        )
        allowed_metric_ids, prohibited_metric_ids = filter_metrics_by_permissions(
            all_metric_ids, user_semantic_permissions
        )

        # 6. 获取维度值和维度详情（并行）
        dimension_values_task = time_task(
            self.semantic_api_client.get_dimension_values_async(
                dimension_ids=allowed_dimension_ids, event_id=event_id
            ),
            name="获取维度值"
        )
        dimensions_task = time_task(
            self.semantic_api_client.get_dimension_info_by_id_async(
                dimension_ids=all_dimension_ids, event_id=event_id
            ),
            name="获取维度详情"
        )

        dimension_values, dimensions = await asyncio.gather(
            dimension_values_task,
            dimensions_task
        )

        model_ids, model_mappings = self._extract_unique_model_ids(dimensions, all_metrics)

        domain_ids = self._extract_unique_domain_ids(dataset_details)

        model_details_task = time_task(
            self.semantic_api_client.get_model_detail_async(model_ids=model_ids),
            name="获取模型详情"
        )
        model_relations_task = time_task(
            self.semantic_api_client.get_model_relationships_async(model_ids=model_ids),
            name="获取模型关系"
        )
        business_term_task = time_task(
            self.semantic_api_client.get_business_term_info_async(
                keyword=segmented_words,
                domain_ids=domain_ids),
            name="获取业务术语"
        )

        model_details, model_relations, business_term_rows = await asyncio.gather(
            model_details_task,
            model_relations_task,
            business_term_task
        )

        # 将维度和指标简化后进行合并，为交给LLM进一步排除冗余语义做准备
        merged_dimensions_and_metrics = merge_dimensions_and_metrics(dimension_values, dimensions, all_metrics)

        await send_event(event_id, {"task_name": "LLM过滤不相关的维度和指标", "task_status": "working"}, "task")
        exclude_dim_and_metric = await time_task(
            self.semantic_relevance_filter.filter_irrelevant_fields(
                user_query=user_query,
                dataset_info=merged_dimensions_and_metrics,
                model_relations=model_relations,
                llm_name=llm_name
            ),
            name="LLM过滤不相关的维度和指标"
        )
        await send_event(event_id, {"task_name": "LLM过滤不相关的维度和指标", "task_status": "completed"}, "task")
        await send_event(event_id, {}, "progress_up")

        dimensions, all_metrics, excluded_details = apply_semantic_filter(
            dimensions=dimensions,
            all_metrics=all_metrics,
            exclude_dim_and_metric=exclude_dim_and_metric,
            log_details=True  # 是否打印详细日志
        )

        # 标记无权限的维度和指标
        for dimension in dimensions:
            if dimension['dimensionId'] in prohibited_dimension_ids:
                dimension['hasPermission'] = False

        for metric in all_metrics:
            if metric["metricId"] in prohibited_metric_ids:
                metric["hasPermission"] = False

        model_ids, model_mappings = self._extract_unique_model_ids(dimensions, all_metrics)
        await send_event(event_id, {"message": "最终模型信息", "data": {
            "model_info_list": model_mappings,
            "dimension_info_list": [
                {
                    "dimension_id": item.get("dimensionId"),
                    "dimension_name": item.get("dimensionName"),
                    "model_name": item.get("modelName", None),
                    "has_permission": item.get("hasPermission", True)
                }
                for item in dimensions],
            "metric_info_list": [
                {
                    "metric_id": item.get("metricId"),
                    "metric_name": item.get("metricName"),
                    "model_name": item.get("modelName", None),
                    "has_permission": item.get("hasPermission", True)
                }
                for item in all_metrics]
        }},
                         "final_data")

        model_relations = filter_model_relations_by_ids(model_ids, model_relations)

        # 9. 构建最终的语义层
        semantic_layer_original = dict(
            dataset_details=dataset_details,
            dimensions=dimensions,
            dimension_values=dimension_values,
            metrics=all_metrics,
            model_details=model_details,
            model_relations=model_relations,
            business_term_rows=business_term_rows
        )

        processed_semantic_layer = process_semantic_layer(
            semantic_layer_original,
            user_semantic_permissions,
            segmented_words
        )

        logger.info(f"processed_semantic_layer: {processed_semantic_layer}")

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

    async def fix_sql_query_with_components(self, user_query: str, original_sql: str, error_message: str,
                                            semantic_layer: Dict[str, Any], llm_name: str) -> Optional[Dict[str, Any]]:
        """
        修复执行失败的SQL查询
        """
        result = await self.nlq_to_initial_sql_generator.fix_sql_query_with_components(
            user_query, original_sql, error_message, semantic_layer, llm_name
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

    async def add_ask_data_history(self, conversation_id: str, ask_id: str, user_id: str, data: str,
                                   round_id: str = None,
                                   user_origin_question: str = None, rewritten_question: str = None,
                                   processed_semantic_layer: str = None):
        """添加一条问数历史记录。"""
        return self.history_service.add_history(self.db, conversation_id, ask_id, data, user_id, round_id,
                                                user_origin_question, rewritten_question, processed_semantic_layer)

    async def get_ask_data_history(self, conversation_id: str, user_id: str) -> list[dict]:
        """根据对话ID获取问数历史记录。"""
        return self.history_service.get_history_by_conversation_id(self.db, conversation_id, user_id)

    async def delete_ask_data_history_by_conversation(self, conversation_id: str, user_id: str) -> int:
        """根据对话ID和用户ID删除问数历史记录。"""
        return self.history_service.delete_by_conversation_id(self.db, conversation_id, user_id)

    async def delete_ask_data_history_by_ask_id(self, ask_id: str, user_id: str) -> int:
        """根据询问ID和用户ID删除问数历史记录。"""
        return self.history_service.delete_by_ask_id(self.db, ask_id, user_id)

    async def get_ask_data_history_by_round(self, round_id: str) -> list[dict]:
        """根据轮次ID获取问数历史记录，按时间从早到晚排列。"""
        return self.history_service.get_by_round_id(self.db, round_id)

    def _extract_unique_model_ids(self, dimensions: List[Any], metrics: List[Any]) -> Tuple[
        List[str], List[Dict[str, str]]]:
        """从维度和指标数据中提取所有modelId并去重，同时返回模型详细信息。

        Returns:
            Tuple[List[str], List[Dict[str, str]]]: (modelId列表, 模型详细信息列表)
        """
        # 收集所有包含modelId的项目
        all_items = [d for d in dimensions if d.get('modelId')] + [m for m in metrics if m.get('modelId')]

        # 使用字典去重，以modelId为key
        unique_models = {}
        for item in all_items:
            model_id = item.get('modelId')
            if model_id and model_id not in unique_models:
                unique_models[model_id] = {
                    'modelId': model_id,
                    'modelName': item.get('modelName', '')  # 如果没有modelName则使用空字符串
                }

        model_ids = list(unique_models.keys())
        model_details = list(unique_models.values())

        return model_ids, model_details

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
                                   model_table_alias_mapping_list: List[Dict[str, Any]],
                                   pagination_info: Optional[Dict[str, Any]]):
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
                    if semantic_field["semantic_type"] == "metric":
                        column_name = f"{table_alias}.{semantic_field.get("semantic_field_name","").split(".")[1]}"
                    else:
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
                        sql_column = filter['sql_column']
                        operator = filter['operator']
                        value = filter['value']

                        # 处理不同的操作符
                        if operator.upper() in ['IS NULL', 'IS NOT NULL']:
                            # NULL 检查不需要参数
                            assembler.add_raw_where(f"{sql_column} {operator}")
                        elif operator.upper() in ['IN', 'NOT IN']:
                            # IN 操作符需要特殊处理
                            if isinstance(value, str):
                                # 如果是字符串，尝试解析为列表
                                import json
                                try:
                                    value_list = json.loads(value)
                                except:
                                    # 如果不是 JSON，尝试分割
                                    value_list = [v.strip().strip("'\"") for v in value.split(',')]
                            else:
                                value_list = value if isinstance(value, list) else [value]

                            placeholders = ','.join(['%s'] * len(value_list))
                            assembler.add_parameterized_where(
                                f"{sql_column} {operator} ({placeholders})",
                                value_list
                            )
                        elif operator.upper() == 'BETWEEN':
                            # BETWEEN 需要两个值
                            # 这里假设 value 包含两个值，可能需要根据实际情况调整
                            assembler.add_raw_where(f"{sql_column} BETWEEN %s AND %s",
                                                    [value, filter.get('value2', value)])
                        else:
                            # 普通操作符，使用参数化查询
                            assembler.add_parameterized_where(f"{sql_column} {operator} %s", [value])

            for order_by in table_config["order_by"]:
                if order_by["is_semantic_field"]:
                    semantic_field = self._find_semantic_field(order_by["id"], all_semantic_fields)
                    table_alias = self._find_table_alias(semantic_field["from_model_id"],
                                                         model_table_alias_mapping_list)
                    column_name = f"{table_alias}.{semantic_field['semantic_field_name']}"
                    direction = order_by["direction"]
                    assembler.add_order_by(column_name, OrderDirection.from_value(direction))
                else:
                    assembler.add_order_by(order_by["sql_column"], OrderDirection.from_value(order_by["direction"]))

            if pagination_info:
                count_sql, count_sql_params = assembler.build_count_sql_for_jdbc()
                page_size = int(pagination_info["page_size"])
                page_index = int(pagination_info["page_index"])
                assembler.set_pagination(page_index, page_size)
                sql, params = assembler.build_sql_for_jdbc()
                return {
                    "sql": sql,
                    "params": params,
                    "count_sql": count_sql,
                    "count_sql_params": count_sql_params,
                }
            else:
                sql, params = assembler.build_sql_for_jdbc()
                return {
                    "sql": sql,
                    "params": params
                }

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
                            alias = f"COUNT_DISTINCT_{model_name}_{semantic_field['field_detail']['metricEnName']}"
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
                    raw_condition = where_condition.get("raw_condition", None)
                    sql_column = where_condition.get("sql_column", None)

                    if raw_condition:
                        # 如果有 raw_condition，直接使用
                        assembler.add_raw_where(raw_condition)
                    elif sql_column:
                        # 如果有 sql_column，需要解析并参数化
                        # sql_column 可能包含完整的条件表达式
                        # 尝试解析操作符和值
                        import re

                        # 尝试匹配常见的 SQL 条件模式
                        patterns = [
                            (r'^(.+?)\s+(IS\s+NULL|IS\s+NOT\s+NULL)\s*$', lambda m: (m.group(1), m.group(2), None)),
                            (r'^(.+?)\s+(IN|NOT\s+IN)\s*\((.+)\)\s*$', lambda m: (m.group(1), m.group(2), m.group(3))),
                            (r'^(.+?)\s+(BETWEEN)\s+(.+?)\s+AND\s+(.+)\s*$',
                             lambda m: (m.group(1), m.group(2), (m.group(3), m.group(4)))),
                            (r'^(.+?)\s*(=|!=|<>|>=|<=|>|<|LIKE|NOT\s+LIKE)\s*(.+)\s*$',
                             lambda m: (m.group(1), m.group(2), m.group(3))),
                        ]

                        matched = False
                        for pattern, extractor in patterns:
                            match = re.match(pattern, sql_column, re.IGNORECASE)
                            if match:
                                result = extractor(match)
                                if len(result) == 3:
                                    field, op, val = result

                                    if op.upper() in ['IS NULL', 'IS NOT NULL']:
                                        assembler.add_raw_where(f"{field} {op}")
                                    elif op.upper() in ['IN', 'NOT IN']:
                                        # 解析 IN 列表
                                        val_list = [v.strip().strip("'\"") for v in val.split(',')]
                                        placeholders = ','.join(['%s'] * len(val_list))
                                        assembler.add_parameterized_where(f"{field} {op} ({placeholders})", val_list)
                                    elif op.upper() == 'BETWEEN':
                                        # BETWEEN 有两个值
                                        val1, val2 = val
                                        val1 = val1.strip().strip("'\"")
                                        val2 = val2.strip().strip("'\"")
                                        assembler.add_parameterized_where(f"{field} BETWEEN %s AND %s", [val1, val2])
                                    else:
                                        # 普通比较操作符
                                        val = val.strip().strip("'\"")
                                        assembler.add_parameterized_where(f"{field} {op} %s", [val])

                                    matched = True
                                    break

                        if not matched:
                            logger.warning(f"无法解析 where 条件，将作为原始 SQL 添加: {sql_column}")
                            assembler.add_raw_where(sql_column)
                    else:
                        # 既没有 raw_condition 也没有 sql_column，跳过
                        pass

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

            if pagination_info:
                count_sql, count_sql_params = assembler.build_count_sql_for_jdbc()
                page_size = int(pagination_info["page_size"])
                page_index = int(pagination_info["page_index"])
                assembler.set_pagination(page_index, page_size)
                sql, params = assembler.build_sql_for_jdbc()
                return {
                    "sql": sql,
                    "params": params,
                    "count_sql": count_sql,
                    "count_sql_params": count_sql_params,
                }
            else:
                sql, params = assembler.build_sql_for_jdbc()
                return {
                    "sql": sql,
                    "params": params
                }

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

    async def generate_widetable_sql(
            self,
            dataset_id: str,
            user_id: str = None
    ) -> str:
        """
        生成宽表查询SQL

        Args:
            dataset_id: 数据集ID
            user_id: 用户ID

        Returns:
            生成的SQL语句
        """
        try:
            logger.info(f"开始生成宽表SQL - 数据集ID: {dataset_id}, 用户ID: {user_id}")

            # 1. 获取数据集详情
            logger.info("步骤1: 获取数据集详情...")
            dataset_details = await self.semantic_api_client.get_dataset_detail_async(dataset_id)

            if not dataset_details or len(dataset_details) == 0:
                raise ValueError(f"未找到数据集 {dataset_id} 的详情信息")

            dataset_detail = dataset_details[0]
            logger.info(f"数据集名称: {dataset_detail.get('datasetName')}")
            logger.info(f"包含模型数: {len(dataset_detail.get('models', []))}")

            # 2. 获取模型关系
            logger.info("步骤2: 获取模型关系...")
            model_ids = [model["modelId"] for model in dataset_detail.get("models", [])]

            if not model_ids:
                raise ValueError("数据集中没有模型")

            model_relationships = await self.semantic_api_client.get_model_relationships_async(model_ids)
            logger.info(f"获取到 {len(model_relationships)} 条模型关系")

            # 3. 获取用户权限（如果提供了用户ID）
            user_permissions = None
            if user_id:
                logger.info("步骤3: 获取用户权限...")
                try:
                    user_permissions = await self.semantic_api_client.get_user_semantic_permissions_async(
                        user_id,
                        [dataset_id]
                    )
                    logger.info(f"成功获取用户 {user_id} 的权限信息")
                except Exception as e:
                    logger.warning(f"获取用户权限失败，将使用默认权限: {str(e)}")
                    user_permissions = None
            else:
                logger.info("步骤3: 未提供用户ID，跳过权限获取")

            # 4. 生成SQL
            logger.info("步骤4: 生成宽表SQL...")
            sql_generator = WideTableSQLGenerator()
            sql = sql_generator.generate_sql(
                dataset_detail=dataset_detail,
                model_relationships=model_relationships,
                user_permissions=user_permissions,
                user_id=user_id or "default"
            )

            logger.info("宽表SQL生成成功")
            return sql

        except Exception as e:
            logger.error(f"生成宽表SQL时发生错误: {str(e)}", exc_info=True)
            raise


def get_askdata_service(db: Session = Depends(get_db), user=Depends(manager)) -> AskdataService:
    """通过依赖注入获取AskdataService实例。"""
    return AskdataService(db, user)


class ChartType(Enum):
    TABLE_ROW_RECORDS = "table-row"
    TABLE_AGGREGATE = "table-aggregate"
    PIVOT_TABLE = "PivotTable"
    PIE_CHART = "PieChart"
    LINE_CHART = "LineChart"

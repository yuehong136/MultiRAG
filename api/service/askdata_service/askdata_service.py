import asyncio
import json
import os
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
from api.service.askdata_service.util.append_join_clauses import append_join_clauses
from api.service.askdata_service.util.apply_permissions import apply_permissions_to_assembler
from api.service.askdata_service.util.build_model_permissions_map import build_model_permissions_map
from api.service.askdata_service.util.convert_aggregation_value import convert_aggregation_value
from api.service.askdata_service.util.convert_where_condition_value import process_where_condition, convert_where_condition_value
from api.service.askdata_service.util.extract_manually_adjusted_field_ids import extract_manually_adjusted_field_ids
from api.service.askdata_service.util.filter_model_relations_by_ids import filter_model_relations_by_ids
from api.service.askdata_service.util.merge_dimensions_and_metrics import merge_dimensions_and_metrics
from api.service.askdata_service.util.parse_from_clause import parse_from_clause
from api.service.askdata_service.util.parse_sql_in_values import parse_sql_in_values
from api.service.askdata_service.util.semantic_filter_processor import apply_semantic_filter
from api.service.askdata_service.util.semantic_permissions_filter import filter_dimensions_by_permissions, \
    filter_metrics_by_permissions
from api.service.askdata_service.util.timer import time_task
from api.service.askdata_service.util.wide_table_sql_generator import WideTableSQLGenerator
from api.service.askdata_service.stop_request_manager import stop_request_manager
from api.service.nl2sql_service.custom_jieba_tokenizer import custom_tokenize_with_semantic_words
from api.service.nl2sql_service.semantic_api_client import SemanticApiClient
from api.utils.prompt_template_util import PromptTemplateUtil
from api.service.askdata_service.model_dataset_resolver import ModelDatasetResolver
from api.service.askdata_service.util.askdata_logger import get_askdata_logger
from api.service.askdata_service.util.queried_fields_extractor import QueriedFieldsExtractor

logger = get_askdata_logger()


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

    def _check_if_stopped(self, ask_id: str) -> bool:
        """
        检查指定ask_id的请求是否已被停止

        Args:
            ask_id: 要检查的请求ID

        Returns:
            bool: 如果请求已被停止返回True，否则返回False

        Raises:
            Exception: 如果检测到请求已被停止
        """
        if not ask_id:
            return False

        try:
            is_stopped = stop_request_manager.is_stopped(ask_id)
            if is_stopped:
                logger.info(f"检测到请求 {ask_id} 已被停止")
                raise Exception(f"请求已被用户停止")
            return False
        except Exception as e:
            if "已被用户停止" in str(e):
                raise e
            else:
                logger.warning(f"检查停止状态时发生错误: {str(e)}")
                return False

    async def _async_check_if_stopped(self, ask_id: str) -> bool:
        """
        异步版本的停止检查方法

        Args:
            ask_id: 要检查的请求ID

        Returns:
            bool: 如果请求已被停止返回True，否则返回False

        Raises:
            Exception: 如果检测到请求已被停止
        """
        return self._check_if_stopped(ask_id)

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
                                      event_id: Optional[str] = None, enable_deep_search: bool = False, ask_id: str = None):
        """
        生成语义层信息

        Args:
            ask_id: 请求ID，用于停止检查
        """
        # 在开始处理前检查是否被停止
        if ask_id:
            await self._async_check_if_stopped(ask_id)

        await send_event(event_id, {"current_step": 0, "total_steps": 13}, "progress_total")

        try:
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
            logger.debug("[semantic_layer] 数据集详情: %d个, 用户权限: %s",
                         len(dataset_details),
                         json.dumps(user_semantic_permissions, ensure_ascii=False) if user_semantic_permissions else "None")

            # 检查是否在获取基础数据后被停止
            if ask_id:
                await self._async_check_if_stopped(ask_id)

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
            logger.debug("[semantic_layer] 并行任务完成: LLM维度=%s, LLM指标=%s, 关键字维度=%s, 关键字指标=%d个, 分词=%s, 推荐图表=%s",
                         llm_dim_ids, llm_metric_ids, keyword_dim_ids,
                         len(keyword_metrics), segmented_words, recommended_chart)

            # 检查是否在并行任务完成后被停止
            if ask_id:
                await self._async_check_if_stopped(ask_id)

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
            logger.debug("[semantic_layer] 合并后: 维度IDs=%s, 指标IDs=%s", all_dimension_ids, all_metric_ids)

            # 5. 使用已经获取的用户权限进行过滤
            # 过滤权限
            allowed_dimension_ids, prohibited_dimension_ids = filter_dimensions_by_permissions(
                all_dimension_ids, user_semantic_permissions
            )
            allowed_metric_ids, prohibited_metric_ids = filter_metrics_by_permissions(
                all_metric_ids, user_semantic_permissions
            )
            logger.debug("[semantic_layer] 权限过滤: allowed_dims=%d, prohibited_dims=%s, allowed_metrics=%d, prohibited_metrics=%s",
                         len(allowed_dimension_ids), prohibited_dimension_ids,
                         len(allowed_metric_ids), prohibited_metric_ids)

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
            logger.debug("[semantic_layer] 维度详情: %d个, 维度值: %d个",
                         len(dimensions), len(dimension_values) if isinstance(dimension_values, list) else 0)

            model_ids, model_mappings = self._extract_unique_model_ids(dimensions, all_metrics)
            if len(model_ids) == 0:
                raise ValueError("没有检索到任何与当前问题相关的语义信息，请换个问题，或者检查语义层数据集。")

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
                business_term_task,
                return_exceptions=True,
            )
            # 模型关系是「非关键、可降级」依赖：中台 getModelRelationships 接口超时/失败时
            # 降级为空关系继续（单模型本就常无跨模型关系），避免这一路抖动把已成功的
            # 模型详情/业务术语连同整个语义层一起拖垮。
            # ⚠ 降级值必须是 []，绝不能是 None——下游 table_config 对 None 会在 get-sql 阶段
            #    重新去打这个会超时的接口，等于把 30s 超时挪到下一步。
            if isinstance(model_relations, BaseException):
                logger.warning("[semantic_layer] 获取模型关系失败，降级为空关系继续: %r", model_relations)
                model_relations = []
            # 模型详情 / 业务术语仍是硬依赖：失败保持原有「整层中止」行为，显式重抛。
            if isinstance(model_details, BaseException):
                raise model_details
            if isinstance(business_term_rows, BaseException):
                raise business_term_rows
            logger.debug("[semantic_layer] model_ids=%s, model_relations=%d个, business_terms=%d个",
                         model_ids, len(model_relations), len(business_term_rows))

            # 检查是否在获取模型详情后被停止
            if ask_id:
                await self._async_check_if_stopped(ask_id)

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
            logger.debug("[semantic_layer] LLM过滤后: 维度=%d个, 指标=%d个, 排除详情=%s",
                         len(dimensions), len(all_metrics),
                         json.dumps(excluded_details, ensure_ascii=False) if excluded_details else "None")
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

            # 这里需要把有关系的模型详情再拉一下（已经存在的模型不需要再次拉），然后放到model_details中
            # 从model_relations中提取所有涉及的模型ID
            related_model_ids = set()
            for relation in model_relations:
                if relation.get('sourceModelId'):
                    related_model_ids.add(relation['sourceModelId'])
                if relation.get('targetModelId'):
                    related_model_ids.add(relation['targetModelId'])

            # 找出已存在的模型ID
            existing_model_ids = {model['modelId'] for model in model_details if 'modelId' in model}

            # 找出需要补充的模型ID
            missing_model_ids = list(related_model_ids - existing_model_ids)

            # 如果有缺失的模型，获取其详情并合并
            if missing_model_ids:
                logger.info(f"发现 {len(missing_model_ids)} 个关联模型需要补充详情")
                additional_model_details = await time_task(
                    self.semantic_api_client.get_model_detail_async(model_ids=missing_model_ids),
                    name="获取关联模型详情"
                )
                model_details.extend(additional_model_details)
                logger.info(f"已补充 {len(additional_model_details)} 个模型详情")

            # 8.5 预拉所有模型的 dimsAndMetrics，供 Phase 2 缓存使用（并行请求）
            dims_metrics_tasks = [
                self.semantic_api_client.get_model_inds_and_dims_by_model_id_async(
                    model_id=model_detail["modelId"]
                )
                for model_detail in model_details
            ]
            if dims_metrics_tasks:
                # 与上面的模型关系 gather 同理：dims/metrics 是可降级依赖，单个模型打中台
                # getModelIndsAndDimsByModelId 超时/失败时降级为空，绝不让它掀翻整个语义层。
                # 降级值必须是 dict（{"dimensions": [], "metrics": []}）不能是 None——理由见
                # model_dataset_resolver._ensure_dims_and_metrics 的说明。每模型用新字面量防串改。
                dims_metrics_results = await asyncio.gather(*dims_metrics_tasks, return_exceptions=True)
                _degraded = 0
                for model_detail, dims_metrics in zip(model_details, dims_metrics_results):
                    if isinstance(dims_metrics, BaseException):
                        logger.warning("[dims_metrics] 预拉模型 %s 的维度/指标失败，降级为空继续: %r",
                                       model_detail.get("modelId"), dims_metrics)
                        dims_metrics = {"dimensions": [], "metrics": []}
                        _degraded += 1
                    elif not isinstance(dims_metrics, dict):
                        dims_metrics = {"dimensions": [], "metrics": []}
                    model_detail['dimsAndMetrics'] = dims_metrics
                logger.info(f"预拉 {len(dims_metrics_tasks)} 个模型的 dimsAndMetrics 完成"
                            + (f"（其中 {_degraded} 个降级为空）" if _degraded else ""))

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
            logger.debug("[semantic_layer] 最终语义层: %s",
                         json.dumps(processed_semantic_layer, ensure_ascii=False))

            await send_event(event_id, {}, "stream_end")

            logger.info(f"recommended_chart: {recommended_chart}, recommendation_reason: {recommendation_reason}")

            # 构建 Phase 2 可复用的预拉数据
            phase2_prefetch_data = {
                "model_details": model_details,
                "model_relations": model_relations,
                "dimension_values": dimension_values,
            }

            return processed_semantic_layer, [model_detail["modelId"] for model_detail in model_details], recommended_chart, recommendation_reason, phase2_prefetch_data
        except Exception as e:
            raise

    async def analyze_user_query_stream(
            self, event_id: str, user_query: str, rewritten_question: Optional[str], round_id: Optional[str],
            semantic_layer: Dict[str, Any],
            llm_name: Optional[str], tenant_id: str, recommended_chart: str, recommendation_reason: str,
            ask_id: Optional[str] = None
    ):
        """分析用户问题并流式返回结果。"""
        # 在开始流式分析前检查是否被停止
        if ask_id:
            await self._async_check_if_stopped(ask_id)

        round_chat_list: List[Dict[str, Any]] = []
        if round_id:
            chat_history = await self.get_ask_data_history_by_round(round_id)
            for round_chat in chat_history:
                round_user_original_question = round_chat.get("user_original_question")
                round_rewritten_question = round_chat.get("rewritten_question", None)
                if round_rewritten_question:
                    round_chat_list.append({
                        "user_query": round_user_original_question or round_rewritten_question,
                        "rewritten_question": round_rewritten_question,
                    })
                else:
                    round_chat_list.append({
                        "user_query": round_user_original_question,
                    })

        llm_service = AsyncLLMService(self.db)
        template_path = os.path.join(self.prompt_dir, "analyze_user_query.txt")
        prompt_template = PromptTemplateUtil.load_template_from_file(template_path)
        prompt = PromptTemplateUtil.fill_template(prompt_template,
                                                  {"user_query": user_query, "rewritten_question": rewritten_question,
                                                   "semantic_layer": semantic_layer,
                                                   "conversation_history": round_chat_list,
                                                   "recommended_chart": recommended_chart,
                                                   "recommendation_reason": recommendation_reason,
                                                   "current_date": date.today().strftime("%Y-%m-%d")})
        history = [{"role": "user", "content": prompt}]
        gen_conf = {"temperature": 0.7, "max_tokens": 2048}

        # 在开始LLM流式处理前最后检查一次是否被停止
        if ask_id:
            await self._async_check_if_stopped(ask_id)

        await llm_service.chat_stream_async(event_id=event_id, tenant_id=tenant_id, history=history, gen_conf=gen_conf,
                                            llm_name=llm_name)

    async def nlq_to_initial_sql(self, user_query: str, llm_name: str, semantic_layer: Dict[str, Any],
                                 recommended_chart: str, ask_id: str = None) -> Optional[
        Dict[str, Any]]:
        """
        从自然语言生成初始SQL，返回包含组件的完整字典。

        Args:
            ask_id: 请求ID，用于停止检查
        """
        logger.info(f"开始为查询 '{user_query}' 生成初始SQL。")

        # 检查请求是否已被停止
        if ask_id:
            await self._async_check_if_stopped(ask_id)

        # 调用更新后的方法
        result = await self.nlq_to_initial_sql_generator.generate_sql_query(
            user_query, semantic_layer, llm_name, recommended_chart
        )

        if not result:
            logger.warning("NLQ to Initial SQL 生成失败，返回 None。")
            return None

        if result.get("status") == "failed":
            return result

        logger.info(f"成功生成SQL: {str(result.get('sql'))[:200]}")
        return result

    async def fix_sql_query_with_components(self, user_query: str, original_sql: str, error_message: str,
                                            semantic_layer: Dict[str, Any], llm_name: str, ask_id: str = None) -> Optional[Dict[str, Any]]:
        """
        修复执行失败的SQL查询

        Args:
            ask_id: 请求ID，用于停止检查
        """
        # 检查请求是否已被停止
        if ask_id:
            await self._async_check_if_stopped(ask_id)

        result = await self.nlq_to_initial_sql_generator.fix_sql_query_with_components(
            user_query, original_sql, error_message, semantic_layer, llm_name
        )
        if not result:
            logger.warning("NLQ to Initial SQL 修复失败，返回 None。")
            return None
        logger.info(f"成功修复SQL: {str(result.get('sql'))[:200]}")
        return result

    async def generate_table_config(self,
                                    used_table_detail_dict: Dict[str, Dict], model_list: List[Dict],
                                    sql_components: Dict[str, Any], recommended_chart: str,
                                    cached_model_relations: list | None = None,
                                    cached_dimension_values: dict | None = None):
        """
        生成表配置信息。
        将逻辑委托给 TableConfigGenerator。
        """
        return await self.table_config_generator.generate(
            used_table_detail_dict=used_table_detail_dict,
            model_list=model_list,
            sql_components=sql_components,
            recommended_chart=recommended_chart,
            cached_model_relations=cached_model_relations,
            cached_dimension_values=cached_dimension_values,
        )

    async def get_model_details_and_determine_dataset(
            self,
            model_ids: List[str],
            used_models: List[str],
            dataset_id_list: List[str],
            cached_model_details: list | None = None,
    ) -> Tuple[Dict, Dict, List, Set]:
        """
        构建模型详情字典，并确定使用的数据集
        委托给专门的解析器处理
        """
        return await self.model_dataset_resolver.get_model_details_and_determine_dataset(
            model_ids, used_models, dataset_id_list,
            cached_model_details=cached_model_details,
        )

    async def add_ask_data_history(self, conversation_id: str, ask_id: str, user_id: str, data: str,
                                   round_id: str = None,
                                   user_origin_question: str = None, rewritten_question: Optional[str] = None,
                                   processed_semantic_layer: str = None, request_ask_id: Optional[str] = None):
        """添加一条问数历史记录。"""
        # 在保存历史记录前检查是否被停止
        if request_ask_id:
            await self._async_check_if_stopped(request_ask_id)

        return self.history_service.add_history(self.db, conversation_id, ask_id, data, user_id, round_id,
                                                user_origin_question, rewritten_question, processed_semantic_layer)

    async def get_ask_data_history(self, conversation_id: str, user_id: str) -> list[dict]:
        """根据对话ID获取问数历史记录。"""
        return self.history_service.get_history_by_conversation_id(self.db, conversation_id, user_id)

    def extract_queried_fields(
        self,
        sql_components: Dict[str, Any],
        semantic_layer: Dict[str, Any],
        used_models: List[str]
    ) -> Dict[str, Any]:
        """
        从SQL组件和语义层中提取查询字段信息

        Args:
            sql_components: SQL组件（包含select、from、where等）
            semantic_layer: 语义层数据
            used_models: SQL中使用的模型列表

        Returns:
            包含维度和指标信息的字典
        """
        return QueriedFieldsExtractor.extract_queried_fields(
            sql_components=sql_components,
            semantic_layer=semantic_layer,
            used_models=used_models
        )

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

    def _apply_nonsemantic_where(self, assembler, cond: Dict[str, Any]) -> None:
        """把一条「非语义」WHERE 条件应用到 assembler —— table-row 与聚合分支共用，杜绝两套漂移。

        优先级：raw_condition(has_or/复杂表达式) > 结构化(operator+value 分开)重组 > 整条 sql_column 正则兜底 > 裸列兜底。
        构建器(table_config_generator._process_where_conditions)对普通非语义条件只存「字段名 + 独立 operator/value」，
        聚合分支历史上却把 sql_column 当整条 'field op value' 正则重解析、无视 operator/value，
        裸字段名解析不出 → add_raw_where(裸列) 生成非法 WHERE。这里统一成「有 operator 就分开重组」，
        与 table-row 一致；仅在缺 operator(用户自定义/历史前端打包整条)时才退回正则解析。
        结构化分支按 dataType 把数值串转成数值，避免数值列被字符串参数绑定(integer > character varying)。
        """
        import re

        raw_condition = cond.get("raw_condition")
        if raw_condition:
            assembler.add_raw_where(raw_condition)
            return

        sql_column = cond.get("sql_column")
        if not sql_column:
            return

        operator = (cond.get("operator") or "").strip()
        data_type = cond.get("dataType")

        # 结构化条目（构建器/前端常规形状）：operator、value 分开带，直接重组，绝不把 sql_column 当整条解析。
        if operator:
            value = cond.get("value")
            op_up = operator.upper()
            if op_up in ('IS NULL', 'IS NOT NULL'):
                assembler.add_raw_where(f"{sql_column} {operator}")
            elif op_up in ('IN', 'NOT IN'):
                if isinstance(value, str):
                    try:
                        value_list = json.loads(value)
                    except Exception:
                        value_list = [v.strip().strip("'\"") for v in value.split(',')]
                else:
                    value_list = value if isinstance(value, list) else [value]
                if data_type:
                    value_list = [convert_where_condition_value(v, data_type, '=') for v in value_list]
                placeholders = ','.join(['%s'] * len(value_list))
                assembler.add_parameterized_where(f"{sql_column} {operator} ({placeholders})", value_list)
            elif op_up == 'BETWEEN':
                value2 = cond.get('value2', value)
                if data_type:
                    value = convert_where_condition_value(value, data_type, operator)
                    value2 = convert_where_condition_value(value2, data_type, operator)
                assembler.add_parameterized_where(f"{sql_column} BETWEEN %s AND %s", [value, value2])
            else:
                if data_type:
                    value = convert_where_condition_value(value, data_type, operator)
                assembler.add_parameterized_where(f"{sql_column} {operator} %s", [value])
            return

        # 无 operator：sql_column 可能是用户自定义/历史前端打包的整条 "field op value"，用正则兜底解析。
        patterns = [
            (r'^(.+?)\s+(IS\s+NULL|IS\s+NOT\s+NULL)\s*$', lambda m: (m.group(1), m.group(2), None)),
            (r'^(.+?)\s+(IN|NOT\s+IN)\s*\((.+)\)\s*$', lambda m: (m.group(1), m.group(2), m.group(3))),
            (r'^(.+?)\s+(BETWEEN)\s+(.+?)\s+AND\s+(.+)\s*$', lambda m: (m.group(1), m.group(2), (m.group(3), m.group(4)))),
            (r'^(.+?)\s*(=|!=|<>|>=|<=|>|<|LIKE|NOT\s+LIKE)\s*(.+)\s*$', lambda m: (m.group(1), m.group(2), m.group(3))),
        ]
        for pattern, extractor in patterns:
            match = re.match(pattern, sql_column, re.IGNORECASE)
            if not match:
                continue
            field, op, val = extractor(match)
            op_up = op.upper()
            if op_up in ('IS NULL', 'IS NOT NULL'):
                assembler.add_raw_where(f"{field} {op}")
            elif op_up in ('IN', 'NOT IN'):
                val_list = [v.strip().strip("'\"") for v in val.split(',')]
                if data_type:
                    val_list = [convert_where_condition_value(v, data_type, '=') for v in val_list]
                placeholders = ','.join(['%s'] * len(val_list))
                assembler.add_parameterized_where(f"{field} {op} ({placeholders})", val_list)
            elif op_up == 'BETWEEN':
                val1, val2 = val
                val1, val2 = val1.strip().strip("'\""), val2.strip().strip("'\"")
                if data_type:
                    val1 = convert_where_condition_value(val1, data_type, op)
                    val2 = convert_where_condition_value(val2, data_type, op)
                assembler.add_parameterized_where(f"{field} BETWEEN %s AND %s", [val1, val2])
            else:
                val = val.strip().strip("'\"")
                if data_type:
                    val = convert_where_condition_value(val, data_type, op)
                assembler.add_parameterized_where(f"{field} {op} %s", [val])
            return

        logger.warning(f"[re-query] 无法解析非语义 where 条件，按原始 SQL 添加(可能非法): {sql_column}")
        assembler.add_raw_where(sql_column)

    def _apply_nonsemantic_having(self, assembler, cond: Dict[str, Any]) -> None:
        """把一条「非语义」HAVING 条件应用到 assembler，与非语义 where 同源对齐。

        构建器非语义 having 同样只存「字段名 + 独立 operator/value」，原先 add_raw_having(sql_column) 把裸字段名
        当整条 HAVING → 非法/丢过滤。这里有 operator 就用 add_having 正常重组(IN 解析、标量按 dataType 转换)；
        raw_condition 走 add_raw_having；缺 operator 才原样兜底。
        """
        raw_condition = cond.get("raw_condition")
        if raw_condition:
            assembler.add_raw_having(raw_condition)
            return

        sql_column = cond.get("sql_column")
        if not sql_column:
            return

        operator = (cond.get("operator") or "").strip()
        if not operator:
            assembler.add_raw_having(sql_column)
            return

        value = cond.get("value")
        data_type = cond.get("dataType")
        op_up = operator.upper()
        if op_up == 'IN':
            value = parse_sql_in_values(value) if isinstance(value, str) else value
        elif op_up not in ('IS NULL', 'IS NOT NULL', 'NOT IN', 'BETWEEN') and data_type:
            value = convert_where_condition_value(value, data_type, operator)
        assembler.add_having(sql_column, FilterOperator.from_value(operator), value)

    def _normalize_cap(self, cap) -> Optional[int]:
        """把可能来自前端回传的行数上限归一成「正整数或 None」。

        cap 类型不固定：int 30 / 数字字符串 "30" / None / 空串 / bool。
        bool 是 int 子类必须排除（True 会被当 1）；字符串按纯数字解析；其余非正数归 None。
        """
        if isinstance(cap, bool):
            return None
        if isinstance(cap, str):
            cap = int(cap.strip()) if cap.strip().isdigit() else None
        return cap if isinstance(cap, int) and cap > 0 else None

    def _effective_limit(self, cap: Optional[int], ceiling: Optional[int]) -> Optional[int]:
        """导出行数上限 = min(用户 N 上限, ceiling 安全阈值)。

        只有 cap → cap；只有 ceiling → ceiling；两者都有 → min；两者都无 → None
        （调用方须保证 ceiling 非空，避免真·无界全量导出，见计划风险 R-上限）。
        """
        candidates = [v for v in (cap, ceiling) if isinstance(v, int) and v > 0]
        return min(candidates) if candidates else None

    def _export_label(self, semantic_field, fallback: str) -> str:
        """导出表头的中文显示名：优先 field_detail 的中文名，缺失回退裸列名/sql_column。"""
        fd = (semantic_field or {}).get("field_detail") or {}
        for k in ("dimensionName", "semanticName", "metricName", "name"):
            if fd.get(k):
                return fd[k]
        for k in ("dimensionName", "semanticName", "name"):
            if (semantic_field or {}).get(k):
                return semantic_field[k]
        return fallback

    def _label_from_sql_column(self, sql_column: str) -> str:
        """从非语义列的原始表达式里取展示用表头。

        非语义列(unmatched)的 sql_column 往往是「整段表达式 + AS 别名」,例如
        `COUNT(t1."zgh") AS "50岁以上老师数量"`——直接当表头会把整串塞进单元格。
        这里优先取最后一个 `AS` 后的别名(去成对引号,即 LLM 起的中文名);无别名则取
        裸列名末段去引号(如 `t1."xm"` → `xm`,与页面现有列身份一致)。
        """
        s = (sql_column or "").strip().rstrip(";").strip()
        idx = s.lower().rfind(" as ")  # 取最后一个顶层 AS,避开 CAST(x AS int) 之类内层 AS
        if idx != -1:
            alias = s[idx + 4:].strip()
            if len(alias) >= 2 and alias[0] in "\"'`" and alias[-1] == alias[0]:
                alias = alias[1:-1]
            if alias:
                return alias.strip()
        return s.split(".")[-1].strip().strip("\"'`")

    def _finalize_requery(self, assembler, cap, export_mode, export_ceiling, export_headers):
        """re-query 无分页分支收尾：导出模式套 effective_limit 并回传中文表头，否则原样全量。"""
        if not export_mode:
            sql, params = assembler.build_sql_for_jdbc()
            return {"sql": sql, "params": params}
        eff = self._effective_limit(self._normalize_cap(cap), export_ceiling)
        if isinstance(eff, int) and eff > 0:
            assembler.set_limit(eff)
        sql, params = assembler.build_sql_for_jdbc()
        return {"sql": sql, "params": params, "headers": export_headers, "effective_limit": eff}

    def _build_requery_pagination_result(self, assembler, pagination_info, cap):
        """构建 re-query 的分页 count_sql/data_sql（table-row 与聚合两分支共用，杜绝形状漂移）。

        cap 为用户在自然语言里给定的行数上限（table_config['limit']）。仅当 cap 为正整数时按
        「硬上限 + 页内分页」处理：
          - count：对带 `LIMIT cap` 的子查询计数 → min(实际, cap)，与首屏 data_count 口径一致；
          - data：把页窗钳到 [offset, offset+page_size) ∩ [0, cap)。越过上限的页返回 0 行
            （合法 `LIMIT 0`），不会泄露 cap 之外的数据。
        cap 为 None / 非正整数时维持原行为（全量分页），输出 SQL 字节级不变（零回归）。
        """
        page_size = int(pagination_info["page_size"])
        page_index = int(pagination_info["page_index"])
        # cap 可能来自前端回传，类型不固定（int/数字串/None/空串/bool），统一归一。
        cap = self._normalize_cap(cap)
        has_cap = isinstance(cap, int) and cap > 0
        count_sql, count_sql_params = assembler.build_count_sql_for_jdbc(cap=cap if has_cap else None)
        if has_cap:
            offset = (page_index - 1) * page_size
            # max(0, ...) 承重：offset≥cap 时算出 0，发合法 `LIMIT 0`，绝不能传负数
            assembler.set_limit(max(0, min(page_size, cap - offset)), offset)
        else:
            assembler.set_pagination(page_index, page_size)
        sql, params = assembler.build_sql_for_jdbc()
        return {
            "sql": sql,
            "params": params,
            "count_sql": count_sql,
            "count_sql_params": count_sql_params,
        }

    async def generate_requery_sql(self, chart_type: str, table_config: Dict[str, Any], sql_components: Dict[str, Any],
                                   model_table_alias_mapping_list: List[Dict[str, Any]],
                                   pagination_info: Optional[Dict[str, Any]], user_id: str,
                                   export_mode: bool = False, export_ceiling: Optional[int] = None):
        """生成重新查询的SQL语句。

        export_mode=True（导出全部数据）时：不分页，按 effective_limit=min(用户N上限, ceiling)
        套 LIMIT，并按 SELECT 顺序收集中文表头 export_headers 一并返回。默认 False ⇒ 现有
        re-query 调用行为字节级不变。
        """
        # 仅 export_mode 收集；按 add_column/add_raw_column 的 SELECT 顺序逐列追加中文显示名，
        # 与中台 getColumnName 有序列按「位置」对齐（不依赖 key 名）。
        export_headers: list[str] = []
        base_from = sql_components["from"]
        all_semantic_fields = table_config["all_semantic_fields"]
        # 用户给定的行数上限（如「30 条」）；翻页时作为硬封顶，缺省/非正整数则全量分页
        cap = table_config.get("limit")
        from_sentence = ""
        if base_from.lower().startswith("from"):
            from_sentence = base_from.split("FROM")[1]
        else:
            from_sentence = base_from

        from_info = parse_from_clause(from_sentence)
        main_table = from_info["main_table"]
        existing_tables = from_info["existing_tables"]
        # 获得用户手动调整/添加的字段ID列表
        adjusted_field_ids = extract_manually_adjusted_field_ids(table_config)
        # 获取这些字段涉及的模型ID列表（去重）
        adjusted_model_ids = list({
            semantic_field["from_model_id"]
            for field_id in adjusted_field_ids
            if (semantic_field := self._find_semantic_field(field_id, all_semantic_fields)) is not None
        })
        # 获取用户手动调整涉及的模型信息
        adjusted_models = [
            mapping for mapping in model_table_alias_mapping_list
            if mapping["modelId"] in adjusted_model_ids
        ]
        # 判断哪些表需要新增 JOIN
        tables_to_add = [
            mapping for mapping in adjusted_models
            if mapping["table"] not in existing_tables
        ]

        # 获取主表的 modelId
        main_table_model_id = next(
            (mapping["modelId"] for mapping in model_table_alias_mapping_list if mapping["table"] == main_table),
            None
        )
        model_permissions_map = None
        if main_table_model_id:
            try:
                # 获取关系信息
                relationships = await self.semantic_api_client.get_model_relationships_async([main_table_model_id])

                # ========== 权限逻辑开始 ==========

                # 1. 提取所有涉及的模型ID
                involved_model_ids = {main_table_model_id}

                for rel in relationships:
                    involved_model_ids.add(rel["sourceModelId"])
                    involved_model_ids.add(rel["targetModelId"])

                # 将用户手动调整涉及的模型也加入
                for adjusted_model_id in adjusted_model_ids:
                    involved_model_ids.add(adjusted_model_id)

                # 2. 获取权限信息
                permissions_response = await self.semantic_api_client.get_user_semantic_permissions_async(
                    user_id=user_id,
                    model_id_list=list(involved_model_ids)
                )

                # 3. 构建权限映射
                model_permissions_map = build_model_permissions_map(
                    permissions_response,
                    model_table_alias_mapping_list
                )

                # 记录有权限限制的模型
                if model_permissions_map:
                    models_with_permissions = [
                        f"{perm['table']}({perm['alias']})"
                        for perm in model_permissions_map.values()
                    ]

                # ========== 权限逻辑结束 ==========

                # 需要新增的表的 modelId 集合（用于快速查找）
                tables_to_add_ids = {m["modelId"] for m in tables_to_add}

                # 筛选相关关系
                relevant_relationships = [
                    rel for rel in relationships
                    if rel["sourceModelId"] in tables_to_add_ids or rel["targetModelId"] in tables_to_add_ids
                ]

                # 只在找到关系时才更新 from_sentence
                if relevant_relationships:
                    from_sentence = append_join_clauses(
                        from_sentence,
                        relevant_relationships,
                        model_table_alias_mapping_list,
                        existing_tables
                    )
                else:
                    logger.warning(f"未找到以下表与主表的关系信息: {[t['table'] for t in tables_to_add]}")

            except Exception as e:
                logger.error(f"获取模型关系失败: {str(e)}", exc_info=True)
                raise e
        elif tables_to_add and not main_table_model_id:
            logger.error(f"无法找到主表 {main_table} 的模型ID，无法添加新的 JOIN")

        assembler = FlexibleSQLAssembler(from_sentence)

        if chart_type == "table-row":
            for column in table_config["columns"]:
                if column["is_semantic_field"]:
                    semantic_field = self._find_semantic_field(column["id"], all_semantic_fields)
                    table_alias = self._find_table_alias(semantic_field["from_model_id"],
                                                         model_table_alias_mapping_list)
                    if semantic_field["semantic_type"] == "metric":
                        column_name = f"{table_alias}.{semantic_field.get('semantic_field_name', '').split('.')[1]}"
                    else:
                        column_name = f"{table_alias}.{semantic_field['semantic_field_name']}"
                    assembler.add_column(column_name)
                    if export_mode:
                        export_headers.append(self._export_label(semantic_field, column_name.split(".")[-1]))
                else:
                    column_name = column["sql_column"]
                    assembler.add_raw_column(column_name)
                    if export_mode:
                        export_headers.append(self._label_from_sql_column(column_name))

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
                    # 非语义条件统一走共享 helper（与聚合分支同一套逻辑，杜绝漂移）
                    self._apply_nonsemantic_where(assembler, filter)

            # 注入权限条件
            apply_permissions_to_assembler(
                assembler,
                model_permissions_map,
                model_table_alias_mapping_list
            )

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
                return self._build_requery_pagination_result(assembler, pagination_info, cap)
            return self._finalize_requery(assembler, cap, export_mode, export_ceiling, export_headers)

        elif chart_type == "table-aggr" or chart_type == "bar" or chart_type == "pie" or chart_type == "line" or chart_type == "area" or chart_type == "matrix" or chart_type == "bubble":
            for dimension in table_config["dimensions"]:
                if dimension["is_semantic_field"]:
                    semantic_field = self._find_semantic_field(dimension["id"], all_semantic_fields)
                    table_alias = self._find_table_alias(semantic_field["from_model_id"],
                                                         model_table_alias_mapping_list)
                    column_name = f"{table_alias}.{semantic_field['semantic_field_name']}"
                    assembler.add_column(column_name)
                    assembler.add_group_by(column_name)
                    if export_mode:
                        export_headers.append(self._export_label(semantic_field, column_name.split(".")[-1]))
                else:
                    assembler.add_raw_column(dimension["sql_column"])
                    assembler.add_raw_group_by(dimension["sql_column"])
                    if export_mode:
                        export_headers.append(self._label_from_sql_column(dimension["sql_column"]))

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
                        if export_mode:
                            export_headers.append(semantic_name or alias)
                    elif metric["semantic_type"] == "metric":
                        expression = semantic_field["field_detail"]["expression"]
                        processor = SQLFieldAliasProcessor()
                        new_expression = processor.add_table_alias_to_expression(expression, table_alias)
                        assembler.add_raw_column(new_expression)
                        if export_mode:
                            export_headers.append(self._export_label(semantic_field, expression))
                else:
                    assembler.add_raw_column(metric["sql_column"])
                    if export_mode:
                        export_headers.append(self._label_from_sql_column(metric["sql_column"]))

            # 兜底：前端聚合表分页/刷新路径可能只回传 dimensions/metrics，漏掉
            # where_conditions/having_conditions/order_by。此时退回到 sql_components
            # 里原始 LLM 生成的 WHERE，防止分页时静默丢失过滤条件导致 count 与首屏不一致。
            # 语义：key 缺失或 None = 前端未传，兜底；显式传 [] = 用户清空，尊重其意图。
            sql_components_safe = sql_components or {}
            where_conditions_cfg = table_config.get("where_conditions")
            if where_conditions_cfg is None:
                raw_where = sql_components_safe.get("where", "").strip()
                if raw_where:
                    logger.info("[re-query][fallback] table_config 缺 where_conditions，回退使用 sql_components.where: %s", raw_where)
                    assembler.add_raw_where(raw_where)
                where_conditions_cfg = []

            for where_condition in where_conditions_cfg:
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
                    # 非语义条件统一走共享 helper：原先把 sql_column 当整条 'field op value' 正则重解析、
                    # 无视同级 operator/value，构建器存的是「裸字段名 + 独立 operator/value」→ 解析不出 →
                    # add_raw_where(裸列) 生成非法 WHERE。改为与 table-row 同一套：有 operator 就分开重组。
                    self._apply_nonsemantic_where(assembler, where_condition)

            # 注入权限条件
            apply_permissions_to_assembler(
                assembler,
                model_permissions_map,
                model_table_alias_mapping_list
            )

            having_conditions_cfg = table_config.get("having_conditions")
            if having_conditions_cfg is None:
                raw_having = sql_components_safe.get("having", "").strip()
                if raw_having:
                    logger.info("[re-query][fallback] table_config 缺 having_conditions，回退使用 sql_components.having: %s", raw_having)
                    assembler.add_raw_having(raw_having)
                having_conditions_cfg = []

            for having_condition in having_conditions_cfg:
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
                    # 非语义 having 同源对齐：有 operator 就分开重组(add_having)，不再把裸字段名当整条 HAVING
                    self._apply_nonsemantic_having(assembler, having_condition)

            order_by_cfg = table_config.get("order_by")
            if order_by_cfg is None:
                raw_order = sql_components_safe.get("orderBy", "").strip()
                if raw_order:
                    logger.info("[re-query][fallback] table_config 缺 order_by，回退使用 sql_components.orderBy: %s", raw_order)
                    assembler.add_raw_order_by(raw_order)
                order_by_cfg = []

            for order_by in order_by_cfg:
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
                return self._build_requery_pagination_result(assembler, pagination_info, cap)
            return self._finalize_requery(assembler, cap, export_mode, export_ceiling, export_headers)

    async def get_hc_dim_values_by_dim_value(
            self,
            keyword: str,
            dimension_id: str,
            page_index: int = 1,
            page_size: int = 20,
            fuzzy_match: bool = True,
            user_id: str = None
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
                fuzzy_match=fuzzy_match,
                user_id=user_id
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
            dataset_details = await self.semantic_api_client.get_dataset_detail_async(dataset_id)

            if not dataset_details or len(dataset_details) == 0:
                raise ValueError(f"未找到数据集 {dataset_id} 的详情信息")

            dataset_detail = dataset_details[0]

            # 2. 获取模型关系
            model_ids = [model["modelId"] for model in dataset_detail.get("models", [])]

            if not model_ids:
                raise ValueError("数据集中没有模型")

            model_relationships = await self.semantic_api_client.get_model_relationships_async(model_ids)
            logger.info(f"获取到 {len(model_relationships)} 条模型关系")

            # 3. 获取用户权限（如果提供了用户ID）
            user_permissions = None
            if user_id:
                try:
                    user_permissions = await self.semantic_api_client.get_user_semantic_permissions_async(
                        user_id,
                        [dataset_id]
                    )
                except Exception as e:
                    logger.warning(f"获取用户权限失败，将使用默认权限: {str(e)}")
                    user_permissions = None

            # 4. 生成SQL
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

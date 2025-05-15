import os
import logging
from typing import Any, List, Dict

from fastapi.params import Depends
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.service.nl2sql_service.EChartsGenerator import EChartsGenerator
from api.service.nl2sql_service.custom_jieba_tokenizer import custom_tokenize_with_semantic_words
from api.service.nl2sql_service.fill_sql_template import fill_sql_template
from api.service.nl2sql_service.generate_nl2sql_prompt import generate_nl2sql_prompt
from api.service.nl2sql_service.llm_sql_generator import LLMSQLGenerator
from api.service.nl2sql_service.llm_sql_templating import LLMSQLTemplating
from api.service.nl2sql_service.query_intent_analyzer import QueryIntentType, QueryIntentAnalyzer
from api.service.nl2sql_service.query_rewriter import QueryRewriter
from api.service.nl2sql_service.semantic_api_client import SemanticApiClient

logger = logging.getLogger(__name__)


class NL2SQLService:
    """服务类，用于处理自然语言到SQL的转换和查询重写。"""

    def __init__(self, db: Session, user: Any):
        self.db = db
        self.user = user
        self.prompt_dir = os.path.join(os.path.dirname(__file__), "prompt")
        # 初始化查询重写器
        self.query_rewriter = QueryRewriter(db, user.id, self.prompt_dir)
        # 初始化查询意图分析器
        self.query_intent_analyzer = QueryIntentAnalyzer(db, user.id, self.prompt_dir)
        self.llm_sql_generator = LLMSQLGenerator(db, user.id)
        self.llm_sql_templating = LLMSQLTemplating(db, user.id)
        self.echarts_generator = EChartsGenerator(db, user.id)
        self.semantic_api_client = SemanticApiClient()

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

    async def _semantic_mapping(self, query_text: str, llm_name: str) -> List[str]:
        pass

    async def nl2sql(self, query_text: str, llm_name: str, dataset_id_list: List[str]):
        """
        使用LLM转换自然语言查询为SQL
        """
        # 1. 获得查询意图
        intents = await self.analyze_query_intent(query_text, llm_name)
        # 2. 重写查询
        # rewritten_queries = await self.query_rewriter.rewrite_query(query_text, llm_name)
        # 3. 分词
        segmented_words = await custom_tokenize_with_semantic_words(text=query_text, dataset_id_list=dataset_id_list)
        # 4. 将分词到语义层结构化数据中进行检索得到相关数据
        # 4.1 根据分词关键字获得维度列表
        dimensions_by_keyword = await self.semantic_api_client.get_dimension_info_by_keyword_async(
            keyword=segmented_words,
            dataset_ids=dataset_id_list)
        # 4.2 分词关键字作为维度值关键字获得获得维度列表
        dimensions_by_value = await self.semantic_api_client.get_dimension_by_dimension_value_async(
            keyword=segmented_words,
            dataset_ids=dataset_id_list)
        # 4.3 根据dimensionId对dimensions_by_keyword和dimensions_by_value进行维度去重，获得最终维度列表
        unique_dimensions = self._deduplicate_dimensions(dimensions_by_keyword, dimensions_by_value)
        dimension_values = await self.semantic_api_client.get_dimension_values_async(dimension_ids=unique_dimensions)
        dimensions = await self.semantic_api_client.get_dimension_info_by_id_async(dimension_ids=unique_dimensions)
        # 4.4 根据分词关键字获得指标列表
        metrics = await self.semantic_api_client.get_metric_info_by_keyword_async(
            keyword=segmented_words,
            dataset_ids=dataset_id_list)

        # 4.5 从维度和指标中提取所有modelId并去重，获得模型ID列表
        model_ids = self._extract_unique_model_ids(dimensions, metrics)

        # 4.6 查询模型详情和关联关系
        model_details = await self.semantic_api_client.get_model_detail_async(model_ids=model_ids)
        model_relations = await self.semantic_api_client.get_model_relationships_async(model_ids=model_ids)

        # 4.7 查询业务术语
        dataset_details = await self.semantic_api_client.get_dataset_detail_async(dataset_ids=dataset_id_list)
        domain_ids = self._extract_unique_domain_ids(dataset_details)
        business_term_rows = await self.semantic_api_client.get_business_term_info_async(keyword=segmented_words,
                                                                                         domain_ids=domain_ids)
        semantic_layer = dict(dataset_details=dataset_details, dimensions=dimensions, dimension_values=dimension_values,
                              metrics=metrics, model_details=model_details,
                              model_relations=model_relations, business_term_rows=business_term_rows)
        prompt, semantic_layer_struct = generate_nl2sql_prompt(user_question=query_text, query_intents=intents,
                                                               semantic_layer=semantic_layer)
        sql = await self.llm_sql_generator.generate_sql(prompt=prompt, llm_name=llm_name)

        return sql, semantic_layer_struct

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

    async def sql_templating(self, original_question: str, llm_name: str, sql: str,
                             semantic_layer: Dict[str, Any]):
        """
        SQL模板化
        """
        # 1. 识别可参数化的内容，返回json
        sql_template_data = await self.llm_sql_templating.generate_sql_template(original_question=original_question,
                                                                                sql=sql,
                                                                                semantic_layer=semantic_layer,
                                                                                llm_name=llm_name)
        # 2. 解析json，填充参数点对应的值
        parameters = sql_template_data["parameters"]
        for parameter in parameters:
            if parameter["type"] == "DIMENSION_FILTER":
                dimension_id = parameter["semantic_info"]["dimension_id"]
                dimension_values = await self.semantic_api_client.get_dimension_values_async(
                    dimension_ids=[dimension_id])
                parameter["possible_values"] = dimension_values[dimension_id]

        return sql_template_data

    async def fill_sql_template(self, templated_sql: str, parameter_definitions: list,
                                user_selected_values: dict) -> str:
        """
        根据用户选择的值填充SQL模板。
        """
        sql = fill_sql_template(templated_sql, parameter_definitions, user_selected_values)
        return sql

    async def generate_echarts(self, user_question: str, sql: str, column_and_type: str, sample_data: str,
                               llm_name: str):
        """
        生成 ECharts配置的 JavaScript 代码
        """
        js_code = await self.echarts_generator.generate_echarts_config(user_question, sql, column_and_type, sample_data,
                                                                       llm_name=llm_name)
        return js_code


def get_nl2sql_service(db: Session = Depends(get_db), user=Depends(manager)) -> NL2SQLService:
    """通过依赖注入获取NL2SQLService实例"""
    return NL2SQLService(db, user)

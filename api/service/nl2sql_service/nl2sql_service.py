import os
import logging
from typing import Any, List

from fastapi.params import Depends
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.service.nl2sql_service.custom_jieba_tokenizer import custom_tokenize_with_semantic_words, custom_tokenize
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

    async def analyze_query_intent(self, query_text: str, llm_name: str) -> List[QueryIntentType]:
        """
        使用LLM分析自然语言查询意图
        """
        return await self.query_intent_analyzer.analyze_query_intent(query_text, llm_name)

    async def _semantic_mapping(self, query_text: str, llm_name: str) -> List[str]:
        pass

    async def nl2sql(self, query_text: str, llm_name: str, dataset_id_list: List[str]) -> str:
        """
        使用LLM转换自然语言查询为SQL
        """
        # 1. 获得查询意图
        # intents = await self.analyze_query_intent(query_text, llm_name)
        # 2. 重写查询
        # rewritten_queries = await self.query_rewriter.rewrite_query(query_text, llm_name)
        # 3. 分词
        segmented_words = await custom_tokenize_with_semantic_words(text=query_text, dataset_id_list=dataset_id_list)
        # 4. 将分词到语义层结构化数据中进行检索得到相关数据
        # 4.1 根据分词关键字获得维度列表
        # dimensions = await self.semantic_api_client.get_dimension_info_by_keyword_async(keyword=query_text,
        #                                                                                 dataset_ids=dataset_id_list)
        # 4.2 分词关键字作为维度值关键字获得获得维度列表
        # dimension_value_rows = await self.semantic_api_client.get_dimension_by_dimension_value_async(keyword=query_text,
        #                                                                                             dataset_ids=dataset_id_list)
        # 4.3 根据分词关键字获得指标列表
        # metric_rows = await self.semantic_api_client.get_metric_info_by_keyword_async(keyword=query_text,
        #                                                                               dataset_ids=dataset_id_list)
        # 4.4 根据维度、指标获得涉及的模型，并查询模型详情
        # 4.5 查询模型关联关系
        # 4.6 查询业务术语
        # business_term_rows = await self.semantic_api_client.get_business_term_info_async(keyword=query_text,
        #                                                                                  domain_ids=domain_ids)

        return ""


def get_nl2sql_service(db: Session = Depends(get_db), user=Depends(manager)) -> NL2SQLService:
    """通过依赖注入获取NL2SQLService实例"""
    return NL2SQLService(db, user)

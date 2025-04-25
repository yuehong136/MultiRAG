import os
import logging
from typing import Any, List

from fastapi.params import Depends
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.service.nl2sql_service.query_rewriter import QueryRewriter

logger = logging.getLogger(__name__)


class NL2SQLService:
    """服务类，用于处理自然语言到SQL的转换和查询重写。"""

    def __init__(self, db: Session, user: Any):
        self.db = db
        self.user = user
        self.prompt_dir = os.path.join(os.path.dirname(__file__), "prompt")
        # 初始化查询重写器
        self.query_rewriter = QueryRewriter(db, user.id, self.prompt_dir)

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


def get_nl2sql_service(db: Session = Depends(get_db), user=Depends(manager)) -> NL2SQLService:
    """通过依赖注入获取NL2SQLService实例"""
    return NL2SQLService(db, user)

import logging
from typing import Optional, Dict, Any, Annotated, Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, Discriminator, model_validator

from common.constants import LLMType
from api.db.db_models import get_db
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from common import settings
from common.constants import RetCode
from api.utils.api_utils import build_error_result, apikey_dependency
from core.app.tag import label_question
from api.db.services.dialog_service import meta_filter, convert_conditions

# 创建路由器
router = APIRouter()


# Pydantic 模型定义

class SparseSearchMode(BaseModel):
    type: Literal["sparse"] = "sparse"


class DenseSearchMode(BaseModel):
    type: Literal["dense"] = "dense"


class HybridSearchMode(BaseModel):
    type: Literal["hybrid"] = "hybrid"
    weight_dense: float = Field(default=0.7, ge=0.0, le=1.0)
    weight_sparse: float = Field(default=0.3, ge=0.0, le=1.0)

    @model_validator(mode='after')
    def validate_weights(self) -> 'HybridSearchMode':
        """确保权重和为1，如果不是则自动调整"""
        total = self.weight_dense + self.weight_sparse
        if abs(total - 1.0) > 0.001:
            # 自动归一化权重
            self.weight_dense = self.weight_dense / total
            self.weight_sparse = self.weight_sparse / total
        return self


class FusionSearchMode(BaseModel):
    type: Literal["fusion"] = "fusion"
    weights: str = Field(default="0.05,0.95")

    @model_validator(mode='after')
    def validate_weights_format(self) -> 'FusionSearchMode':
        """验证weights格式"""
        try:
            parts = self.weights.split(',')
            if len(parts) != 2:
                raise ValueError("weights must contain exactly two comma-separated values")
            float(parts[0].strip())
            float(parts[1].strip())
        except (ValueError, AttributeError):
            raise ValueError("weights must be in format 'float,float' (e.g., '0.05,0.95')")
        return self


# 使用 Discriminator 的高效版本
SearchModeType = Annotated[
    SparseSearchMode | DenseSearchMode | HybridSearchMode | FusionSearchMode,
    Discriminator('type')
]

class RetrievalSettingModel(BaseModel):
    """检索设置模型"""
    score_threshold: float = Field(default=0.0, description="相似度阈值")
    top_k: int = Field(default=1024, description="返回结果数量")


class RetrievalRequestModel(BaseModel):
    """检索请求模型"""
    knowledge_id: str = Field(..., description="知识库ID")
    query: str = Field(..., description="查询问题")
    use_kg: bool = Field(default=False, description="是否使用知识图谱")
    retrieval_setting: RetrievalSettingModel = Field(default_factory=RetrievalSettingModel, description="检索设置")
    metadata_condition: Optional[Dict[str, Any]] = Field(default=None, description="元数据过滤条件")
    search_mode: SearchModeType | None  = Field(default=None, description="检索模式")

    def get_search_mode_dict(self) -> dict[str, Any] | None:
        """将搜索模式转换为字典格式供底层函数使用"""
        if self.search_mode is None:
            return None

        mode_data = self.search_mode.model_dump()
        mode_type = mode_data.pop('type')
        return {mode_type: mode_data}


@router.post('/retrieval', summary="Dify 检索接口")
async def retrieval(
    request_data: RetrievalRequestModel,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(apikey_dependency)
):
    """
    Dify 兼容的检索接口
    
    Args:
        request_data: 检索请求数据
        db: 数据库会话
        tenant_id: 租户ID（从API Key中提取）
    
    Returns:
        检索结果
    """
    question = request_data.query
    kb_id = request_data.knowledge_id
    use_kg = request_data.use_kg
    similarity_threshold = float(request_data.retrieval_setting.score_threshold)
    top = int(request_data.retrieval_setting.top_k)
    metadata_condition = request_data.metadata_condition
    search_mode_dict = request_data.get_search_mode_dict()

    metas = DocumentService.get_meta_by_kbs(db, [kb_id])
    doc_ids = []
    
    try:
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        if not kb:
            return build_error_result(
                error_msg="Knowledgebase not found!", 
                retcode=RetCode.NOT_FOUND
            )

        embd_mdl = LLMBundle(db, kb.tenant_id, LLMType.EMBEDDING.value, llm_name=kb.embd_id)
        print(metadata_condition)
        # print("after", convert_conditions(metadata_condition))
        doc_ids.extend(meta_filter(metas, convert_conditions(metadata_condition)))
        # print("doc_ids", doc_ids)
        
        if not doc_ids and metadata_condition is not None:
            doc_ids = ['-999']
            
        ranks = settings.retriever.retrieval(
            question,
            embd_mdl,
            kb.tenant_id,
            [kb.name],
            page=1,
            page_size=top,
            similarity_threshold=similarity_threshold,
            vector_similarity_weight=0.3,
            top=top,
            doc_ids=doc_ids,
            rank_feature=label_question(db, question, [kb]),
            search_mode=search_mode_dict
        )

        if use_kg:
            ck = settings.kg_retriever.retrieval(
                question,
                [tenant_id],
                [kb.name],
                embd_mdl,
                LLMBundle(db, kb.tenant_id, LLMType.CHAT)
            )
            if ck["content_with_weight"]:
                ranks["chunks"].insert(0, ck)

        records = []
        for c in ranks["chunks"]:
            e, doc = DocumentService.get_by_id(db, c["doc_id"])
            c.pop("vector", None)
            meta = getattr(doc, 'meta_fields', {})
            meta["doc_id"] = c["doc_id"]
            records.append({
                "content": c["content_with_weight"],
                "score": c["similarity"],
                "title": c["docnm_kwd"],
                "metadata": meta
            })

        return {"records": records}
        
    except Exception as e:
        if str(e).find("not_found") > 0:
            return build_error_result(
                error_msg='No chunk found! Check the chunk status please!',
                retcode=RetCode.NOT_FOUND
            )
        logging.exception(e)
        return build_error_result(
            error_msg=str(e), 
            retcode=RetCode.SERVER_ERROR
        )
"""
Dify 兼容的检索接口模块

使用 Pydantic V2 和 Python 3.12+ 语法
"""
import logging
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, Discriminator, model_validator
from sqlalchemy.orm import Session

from api.db.db_models import get_db
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.utils.api_utils import apikey_dependency, build_error_result
from common import settings
from common.metadata_utils import convert_conditions, meta_filter
from common.constants import LLMType, RetCode
from core.app.tag import label_question

router = APIRouter()


# ============================================================================
# 搜索模式模型定义
# ============================================================================

class SparseSearchMode(BaseModel):
    """稀疏检索模式（关键词匹配）"""
    model_config = ConfigDict(frozen=True)

    type: Literal["sparse"] = "sparse"


class DenseSearchMode(BaseModel):
    """密集检索模式（向量相似度）"""
    model_config = ConfigDict(frozen=True)

    type: Literal["dense"] = "dense"


class HybridSearchMode(BaseModel):
    """混合检索模式（稀疏+密集加权融合）"""
    model_config = ConfigDict(validate_assignment=True)

    type: Literal["hybrid"] = "hybrid"
    weight_dense: float = Field(default=0.7, ge=0.0, le=1.0, description="密集检索权重")
    weight_sparse: float = Field(default=0.3, ge=0.0, le=1.0, description="稀疏检索权重")

    @model_validator(mode="after")
    def normalize_weights(self) -> Self:
        """确保权重和为1，自动归一化"""
        total = self.weight_dense + self.weight_sparse
        if abs(total - 1.0) > 1e-6:
            ratio = 1.0 / total
            object.__setattr__(self, "weight_dense", self.weight_dense * ratio)
            object.__setattr__(self, "weight_sparse", self.weight_sparse * ratio)
        return self


class FusionSearchMode(BaseModel):
    """融合检索模式（RRF 排序融合）"""
    model_config = ConfigDict(validate_assignment=True)

    type: Literal["fusion"] = "fusion"
    weights: str = Field(default="0.05,0.95", pattern=r"^\d+\.?\d*,\d+\.?\d*$")

    @model_validator(mode="after")
    def validate_weights_format(self) -> Self:
        """验证并解析 weights 格式"""
        parts = self.weights.split(",")
        if len(parts) != 2:
            raise ValueError("weights 必须包含两个逗号分隔的数值")
        try:
            w1, w2 = float(parts[0].strip()), float(parts[1].strip())
            if w1 < 0 or w2 < 0:
                raise ValueError("权重值不能为负数")
        except ValueError as e:
            raise ValueError(f"weights 格式无效: {e}") from e
        return self


# Python 3.12+ 类型别名语法
type SearchMode = SparseSearchMode | DenseSearchMode | HybridSearchMode | FusionSearchMode

# 使用 Discriminator 实现高效的联合类型解析
# 注意：Discriminator 必须直接与 Union 类型一起使用，不能使用类型别名
SearchModeUnion = Annotated[
    SparseSearchMode | DenseSearchMode | HybridSearchMode | FusionSearchMode,
    Discriminator("type")
]


# ============================================================================
# 请求/响应模型
# ============================================================================

class RetrievalSetting(BaseModel):
    """检索设置"""
    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    score_threshold: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="相似度阈值，低于此值的结果将被过滤",
    )
    top_k: int = Field(
        default=1024,
        ge=1,
        le=10000,
        description="返回结果的最大数量",
    )


class MetadataCondition(BaseModel):
    """元数据过滤条件"""
    model_config = ConfigDict(extra="allow")

    logic: Literal["and", "or"] = Field(default="and", description="条件逻辑运算符")
    conditions: list[dict[str, Any]] = Field(default_factory=list, description="过滤条件列表")


class RetrievalRequest(BaseModel):
    """检索请求模型"""
    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "knowledge_id": "kb_123456",
                    "query": "什么是机器学习？",
                    "use_kg": False,
                    "retrieval_setting": {"score_threshold": 0.5, "top_k": 10},
                }
            ]
        },
    )

    knowledge_id: str = Field(..., min_length=1, description="知识库ID")
    query: str = Field(..., min_length=1, description="查询问题")
    use_kg: bool = Field(default=False, description="是否使用知识图谱增强检索")
    retrieval_setting: RetrievalSetting = Field(
        default_factory=RetrievalSetting,
        description="检索设置",
    )
    metadata_condition: MetadataCondition | dict[str, Any] | None = Field(
        default=None,
        description="元数据过滤条件",
    )
    search_mode: SearchModeUnion | None = Field(
        default=None,
        description="检索模式配置",
    )

    def get_search_mode_dict(self) -> dict[str, Any] | None:
        """将搜索模式转换为底层函数所需的字典格式"""
        if self.search_mode is None:
            return None
        mode_data = self.search_mode.model_dump()
        mode_type = mode_data.pop("type")
        return {mode_type: mode_data}

    def get_metadata_condition_dict(self) -> dict[str, Any] | None:
        """获取元数据条件的字典格式"""
        if self.metadata_condition is None:
            return None
        if isinstance(self.metadata_condition, MetadataCondition):
            return self.metadata_condition.model_dump()
        return self.metadata_condition


class RetrievalRecord(BaseModel):
    """单条检索结果"""
    content: str = Field(..., description="检索内容")
    score: float = Field(..., description="相似度分数")
    title: str = Field(..., description="文档标题")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")


class RetrievalResponse(BaseModel):
    """检索响应模型"""
    records: list[RetrievalRecord] = Field(default_factory=list, description="检索结果列表")


# ============================================================================
# API 端点
# ============================================================================

@router.post(
    "/retrieval",
    summary="Dify 检索接口",
    response_model=RetrievalResponse,
    responses={
        200: {"description": "检索成功"},
        404: {"description": "知识库或文档块未找到"},
        500: {"description": "服务器内部错误"},
    },
)
async def retrieval(
    request_data: RetrievalRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(apikey_dependency),
) -> RetrievalResponse | dict[str, Any]:
    """
    Dify 兼容的检索接口

    支持多种检索模式：
    - sparse: 稀疏检索（关键词匹配）
    - dense: 密集检索（向量相似度）
    - hybrid: 混合检索（加权融合）
    - fusion: 融合检索（RRF 排序）
    """
    # 提取请求参数
    question = request_data.query
    kb_id = request_data.knowledge_id
    use_kg = request_data.use_kg
    similarity_threshold = request_data.retrieval_setting.score_threshold
    top_k = request_data.retrieval_setting.top_k
    metadata_condition = request_data.get_metadata_condition_dict()
    search_mode_dict = request_data.get_search_mode_dict()

    # 获取元数据并处理过滤条件
    metas = DocumentService.get_meta_by_kbs(db, [kb_id])
    doc_ids: list[str] = []

    try:
        # 验证知识库存在
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        if not kb:
            return build_error_result(
                error_msg="知识库未找到",
                retcode=RetCode.NOT_FOUND,
            )

        # 初始化嵌入模型
        embd_mdl = LLMBundle(db, kb.tenant_id, LLMType.EMBEDDING.value, llm_name=kb.embd_id)

        # 处理元数据过滤
        if metadata_condition:
            filtered_ids = meta_filter(
                metas,
                convert_conditions(metadata_condition),
                metadata_condition.get("logic", "and"),
            )
            doc_ids.extend(filtered_ids)
            # 如果有过滤条件但没有匹配结果，使用占位符避免返回所有文档
            if not doc_ids:
                doc_ids = ["-999"]

        # 执行检索
        ranks = await settings.retriever.retrieval(
            question,
            embd_mdl,
            kb.tenant_id,
            [kb.name],
            page=1,
            page_size=top_k,
            similarity_threshold=similarity_threshold,
            vector_similarity_weight=0.3,
            top=top_k,
            doc_ids=doc_ids or None,
            rank_feature=label_question(db, question, [kb]),
            search_mode=search_mode_dict,
        )
        ranks["chunks"] = settings.retriever.retrieval_by_children(ranks["chunks"], [tenant_id])

        # 知识图谱增强检索
        if use_kg:
            kg_result = await settings.kg_retriever.retrieval(
                question,
                [tenant_id],
                [kb_id],
                embd_mdl,
                LLMBundle(db, kb.tenant_id, LLMType.CHAT),
            )
            if kg_result.get("content_with_weight"):
                ranks["chunks"].insert(0, kg_result)

        # 构建响应
        records: list[RetrievalRecord] = []
        for chunk in ranks.get("chunks", []):
            _, doc = DocumentService.get_by_id(db, chunk["doc_id"])
            chunk.pop("vector", None)  # 移除向量数据

            meta = getattr(doc, "meta_fields", {}) if doc else {}
            meta["doc_id"] = chunk["doc_id"]

            records.append(
                RetrievalRecord(
                    content=chunk.get("content_with_weight", ""),
                    score=chunk.get("similarity", 0.0),
                    title=chunk.get("docnm_kwd", ""),
                    metadata=meta,
                )
            )

        return RetrievalResponse(records=records)

    except Exception as e:
        error_msg = str(e)
        if "not_found" in error_msg.lower():
            return build_error_result(
                error_msg="未找到文档块，请检查文档块状态",
                retcode=RetCode.NOT_FOUND,
            )
        logging.exception("检索过程中发生错误: %s", e)
        return build_error_result(
            error_msg=error_msg,
            retcode=RetCode.SERVER_ERROR,
        )

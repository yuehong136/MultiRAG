# coding=utf-8
"""
@project: multirag
@Author：龙
@file： datav_app.py
@date：2025/1/16
@desc: 向量数据库统一检索接口
       提供跨数据库的统一向量检索能力，支持 Milvus、Elasticsearch、Infinity 等
"""
import logging
import time
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.utils.api_utils import get_data_error_result, get_json_result, server_error_response
from common import settings
from core.llm import EmbeddingModel

logger = logging.getLogger("multirag.datav_app")

router = APIRouter()


# =============================================================================
# 枚举类型定义
# =============================================================================

class SupportedVectorDBType(StrEnum):
    """支持的向量数据库类型"""
    MILVUS = "milvus"
    ELASTICSEARCH = "elasticsearch"
    INFINITY = "infinity"
    OPENSEARCH = "opensearch"
    OCEANBASE = "oceanbase"
    VASTBASE = "vastbase"


class DistanceType(StrEnum):
    """向量距离计算类型 - 统一抽象，内部转换为各数据库语法"""
    COSINE = "COSINE"       # 余弦相似度 (推荐)
    L2 = "L2"               # 欧氏距离
    IP = "IP"               # 内积 (Inner Product)


class SearchMode(StrEnum):
    """检索模式"""
    VECTOR = "vector"       # 纯向量检索
    HYBRID = "hybrid"       # 混合检索 (向量 + 文本)


# =============================================================================
# Pydantic 请求/响应模型
# =============================================================================

class EmbeddingConfig(BaseModel):
    """
    向量模型配置
    
    调用者只需提供 model_name，系统根据当前用户的 tenant_id 从数据库查询完整配置。
    
    Attributes:
        model_name: 模型名称，格式为 "模型名@厂商" 或 "模型名"
                   例如: "embedding-3@ZHIPU-AI", "text-embedding-ada-002@OpenAI"
                   如果不包含 @，则使用系统默认厂商
    
    Examples:
        >>> {"model_name": "embedding-3@ZHIPU-AI"}
        >>> {"model_name": "BAAI/bge-large-zh-v1.5@BAAI"}
    """
    model_config = ConfigDict(extra="allow")
    
    model_name: str


class SearchConfig(BaseModel):
    """
    搜索配置 - 可扩展参数组
    
    Attributes:
        distance_type: 距离计算类型，默认 COSINE
        limit: 返回结果数量限制，默认 10
        offset: 分页偏移量，默认 0
        text_fields: 混合搜索时的文本字段列表
        text_weight: 文本搜索权重 (0-1)
        vector_weight: 向量搜索权重 (0-1)
        similarity_threshold: 相似度阈值，低于此值的结果将被过滤
        ef_search: HNSW 索引的 ef_search 参数
        nprobe: IVF 索引的 nprobe 参数
        rerank: 是否启用重排序
    """
    model_config = ConfigDict(extra="allow")
    
    # 基础配置 (稳定)
    distance_type: DistanceType = DistanceType.COSINE
    limit: int = Field(default=10, ge=1, le=10000)
    offset: int = Field(default=0, ge=0)
    
    # 混合搜索配置 (可选)
    text_fields: list[str] | None = None
    text_weight: float = Field(default=0.5, ge=0, le=1)
    vector_weight: float = Field(default=0.5, ge=0, le=1)
    
    # 高级配置 (可选)
    similarity_threshold: float | None = None
    ef_search: int | None = None
    nprobe: int | None = None
    rerank: bool = False


class FilterCondition(BaseModel):
    """
    统一过滤条件 - 内部转换为各数据库语法
    
    设计目标: 调用方无需关心底层数据库语法差异
    
    Attributes:
        equals: 等值过滤，如 {"kb_id": "xxx"}
        in_list: 列表过滤，如 {"doc_type_kwd": ["pdf", "docx"]}
        not_equals: 不等值过滤
        range_filter: 范围过滤，如 {"score": {"gte": 0.5, "lte": 1.0}}
        exists: 字段存在过滤
        not_exists: 字段不存在过滤
        raw_expr: 原生过滤表达式 (高级用户使用，直接透传)
    """
    model_config = ConfigDict(extra="allow")
    
    # 结构化过滤 (推荐，跨数据库兼容)
    equals: dict[str, str | int | float] | None = None
    in_list: dict[str, list] | None = None
    not_equals: dict[str, str | int | float] | None = None
    range_filter: dict[str, dict] | None = None
    exists: list[str] | None = None
    not_exists: list[str] | None = None
    
    # 原生表达式 (高级用户，数据库特定)
    raw_expr: str | None = None


class VectorSearchRequest(BaseModel):
    """
    向量检索统一请求模型
    
    设计原则:
    1. 核心参数稳定，不轻易变动
    2. 配置对象可扩展，新增不影响现有调用
    3. extra 字段预留未来扩展
    
    Attributes:
        db_type: 向量数据库类型
        collection_name: 集合/索引名称
        query_text: 查询文本
        vector_field: 向量字段名，如 q_768_vec
        output_fields: 要返回的字段列表
        embedding: 向量模型配置 (可选，为空使用系统默认)
        search: 搜索配置 (可选)
        filter: 过滤条件 (可选)
        mode: 检索模式，默认纯向量检索
        extra: 扩展字段，透传给底层
        db_config: 自定义数据库连接配置 (高级)
    """
    model_config = ConfigDict(extra="allow")
    
    # ===== 核心参数 (必填，稳定) =====
    db_type: SupportedVectorDBType
    collection_name: str
    query_text: str
    vector_field: str
    output_fields: list[str]
    
    # ===== 配置对象 (可选，可扩展) =====
    embedding: EmbeddingConfig | None = None
    search: SearchConfig | None = None
    filter: FilterCondition | None = None
    
    # ===== 检索模式 =====
    mode: SearchMode = SearchMode.VECTOR
    
    # ===== 扩展预留 =====
    extra: dict | None = None
    db_config: dict | None = None
    
    @field_validator("collection_name", "query_text", "vector_field")
    @classmethod
    def validate_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("字段不能为空")
        return v.strip()
    
    @field_validator("output_fields")
    @classmethod
    def validate_output_fields(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("output_fields 不能为空")
        return v


class SearchResultItem(BaseModel):
    """单条检索结果"""
    id: str
    score: float
    distance: float | None = None
    fields: dict[str, Any]
    highlight: dict[str, str] | None = None


class SearchMetadata(BaseModel):
    """检索元数据"""
    db_type: str
    embedding_model: str | None = None
    embedding_tokens: int = 0
    search_time_ms: float = 0
    vector_dimension: int | None = None


class VectorSearchResponse(BaseModel):
    """向量检索统一响应"""
    total: int
    results: list[SearchResultItem]
    metadata: SearchMetadata


# =============================================================================
# 工具类: 过滤条件转换器
# =============================================================================

class FilterConverter:
    """将统一过滤条件转换为各数据库语法"""
    
    @staticmethod
    def to_milvus(filter_cond: FilterCondition | None) -> str:
        """转换为 Milvus 过滤表达式"""
        if filter_cond is None:
            return ""
        
        # 如果有原生表达式，直接返回
        if filter_cond.raw_expr:
            return filter_cond.raw_expr
        
        parts = []
        
        # 等值过滤
        if filter_cond.equals:
            for field, value in filter_cond.equals.items():
                if isinstance(value, str):
                    parts.append(f"{field} == '{value}'")
                else:
                    parts.append(f"{field} == {value}")
        
        # 列表过滤
        if filter_cond.in_list:
            for field, values in filter_cond.in_list.items():
                formatted_values = [f"'{v}'" if isinstance(v, str) else str(v) for v in values]
                parts.append(f"{field} in [{','.join(formatted_values)}]")
        
        # 不等值过滤
        if filter_cond.not_equals:
            for field, value in filter_cond.not_equals.items():
                if isinstance(value, str):
                    parts.append(f"{field} != '{value}'")
                else:
                    parts.append(f"{field} != {value}")
        
        # 范围过滤
        if filter_cond.range_filter:
            for field, conditions in filter_cond.range_filter.items():
                for op, value in conditions.items():
                    op_map = {"gte": ">=", "gt": ">", "lte": "<=", "lt": "<"}
                    if op in op_map:
                        parts.append(f"{field} {op_map[op]} {value}")
        
        # 字段存在
        if filter_cond.exists:
            for field in filter_cond.exists:
                parts.append(f'{field} != ""')
        
        # 字段不存在
        if filter_cond.not_exists:
            for field in filter_cond.not_exists:
                parts.append(f'{field} == ""')
        
        return " && ".join(parts) if parts else ""
    
    @staticmethod
    def to_elasticsearch(filter_cond: FilterCondition | None) -> dict | None:
        """转换为 Elasticsearch DSL 格式"""
        if filter_cond is None:
            return None
        
        # 如果有原生表达式，返回 query_string
        if filter_cond.raw_expr:
            return {"query_string": {"query": filter_cond.raw_expr}}
        
        must = []
        must_not = []
        
        # 等值过滤
        if filter_cond.equals:
            for field, value in filter_cond.equals.items():
                must.append({"term": {field: value}})
        
        # 列表过滤
        if filter_cond.in_list:
            for field, values in filter_cond.in_list.items():
                must.append({"terms": {field: values}})
        
        # 不等值过滤
        if filter_cond.not_equals:
            for field, value in filter_cond.not_equals.items():
                must_not.append({"term": {field: value}})
        
        # 范围过滤
        if filter_cond.range_filter:
            for field, conditions in filter_cond.range_filter.items():
                must.append({"range": {field: conditions}})
        
        # 字段存在
        if filter_cond.exists:
            for field in filter_cond.exists:
                must.append({"exists": {"field": field}})
        
        # 字段不存在
        if filter_cond.not_exists:
            for field in filter_cond.not_exists:
                must_not.append({"exists": {"field": field}})
        
        if not must and not must_not:
            return None
        
        bool_query = {}
        if must:
            bool_query["must"] = must
        if must_not:
            bool_query["must_not"] = must_not
        
        return {"bool": bool_query}
    
    @staticmethod
    def to_infinity(filter_cond: FilterCondition | None) -> str:
        """转换为 Infinity 过滤表达式"""
        # Infinity 的过滤语法与 Milvus 类似
        return FilterConverter.to_milvus(filter_cond)


# =============================================================================
# 工具类: 结果归一化器
# =============================================================================

class ResultNormalizer:
    """统一各数据库返回结果格式"""
    
    @staticmethod
    def normalize_milvus(
        hits: list,
        output_fields: list[str],
        distance_type: DistanceType
    ) -> list[SearchResultItem]:
        """归一化 Milvus 检索结果"""
        results = []
        for hit in hits:
            # Milvus 返回格式可能是 dict 或带有 entity 属性的对象
            if isinstance(hit, dict):
                doc_id = hit.get("id") or hit.get("pk", "")
                distance = hit.get("distance", 0)
                fields = {k: hit.get(k) for k in output_fields if k in hit}
            else:
                doc_id = getattr(hit, "id", "") or getattr(hit, "pk", "")
                distance = getattr(hit, "distance", 0)
                entity = getattr(hit, "entity", {})
                if isinstance(entity, dict):
                    fields = {k: entity.get(k) for k in output_fields if k in entity}
                else:
                    fields = {k: getattr(entity, k, None) for k in output_fields if hasattr(entity, k)}
            
            # 归一化分数: COSINE/IP 越大越好，L2 越小越好
            if distance_type == DistanceType.L2:
                # L2 距离转换为相似度分数 (0-1)
                score = 1.0 / (1.0 + distance) if distance >= 0 else 0
            else:
                # COSINE/IP: distance 本身就是相似度
                score = float(distance) if distance else 0
            
            results.append(SearchResultItem(
                id=str(doc_id),
                score=score,
                distance=float(distance) if distance else None,
                fields=fields,
                highlight=None
            ))
        
        return results
    
    @staticmethod
    def normalize_elasticsearch(
        response: dict,
        output_fields: list[str]
    ) -> tuple[list[SearchResultItem], int]:
        """归一化 Elasticsearch 检索结果"""
        results = []
        hits = response.get("hits", {})
        total = hits.get("total", {})
        total_count = total.get("value", 0) if isinstance(total, dict) else total
        
        for hit in hits.get("hits", []):
            doc_id = hit.get("_id", "")
            score = hit.get("_score", 0) or 0
            source = hit.get("_source", {})
            highlight = hit.get("highlight", {})
            
            # 提取指定字段
            fields = {k: source.get(k) for k in output_fields if k in source}
            
            # 处理高亮
            highlight_dict = None
            if highlight:
                highlight_dict = {k: "".join(v) if isinstance(v, list) else v 
                                 for k, v in highlight.items()}
            
            results.append(SearchResultItem(
                id=str(doc_id),
                score=float(score),
                distance=None,
                fields=fields,
                highlight=highlight_dict
            ))
        
        return results, total_count
    
    @staticmethod
    def normalize_infinity(
        df,  # pandas DataFrame
        output_fields: list[str]
    ) -> list[SearchResultItem]:
        """归一化 Infinity 检索结果"""
        results = []
        
        if df is None or len(df) == 0:
            return results
        
        for _, row in df.iterrows():
            doc_id = row.get("id", "")
            # Infinity 可能返回 SCORE 或 SIMILARITY
            score = row.get("SCORE", 0) or row.get("SIMILARITY", 0) or 0
            
            fields = {k: row.get(k) for k in output_fields if k in row.index}
            
            results.append(SearchResultItem(
                id=str(doc_id),
                score=float(score),
                distance=None,
                fields=fields,
                highlight=None
            ))
        
        return results


# =============================================================================
# 工具类: 向量数据库连接工厂
# =============================================================================

class VectorDBFactory:
    """向量数据库连接工厂"""
    
    @staticmethod
    def get_connection(db_type: SupportedVectorDBType):
        """
        获取向量数据库连接
        
        注意: 使用项目现有的单例连接，避免重复创建
        """
        import core.utils.milvus_conn
        import core.utils.es_conn
        import core.utils.infinity_conn
        import core.utils.opensearch_conn
        import core.utils.ob_conn
        import core.utils.vastbase_conn
        
        if db_type == SupportedVectorDBType.MILVUS:
            return core.utils.milvus_conn.MilvusConnection()
        elif db_type == SupportedVectorDBType.ELASTICSEARCH:
            return core.utils.es_conn.ESConnection()
        elif db_type == SupportedVectorDBType.INFINITY:
            return core.utils.infinity_conn.InfinityConnection()
        elif db_type == SupportedVectorDBType.OPENSEARCH:
            return core.utils.opensearch_conn.OSConnection()
        elif db_type == SupportedVectorDBType.OCEANBASE:
            return core.utils.ob_conn.OBConnection()
        elif db_type == SupportedVectorDBType.VASTBASE:
            return core.utils.vastbase_conn.VastBaseConnection()
        else:
            raise ValueError(f"不支持的向量数据库类型: {db_type}")


# =============================================================================
# 核心检索逻辑
# =============================================================================

def get_embedding_model(
    db: Session,
    tenant_id: str,
    config: EmbeddingConfig | None
):
    """
    获取向量模型实例
    
    Args:
        db: 数据库会话
        tenant_id: 租户 ID (当前用户 ID)
        config: 向量模型配置，为空则使用系统默认配置
    
    Returns:
        向量模型实例
    """
    from api.db.services.tenant_llm_service import TenantLLMService
    from common.constants import LLMType
    
    if config is None:
        # 使用系统默认配置，llm_name=None 会使用 tenant.embd_id
        model_name = None
    else:
        model_name = config.model_name
    
    try:
        # 使用项目现有的 model_instance 方法获取模型实例
        embedding_model = TenantLLMService.model_instance(
            db=db,
            tenant_id=tenant_id,
            llm_type=LLMType.EMBEDDING.value,
            llm_name=model_name
        )
        
        if embedding_model is None:
            raise ValueError(f"无法创建向量模型实例: {model_name or '默认模型'}")
        
        return embedding_model
    
    except LookupError as e:
        raise ValueError(f"模型配置未授权: {model_name or '默认模型'}. {str(e)}")
    except Exception as e:
        logger.exception(f"获取向量模型失败: {str(e)}")
        raise ValueError(f"获取向量模型失败: {str(e)}")


def execute_vector_search(
    conn,
    db_type: SupportedVectorDBType,
    collection_name: str,
    query_vector: list[float],
    vector_field: str,
    output_fields: list[str],
    filter_expr: str,
    search_config: SearchConfig
) -> tuple[list, int]:
    """
    执行向量检索
    
    Returns:
        (检索结果列表, 总数)
    """
    limit = search_config.limit
    offset = search_config.offset
    distance_type = search_config.distance_type
    
    if db_type == SupportedVectorDBType.MILVUS:
        # Milvus 检索
        search_params = {
            "metric_type": distance_type.value,
            "params": {}
        }
        if search_config.nprobe:
            search_params["params"]["nprobe"] = search_config.nprobe
        if search_config.ef_search:
            search_params["params"]["ef"] = search_config.ef_search
        
        results = conn.search_by_milvus(
            collection_name=collection_name,
            data=[query_vector],
            filter=filter_expr,
            limit=limit,
            output_fields=output_fields,
            search_params=search_params,
            anns_field=vector_field
        )
        
        return results, len(results)
    
    elif db_type == SupportedVectorDBType.ELASTICSEARCH:
        # ES 检索 - 使用 KNN
        from common.doc_store.doc_store_base import MatchDenseExpr, OrderByExpr
        
        dense_expr = MatchDenseExpr(
            vector_column_name=vector_field,
            embedding_data=query_vector,
            embedding_data_type="float",
            distance_type=distance_type.value.lower(),
            topn=limit,
            extra_options={"similarity": search_config.similarity_threshold or 0}
        )
        
        # 构建条件
        condition = {}
        if filter_expr:
            # ES 需要解析过滤条件，这里简化处理
            condition["raw_filter"] = filter_expr
        
        results = conn.search(
            select_fields=output_fields,
            highlight_fields=[],
            condition=condition,
            match_expressions=[dense_expr],
            order_by=OrderByExpr(),
            offset=offset,
            limit=limit,
            index_names=collection_name,
            knowledgebase_ids=[]
        )
        
        return results, 0  # ES 结果需要特殊处理
    
    elif db_type == SupportedVectorDBType.INFINITY:
        # Infinity 检索
        from common.doc_store.doc_store_base import MatchDenseExpr, OrderByExpr
        
        dense_expr = MatchDenseExpr(
            vector_column_name=vector_field,
            embedding_data=query_vector,
            embedding_data_type="float",
            distance_type=distance_type.value.lower(),
            topn=limit,
            extra_options={}
        )
        
        results, total = conn.search(
            select_fields=output_fields,
            highlight_fields=[],
            condition={},
            match_expressions=[dense_expr],
            order_by=OrderByExpr(),
            offset=offset,
            limit=limit,
            index_names=collection_name,
            knowledgebase_ids=[]
        )
        
        return results, total
    
    else:
        # 其他数据库使用通用接口
        from common.doc_store.doc_store_base import MatchDenseExpr, OrderByExpr
        
        dense_expr = MatchDenseExpr(
            vector_column_name=vector_field,
            embedding_data=query_vector,
            embedding_data_type="float",
            distance_type=distance_type.value,
            topn=limit,
            extra_options={}
        )
        
        results, total = conn.search(
            select_fields=output_fields,
            highlight_fields=[],
            condition={},
            match_expressions=[dense_expr],
            order_by=OrderByExpr(),
            offset=offset,
            limit=limit,
            index_names=collection_name,
            knowledgebase_ids=[]
        )
        
        return results, total


# =============================================================================
# FastAPI 路由
# =============================================================================

@router.post("/search", summary="向量检索", response_description="检索结果")
def vector_search(
    request: VectorSearchRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### POST `/search` 向量数据库统一检索接口
    
    **功能描述**:
    提供跨向量数据库的统一检索能力，支持 Milvus、Elasticsearch、Infinity 等多种向量数据库。
    调用方可指定向量模型、过滤条件、返回字段等参数进行灵活检索。
    
    ---
    ### 请求体 (Request Body)
    
    #### 核心参数 (必填)
    | 字段 | 类型 | 描述 |
    |------|------|------|
    | `db_type` | `string` | 向量数据库类型: milvus, elasticsearch, infinity, opensearch, oceanbase, vastbase |
    | `collection_name` | `string` | 集合/索引名称 |
    | `query_text` | `string` | 查询文本 |
    | `vector_field` | `string` | 向量字段名，如 q_768_vec |
    | `output_fields` | `list[string]` | 要返回的字段列表 |
    
    #### 配置参数 (可选)
    | 字段 | 类型 | 描述 |
    |------|------|------|
    | `embedding` | `object` | 向量模型配置，为空使用系统默认 |
    | `search` | `object` | 搜索配置 (limit, offset, distance_type 等) |
    | `filter` | `object` | 过滤条件 |
    | `mode` | `string` | 检索模式: vector (默认), hybrid |
    | `extra` | `object` | 扩展字段，透传给底层 |
    
    ---
    ### 请求示例
    
    #### 最简调用
    ```json
    {
        "db_type": "milvus",
        "collection_name": "my_collection",
        "query_text": "什么是人工智能",
        "vector_field": "q_768_vec",
        "output_fields": ["content", "title", "kb_id"]
    }
    ```
    
    #### 完整调用
    ```json
    {
        "db_type": "milvus",
        "collection_name": "my_collection",
        "query_text": "什么是人工智能",
        "vector_field": "q_2048_vec",
        "output_fields": ["content", "title", "kb_id"],
        "embedding": {
            "model_name": "embedding-3@ZHIPU-AI"
        },
        "search": {
            "distance_type": "COSINE",
            "limit": 20,
            "similarity_threshold": 0.6
        },
        "filter": {
            "equals": {"kb_id": "xxx"},
            "in_list": {"doc_type": ["pdf", "docx"]}
        }
    }
    ```
    
    ---
    ### 响应
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "total": 100,
            "results": [
                {
                    "id": "chunk_001",
                    "score": 0.95,
                    "distance": 0.05,
                    "fields": {"content": "...", "title": "..."},
                    "highlight": null
                }
            ],
            "metadata": {
                "db_type": "milvus",
                "embedding_model": "embedding-3@ZHIPU-AI",
                "embedding_tokens": 8,
                "search_time_ms": 45.2,
                "vector_dimension": 2048
            }
        }
    }
    ```
    """
    start_time = time.time()
    
    try:
        # 1. 获取搜索配置
        search_config = request.search or SearchConfig()
        
        # 2. 获取向量模型并生成查询向量
        embedding_model = get_embedding_model(
            db=db,
            tenant_id=user.id,
            config=request.embedding
        )
        query_vector, token_count = embedding_model.encode_queries(request.query_text)
        
        # 转换为列表格式
        if hasattr(query_vector, "tolist"):
            query_vector = query_vector.tolist()
        
        vector_dimension = len(query_vector) if query_vector else None
        
        # 3. 获取数据库连接
        conn = VectorDBFactory.get_connection(request.db_type)
        
        # 4. 转换过滤条件
        filter_expr = FilterConverter.to_milvus(request.filter)
        
        # 5. 执行检索
        raw_results, total = execute_vector_search(
            conn=conn,
            db_type=request.db_type,
            collection_name=request.collection_name,
            query_vector=query_vector,
            vector_field=request.vector_field,
            output_fields=request.output_fields,
            filter_expr=filter_expr,
            search_config=search_config
        )
        
        # 6. 归一化结果
        if request.db_type == SupportedVectorDBType.MILVUS:
            results = ResultNormalizer.normalize_milvus(
                raw_results, request.output_fields, search_config.distance_type
            )
            if total == 0:
                total = len(results)
        elif request.db_type == SupportedVectorDBType.ELASTICSEARCH:
            results, total = ResultNormalizer.normalize_elasticsearch(
                raw_results, request.output_fields
            )
        elif request.db_type == SupportedVectorDBType.INFINITY:
            results = ResultNormalizer.normalize_infinity(
                raw_results, request.output_fields
            )
            if total == 0:
                total = len(results)
        else:
            # 通用处理
            results = ResultNormalizer.normalize_milvus(
                raw_results, request.output_fields, search_config.distance_type
            )
            if total == 0:
                total = len(results)
        
        # 7. 构建响应
        elapsed_ms = (time.time() - start_time) * 1000
        
        # 构建模型名称
        if request.embedding:
            embedding_model_name = request.embedding.model_name
        else:
            embedding_model_name = settings.EMBEDDING_MDL
        
        response = VectorSearchResponse(
            total=total,
            results=results,
            metadata=SearchMetadata(
                db_type=request.db_type.value,
                embedding_model=embedding_model_name,
                embedding_tokens=token_count,
                search_time_ms=round(elapsed_ms, 2),
                vector_dimension=vector_dimension
            )
        )
        
        return get_json_result(data=response.model_dump())
    
    except ValueError as e:
        logger.warning(f"向量检索参数错误: {str(e)}")
        return get_data_error_result(retmsg=str(e))
    except Exception as e:
        logger.exception(f"向量检索失败: {str(e)}")
        return server_error_response(e)


@router.get("/health", summary="健康检查", response_description="健康状态")
def health_check():
    """
    ### GET `/health` 健康检查接口
    
    检查向量检索服务的健康状态。
    
    ---
    ### 响应
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "status": "healthy",
            "supported_db_types": ["milvus", "elasticsearch", "infinity", ...],
            "default_embedding_model": "embedding-3@ZHIPU-AI"
        }
    }
    ```
    """
    try:
        data = {
            "status": "healthy",
            "supported_db_types": [t.value for t in SupportedVectorDBType],
            "default_embedding_model": settings.EMBEDDING_MDL or "未配置"
        }
        return get_json_result(data=data)
    except Exception as e:
        return server_error_response(e)


@router.get("/db_types", summary="获取支持的数据库类型", response_description="数据库类型列表")
def get_supported_db_types():
    """
    ### GET `/db_types` 获取支持的向量数据库类型
    
    ---
    ### 响应
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "db_types": [
                {"value": "milvus", "description": "Milvus 向量数据库"},
                {"value": "elasticsearch", "description": "Elasticsearch"},
                ...
            ]
        }
    }
    ```
    """
    descriptions = {
        SupportedVectorDBType.MILVUS: "Milvus 向量数据库 (推荐)",
        SupportedVectorDBType.ELASTICSEARCH: "Elasticsearch 搜索引擎",
        SupportedVectorDBType.INFINITY: "Infinity 向量数据库",
        SupportedVectorDBType.OPENSEARCH: "OpenSearch 搜索引擎",
        SupportedVectorDBType.OCEANBASE: "OceanBase 分布式数据库",
        SupportedVectorDBType.VASTBASE: "VastBase 国产数据库",
    }
    
    data = {
        "db_types": [
            {"value": t.value, "description": descriptions.get(t, t.value)}
            for t in SupportedVectorDBType
        ]
    }
    return get_json_result(data=data)

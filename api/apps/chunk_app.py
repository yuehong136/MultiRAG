import datetime
import json
from typing import Literal, Annotated, Any
import xxhash
import re
import base64

from fastapi import APIRouter, Depends
import numpy as np
from pydantic import BaseModel, Field, Discriminator, model_validator
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.db.services.search_service import SearchService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.db.services.user_service import UserTenantService
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name, get_tenant_default_model_by_type
from api.db.services.document_service import DocumentService
from api.db.services.doc_metadata_service import DocMetadataService
from api.utils.api_utils import server_error_response, get_data_error_result
from api.utils.api_utils import get_json_result
from api.utils.image_utils import store_chunk_image
from core.app.qa import rmPrefix, beAdoc
from core.app.tag import label_question
from core.nlp import search, rag_tokenizer
from common import settings
from core.prompts.generator import keyword_extraction, cross_languages
from common.doc_store.doc_store_base import OrderByExpr
from common.metadata_utils import apply_meta_data_filter
from common.constants import RetCode, LLMType, ParserType, PAGERANK_FLD
from common.misc_utils import thread_pool_exec
from common.string_utils import remove_redundant_spaces
from common.tag_feature_utils import validate_tag_features

router = APIRouter()


class ListChunkRequest(BaseModel):
    doc_id: str
    page: int | None = 1
    size: int | None = 30
    keywords: str | None = ""
    available_int: int | None = None


class SetChunkRequest(BaseModel):
    doc_id: str
    chunk_id: str
    content_with_weight: str
    important_kwd: list[str] | None = []
    question_kwd: list[str] | None = []
    available_int: int | None = None
    tag_kwd: Any | None = None
    tag_feas: Any | None = None
    image_base64: str | None = None
    img_id: str | None = None


class SwitchChunkRequest(BaseModel):
    doc_id: str
    chunk_ids: list[str]
    available_int: int


class RmChunkRequest(BaseModel):
    doc_id: str
    chunk_ids: list[str] | None = None
    delete_all: bool = False


class CreateChunkRequest(BaseModel):
    doc_id: str
    content_with_weight: str
    question_kwd: list[str] | None = None
    important_kwd: list[str] | None = None
    tag_kwd: list[str] | None = None
    tag_feas: Any | None = None
    image_base64: str | None = None


class VectorStoreQueryRequest(BaseModel):
    doc_id: str | None = None
    kb_id: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    fields: list[str] = Field(default_factory=list)
    question: str | None = None
    similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    include_score: bool = False
    page: int = Field(default=1, ge=1)
    size: int = Field(default=100, ge=1, le=1000)


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


class RetrievalTestRequest(BaseModel):
    kb_ids: list[str]
    question: str
    page: int = 1
    size: int = 30
    doc_ids: list[str] | None = None
    similarity_threshold: float = 0.0
    vector_similarity_weight: float = 0.3
    use_kg: bool = False
    top_k: int = 1024
    rerank_id: str | None = None
    tenant_rerank_id: int | None = None
    highlight: bool = False
    keyword: bool = False
    search_mode: SearchModeType | None = None
    cross_languages: list[str] | None = None
    search_id: str | None = None
    meta_data_filter: dict | None = None

    def get_search_mode_dict(self) -> dict[str, Any] | None:
        """将搜索模式转换为字典格式供底层函数使用"""
        if self.search_mode is None:
            return None

        mode_data = self.search_mode.model_dump()
        mode_type = mode_data.pop('type')
        return {mode_type: mode_data}


@router.post('/list', summary="列出文档块")
async def list_chunk(request: ListChunkRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/list` 列出文档块接口

**功能描述**:
此接口用于根据文档 ID 列出文档块，支持分页查询、关键词搜索和高亮显示内容，返回匹配的文档块信息。

---

### 请求体 (Request Body)

| 字段          | 类型          | 必填 | 描述                                                                                 |
|---------------|---------------|------|--------------------------------------------------------------------------------------|
| `doc_id`      | `string`      | 是   | 文档的唯一标识符。                                                                   |
| `page`        | `int`         | 是   | 当前页码，用于分页查询。                                                             |
| `size`        | `int`         | 是   | 每页返回的文档块数量。                                                               |
| `keywords`    | `string`      | 否   | 搜索关键词，用于高亮匹配文档块的内容。                                               |

---

### 响应 (Response)

#### 成功响应 (200)

- **`Content-Type: application/json`**
- **示例**:
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "total": 2,
            "chunks": [
                {
                    "chunk_id": "chunk_001",
                    "content_with_weight": "问题：人工智能是什么？ 答案：人工智能是计算机科学的一个分支。",
                    "doc_id": "67890",
                    "docnm_kwd": ["人工智能"],
                    "important_kwd": ["计算机科学"],
                    "img_id": "img_123",
                    "available_int": 1,
                    "positions": [
                        [0.1, 0.2, 0.3, 0.4, 0.5]
                    ]
                },
                {
                    "chunk_id": "chunk_002",
                    "content_with_weight": "人工智能涉及机器学习和深度学习。",
                    "doc_id": "67890",
                    "docnm_kwd": ["机器学习"],
                    "important_kwd": ["深度学习"],
                    "img_id": "img_124",
                    "available_int": 1,
                    "positions": []
                }
            ],
            "doc": {
                "doc_id": "67890",
                "name": "人工智能概述",
                "kb_id": "kb_001"
            }
        }
    }
    ```

#### 错误响应

- **404: Tenant not found**
    - **描述**: 当根据 `doc_id` 查询租户信息失败时，返回此错误。
    - **示例**:
        ```json
        {
            "detail": "Tenant not found!"
        }
        ```

- **404: Document not found**
    - **描述**: 当根据 `doc_id` 查询文档信息失败时，返回此错误。
    - **示例**:
        ```json
        {
            "detail": "Document not found!"
        }
        ```

- **404: No chunk found**
    - **描述**: 当没有找到匹配的文档块时，返回此错误。
    - **示例**:
        ```json
        {
            "retcode": 404,
            "retmsg": "No chunk found!",
            "data": false
        }
        ```

- **500: 内部错误**
    - **描述**: 当发生意外错误时，返回此错误。
    - **示例**:
        ```json
        {
            "retcode": 500,
            "retmsg": "Internal server error",
            "detail": "具体错误信息"
        }
        ```

---

### 主要流程

1. 从请求体提取 `doc_id`、`page`、`size` 和 `keywords`。
2. 验证文档块所属的租户 (`tenant_id`) 和文档是否存在。
3. 根据分页参数和关键词搜索查询文档块数据。
4. 处理搜索结果：
    - 如果 `keywords` 存在，则高亮显示匹配的内容。
    - 将位置信息按每 5 个数值分组，解析为数组结构。
5. 返回文档块列表和文档基本信息。

---

### 注意事项

- **关键词搜索**:
    - 如果传入 `keywords`，将匹配的内容高亮显示。
- **位置信息解析**:
    - 位置信息字段 `positions` 的值按 5 个一组解析为数组结构，用于表示块的坐标或其他标记。
- **分页查询**:
    - `page` 和 `size` 字段控制分页查询，每次返回指定页码的文档块集合。
- **高亮内容**:
    - 若存在匹配的关键词，高亮显示结果会替换原始 `content_with_weight`。

---

### 示例请求

#### 请求体:
```json
{
    "doc_id": "67890",
    "page": 1,
    "size": 10,
    "keywords": "人工智能"
}
```

- **成功响应**:
```json
{
    "retcode": 0,
    "retmsg": "success",
    "data": {
        "total": 2,
        "chunks": [
            {
                "chunk_id": "chunk_001",
                "content_with_weight": "问题：人工智能是什么？ 答案：人工智能是计算机科学的一个分支。",
                "doc_id": "67890",
                "docnm_kwd": ["人工智能"],
                "important_kwd": ["计算机科学"],
                "img_id": "img_123",
                "available_int": 1,
                "positions": [
                    [0.1, 0.2, 0.3, 0.4, 0.5]
                ]
            }
        ],
        "doc": {
            "doc_id": "67890",
            "name": "人工智能概述",
            "kb_id": "kb_001"
        }
    }
}
```

- **错误响应 (无匹配文档块)**:
```json
{
    "retcode": 404,
    "retmsg": "No chunk found!",
    "data": false
}
```
    """
    try:
        tenant_id = DocumentService.get_tenant_id(db, request.doc_id)
        if not tenant_id:
            return get_data_error_result(retmsg="Tenant not found!")
        doc = DocumentService.get_by_id(db, request.doc_id)
        if not doc:
            return get_data_error_result(retmsg="Document not found!")
        kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
        kb_ids = KnowledgebaseService.get_kb_ids(db, tenant_id)
        query = {
            "doc_ids": [request.doc_id], "page": request.page, "size": request.size, "question": request.keywords,
            "sort": True
        }
        if request.keywords:
            query["filter_exp"] = f"content_with_weight like '%{request.keywords}%'" if request.keywords else None

        # 先计算出所有问题块的总数
        # query_count = {
        #     "doc_ids": [request.doc_id], "question": request.keywords,
        #     "sort": True
        # }
        if request.available_int:
            query["available_int"] = request.available_int
            # query_count["available_int"] = request["available_int"]
        # total = settings.retriever.count(query_count, search.index_name_one(tenant_id, kb.name)).total
        # sres = settings.retriever.search(query, search.index_name_one(tenant_id, kb.name))
        # total = settings.retriever.search(query_count, search.index_name_one(tenant_id, kb.name), kb_ids).total
        sres = await settings.retriever.search(query, search.index_name_one(tenant_id, kb.name), kb_ids, highlight=["content_ltks"])
        res = {"total": sres.total, "chunks": [], "doc": DocumentService.serialize_document(db, doc)}
        for id in sres.ids:
            d = {
                "chunk_id": id,
                "content_with_weight": remove_redundant_spaces(sres.highlight[id]) if request.keywords and id in sres.highlight else
                sres.field[id].get(
                    "content_with_weight", ""),
                "doc_id": sres.field[id]["doc_id"],
                "docnm_kwd": sres.field[id]["docnm_kwd"],
                "important_kwd": sres.field[id].get("important_kwd", []),
                "question_kwd": sres.field[id].get("question_kwd", []),
                "img_id": sres.field[id].get("img_id", ""),
                "available_int": int(sres.field[id].get("available_int", 1)),
                "positions": sres.field[id].get("position_int", []),
                "doc_type_kwd": sres.field[id].get("doc_type_kwd")
            }
            # if len(d["positions"]) % 5 == 0:
            #     poss = []
            #     for i in range(0, len(d["positions"]), 5):
            #         poss.append([float(d["positions"][i]), float(d["positions"][i + 1]), float(d["positions"][i + 2]),
            #                      float(d["positions"][i + 3]), float(d["positions"][i + 4])])
            #     d["positions"] = poss

            # assert isinstance(d["positions"], list)
            # assert len(d["positions"]) == 0 or (isinstance(d["positions"][0], list) and len(d["positions"][0]) == 5)
            if isinstance(d["positions"], str):
                d["positions"] = json.loads(d["positions"])
            assert isinstance(d["positions"], list)
            assert len(d["positions"]) == 0 or (isinstance(d["positions"][0], list) and len(d["positions"][0]) == 5)
            res["chunks"].append(d)
        return get_json_result(data=res)
    except Exception as e:
        if str(e).find("not_found") > 0:
            return get_json_result(data=False, retmsg=f'No chunk found!',
                                   retcode=RetCode.DATA_ERROR)
        return server_error_response(e)


@router.get('/get', summary="获取文档块")
def get(chunk_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### GET `/get` 获取文档块详细信息接口

    **功能描述**:
    此接口用于根据文档块ID获取文档块的详细信息，包括内容、关键词、位置信息等，同时会过滤掉向量和分词等技术字段。

    ---

    ### 请求参数 (Query Parameters)

    | 参数名     | 类型     | 必填 | 描述                    |
    |------------|----------|------|-------------------------|
    | `chunk_id` | `string` | 是   | 文档块的唯一标识符      |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    - **`Content-Type: application/json`**
    - **示例**:
        ```json
        {
            "retcode": 0,
            "retmsg": "success",
            "data": {
                "chunk_id": "chunk_123",
                "content_with_weight": "文档块的内容",
                "doc_id": "doc_456",
                "docnm_kwd": "文档名称",
                "important_kwd": ["关键词1", "关键词2"],
                "question_kwd": ["问题关键词"],
                "img_id": "img_789",
                "available_int": 1,
                "positions": [[0.1, 0.2, 0.3, 0.4, 0.5]],
                "create_time": "2024-01-01 12:00:00",
                "kb_id": "kb_001"
            }
        }
        ```

    #### 错误响应
    - **404: Tenant not found**
        - 当用户没有关联的租户时返回此错误
    - **404: Chunk not found**
        - 当指定的文档块不存在时返回此错误

    ---

    ### 主要流程
    1. 获取用户关联的所有租户信息
    2. 遍历租户下的所有知识库，查找指定的文档块
    3. 过滤技术字段（向量、分词等）
    4. 转换NumPy数据类型为Python原生类型
    5. 返回清理后的文档块信息

    ---

    ### 注意事项
    - **字段过滤**: 自动过滤以下技术字段：
      - `_vec$`: 向量字段
      - `_sm_`: 细粒度分词字段
      - `_tks`: 分词字段
      - `_ltks`: 长分词字段
      - `vector`: 标准向量字段
    - **数据类型转换**: 自动将NumPy数据类型转换为JSON兼容的Python原生类型
    - **权限控制**: 只能获取用户有权限访问的租户下的文档块

    ---

    ### 使用示例

    #### 请求:
    ```
    GET /get?chunk_id=chunk_123456
    ```

    #### 成功响应:
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "chunk_id": "chunk_123456",
            "content_with_weight": "这是一个关于机器学习的文档块内容",
            "doc_id": "doc_789",
            "docnm_kwd": "机器学习入门.pdf",
            "important_kwd": ["机器学习", "算法"],
            "available_int": 1
        }
    }
    ```

    #### 错误响应:
    ```json
    {
        "retcode": 404,
        "retmsg": "Chunk not found!",
        "data": false
    }
    ```
    """
    try:
        chunk = None
        tenants = UserTenantService.query(db, user_id=user.id)
        if not tenants:
            return get_data_error_result(retmsg="Tenant not found!")
        for tenant in tenants:
            kb_ids = KnowledgebaseService.get_kb_ids(db, tenant.tenant_id)
            for kb_id in kb_ids:
                kb = KnowledgebaseService.get_by_id(db, kb_id)
                chunk = settings.docStoreConn.get(chunk_id, search.index_name_one(tenant.tenant_id, kb.name), kb_ids)
                if chunk:
                    break
        if chunk is None:
            return server_error_response(Exception("Chunk not found"))

        k = []
        for n in chunk.keys():
            if re.search(r"(_vec$|_sm_|_tks|_ltks|vector)", n):
                k.append(n)
        for n in k:
            del chunk[n]

        def convert_numpy_types(obj):
            """递归转换对象中的所有 NumPy 类型为 Python 原生类型"""
            if isinstance(obj, np.integer):
                return int(obj)
            elif isinstance(obj, np.floating):  # 这会处理所有的浮点类型，包括 float32
                return float(obj)
            elif isinstance(obj, np.ndarray):
                return obj.tolist()
            elif isinstance(obj, dict):
                return {key: convert_numpy_types(value) for key, value in obj.items()}
            elif isinstance(obj, list) or isinstance(obj, tuple):
                return [convert_numpy_types(item) for item in obj]
            else:
                return obj

        converted_chunk = convert_numpy_types(chunk)

        return get_json_result(data=converted_chunk)
    except Exception as e:
        if str(e).find("NotFoundError") >= 0:
            return get_json_result(data=False, retmsg='Chunk not found!',
                                   retcode=RetCode.DATA_ERROR)
        return server_error_response(e)


@router.post('/vector_store/query', summary="向量存储字段查询（可选语义检索）")
async def query_vector_store(request: VectorStoreQueryRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    根据 `doc_id` 或自定义过滤条件从向量存储中查询指定字段。

    - 通过 `doc_id` 会自动定位所属租户与知识库。
    - `filters` 允许追加更多过滤条件（与 `doc_id` 取交集）。
    - `fields` 为必填，决定返回哪些字段，如 `["img_id"]`。
    - `question` 可选，提供自然语言问题时将进行向量语义检索。
    - `include_score` 控制是否返回语义相似度分值（若可用）。
    - 支持分页：`page`、`size`。

    返回示例：
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "total": 2,
            "rows": [
                {"chunk_id": "xxx", "img_id": "img_1"},
                {"chunk_id": "yyy", "img_id": "img_2"}
            ]
        }
    }
    ```
    """
    if not request.doc_id and not request.kb_id:
        return get_data_error_result(retmsg="`doc_id` or `kb_id` is required!")
    if not request.fields:
        return get_data_error_result(retmsg="`fields` cannot be empty!")

    try:
        kb = None
        tenant_id = None
        condition = dict(request.filters or {})

        if request.doc_id:
            doc = DocumentService.get_by_id(db, request.doc_id)
            if not doc:
                return get_data_error_result(retmsg="Document not found!")
            tenant_id = DocumentService.get_tenant_id(db, request.doc_id)
            if not tenant_id:
                return get_data_error_result(retmsg="Tenant not found!")
            kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
            if not kb:
                return get_data_error_result(retmsg="Knowledgebase not found!")
            condition["doc_id"] = request.doc_id
        else:
            kb = KnowledgebaseService.get_by_id(db, request.kb_id)
            if not kb:
                return get_data_error_result(retmsg="Knowledgebase not found!")
            tenant_id = kb.tenant_id
            if not tenant_id:
                return get_data_error_result(retmsg="Tenant not found!")

        index_name = search.index_name_one(tenant_id, kb.name)
        offset = (request.page - 1) * request.size

        match_exprs = []
        if request.question:
            if kb.tenant_embd_id:
                embd_config = get_model_config_by_id(db, kb.tenant_embd_id)
            else:
                embd_config = get_model_config_by_type_and_name(db, tenant_id, LLMType.EMBEDDING.value, kb.embd_id)
            embd_mdl = LLMBundle(db, tenant_id, embd_config)
            dealer = search.Dealer(settings.docStoreConn)
            # 使用固定的 topk 以确保 total 保持一致
            match_exprs.append(
                await dealer.get_vector(
                    request.question,
                    embd_mdl,
                    topk=1024,
                    similarity=request.similarity if request.similarity is not None else 0.1
                )
            )

        search_res = await thread_pool_exec(
            settings.docStoreConn.search,
            request.fields,
            [],
            condition,
            match_exprs,
            OrderByExpr(),
            offset,
            request.size,
            index_name,
            [kb.id],
        )

        total = settings.docStoreConn.get_total(search_res)
        field_data = settings.docStoreConn.get_fields(search_res, request.fields)
        distances = field_data.pop("distance", [])

        rows = []
        for idx, (pk, values) in enumerate(field_data.items()):
            row = {"chunk_id": pk}
            row.update(values)
            if request.include_score and idx < len(distances):
                row["score"] = distances[idx]
            rows.append(row)

        return get_json_result(data={"total": total, "rows": rows})
    except Exception as e:
        return server_error_response(e)


@router.post('/set', summary="设置文档块")
def set(request: SetChunkRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/set` 更新文档块

    更新指定文档块的内容、关键词、向量及关联图片，变更立即写入向量存储。

    ---

    ### 请求体

    | 字段                  | 类型         | 必填 | 描述                                                                          |
    |-----------------------|--------------|------|-------------------------------------------------------------------------------|
    | `chunk_id`            | `string`     | 是   | 文档块唯一标识符。                                                            |
    | `doc_id`              | `string`     | 是   | 所属文档唯一标识符。                                                          |
    | `content_with_weight` | `string`     | 是   | 文档块正文，用于分词与向量计算。QA 模式下需包含问题和答案，以 TAB 或换行分隔。|
    | `important_kwd`       | `list[str]`  | 否   | 重要关键词列表，参与额外分词与向量加权。                                      |
    | `question_kwd`        | `list[str]`  | 否   | 问题关键词列表；存在时以其内容替代正文参与向量计算。                          |
    | `available_int`       | `int`        | 否   | 可用性标记（如 `1` 启用、`0` 禁用）。                                        |
    | `tag_kwd`             | `string`     | 否   | 标签关键词，用于分类或过滤。                                                  |
    | `tag_feas`            | `object`     | 否   | 标签特征值，与 `tag_kwd` 配合使用，格式为 `{tag: score}`。                    |
    | `img_id`              | `string`     | 否   | 关联图片的存储路径，格式为 `{bucket}-{object_name}`，与 `image_base64` 配合使用。|
    | `image_base64`        | `string`     | 否   | Base64 编码的图片数据；仅当 `img_id` 格式合法（含 `-`）时才会写入对象存储。  |

    ---

    ### 处理流程

    1. 对 `content_with_weight` 执行分词，提取 `important_kwd` 和 `question_kwd` 的分词结果。
    2. 校验 `doc_id` 对应的租户与文档是否存在。
    3. 若文档解析类型为 `QA`，验证正文包含问题和答案（TAB 或换行分隔），并自动识别语言。
    4. 调用 Embedding 模型生成语义向量（非 QA：`0.1 × 文档名向量 + 0.9 × 正文向量`；QA：仅用答案向量）。
    5. 将结果写入向量存储（`vector` 字段及维度特定字段 `q_{dim}_vec`）。
    6. 若 `image_base64` 与合法 `img_id` 同时提供，将图片写入对象存储的对应 bucket。

    ---

    ### 响应

    #### 成功 (200)

    ```json
    {"retcode": 0, "retmsg": "success", "data": true}
    ```

    #### 错误

    | 场景                        | retcode | retmsg                                      |
    |-----------------------------|---------|---------------------------------------------|
    | 租户不存在                  | 400     | `Tenant not found!`                         |
    | 文档不存在                  | 400     | `Document not found!`                       |
    | `important_kwd` 非列表      | 400     | `` `important_kwd` should be a list ``      |
    | `question_kwd` 非列表       | 400     | `` `question_kwd` should be a list ``       |
    | QA 格式不合法               | 400     | `Q&A must be separated by TAB/ENTER key.`   |
    | 服务内部异常                | 500     | 具体错误信息                                |

    ---

    ### 示例

    **请求体（QA 模式）**:
    ```json
    {
        "chunk_id": "12345",
        "doc_id": "67890",
        "content_with_weight": "人工智能是什么？\t人工智能是计算机科学的一个分支。",
        "important_kwd": ["人工智能", "计算机科学"],
        "available_int": 1,
        "img_id": "mybucket-images/fig1.png",
        "image_base64": "<base64-encoded-data>"
    }
    ```

    **成功响应**:
    ```json
    {"retcode": 0, "retmsg": "success", "data": true}
    ```
    """
    d = {
        "id": request.chunk_id,
        "content_with_weight": request.content_with_weight,
        "content_ltks": rag_tokenizer.tokenize(request.content_with_weight),
        "content_sm_ltks": rag_tokenizer.fine_grained_tokenize(rag_tokenizer.tokenize(request.content_with_weight)),
    }
    important_kwd = request.important_kwd if request.important_kwd is not None else []
    if not isinstance(important_kwd, list):
        return get_data_error_result(retmsg="`important_kwd` should be a list")
    d["important_kwd"] = important_kwd
    d["important_tks"] = rag_tokenizer.tokenize(" ".join(important_kwd)) if important_kwd else ""

    question_kwd = request.question_kwd if request.question_kwd is not None else []
    if not isinstance(question_kwd, list):
        return get_data_error_result(retmsg="`question_kwd` should be a list")
    d["question_kwd"] = question_kwd
    d["question_tks"] = rag_tokenizer.tokenize("\n".join(question_kwd)) if question_kwd else ""

    if request.tag_kwd is not None:
        if not isinstance(request.tag_kwd, list):
            return get_data_error_result(retmsg="`tag_kwd` should be a list")
        if not all(isinstance(t, str) for t in request.tag_kwd):
            return get_data_error_result(retmsg="`tag_kwd` must be a list of strings")
        d["tag_kwd"] = request.tag_kwd

    if request.tag_feas is not None:
        try:
            d["tag_feas"] = validate_tag_features(request.tag_feas)
        except ValueError as exc:
            return get_data_error_result(retmsg=f"`tag_feas` {exc}")

    if request.available_int is not None:
        d["available_int"] = request.available_int

    try:
        tenant_id = DocumentService.get_tenant_id(db, request.doc_id)
        if not tenant_id:
            return get_data_error_result(retmsg="Tenant not found!")

        doc = DocumentService.get_by_id(db, request.doc_id)
        if not doc:
            return get_data_error_result(retmsg="Document not found!")

        tenant_embd_id = DocumentService.get_tenant_embd_id(db, request.doc_id)
        if tenant_embd_id:
            embd_config = get_model_config_by_id(db, tenant_embd_id)
        else:
            embd_id = DocumentService.get_embd_id(db, request.doc_id)
            if embd_id:
                embd_config = get_model_config_by_type_and_name(db, tenant_id, LLMType.EMBEDDING.value, embd_id)
            else:
                embd_config = get_tenant_default_model_by_type(db, tenant_id, LLMType.EMBEDDING)
        embd_mdl = LLMBundle(db, tenant_id, embd_config)

        if doc.parser_id == ParserType.QA:
            arr = [
                t for t in re.split(
                    r"[\n\t]",
                    request.content_with_weight) if len(t) > 1]
            q, a = rmPrefix(arr[0]), rmPrefix("\n".join(arr[1:]))
            d = beAdoc(d, q, a, not any(
                [rag_tokenizer.is_chinese(t) for t in q + a]))

        # 计算向量
        v, c = embd_mdl.encode(
            [doc.name, request.content_with_weight if not d["question_kwd"] else "\n".join(d["question_kwd"])])
        v = 0.1 * v[0] + 0.9 * v[1] if doc.parser_id != ParserType.QA else v[1]

        # 同时存储到标准vector字段和维度特定字段
        vector_dim = len(v.tolist())
        d["vector"] = v.tolist()  # 始终保存到标准vector字段以保持兼容性
        d[f"q_{vector_dim}_vec"] = v.tolist()  # 同时保存到维度特定字段

        # 更新数据库
        update_condition = {"id": request.chunk_id}  # 主键查询条件
        kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
        settings.docStoreConn.update(update_condition, d, search.index_name_one(tenant_id, kb.name), doc.kb_id)

        # update image
        img_id = request.img_id or ""
        if request.image_base64 and img_id and "-" in img_id:
            bkt, name = img_id.split("-", 1)
            image_binary = base64.b64decode(request.image_base64)
            settings.STORAGE_IMPL.put(bkt, name, image_binary)

        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post('/switch', summary="切换文档块状态")
def switch(request: SwitchChunkRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/switch` 切换文档块状态接口

    **功能描述**:
    此接口用于批量切换指定文档中多个文档块的可用状态，支持启用或禁用文档块在检索中的可见性。

    ---

    ### 请求体 (Request Body)

    | 字段            | 类型           | 必填 | 描述                                                    |
    |-----------------|----------------|------|---------------------------------------------------------|
    | `doc_id`        | `string`       | 是   | 文档的唯一标识符                                        |
    | `chunk_ids`     | `list[string]` | 是   | 要切换状态的文档块ID列表                                |
    | `available_int` | `int`          | 是   | 新的可用状态 (1: 启用, 0: 禁用)                         |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    - **`Content-Type: application/json`**
    - **示例**:
        ```json
        {
            "retcode": 0,
            "retmsg": "success",
            "data": true
        }
        ```

    #### 错误响应
    - **404: Document not found**
        - 当指定的文档不存在时返回此错误
    - **400: Index updating failure**
        - 当更新索引失败时返回此错误

    ---

    ### 主要流程
    1. 验证文档存在性
    2. 获取文档所属的知识库信息
    3. 批量更新指定文档块的可用状态
    4. 返回操作结果

    ---

    ### 使用示例

    #### 启用文档块:
    ```json
    {
        "doc_id": "doc_123",
        "chunk_ids": ["chunk_001", "chunk_002"],
        "available_int": 1
    }
    ```

    #### 禁用文档块:
    ```json
    {
        "doc_id": "doc_123",
        "chunk_ids": ["chunk_003", "chunk_004"],
        "available_int": 0
    }
    ```
    """


    req = request.model_dump()
    try:
        doc = DocumentService.get_by_id(db, req["doc_id"])
        kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
        if not doc:
            return get_data_error_result(retmsg="Document not found!")
        for cid in req["chunk_ids"]:
            if not settings.docStoreConn.update({"id": cid},
                                                {"available_int": int(req["available_int"])},
                                                search.index_name_one(DocumentService.get_tenant_id(db, req["doc_id"]), kb.name),
                                                doc.kb_id):
                return get_data_error_result(retmsg="Index updating failure")
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post('/rm', summary="删除文档块")
def rm(request: RmChunkRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/rm` 删除文档块接口

**功能描述**:
此接口用于删除指定文档的块数据，同时更新相关的索引和文档统计信息。

---

### 请求体 (Request Body)

| 字段       | 类型          | 必填 | 描述                     |
|------------|---------------|------|--------------------------|
| `doc_id`   | `string`      | 是   | 需要操作的文档唯一标识符 |
| `chunk_ids`| `list[string]`| 是   | 要删除的文档块 ID 列表   |

---

### 响应 (Response)

#### 成功响应 (200)

- **`Content-Type: application/json`**
- **示例**:
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": true
    }
    ```

#### 错误响应

- **404: Document not found**
    - **描述**: 当根据 `doc_id` 查询不到对应文档时返回此错误。
    - **示例**:
        ```json
        {
            "retcode": 404,
            "retmsg": "Document not found!"
        }
        ```

- **404: KnowledgeBase not found**
    - **描述**: 当文档对应的知识库不存在时返回此错误。
    - **示例**:
        ```json
        {
            "retcode": 404,
            "retmsg": "KnowledgeBase not found!"
        }
        ```

- **400: Index updating failure**
    - **描述**: 当删除索引中的文档块失败时返回此错误。
    - **示例**:
        ```json
        {
            "retcode": 400,
            "retmsg": "Index updating failure"
        }
        ```

- **500: 内部错误**
    - **描述**: 当发生意外错误时返回此错误。
    - **示例**:
        ```json
        {
            "retcode": 500,
            "retmsg": "Internal server error",
            "detail": "具体错误信息"
        }
        ```

---

### 主要流程

1. 验证文档和知识库的存在性：
    - 根据 `doc_id` 获取文档信息。
    - 根据文档的 `kb_id` 获取知识库信息。
2. 删除索引中的指定文档块：
    - 调用 `delete` 方法从索引中删除文档块。
3. 更新文档的块统计信息：
    - 调用 `DocumentService.decrement_chunk_num` 减少文档的块数量统计。
4. 返回操作结果。

---

### 注意事项

- **索引删除**:
  文档块的删除操作会同步影响索引中的数据，确保 `chunk_ids` 的完整性和正确性。
- **统计更新**:
  删除操作会调整文档的块数量统计，若删除操作失败，将不更新统计信息。

---

### 示例请求

#### 请求体:
```json
{
    "doc_id": "12345",
    "chunk_ids": ["67890", "98765"]
}
```

- **成功响应**:
```json
{
    "retcode": 0,
    "retmsg": "success",
    "data": true
}
```

- **错误响应 (文档不存在)**:
```json
{
    "retcode": 404,
    "retmsg": "Document not found!"
}
```
```json
{
    "retcode": 400,
    "retmsg": "Index updating failure"
}
```
```json
{
    "retcode": 500,
    "retmsg": "Internal server error",
    "detail": "详细的错误信息"
}
```
    """
    req = request.model_dump()
    try:
        deleted_chunk_ids = req.get("chunk_ids")
        if isinstance(deleted_chunk_ids, list):
            unique_chunk_ids = list(dict.fromkeys(deleted_chunk_ids))
            has_ids = len(unique_chunk_ids) > 0
        elif deleted_chunk_ids is not None:
            unique_chunk_ids = [deleted_chunk_ids]
            has_ids = deleted_chunk_ids not in (None, "")
        else:
            unique_chunk_ids = []
            has_ids = False
        if not has_ids:
            if req.get("delete_all") is True:
                doc = DocumentService.get_by_id(db, req["doc_id"])
                if not doc:
                    return get_data_error_result(retmsg="Document not found!")
                kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
                if not kb:
                    return get_data_error_result(retmsg="KnowledgeBase not found!")
                tenant_id = DocumentService.get_tenant_id(db, req["doc_id"])
                collection_name = search.index_name_one(tenant_id, kb.name)
                # Clean up storage assets
                DocumentService.delete_chunk_images(doc, collection_name)
                condition = {"doc_id": req["doc_id"]}
                try:
                    deleted_count = settings.docStoreConn.delete(condition, collection_name, doc.kb_id)
                except Exception:
                    return get_data_error_result(retmsg="Chunk deleting failure")
                if deleted_count > 0:
                    DocumentService.decrement_chunk_num(db, doc.id, doc.kb_id, 1, deleted_count, 0)
                return get_json_result(data=True)
            return get_json_result(data=True)

        doc = DocumentService.get_by_id(db, req["doc_id"])
        if not doc:
            return get_data_error_result(retmsg="Document not found!")
        kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
        if not kb:
            return get_data_error_result(retmsg="KnowledgeBase not found!")

        collection_name = search.index_name_one(kb.tenant_id, kb.name)
        db_type = settings.docStoreConn.db_type()
        condition = {"id": req["chunk_ids"], "doc_id": req["doc_id"]}
        try:
            if db_type == "milvus":
                deleted_count = settings.docStoreConn.delete(
                    condition=condition,
                    index_name=collection_name,
                    dataset_id=kb.id
                )
            else:
                deleted_count = settings.docStoreConn.delete(
                    condition,
                    collection_name,
                    kb.id
                )
        except Exception:
            return get_data_error_result(retmsg="Chunk deleting failure")
        if has_ids and deleted_count == 0:
            return get_data_error_result(retmsg="Index updating failure")
        chunk_number = deleted_count if deleted_count else 0
        DocumentService.decrement_chunk_num(db, doc.id, doc.kb_id, 1, chunk_number, 0)
        for cid in deleted_chunk_ids:
            if settings.STORAGE_IMPL.obj_exist(doc.kb_id, cid):
                settings.STORAGE_IMPL.rm(doc.kb_id, cid)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)

@router.post('/create', summary="创建文档块")
def create(request: CreateChunkRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/create` 创建文档块接口

    **功能描述**:
    此接口用于创建文档块，支持内容分词、关键词提取、向量计算，并将生成的数据存储到数据库和知识库中。

    ---

    ### 请求体 (Request Body)

    | 字段                  | 类型           | 必填 | 描述                                              |
    |-----------------------|----------------|------|---------------------------------------------------|
    | `doc_id`             | `string`      | 是   | 文档的唯一标识符。                                |
    | `content_with_weight`| `string`      | 是   | 包含权重的内容字符串，用于分词和向量计算。        |
    | `question_kwd`       | `list[string]`| 否   | 问题关键词列表，用于问答模式或补充向量计算。      |
    | `important_kwd`      | `list[string]`| 否   | 重要关键词列表，用于额外的分词和向量计算。        |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)

    - **`Content-Type: application/json`**
    - **示例**:
        ```json
        {
            "retcode": 0,
            "retmsg": "success",
            "data": {
                "chunk_id": "a1b2c3d4e5"
            }
        }
        ```

    #### 错误响应

    - **404: Document not found**
        - **描述**: 当根据 `doc_id` 查询文档信息失败时，返回此错误。
        - **示例**:
            ```json
            {
                "detail": "Document not found!"
            }
            ```

    - **404: Tenant not found**
        - **描述**: 当根据 `doc_id` 查询租户信息失败时，返回此错误。
        - **示例**:
            ```json
            {
                "detail": "Tenant not found!"
            }
            ```

    - **404: Knowledgebase not found**
        - **描述**: 当根据 `kb_id` 查询知识库信息失败时，返回此错误。
        - **示例**:
            ```json
            {
                "detail": "Knowledgebase not found!"
            }
            ```

    - **500: 内部错误**
        - **描述**: 当发生意外错误时，返回此错误。
        - **示例**:
            ```json
            {
                "retcode": 500,
                "retmsg": "Internal server error",
                "detail": "具体错误信息"
            }
            ```

    ---

    ### 主要流程

    1. 解析请求体内容并生成唯一标识符 (`chunk_id`)。
        - 基于 `content_with_weight` 和 `doc_id` 计算 MD5 哈希值作为 `chunk_id`。
    2. 分词与关键词提取:
        - 对 `content_with_weight` 进行分词 (`content_ltks`) 和细粒度分词 (`content_sm_ltks`)。
        - 对 `important_kwd` 提取关键词并分词 (`important_tks`)。
    3. 检查文档 (`doc_id`) 所属租户和知识库信息:
        - 如果文档或知识库不存在，返回相应的错误响应。
    4. 向量计算:
        - 使用嵌入模型 (`LLMBundle`) 对文档标题和内容生成语义向量 (`vector`)。
        - 支持权重配置 (`0.1` 标题向量 + `0.9` 内容向量)。
    5. 数据存储:
        - 将分词结果、关键词、向量等数据存入知识库。
    6. 更新文档块计数:
        - 调用 `DocumentService.increment_chunk_num` 更新文档块的相关计数。

    ---

    ### 注意事项

    - **关键词提取**:
        - `important_kwd` 提供额外的分词和向量计算输入。
        - 如果关键词为空，系统会自动跳过对应处理。
    - **向量计算**:
        - 使用权重对标题和内容向量进行加权合成。
        - 当前支持固定字段名 `vector`，未来可能支持动态配置。
    - **错误处理**:
        - 针对文档、租户和知识库的不存在分别返回特定错误响应。
        - 捕获所有异常并返回服务器错误响应。

    ---

    ### 示例请求

    #### 请求体:
    ```json
    {
        "doc_id": "doc123",
        "content_with_weight": "文本内容带权重的示例",
        "important_kwd": ["关键词1", "关键词2"],
        "question_kwd": ["问题关键词1", "问题关键词2"]
    }
    ```

    - **成功响应**:
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "chunk_id": "a1b2c3d4e5"
        }
    }
    ```

    - **错误响应 (文档不存在):**:
    ```json
    {
        "detail": "Document not found!"
    }
    ```
    """
    req = request.model_dump()
    chunk_id = xxhash.xxh64((request.content_with_weight + request.doc_id).encode("utf-8")).hexdigest()
    d = {"id": chunk_id, "content_ltks": rag_tokenizer.tokenize(req["content_with_weight"]),
         "content_with_weight": req["content_with_weight"]}
    d["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(d["content_ltks"])
    d["important_kwd"] = req.get("important_kwd", [])
    if not isinstance(d["important_kwd"], list):
        return get_data_error_result(retmsg="`important_kwd` is required to be a list")
    d["important_tks"] = rag_tokenizer.tokenize(" ".join(d["important_kwd"]))
    d["question_kwd"] = req.get("question_kwd", [])
    if not isinstance(d["question_kwd"], list):
        return get_data_error_result(retmsg="`question_kwd` is required to be a list")
    d["question_tks"] = rag_tokenizer.tokenize("\n".join(d["question_kwd"]))
    d["create_time"] = str(datetime.datetime.now()).replace("T", " ")[:19]
    d["create_timestamp_flt"] = datetime.datetime.now().timestamp()
    if req.get("tag_kwd") is not None:
        if not isinstance(req["tag_kwd"], list):
            return get_data_error_result(retmsg="`tag_kwd` is required to be a list")
        if not all(isinstance(t, str) for t in req["tag_kwd"]):
            return get_data_error_result(retmsg="`tag_kwd` must be a list of strings")
        d["tag_kwd"] = req["tag_kwd"]
    if req.get("tag_feas") is not None:
        try:
            d["tag_feas"] = validate_tag_features(req["tag_feas"])
        except ValueError as exc:
            return get_data_error_result(retmsg=f"`tag_feas` {exc}")

    try:
        doc = DocumentService.get_by_id(db, req["doc_id"])
        if not doc:
            return get_data_error_result(retmsg="Document not found!")
        d["kb_id"] = doc.kb_id
        d["docnm_kwd"] = doc.name
        d["title_tks"] = rag_tokenizer.tokenize(doc.name.split(".")[0])
        d["title_sm_tks"] = rag_tokenizer.fine_grained_tokenize(d["title_tks"])
        d["doc_id"] = doc.id
        d["page_num_int"] = []
        d["position_int"] = []
        d["top_int"] = []
        d["img_id"] = ""
        d["auth"] = []
        d["available_int"] = 1
        image_base64 = req.get("image_base64")
        if image_base64:
            d["img_id"] = f"{doc.kb_id}-{chunk_id}"
            d["doc_type_kwd"] = "image"

        tenant_id = DocumentService.get_tenant_id(db, req["doc_id"])
        if not tenant_id:
            return get_data_error_result(retmsg="Tenant not found!")

        kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
        if not kb:
            return get_data_error_result(retmsg="Knowledgebase not found!")
        if kb.pagerank:
            d[PAGERANK_FLD] = kb.pagerank

        tenant_embd_id = DocumentService.get_tenant_embd_id(db, req["doc_id"])
        if tenant_embd_id:
            embd_config = get_model_config_by_id(db, tenant_embd_id)
        else:
            embd_id = DocumentService.get_embd_id(db, req["doc_id"])
            if embd_id:
                embd_config = get_model_config_by_type_and_name(db, tenant_id, LLMType.EMBEDDING.value, embd_id)
            else:
                embd_config = get_tenant_default_model_by_type(db, tenant_id, LLMType.EMBEDDING)
        embd_mdl = LLMBundle(db, tenant_id, embd_config)

        v, c = embd_mdl.encode(
            [doc.name, req["content_with_weight"] if not d["question_kwd"] else "\n".join(d["question_kwd"])])
        v = 0.1 * v[0] + 0.9 * v[1]

        # 同时存储到标准vector字段和维度特定字段
        vector_dim = len(v.tolist())
        d["vector"] = v.tolist()  # 始终保存到标准vector字段以保持兼容性
        d[f"q_{vector_dim}_vec"] = v.tolist()  # 同时保存到维度特定字段

        settings.docStoreConn.insert([d], search.index_name_one(tenant_id, kb.name), kb.id)

        if image_base64:
            store_chunk_image(doc.kb_id, chunk_id, base64.b64decode(image_base64))

        DocumentService.increment_chunk_num(
            db, doc.id, doc.kb_id, c, 1, 0)
        return get_json_result(data={"chunk_id": chunk_id, "image_id": d.get("img_id", "")})
    except Exception as e:
        return server_error_response(e)


@router.post('/retrieval_test', summary="检索测试")
async def retrieval_test(request: RetrievalTestRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    检索测试接口

    该接口用于测试不同类型的检索策略，支持多种搜索模式，包括向量检索、全文检索、混合检索和融合检索。

    ## 请求参数

    ### 基础参数
    - **kb_ids**: 知识库ID列表，必填。指定要在哪些知识库中进行检索
    - **question**: 检索问题，必填。用户输入的查询文本
    - **page**: 页码，默认为1。用于分页返回结果
    - **size**: 每页返回的结果数量，默认为30
    - **doc_ids**: 文档ID列表，可选。限制只在指定的文档中检索
    - **similarity_threshold**: 相似度阈值，默认为0.0。低于此阈值的结果将被过滤
    - **vector_similarity_weight**: 向量相似度权重，默认为0.3。用于重新排序阶段的权重调整
    - **use_kg**: 是否使用知识图谱，默认为False。启用后会结合知识图谱进行检索
    - **top_k**: 最大召回数量，默认为1024。初始检索阶段返回的最大结果数
    - **rerank_id**: 重排序模型ID，可选。用于对初始检索结果进行重新排序
    - **highlight**: 是否高亮显示匹配文本，默认为False
    - **keyword**: 是否进行关键词提取增强，默认为False。启用后会自动提取问题关键词
    - **cross_languages**: 跨语言翻译列表，可选。指定要将问题翻译成的目标语言列表，如["English", "French"]
    - **search_id**: 检索配置记录表的唯一id

    ### 搜索模式 (search_mode)
    搜索模式决定了使用哪种检索策略，支持以下四种类型：

    #### 1. 稀疏检索 (Sparse Search)「也叫全文检索」
    ```json
    {
        "type": "sparse"
    }
    ```
    - **描述**: 基于关键词和词频的传统全文检索
    - **适用场景**:
      - 专业术语查询
      - 特定代码、型号或标识符搜索
      - 需要精确关键词匹配的场景
    - **特点**: 快速、精确匹配，但不理解语义关系

    #### 2. 密集检索 (Dense Search)「暂时不启用，传惨不报错」
    ```json
    {
        "type": "dense"
    }
    ```
    - **描述**: 基于向量嵌入的语义检索
    - **适用场景**:
      - 概念性问题查询
      - 同义词和相关概念搜索
      - 需要理解语义关系的场景
    - **特点**: 理解语义，但可能错过精确关键词匹配

    #### 3. 混合检索 (Hybrid Search)
    ```json
    {
        "type": "hybrid",
        "weight_dense": 0.7,
        "weight_sparse": 0.3
    }
    ```
    - **描述**: 结合稀疏检索和密集检索的优势
    - **参数**:
      - `weight_dense`: 向量检索权重 (0.0-1.0)，默认0.7
      - `weight_sparse`: 全文检索权重 (0.0-1.0)，默认0.3
      - 权重和会自动归一化为1.0
    - **权重推荐**:
      - `[0.8, 0.2]`: 偏向语义理解 (概念性问题)
      - `[0.7, 0.3]`: 平衡配置 (通用推荐)
      - `[0.4, 0.6]`: 偏向关键词匹配 (专业术语)
      - `[0.2, 0.8]`: 强调精确匹配 (代码、API等)

    #### 4. 向量融合检索 (Fusion Search)「默认检索模式」
    ```json
    {
        "type": "fusion",
        "weights": "0.05,0.95"
    }
    ```
    - **描述**: 传统的检索融合策略
    - **参数**:
      - `weights`: "文本权重,向量权重" 格式的字符串，默认"0.05,0.95"
    - **特点**: 如果不指定search_mode，将使用此模式作为默认值

    ## 返回结果

    成功时返回JSON格式结果：
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "total": 100,
            "chunks": [
                {
                    "chunk_id": "chunk_123",
                    "text": "检索到的文本内容",
                    "doc_id": "doc_456",
                    "docnm_kwd": "文档名称",
                    "kb_id": "kb_789",
                    "similarity": 0.85,
                    "vector_similarity": 0.82,
                    "term_similarity": 0.88,
                    "highlight": "高亮显示的内容",
                    "positions": [...]
                }
            ],
            "doc_aggs": [
                {
                    "doc_name": "文档1",
                    "doc_id": "doc_456",
                    "count": 5
                }
            ],
            "labels": {...}
        }
    }
    ```

    ## 使用示例

    ### 基础检索
    ```json
    {
        "kb_ids": ["kb_123"],
        "question": "什么是机器学习？"
    }
    ```

    ### 混合检索（概念性查询）
    ```json
    {
        "kb_ids": ["kb_123"],
        "question": "深度学习的原理是什么？",
        "search_mode": {
            "type": "hybrid",
            "weight_dense": 0.8,
            "weight_sparse": 0.2
        }
    }
    ```

    ### 精确匹配查询
    ```json
    {
        "kb_ids": ["kb_123"],
        "question": "getUserInfo API参数",
        "search_mode": {
            "type": "hybrid",
            "weight_dense": 0.3,
            "weight_sparse": 0.7
        }
    }
    ```

    ### 高级配置
    ```json
    {
        "kb_ids": ["kb_123"],
        "question": "如何优化模型性能？",
        "page": 1,
        "size": 20,
        "similarity_threshold": 0.1,
        "vector_similarity_weight": 0.7,
        "use_kg": true,
        "rerank_id": "rerank_model_123",
        "highlight": true,
        "keyword": true,
        "search_mode": {
            "type": "hybrid",
            "weight_dense": 0.75,
            "weight_sparse": 0.25
        }
    }
    ```

    ### 跨语言检索
    ```json
    {
        "kb_ids": ["kb_123"],
        "question": "什么是机器学习？",
        "cross_languages": ["English", "French"],
        "search_mode": {
            "type": "hybrid",
            "weight_dense": 0.7,
            "weight_sparse": 0.3
        }
    }
    ```

    ## 错误处理

    - **权限错误**: 只有知识库的所有者才能进行检索测试
    - **知识库不存在**: 指定的知识库ID无效
    - **无结果**: 未找到匹配的内容块
    - **参数错误**: 搜索模式参数格式错误

    ## 性能优化建议

    1. **合理设置top_k**: 过大会影响性能，过小可能遗漏相关结果
    2. **调整相似度阈值**: 根据业务需求设置合适的similarity_threshold
    3. **选择合适的搜索模式**: 根据查询类型选择最优的检索策略
    4. **使用重排序**: 对于对质量要求高的场景，建议使用rerank_id
    """
    doc_ids = request.doc_ids
    kb_ids = request.kb_ids
    question = request.question
    if not kb_ids:
        return get_json_result(data=False, retmsg='Please specify dataset firstly.', retcode=RetCode.DATA_ERROR)

    meta_data_filter = {}
    chat_mdl = None
    if request.search_id:
        search_config = SearchService.get_detail(db, request.search_id or "").get("search_config", {})
        meta_data_filter = search_config.get("meta_data_filter", {})
        if meta_data_filter.get("method") in ["auto", "semi_auto"]:
            chat_id = search_config.get("chat_id", "")
            if chat_id:
                chat_config = get_model_config_by_type_and_name(db, user.id, LLMType.CHAT.value, chat_id)
            else:
                chat_config = get_tenant_default_model_by_type(db, user.id, LLMType.CHAT)
            chat_mdl = LLMBundle(db, user.id, chat_config)
    else:
        meta_data_filter = request.meta_data_filter or {}
        if meta_data_filter.get("method") in ["auto", "semi_auto"]:
            chat_config = get_tenant_default_model_by_type(db, user.id, LLMType.CHAT)
            chat_mdl = LLMBundle(db, user.id, chat_config)

    if meta_data_filter:
        metas = DocMetadataService.get_flatted_meta_by_kbs(db, kb_ids)
        doc_ids = await apply_meta_data_filter(meta_data_filter, metas, question, chat_mdl, doc_ids)

    try:
        tenants = UserTenantService.query(db, user_id=user.id)
        for kid in request.kb_ids:
            for tenant in tenants:
                if KnowledgebaseService.query(
                        db, tenant_id=tenant.tenant_id, id=kid):
                    break
            else:
                return get_json_result(
                    data=False, retmsg=f'Only owner of dataset authorized for this operation.',
                    retcode=RetCode.OPERATING_ERROR)

        kb = KnowledgebaseService.get_by_id(db, request.kb_ids[0])
        if not kb:
            return get_data_error_result(retmsg="Knowledgebase not found!")

        if request.cross_languages:
            question = await cross_languages(kb.tenant_id, None, question, request.cross_languages)

        if kb.tenant_embd_id:
            embd_config = get_model_config_by_id(db, kb.tenant_embd_id)
        elif kb.embd_id:
            embd_config = get_model_config_by_type_and_name(db, kb.tenant_id, LLMType.EMBEDDING.value, kb.embd_id)
        else:
            embd_config = get_tenant_default_model_by_type(db, kb.tenant_id, LLMType.EMBEDDING)
        embd_mdl = LLMBundle(db, kb.tenant_id, embd_config)

        rerank_mdl = None
        if request.tenant_rerank_id:
            rerank_config = get_model_config_by_id(db, request.tenant_rerank_id)
            rerank_mdl = LLMBundle(db, kb.tenant_id, rerank_config)
        elif request.rerank_id:
            rerank_config = get_model_config_by_type_and_name(db, kb.tenant_id, LLMType.RERANK.value, request.rerank_id)
            rerank_mdl = LLMBundle(db, kb.tenant_id, rerank_config)
        if request.keyword:
            chat_config = get_tenant_default_model_by_type(db, kb.tenant_id, LLMType.CHAT)
            chat_mdl = LLMBundle(db, kb.tenant_id, chat_config)
            question += await keyword_extraction(chat_mdl, question)
        filter_exp = ""
        labels = label_question(db, question, [kb])

        # 使用实例方法获取搜索模式
        search_mode_dict = request.get_search_mode_dict()

        # 当调用retrieval函数时，传递维度信息
        # 注意：kb_ids 参数应传递知识库 ID 列表，而不是名称列表，用于 ES 的 kb_id 过滤
        ranks = await settings.retriever.retrieval(question, filter_exp, embd_mdl, kb.tenant_id, [kb.name], request.page,
                               request.size, request.similarity_threshold, request.vector_similarity_weight,
                               request.top_k, doc_ids, rerank_mdl=rerank_mdl,
                               highlight=request.highlight if request.highlight is not None else False,
                               rank_feature=labels, search_mode=search_mode_dict, kb_ids=request.kb_ids)
        if request.use_kg:
            ck = await settings.kg_retriever.retrieval(question,
                                                   kb.tenant_id,
                                                   request.kb_ids,
                                                   embd_mdl,
                                                   LLMBundle(db, kb.tenant_id, get_tenant_default_model_by_type(db, kb.tenant_id, LLMType.CHAT)))
            if ck["content_with_weight"]:
                ranks["chunks"].insert(0, ck)
        ranks["chunks"] = settings.retriever.retrieval_by_children(ranks["chunks"], [kb.tenant_id])

        for c in ranks["chunks"]:
            c.pop("vector", None)
        ranks["labels"] = labels

        return get_json_result(data=ranks)
    except Exception as e:
        if str(e).find("not_found") > 0:
            return get_json_result(data=False, retmsg=f'No chunk found! Check the chunk status please!',
                                   retcode=RetCode.DATA_ERROR)
        return server_error_response(e)


@router.get('/knowledge_graph', summary="获取知识图谱")
async def knowledge_graph(doc_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### GET `/knowledge_graph` 获取文档知识图谱接口

    **功能描述**:
    此接口用于获取指定文档的知识图谱和思维导图数据，支持图形化展示文档中的实体关系和层次结构。

    ---

    ### 请求参数 (Query Parameters)

    | 参数名   | 类型     | 必填 | 描述                    |
    |----------|----------|------|-------------------------|
    | `doc_id` | `string` | 是   | 文档的唯一标识符        |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    - **`Content-Type: application/json`**
    - **示例**:
        ```json
        {
            "retcode": 0,
            "retmsg": "success",
            "data": {
                "graph": {
                    "nodes": [
                        {"id": "entity1", "label": "实体1", "type": "person"},
                        {"id": "entity2", "label": "实体2", "type": "organization"}
                    ],
                    "edges": [
                        {"source": "entity1", "target": "entity2", "relation": "works_for"}
                    ]
                },
                "mind_map": {
                    "id": "root",
                    "label": "主题",
                    "children": [
                        {
                            "id": "subtopic1",
                            "label": "子主题1",
                            "children": []
                        }
                    ]
                }
            }
        }
        ```

    ---

    ### 主要流程
    1. 根据文档ID获取租户信息
    2. 构建知识图谱搜索查询
    3. 从索引中检索图谱和思维导图数据
    4. 解析JSON格式的图谱内容
    5. 处理思维导图节点重复问题
    6. 返回结构化的图谱数据

    ---

    ### 注意事项
    - **当前状态**: 此功能目前处于开发阶段，需要调整Milvus集合schema以支持knowledge_graph_kwd字段
    - **数据格式**: 返回的图谱数据为JSON格式，包含节点和边的信息
    - **重复处理**: 思维导图中的重复节点会自动添加序号标识
    - **权限控制**: 只能访问用户有权限的文档

    ---

    ### 返回数据结构

    #### 知识图谱 (graph)
    - **nodes**: 实体节点数组
      - `id`: 节点唯一标识
      - `label`: 节点显示名称
      - `type`: 节点类型（如person, organization等）
    - **edges**: 关系边数组
      - `source`: 源节点ID
      - `target`: 目标节点ID
      - `relation`: 关系类型

    #### 思维导图 (mind_map)
    - **层次结构**: 树形结构数据
      - `id`: 节点唯一标识
      - `label`: 节点显示名称
      - `children`: 子节点数组

    ---

    ### 使用示例

    #### 请求:
    ```
    GET /knowledge_graph?doc_id=doc_123456
    ```

    #### 成功响应:
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "graph": {},
            "mind_map": {}
        }
    }
    ```
    """
    obj = {"graph": {}, "mind_map": {}}
    doc = DocumentService.get_by_id(db, doc_id)
    if not doc:
        return get_json_result(data=obj)

    kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
    if not kb:
        return get_json_result(data=obj)

    index_name = search.index_name_one(kb.tenant_id, kb.name)
    if not settings.docStoreConn.index_exist(index_name, doc.kb_id):
        return get_json_result(data=obj)

    select_fields = ["knowledge_graph_kwd", "content_with_weight"]
    graph_res = await thread_pool_exec(
        settings.docStoreConn.search,
        select_fields,
        [],
        {
            "kb_id": doc.kb_id,
            "knowledge_graph_kwd": ["subgraph"],
            "source_id": doc_id,
            "removed_kwd": "N",
        },
        [],
        OrderByExpr(),
        0,
        1,
        index_name,
        [doc.kb_id],
    )
    mind_map_res = await thread_pool_exec(
        settings.docStoreConn.search,
        select_fields,
        [],
        {
            "kb_id": doc.kb_id,
            "doc_id": doc_id,
            "knowledge_graph_kwd": ["mind_map"],
        },
        [],
        OrderByExpr(),
        0,
        1,
        index_name,
        [doc.kb_id],
    )
    graph_fields = settings.docStoreConn.get_fields(graph_res, select_fields)
    mind_map_fields = settings.docStoreConn.get_fields(mind_map_res, select_fields)

    for search_fields in (graph_fields, mind_map_fields):
        for _, field in search_fields.items():
            if not isinstance(field, dict):
                continue
            ty = field["knowledge_graph_kwd"]
            try:
                content_json = json.loads(field["content_with_weight"])
            except Exception:
                continue

            if ty == "mind_map":
                node_dict = {}

                def repeat_deal(content_json, node_dict):
                    if 'id' in content_json:
                        if content_json['id'] in node_dict:
                            node_name = content_json['id']
                            content_json['id'] += f"({node_dict[content_json['id']]})"
                            node_dict[node_name] += 1
                        else:
                            node_dict[content_json['id']] = 1
                    if 'children' in content_json and content_json['children']:
                        for item in content_json['children']:
                            repeat_deal(item, node_dict)

                repeat_deal(content_json, node_dict)

            obj["graph" if ty == "subgraph" else ty] = content_json

    return get_json_result(data=obj)

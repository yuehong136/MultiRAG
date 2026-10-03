import base64
import datetime
import json
import re
from typing import Any

import numpy as np
import xxhash
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import Document, get_db
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name, get_tenant_default_model_by_type
from api.db.services.document_image_lock import image_reference_key, image_write_locks
from api.db.services.document_service import DocumentService
from api.db.services.document_status_service import insert_source_chunks
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import get_data_error_result, get_json_result, server_error_response
from api.utils.image_utils import store_chunk_image
from common import settings
from common.constants import PAGERANK_FLD, LLMType, ParserType, RetCode
from common.doc_store.doc_store_base import OrderByExpr
from common.misc_utils import thread_pool_exec
from common.string_utils import is_content_empty, remove_redundant_spaces
from common.tag_feature_utils import validate_tag_features
from core.app.qa import beAdoc, rmPrefix
from core.nlp import rag_tokenizer, search

router = APIRouter()


def _locked_document(db: Session, document_id: str) -> Document | None:
    return db.scalar(select(Document).where(Document.id == document_id).with_for_update().execution_options(populate_existing=True))


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


@router.post("/list", summary="列出文档块", deprecated=True)
async def list_chunk(request: ListChunkRequest, db: Session = Depends(get_db), user: Any = Depends(manager)) -> JSONResponse:
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
    | `available_int` | `int`       | 否   | 可用性过滤：`0` 仅禁用块，`1` 仅启用块；省略或传 `null` 时不过滤。                     |

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
        query = {"doc_ids": [request.doc_id], "page": request.page, "size": request.size, "question": request.keywords, "sort": True}
        if request.keywords:
            query["filter_exp"] = f"content_with_weight like '%{request.keywords}%'" if request.keywords else None

        # 先计算出所有问题块的总数
        # query_count = {
        #     "doc_ids": [request.doc_id], "question": request.keywords,
        #     "sort": True
        # }
        if request.available_int is not None:
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
                "content_with_weight": remove_redundant_spaces(sres.highlight[id]) if request.keywords and id in sres.highlight else sres.field[id].get("content_with_weight", ""),
                "doc_id": sres.field[id]["doc_id"],
                "docnm_kwd": sres.field[id]["docnm_kwd"],
                "important_kwd": sres.field[id].get("important_kwd", []),
                "tag_kwd": sres.field[id].get("tag_kwd", []),
                "question_kwd": sres.field[id].get("question_kwd", []),
                "img_id": sres.field[id].get("img_id", ""),
                "available_int": int(sres.field[id].get("available_int", 1)),
                "positions": sres.field[id].get("position_int", []),
                "doc_type_kwd": sres.field[id].get("doc_type_kwd"),
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
            return get_json_result(data=False, retmsg="No chunk found!", retcode=RetCode.DATA_ERROR)
        return server_error_response(e)


@router.get("/get", summary="获取文档块", deprecated=True)
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
            return get_json_result(data=False, retmsg="Chunk not found!", retcode=RetCode.DATA_ERROR)
        return server_error_response(e)


@router.post("/vector_store/query", summary="向量存储字段查询（可选语义检索）")
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
            match_exprs.append(await dealer.get_vector(request.question, embd_mdl, topk=1024, similarity=request.similarity if request.similarity is not None else 0.1))

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


@router.post("/set", summary="设置文档块", deprecated=True)
def set(request: SetChunkRequest, db: Session = Depends(get_db), user: Any = Depends(manager)) -> JSONResponse:
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
    | `content_with_weight` 为空/纯空白 | 400 | `` `content_with_weight` is required ``     |
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
    if is_content_empty(request.content_with_weight):
        return get_data_error_result(retmsg="`content_with_weight` is required")
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

        doc = _locked_document(db, request.doc_id)
        if not doc:
            return get_data_error_result(retmsg="Document not found!")

        kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
        index_name = search.index_name_one(tenant_id, kb.name)
        current = settings.docStoreConn.get(request.chunk_id, index_name, [doc.kb_id])
        if not current or str(current.get("doc_id", current.get("document_id"))) != request.doc_id:
            return get_data_error_result(retmsg="Chunk not found in this document!")

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
            arr = [t for t in re.split(r"[\n\t]", request.content_with_weight) if len(t) > 1]
            q, a = rmPrefix(arr[0]), rmPrefix("\n".join(arr[1:]))
            d = beAdoc(d, q, a, not any(rag_tokenizer.is_chinese(t) for t in q + a))

        # 计算向量
        # Keep the Document lock when LLMBundle releases its read session.
        embd_mdl.db = None
        v, c = embd_mdl.encode([doc.name, request.content_with_weight if not d["question_kwd"] else "\n".join(d["question_kwd"])])
        v = 0.1 * v[0] + 0.9 * v[1] if doc.parser_id != ParserType.QA else v[1]

        # 同时存储到标准vector字段和维度特定字段
        vector_dim = len(v.tolist())
        d["vector"] = v.tolist()  # 始终保存到标准vector字段以保持兼容性
        d[f"q_{vector_dim}_vec"] = v.tolist()  # 同时保存到维度特定字段

        # 更新数据库
        update_condition = {"id": request.chunk_id, "doc_id": request.doc_id}
        img_id = request.img_id or ""
        key = image_reference_key(img_id)
        with image_write_locks(db.get_bind(), [key] if key is not None else []):
            settings.docStoreConn.update(update_condition, d, index_name, doc.kb_id)
            if request.image_base64 and key is not None:
                bkt, name = key
                image_binary = base64.b64decode(request.image_base64)
                settings.STORAGE_IMPL.put(bkt, name, image_binary)

        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post("/switch", summary="切换文档块状态", deprecated=True)
def switch(request: SwitchChunkRequest, db: Session = Depends(get_db), user: Any = Depends(manager)) -> JSONResponse:
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
        doc = _locked_document(db, req["doc_id"])
        if not doc:
            return get_data_error_result(retmsg="Document not found!")
        kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
        index_name = search.index_name_one(DocumentService.get_tenant_id(db, req["doc_id"]), kb.name)
        for cid in req["chunk_ids"]:
            current = settings.docStoreConn.get(cid, index_name, [doc.kb_id])
            if not current or str(current.get("doc_id", current.get("document_id"))) != req["doc_id"]:
                return get_data_error_result(retmsg="Chunk not found in this document!")
        for cid in req["chunk_ids"]:
            if not settings.docStoreConn.update({"id": cid, "doc_id": req["doc_id"]}, {"available_int": int(req["available_int"])}, index_name, doc.kb_id):
                return get_data_error_result(retmsg="Index updating failure")
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post("/rm", summary="删除文档块", deprecated=True)
def rm(request: RmChunkRequest, db: Session = Depends(get_db), user: Any = Depends(manager)) -> JSONResponse:
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
                doc = _locked_document(db, req["doc_id"])
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

        doc = _locked_document(db, req["doc_id"])
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
                deleted_count = settings.docStoreConn.delete(condition=condition, index_name=collection_name, dataset_id=kb.id)
            else:
                deleted_count = settings.docStoreConn.delete(condition, collection_name, kb.id)
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


@router.post("/create", summary="创建文档块", deprecated=True)
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
    d = {"id": chunk_id, "content_ltks": rag_tokenizer.tokenize(req["content_with_weight"]), "content_with_weight": req["content_with_weight"]}
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

        v, c = embd_mdl.encode([doc.name, req["content_with_weight"] if not d["question_kwd"] else "\n".join(d["question_kwd"])])
        v = 0.1 * v[0] + 0.9 * v[1]

        # 同时存储到标准vector字段和维度特定字段
        vector_dim = len(v.tolist())
        d["vector"] = v.tolist()  # 始终保存到标准vector字段以保持兼容性
        d[f"q_{vector_dim}_vec"] = v.tolist()  # 同时保存到维度特定字段

        insert_source_chunks(db.get_bind(), [d], search.index_name_one(tenant_id, kb.name), kb.id)

        if image_base64:
            store_chunk_image(doc.kb_id, chunk_id, base64.b64decode(image_base64))

        DocumentService.increment_chunk_num(db, doc.id, doc.kb_id, c, 1, 0)
        return get_json_result(data={"chunk_id": chunk_id, "image_id": d.get("img_id", "")})
    except Exception as e:
        return server_error_response(e)

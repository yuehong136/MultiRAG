# coding=utf-8
"""
@project: multirag
@Author：龙
@file： chunk_app.py
@date：2024/7/30 13:56
@desc:
"""
import datetime
import json
import logging

import numpy as np
import xxhash
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.db.services.dialog_service import keyword_extraction, label_question
from core.app.qa import rmPrefix, beAdoc
from core.nlp import search, rag_tokenizer
from core.utils import rmSpace
from api.db import LLMType, ParserType
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import TenantLLMService, LLMBundle
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import server_error_response, get_data_error_result
from api.db.services.document_service import DocumentService
from api import settings
from core.settings import PAGERANK_FLD
# from api.settings import RetCode, retrievaler#, kg_retrievaler
from api.utils.api_utils import get_json_result
from api.db.database import get_db
from api.apps import manager

router = APIRouter()


class ListChunkRequest(BaseModel):
    doc_id: str
    page: int | None = 1
    size: int | None = 30
    keywords: str | None = ""


class SetChunkRequest(BaseModel):
    doc_id: str
    chunk_id: str
    content_with_weight: str
    important_kwd: list[str] | None = []
    question_kwd: list[str] | None = []
    available_int: int | None = None
    tag_kwd: str | None = None
    tag_feas: str | None = None


class SwitchChunkRequest(BaseModel):
    doc_id: str
    chunk_ids: list[str]
    available_int: int


class RmChunkRequest(BaseModel):
    doc_id: str
    chunk_ids: list[str]


class CreateChunkRequest(BaseModel):
    doc_id: str
    content_with_weight: str
    question_kwd: list[str] | None = None
    important_kwd: list[str] | None = None


class RetrievalTestRequest(BaseModel):
    kb_ids: list[str]
    question: str
    page: int | None = 1
    size: int | None = 30
    doc_ids: list[str] | None
    similarity_threshold: float | None = 0.0
    vector_similarity_weight: float | None = 0.3
    top_k: int | None = 1024
    rerank_id: str | None = None
    keyword: bool | None = False


@router.post('/list', summary="列出文档块")
def list_chunk(request: ListChunkRequest, db: Session = Depends(get_db), user=Depends(manager)):
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
        # 先计算出所有问题块的总数
        query_count = {
            "doc_ids": [request.doc_id], "question": request.keywords,
            "sort": True
        }
        if "available_int" in request:
            query["available_int"] = int(request["available_int"])
            query_count["available_int"] = int(request["available_int"])
        # total = settings.retrievaler.count(query_count, search.index_name_one(tenant_id, kb.name)).total
        # sres = settings.retrievaler.search(query, search.index_name_one(tenant_id, kb.name))
        # total = settings.retrievaler.search(query_count, search.index_name_one(tenant_id, kb.name), kb_ids).total
        sres = settings.retrievaler.search(query, search.index_name_one(tenant_id, kb.name), kb_ids)
        res = {"total": sres.total, "chunks": [], "doc": doc.to_dict()}
        for id in sres.ids:
            d = {
                "chunk_id": id,
                "content_with_weight": rmSpace(sres.highlight[id]) if request.keywords and id in sres.highlight else
                sres.field[id].get(
                    "content_with_weight", ""),
                "doc_id": sres.field[id]["doc_id"],
                "docnm_kwd": sres.field[id]["docnm_kwd"],
                "important_kwd": sres.field[id].get("important_kwd", []),
                "question_kwd": sres.field[id].get("question_kwd", []),
                "img_id": sres.field[id].get("img_id", ""),
                "available_int": int(sres.field[id].get("available_int", 1)),
                "positions": sres.field[id].get("position_int", [])
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
                                   retcode=settings.RetCode.DATA_ERROR)
        return server_error_response(e)


@router.get('/get', summary="获取文档块")
def get(chunk_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取文档块

    该接口用于获取指定ID的文档块信息。

    参数:
    - chunk_id: 文档块的唯一标识符
    - db: 数据库会话对象
    - user: 当前用户对象

    返回:
    - 成功时返回包含文档块详细信息的JSON结果
    - 失败时返回错误信息
    """
    try:
        tenants = UserTenantService.query(db, user_id=user.id)
        if not tenants:
            return get_data_error_result(retmsg="Tenant not found!")

        tenant_id = tenants[0].tenant_id
        kb_ids = KnowledgebaseService.get_kb_ids(db, tenant_id)
        chunk = settings.docStoreConn.get(chunk_id, search.index_name(db, tenant_id), kb_ids)
        if chunk is None:
            return server_error_response(Exception("Chunk not found"))
        res = ELASTICSEARCH.get(
            chunk_id, search.index_name(
                tenants[0].tenant_id))
        if not res.get("found"):
            return server_error_response("Chunk not found")
        id = res["_id"]
        res = res["_source"]
        res["chunk_id"] = id
        k = []
        for n in chunk.keys():
            if re.search(r"(_vec$|_sm_|_tks|_ltks)", n):
                k.append(n)
        for n in k:
            del chunk[n]

        return get_json_result(data=chunk)
    except Exception as e:
        if str(e).find("NotFoundError") >= 0:
            return get_json_result(data=False, retmsg='Chunk not found!',
                                   retcode=settings.RetCode.DATA_ERROR)
        return server_error_response(e)


@router.post('/set', summary="设置文档块")
def set(request: SetChunkRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/set` 设置文档块接口

**功能描述**:
此接口用于设置和更新文档块，支持处理内容的分词、关键词提取、问答模式验证以及向量计算，并将结果存储到数据库中。

---

### 请求体 (Request Body)

| 字段                  | 类型          | 必填 | 描述                                                                                 |
|-----------------------|---------------|------|--------------------------------------------------------------------------------------|
| `chunk_id`            | `string`     | 是   | 文档块的唯一标识符。                                                                 |
| `doc_id`              | `string`     | 是   | 所属文档的唯一标识符。                                                               |
| `content_with_weight` | `string`     | 是   | 带权重的内容字符串，用于分词和向量计算。                                             |
| `important_kwd`       | `list[str]`  | 否   | 重要关键词列表，用于额外的分词和向量计算。                                            |
| `question_kwd`        | `list[str]`  | 否   | 问题关键词列表，用于问答模式或补充向量计算。                                          |
| `available_int`       | `int`        | 否   | 可用性标记，用于记录当前文档块的可用状态。                                            |

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

- **400: Q&A 格式错误**
    - **描述**: 当文档块的内容不符合问答模式的要求时返回此错误。
    - **示例**:
        ```json
        {
            "retcode": 400,
            "retmsg": "Q&A must be separated by TAB/ENTER key."
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

1. 从请求体提取内容并解析关键词。
    - 对 `content_with_weight` 执行分词。
    - 分析重要关键词 (`important_kwd`) 和问题关键词 (`question_kwd`)。
2. 验证文档块所属的租户 (`tenant_id`) 和文档是否存在。
3. 判断文档解析类型 (`parser_id`)，处理问答模式的特殊逻辑：
    - 验证问答内容是否由 TAB 或 ENTER 分隔。
    - 检测问答内容的语言特性（是否包含中文）。
4. 通过向量模型生成文档块的语义向量。
5. 更新数据库：
    - 根据主键 (`chunk_id`) 更新或插入文档块数据。
    - 将内容、关键词及向量信息存储到对应的知识库中。

---

### 注意事项

- **问答模式验证**:
    - 文档解析类型为 `QA` 时，`content_with_weight` 必须包含两个部分（问题和答案），通过 TAB 或 ENTER 分隔。
    - 自动判断问答内容的语言，决定是否使用特定的分词逻辑。
- **向量计算**:
    - 文档块的向量由内容和关键词生成。
    - 支持加权计算：如果 `question_kwd` 存在，则使用其内容替代默认的内容生成逻辑。
    - 当前向量字段名为 `vector`，未来可能支持动态字段名。
- **数据库更新**:
    - 更新操作基于主键 `chunk_id`。
    - 确保知识库名称 (`kb.name`) 与租户信息一致。

---

### 示例请求

#### 请求体:
```json
{
    "chunk_id": "12345",
    "doc_id": "67890",
    "content_with_weight": "问题：人工智能是什么？\n答案：人工智能是计算机科学的一个分支。",
    "important_kwd": ["人工智能", "计算机科学"],
    "question_kwd": ["人工智能是什么", "计算机科学"],
    "available_int": 1
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

- **错误响应 (问答格式错误)**:
```json
{
    "retcode": 400,
    "retmsg": "Q&A must be separated by TAB/ENTER key."
}
```
    """
    d = {
        "pk": request.chunk_id,
        "content_with_weight": request.content_with_weight,
        "content_ltks": rag_tokenizer.tokenize(request.content_with_weight),
        "content_sm_ltks": rag_tokenizer.fine_grained_tokenize(rag_tokenizer.tokenize(request.content_with_weight)),
    }
    important_kwd = request.important_kwd if request.important_kwd is not None else []
    d["important_kwd"] = important_kwd
    d["important_tks"] = rag_tokenizer.tokenize(" ".join(important_kwd)) if important_kwd else ""

    question_kwd = request.question_kwd if request.question_kwd is not None else []
    d["question_kwd"] = question_kwd
    d["question_tks"] = rag_tokenizer.tokenize("\n".join(question_kwd)) if question_kwd else ""

    if request.tag_kwd is not None:
        d["tag_kwd"] = request.tag_kwd

    if request.tag_feas is not None:
        d["tag_feas"] = request.tag_feas

    if request.available_int is not None:
        d["available_int"] = request.available_int

    try:
        tenant_id = DocumentService.get_tenant_id(db, request.doc_id)
        if not tenant_id:
            return get_data_error_result(retmsg="Tenant not found!")

        embd_id = DocumentService.get_embd_id(db, request.doc_id)
        embd_mdl = LLMBundle(db, tenant_id, LLMType.EMBEDDING, embd_id)

        doc = DocumentService.get_by_id(db, request.doc_id)
        if not doc:
            return get_data_error_result(retmsg="Document not found!")

        if doc.parser_id == ParserType.QA:
            arr = [
                t for t in re.split(
                    r"[\n\t]",
                    request.content_with_weight) if len(t) > 1]
            q, a = rmPrefix(arr[0]), rmPrefix("\n".join(arr[1:]))
            d = beAdoc(d, arr[0], arr[1], not any(
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
        update_condition = {"pk": request.chunk_id}  # 主键查询条件
        kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
        settings.docStoreConn.update(update_condition, d, search.index_name_one(tenant_id, kb.name), doc.kb_id)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post('/switch', summary="切换文档块状态")
def switch(request: SwitchChunkRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    切换文档块状态

    该接口用于切换文档块的可用状态。

    参数:
    - request: SwitchChunkRequest对象，包含文档块ID和新状态
        - doc_id: 文档的唯一标识符
        - chunk_ids: 文档块的唯一标识符列表
        - available_int: 新的可用状态
    - db: 数据库会话对象
    - user: 当前用户对象

    返回:
    - 成功时返回包含操作结果的JSON结果
    - 失败时返回错误信息
    """
    try:
        tenant_id = DocumentService.get_tenant_id(db, request.doc_id)
        if not tenant_id:
            return get_data_error_result(retmsg="Tenant not found!")
        if not ELASTICSEARCH.upsert([{"id": i, "available_int": request.available_int} for i in request.chunk_ids],
                                    search.index_name(tenant_id)):
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
        doc = DocumentService.get_by_id(db, req["doc_id"])
        if not doc:
            return get_data_error_result(retmsg="Document not found!")
        kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
        if not kb:
            return get_data_error_result(retmsg="KnowledgeBase not found!")

        if not settings.docStoreConn.delete(collection_name=search.index_name_one(kb.tenant_id, kb.name), ids=req["chunk_ids"]):
            return get_data_error_result(retmsg="Index updating failure")
        deleted_chunk_ids = req["chunk_ids"]
        chunk_number = len(deleted_chunk_ids)
        DocumentService.decrement_chunk_num(db, doc.id, doc.kb_id, 1, chunk_number, 0)
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
    d = {"pk": chunk_id, "content_ltks": rag_tokenizer.tokenize(req["content_with_weight"]),
         "content_with_weight": req["content_with_weight"]}
    d["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(d["content_ltks"])
    d["important_kwd"] = req.get("important_kwd", [])
    d["important_tks"] = rag_tokenizer.tokenize(" ".join(req.get("important_kwd", [])))
    d["question_kwd"] = req.get("question_kwd", [])
    d["question_tks"] = rag_tokenizer.tokenize("\n".join(req.get("question_kwd", [])))
    d["create_time"] = str(datetime.datetime.now()).replace("T", " ")[:19]
    d["create_timestamp_flt"] = datetime.datetime.now().timestamp()

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

        tenant_id = DocumentService.get_tenant_id(db, req["doc_id"])
        if not tenant_id:
            return get_data_error_result(retmsg="Tenant not found!")

        kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
        if not kb:
            return get_data_error_result(retmsg="Knowledgebase not found!")
        if kb.pagerank is not None:
            d["pagerank_fea"] = kb.pagerank

        embd_id = DocumentService.get_embd_id(db, req["doc_id"])
        embd_mdl = LLMBundle(db, tenant_id, LLMType.EMBEDDING.value, embd_id)

        v, c = embd_mdl.encode(
            [doc.name, req["content_with_weight"] if not d["question_kwd"] else "\n".join(d["question_kwd"])])
        v = 0.1 * v[0] + 0.9 * v[1]

        # 同时存储到标准vector字段和维度特定字段
        vector_dim = len(v.tolist())
        d["vector"] = v.tolist()  # 始终保存到标准vector字段以保持兼容性
        d[f"q_{vector_dim}_vec"] = v.tolist()  # 同时保存到维度特定字段

        settings.docStoreConn.insert(search.index_name_one(tenant_id, kb.name), [d])

        DocumentService.increment_chunk_num(
            db, doc.id, doc.kb_id, c, 1, 0)
        return get_json_result(data={"chunk_id": chunk_id})
    except Exception as e:
        return server_error_response(e)


@router.post('/retrieval_test', summary="检索测试")
def retrieval_test(request: RetrievalTestRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    检索测试

    该接口用于执行检索测试，返回检索结果。

    参数:
    - request: RetrievalTestRequest对象，包含检索参数
        - kb_ids: 知识库的唯一标识符
        - question: 检索问题
        - page: 页码，默认值为1
        - size: 每页的结果数，默认值为30
        - doc_ids: 文档ID列表，默认值为空列表
        - similarity_threshold: 相似度阈值，默认值为0.2
        - vector_similarity_weight: 向量相似度权重，默认值为0.3
        - top_k: 最大检索条目数，默认值为1024
        - rerank_id: 重新排序的ID，默认值为空字符串
        - keyword: 是否进行关键字提取，默认值为False
    - db: 数据库会话对象
    - user: 当前用户对象

    返回:
    - 成功时返回包含检索结果的JSON结果
    - 失败时返回错误信息
    """
    req = request.model_dump()
    try:
        tenants = UserTenantService.query(db, user_id=user.id)
        for kid in req["kb_ids"]:
            for tenant in tenants:
                if KnowledgebaseService.query(
                        db, tenant_id=tenant.tenant_id, id=kid):
                    break
            else:
                return get_json_result(
                    data=False, retmsg=f'Only owner of knowledgebase authorized for this operation.',
                    retcode=settings.RetCode.OPERATING_ERROR)

        kb = KnowledgebaseService.get_by_id(db, request.kb_ids[0])
        if not kb:
            return get_data_error_result(retmsg="Knowledgebase not found!")

        embd_mdl = LLMBundle(db, kb.tenant_id, LLMType.EMBEDDING.value, llm_name=kb.embd_id)

        rerank_mdl = None
        if req.get("rerank_id"):
            rerank_mdl = LLMBundle(db, kb.tenant_id, LLMType.RERANK.value, llm_name=req["rerank_id"])

        question = req["question"]
        if req.get("keyword", False):
            chat_mdl = LLMBundle(db, kb.tenant_id, LLMType.CHAT)
            question += keyword_extraction(chat_mdl, question)
        filter_exp = ""
        labels = label_question(db, question, [kb])
        retr = settings.retrievaler if kb.parser_id != ParserType.KG else settings.kg_retrievaler

        # 在embd_mdl定义后添加以下代码以获取向量维度
        sample_vec, _ = embd_mdl.encode(["测试文本"])
        vector_dim = None
        if isinstance(sample_vec, np.ndarray) and sample_vec.ndim > 1:
            vector_dim = sample_vec.shape[1]
        elif isinstance(sample_vec, list) and len(sample_vec) > 0:
            if isinstance(sample_vec[0], np.ndarray) or isinstance(sample_vec[0], list):
                vector_dim = len(sample_vec[0])
            elif isinstance(sample_vec[0], (float, np.float32, np.float64)):
                vector_dim = len(sample_vec)

        # 如果获取到了向量维度，可以在过滤条件中包含这个信息
        if vector_dim:
            logging.info(f"检索使用向量维度: {vector_dim}")

        # 当调用retrieval函数时，传递维度信息
        ranks = retr.retrieval(question, filter_exp, embd_mdl, kb.tenant_id, [kb.name], req["page"],
                               req["size"], req["similarity_threshold"], req["vector_similarity_weight"],
                               req["top_k"], req["doc_ids"], rerank_mdl=rerank_mdl, highlight=req.get("highlight"),
                               rank_feature=labels)
        for c in ranks["chunks"]:
            c.pop("vector", None)
        ranks["labels"] = labels

        return get_json_result(data=ranks)
    except Exception as e:
        if str(e).find("not_found") > 0:
            return get_json_result(data=False, retmsg=f'No chunk found! Check the chunk status please!',
                                   retcode=settings.RetCode.DATA_ERROR)
        return server_error_response(e)


@router.get('/knowledge_graph')
def knowledge_graph(doc_id, db: Session = Depends(get_db), user=Depends(manager)):
    req = {
        "doc_ids":[doc_id],
        "knowledge_graph_kwd": ["graph", "mind_map"]
    }
    tenant_id = DocumentService.get_tenant_id(db, doc_id)
    kb_names = KnowledgebaseService.get_kb_ids(db, tenant_id)
    # todo 因为search参数里缺少knowledge_graph_kwd ，所以暂时无法使用，后续需要调整milvus集合创建的schema
    sres = settings.retrievaler.search(req, search.index_name_one(tenant_id, kb_names))
    obj = {"graph": {}, "mind_map": {}}
    for id in sres.ids[:2]:
        ty = sres.field[id]["knowledge_graph_kwd"]
        try:
            content_json = json.loads(sres.field[id]["content_with_weight"])
        except Exception:
            continue

        if ty == 'mind_map':
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

        obj[ty] = content_json

    return get_json_result(data=obj)


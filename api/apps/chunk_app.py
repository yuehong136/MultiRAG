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
import traceback

import hashlib
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.app.qa import rmPrefix, beAdoc
from core.nlp import search, rag_tokenizer, keyword_extraction
# from core.utils.es_conn import ELASTICSEARCH
from core.utils.milvus_conn import MILVUS_CONNECTION
from core.utils import rmSpace
from api.db import LLMType, ParserType
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import TenantLLMService, LLMBundle
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import server_error_response, get_data_error_result
from api.db.services.document_service import DocumentService
from api.settings import RetCode, retrievaler#, kg_retrievaler
from api.utils.api_utils import get_json_result
from api.db.database import get_db
from api.apps import manager

router = APIRouter()


class ListChunkRequest(BaseModel):
    doc_id: str
    page: Optional[int] = 1
    size: Optional[int] = 30
    keywords: Optional[str] = ""


class SetChunkRequest(BaseModel):
    doc_id: str
    chunk_id: str
    content_with_weight: str
    important_kwd: List[str]
    available_int: Optional[int] = None


class SwitchChunkRequest(BaseModel):
    doc_id: str
    chunk_ids: List[str]
    available_int: int


class RmChunkRequest(BaseModel):
    doc_id: str
    chunk_ids: List[str]


class CreateChunkRequest(BaseModel):
    doc_id: str
    content_with_weight: str
    important_kwd: Optional[List[str]] = []


class RetrievalTestRequest(BaseModel):
    kb_id: str
    question: str
    page: Optional[int] = 1
    size: Optional[int] = 30
    doc_ids: Optional[List[str]] = []
    similarity_threshold: Optional[float] = 0.0
    vector_similarity_weight: Optional[float] = 0.3
    top_k: Optional[int] = 1024
    rerank_id: Optional[str] = None
    keyword: Optional[bool] = False


@router.post('/list', summary="列出文档块")
async def list_chunk(request: ListChunkRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    列出文档块

    该接口用于列出指定文档的所有块。

    参数:
    - request: ListChunkRequest对象，包含文档ID、分页参数和关键字
        - doc_id: 文档的唯一标识符
        - page: 页码，默认值为1
        - size: 每页的块数，默认值为30
        - keywords: 搜索关键字，默认值为空字符串
    - db: 数据库会话对象
    - user: 当前用户对象

    返回:
    - 成功时返回包含文档块信息的JSON结果
    - 失败时返回错误信息
    """
    try:
        tenant_id = DocumentService.get_tenant_id(db, request.doc_id)
        if not tenant_id:
            return get_data_error_result(retmsg="Tenant not found!")
        doc = DocumentService.get_by_id(db, request.doc_id)
        if not doc:
            return get_data_error_result(retmsg="Document not found!")
        kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
        query = {
            "doc_ids": [request.doc_id], "page": request.page, "size": request.size, "question": request.keywords,
            "sort": True
        }
        sres = retrievaler.search(query, search.index_name(tenant_id, kb.name))
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
                "img_id": sres.field[id].get("img_id", ""),
                "available_int": sres.field[id].get("available_int", 1),
                "positions": sres.field[id].get("position_int", "").split("\t")
            }
            if len(d["positions"]) % 5 == 0:
                poss = []
                for i in range(0, len(d["positions"]), 5):
                    poss.append([float(d["positions"][i]), float(d["positions"][i + 1]), float(d["positions"][i + 2]),
                                 float(d["positions"][i + 3]), float(d["positions"][i + 4])])
                d["positions"] = poss
            res["chunks"].append(d)
        return get_json_result(data=res)
    except Exception as e:
        if str(e).find("not_found") > 0:
            return get_json_result(data=False, retmsg=f'No chunk found!',
                                   retcode=RetCode.DATA_ERROR)
        return server_error_response(e)


@router.get('/get', summary="获取文档块")
async def get(chunk_id: str, db: Session = Depends(get_db), user=Depends(manager)):
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
        res = ELASTICSEARCH.get(
            chunk_id, search.index_name(
                tenants[0].tenant_id))
        if not res.get("found"):
            return server_error_response("Chunk not found")
        id = res["_id"]
        res = res["_source"]
        res["chunk_id"] = id
        k = []
        for n in res.keys():
            if re.search(r"(_vec$|_sm_|_tks|_ltks)", n):
                k.append(n)
        for n in k:
            del res[n]

        return get_json_result(data=res)
    except Exception as e:
        if str(e).find("NotFoundError") >= 0:
            return get_json_result(data=False, retmsg=f'Chunk not found!',
                                   retcode=RetCode.DATA_ERROR)
        return server_error_response(e)


@router.post('/set', summary="设置文档块")
async def set(request: SetChunkRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    设置文档块

    该接口用于创建或更新文档块信息。

    参数:
    - request: SetChunkRequest对象，包含文档块的配置信息
        - doc_id: 文档的唯一标识符
        - chunk_id: 文档块的唯一标识符
        - content_with_weight: 文档块的内容和权重
        - important_kwd: 重要关键字列表
        - available_int: 可用状态，默认值为None
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

        embd_id = DocumentService.get_embd_id(db, request.doc_id)
        embd_mdl = LLMBundle(db, tenant_id, LLMType.EMBEDDING, embd_id)

        doc = DocumentService.get_by_id(db, request.doc_id)
        if not doc:
            return get_data_error_result(retmsg="Document not found!")

        d = {
            "id": request.chunk_id,
            "content_with_weight": request.content_with_weight,
            "content_ltks": rag_tokenizer.tokenize(request.content_with_weight),
            "content_sm_ltks": rag_tokenizer.fine_grained_tokenize(rag_tokenizer.tokenize(request.content_with_weight)),
            "important_kwd": request.important_kwd,
            "important_tks": rag_tokenizer.tokenize(" ".join(request.important_kwd))
        }
        if request.available_int is not None:
            d["available_int"] = request.available_int

        if doc.parser_id == ParserType.QA:
            arr = [
                t for t in re.split(
                    r"[\n\t]",
                    request.content_with_weight) if len(t) > 1]
            if len(arr) != 2:
                return get_data_error_result(
                    retmsg="Q&A must be separated by TAB/ENTER key.")
            q, a = rmPrefix(arr[0]), rmPrefix(arr[1])
            d = beAdoc(d, arr[0], arr[1], not any(
                [rag_tokenizer.is_chinese(t) for t in q + a]))

        v, c = embd_mdl.encode([doc.name, request.content_with_weight])
        v = 0.1 * v[0] + 0.9 * v[1] if doc.parser_id != ParserType.QA else v[1]
        d["q_%d_vec" % len(v)] = v.tolist()
        ELASTICSEARCH.upsert([d], search.index_name(tenant_id))
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post('/switch', summary="切换文档块状态")
async def switch(request: SwitchChunkRequest, db: Session = Depends(get_db), user=Depends(manager)):
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
async def rm(request: RmChunkRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    删除文档块

    该接口用于删除指定的文档块。

    参数:
    - request: RmChunkRequest对象，包含要删除的文档块ID
        - doc_id: 文档的唯一标识符
        - chunk_ids: 要删除的文档块ID列表
    - db: 数据库会话对象
    - user: 当前用户对象

    返回:
    - 成功时返回包含操作结果的JSON结果
    - 失败时返回错误信息
    """
    try:
        if not ELASTICSEARCH.deleteByQuery(
                Q("ids", values=request.chunk_ids), search.index_name(current_user.id)):
            return get_data_error_result(retmsg="Index updating failure")
        e, doc = DocumentService.get_by_id(db, request.doc_id)
        if not e:
            return get_data_error_result(retmsg="Document not found!")
        deleted_chunk_ids = request.chunk_ids
        chunk_number = len(deleted_chunk_ids)
        DocumentService.decrement_chunk_num(db, doc.id, doc.kb_id, 1, chunk_number, 0)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post('/create', summary="创建文档块")
async def create(request: CreateChunkRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    创建文档块

    该接口用于创建新的文档块。

    参数:
    - request: CreateChunkRequest对象，包含文档块的内容和相关信息
        - doc_id: 文档的唯一标识符
        - content_with_weight: 文档块的内容和权重
        - important_kwd: 重要关键字列表，默认值为空列表
    - db: 数据库会话对象
    - user: 当前用户对象

    返回:
    - 成功时返回包含新创建文档块ID的JSON结果
    - 失败时返回错误信息
    """
    try:
        md5 = hashlib.md5()
        md5.update((request.content_with_weight + request.doc_id).encode("utf-8"))
        chunk_id = md5.hexdigest()
        d = {
            "id": chunk_id,
            "content_with_weight": request.content_with_weight,
            "content_ltks": rag_tokenizer.tokenize(request.content_with_weight),
            "content_sm_ltks": rag_tokenizer.fine_grained_tokenize(rag_tokenizer.tokenize(request.content_with_weight)),
            "important_kwd": request.important_kwd,
            "important_tks": rag_tokenizer.tokenize(" ".join(request.important_kwd)),
            "create_time": str(datetime.datetime.now()).replace("T", " ")[:19],
            "create_timestamp_flt": datetime.datetime.now().timestamp()
        }

        doc = DocumentService.get_by_id(db, request.doc_id)
        if not doc:
            return get_data_error_result(retmsg="Document not found!")
        d["kb_id"] = [doc.kb_id]
        d["docnm_kwd"] = doc.name
        d["doc_id"] = doc.id

        tenant_id = DocumentService.get_tenant_id(db, request.doc_id)
        if not tenant_id:
            return get_data_error_result(retmsg="Tenant not found!")

        embd_id = DocumentService.get_embd_id(db, request.doc_id)
        embd_mdl = LLMBundle(db, tenant_id, LLMType.EMBEDDING, embd_id)

        v, c = embd_mdl.encode([doc.name, request.content_with_weight])
        v = 0.1 * v[0] + 0.9 * v[1]
        d["q_%d_vec" % len(v)] = v.tolist()
        ELASTICSEARCH.upsert([d], search.index_name(tenant_id))

        DocumentService.increment_chunk_num(
            db, doc.id, doc.kb_id, c, 1, 0)
        return get_json_result(data={"chunk_id": chunk_id})
    except Exception as e:
        return server_error_response(e)


@router.post('/retrieval_test', summary="检索测试")
async def retrieval_test(request: RetrievalTestRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    检索测试

    该接口用于执行检索测试，返回检索结果。

    参数:
    - request: RetrievalTestRequest对象，包含检索参数
        - kb_id: 知识库的唯一标识符
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
        for kid in req["kb_id"]:
            for tenant in tenants:
                if KnowledgebaseService.query(
                        db, tenant_id=tenant.tenant_id, id=kid):
                    break
            else:
                return get_json_result(
                    data=False, retmsg=f'Only owner of knowledgebase authorized for this operation.',
                    retcode=RetCode.OPERATING_ERROR)

        kb = KnowledgebaseService.get_by_id(db, request.kb_id)
        if not kb:
            return get_data_error_result(retmsg="Knowledgebase not found!")

        embd_mdl = LLMBundle(db, db, kb.tenant_id, LLMType.EMBEDDING.value, llm_name=kb.embd_id)

        rerank_mdl = None
        if req.get("rerank_id"):
            rerank_mdl = LLMBundle(kb.tenant_id, LLMType.RERANK.value, llm_name=req["rerank_id"])

        question = req["question"]
        if req.get("keyword", False):
            chat_mdl = LLMBundle(db, kb.tenant_id, LLMType.CHAT)
            question += keyword_extraction(chat_mdl, question)
        filter_exp = ""
        kb = KnowledgebaseService.get_by_id(db, req["kb_id"])
        ranks = retrievaler.retrieval(question, filter_exp, embd_mdl, kb.tenant_id, kb.name, req["page"], req["size"],
                                      req["similarity_threshold"], req["vector_similarity_weight"], req["top_k"],
                                      req["doc_ids"], rerank_mdl=rerank_mdl)
        for c in ranks["chunks"]:
            if "vector" in c:
                del c["vector"]

        return get_json_result(data=ranks)
    except Exception as e:
        if str(e).find("not_found") > 0:
            return get_json_result(data=False, retmsg=f'No chunk found! Check the chunk status please!',
                                   retcode=RetCode.DATA_ERROR)
        return server_error_response(e)

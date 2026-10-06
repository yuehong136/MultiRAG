"""Dataset retrieval and document graph orchestration for REST consumers."""

import asyncio
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, Search, User
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name, get_tenant_default_model_by_type
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.knowledgebase_service import EmbeddingModelMismatchError, KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.utils.dataset_search import SearchDatasetRequest
from api.utils.reference_metadata import enrich_reference_metadata_async, resolve_reference_metadata_preferences
from common import settings
from common.constants import LLMType, RetCode, StatusEnum
from common.doc_store.doc_store_base import OrderByExpr
from common.metadata_utils import apply_meta_data_filter
from core.app.tag import label_question
from core.nlp import search
from core.prompts.generator import cross_languages, keyword_extraction


async def _bundle(db: AsyncSession, tenant_id: str, model_type: LLMType, *, model_id: int | None = None, name: str | None = None) -> LLMBundle:
    def build(s: Session) -> LLMBundle:
        if model_id:
            config = get_model_config_by_id(s, model_id)
        elif name:
            config = get_model_config_by_type_and_name(s, tenant_id, model_type.value, name)
        else:
            config = get_tenant_default_model_by_type(s, tenant_id, model_type)
        bundle = LLMBundle(s, tenant_id, config)
        # The sync facade must not escape run_sync or roll back ORM objects.
        bundle.db = None
        return bundle

    return await db.run_sync(build)  # TODO(async-phase4): legacy model construction


async def search_dataset(db: AsyncSession, tenant_id: str, dataset_id: str, request: SearchDatasetRequest) -> tuple[bool, Any, RetCode]:
    dataset_ids = list(dict.fromkeys(request.dataset_ids if request.dataset_ids is not None else [dataset_id]))
    if dataset_id not in dataset_ids:
        return False, "Path dataset must be included in dataset_ids.", RetCode.ARGUMENT_ERROR
    # Authorize the complete selection before reading metadata or model config.
    for selected_id in dataset_ids:
        if not await KnowledgebaseService.accessible_async(db, selected_id, tenant_id):
            return False, "No authorization.", RetCode.AUTHENTICATION_ERROR
    rows = (await db.scalars(select(Knowledgebase).where(Knowledgebase.id.in_(dataset_ids)))).all()
    by_id = {kb.id: kb for kb in rows}
    if len(by_id) != len(dataset_ids):
        return False, "Knowledgebase not found!", RetCode.DATA_ERROR
    kbs = [by_id[selected_id] for selected_id in dataset_ids]
    try:
        KnowledgebaseService.ensure_same_embedding_model(kbs)
    except EmbeddingModelMismatchError as exc:
        return False, str(exc), RetCode.DATA_ERROR
    kb = kbs[0]
    tenant_ids = list(dict.fromkeys(item.tenant_id for item in kbs))
    # KG aggregates are dataset-wide and cannot honor document predicates.
    # Saved searches may supply filters even when the request has none.
    if request.use_kg and (request.doc_ids or request.meta_data_filter or request.search_id):
        return False, "Knowledge graph enhancement cannot be combined with document filters, metadata filters, or a saved search.", RetCode.BAD_REQUEST
    metadata_filter = request.meta_data_filter or {}
    search_config: dict[str, Any] = {}
    if request.search_id:
        saved_search = await db.scalar(
            select(Search).join(User, User.id == Search.tenant_id).where(Search.id == request.search_id, Search.status == StatusEnum.VALID.value, User.status == StatusEnum.VALID.value)
        )
        if saved_search is None:
            return False, "Search app not found!", RetCode.DATA_ERROR
        # Use the same membership contract as the REST search-app detail route.
        from api.db.services.user_service import UserTenantService

        membership = await db.run_sync(lambda s: UserTenantService.get_membership(s, tenant_id=saved_search.tenant_id, user_id=tenant_id))  # TODO(async-phase4)
        if membership is None or not UserTenantService.can_access_tenant_resources(membership.role):
            return False, "No authorization.", RetCode.AUTHENTICATION_ERROR
        search_config = saved_search.search_config or {}
        metadata_filter = search_config.get("meta_data_filter", {})
    doc_ids = request.doc_ids
    question = request.question
    if metadata_filter:
        chat = None
        if metadata_filter.get("method") in ("auto", "semi_auto"):
            chat = await _bundle(db, tenant_id, LLMType.CHAT, name=search_config.get("chat_id"))

        async def read_metadata() -> dict[str, Any]:
            return await db.run_sync(lambda s: DocMetadataService.get_flatted_meta_by_kbs(s, dataset_ids))  # TODO(async-phase4)

        metas = await read_metadata()
        doc_ids = await apply_meta_data_filter(metadata_filter, metas, question, chat, doc_ids, metadata_refresher=read_metadata if metadata_filter.get("method") == "semi_auto" else None)
    if request.cross_languages:
        question = await cross_languages(kb.tenant_id, None, question, request.cross_languages)
    embedding = await _bundle(db, kb.tenant_id, LLMType.EMBEDDING, model_id=kb.tenant_embd_id, name=kb.embd_id)
    reranker = None
    if request.tenant_rerank_id or request.rerank_id:
        reranker = await _bundle(db, kb.tenant_id, LLMType.RERANK, model_id=request.tenant_rerank_id, name=request.rerank_id)
    if request.keyword:
        chat = await _bundle(db, kb.tenant_id, LLMType.CHAT)
        question += "," + await keyword_extraction(chat, question)
    labels = await db.run_sync(lambda s: label_question(s, question, kbs))  # TODO(async-phase4)
    ranks = await settings.retriever.retrieval(
        question,
        "",
        embedding,
        [item.tenant_id for item in kbs],
        [item.name for item in kbs],
        request.page,
        request.size,
        request.similarity_threshold,
        request.vector_similarity_weight,
        min(request.top_k, 2048),
        doc_ids,
        rerank_mdl=reranker,
        highlight=request.highlight,
        rank_feature=labels,
        search_mode=request.get_search_mode_dict(),
        kb_ids=dataset_ids,
    )
    if request.use_kg:
        chat = await _bundle(db, kb.tenant_id, LLMType.CHAT)
        chunk = await settings.kg_retriever.retrieval(question, tenant_ids, dataset_ids, embedding, chat)
        if chunk["content_with_weight"]:
            ranks["chunks"].insert(0, chunk)
    ranks["chunks"] = await asyncio.to_thread(settings.retriever.retrieval_by_children, ranks["chunks"], tenant_ids)
    for chunk in ranks["chunks"]:
        chunk.pop("vector", None)
    await enrich_reference_metadata_async(db, ranks["chunks"], resolve_reference_metadata_preferences(request.model_dump(), search_config))
    ranks["labels"] = labels
    # Preserve the retrieval total, not the count of the current page/children.
    return True, ranks, RetCode.SUCCESS


def _deduplicate_mind_map(node: dict[str, Any], seen: dict[str, int]) -> None:
    if "id" in node:
        original = node["id"]
        count = seen.get(original, 0)
        if count:
            node["id"] = f"{original}({count})"
        seen[original] = count + 1
    for child in node.get("children") or []:
        _deduplicate_mind_map(child, seen)


async def get_document_graph(db: AsyncSession, tenant_id: str, dataset_id: str, doc_id: str) -> tuple[bool, Any]:
    if not await KnowledgebaseService.accessible_async(db, dataset_id, tenant_id):
        return False, "No authorization."
    doc = await db.get(Document, doc_id)
    # Never resolve an index using a document belonging to another dataset.
    if doc is None or doc.kb_id != dataset_id:
        return False, "Document not found in dataset."
    kb = await db.get(Knowledgebase, dataset_id)
    index_name = search.index_name_one(kb.tenant_id, kb.name)

    def read() -> dict[str, Any]:
        obj: dict[str, Any] = {"graph": {}, "mind_map": {}}
        if not settings.docStoreConn.index_exist(index_name, dataset_id):
            return obj
        fields = ["knowledge_graph_kwd", "content_with_weight"]
        # Graph artifacts are intentionally disabled chunks; no available_int=1.
        for kind, condition in (
            ("subgraph", {"source_id": doc_id, "removed_kwd": "N"}),
            ("mind_map", {"doc_id": doc_id}),
        ):
            result = settings.docStoreConn.search(fields, [], {"kb_id": dataset_id, "knowledge_graph_kwd": [kind], **condition}, [], OrderByExpr(), 0, 1, index_name, [dataset_id])
            for field in settings.docStoreConn.get_fields(result, fields).values():
                if not isinstance(field, dict):
                    continue
                try:
                    content = json.loads(field["content_with_weight"])
                except (ValueError, TypeError, KeyError):
                    continue
                if not isinstance(content, dict):
                    continue
                if kind == "mind_map":
                    _deduplicate_mind_map(content, {})
                obj["graph" if kind == "subgraph" else kind] = content
        return obj

    return True, await asyncio.to_thread(read)

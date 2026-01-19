# coding=utf-8
"""
@project: multirag
@Author：龙
@file： document_service.py
@date：2024/8/14 11:00
@desc:
"""
import asyncio
import json
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from io import BytesIO
from typing import Any

import xxhash
from sqlalchemy.exc import NoResultFound, OperationalError
from sqlalchemy.orm import Session, aliased
from sqlalchemy import func, asc, and_, or_, select, desc as sa_desc, update

from api.constants import IMG_BASE64_PREFIX, FILE_NAME_LEN_LIMIT
from api.db import FileType, UserTenantRole, CanvasCategory
from api.db.db_models import Document, Knowledgebase, Tenant, Task, UserTenant, File2Document, File, UserCanvas, \
    User
from api.db.services.common_service import CommonService
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp, get_format_time
from api.utils.db_utils import bulk_insert_into_db
from common import settings
from common.constants import LLMType, ParserType, TaskStatus, StatusEnum, SVR_CONSUMER_GROUP_NAME, PIPELINE_SPECIAL_PROGRESS_FREEZE_TASK_TYPES
from core.nlp import search, rag_tokenizer
from core.utils.redis_conn import REDIS_CONN
from core.utils.doc_store_conn import OrderByExpr


class DocumentService(CommonService):
    model = Document

    def __init__(self):
        super().__init__(Document)

    @classmethod
    def get_cls_model_fields(cls):
        return [
            cls.model.id,
            cls.model.thumbnail,
            cls.model.kb_id,
            cls.model.parser_id,
            cls.model.pipeline_id,
            cls.model.parser_config,
            cls.model.source_type,
            cls.model.type,
            cls.model.created_by,
            cls.model.name,
            cls.model.location,
            cls.model.size,
            cls.model.token_num,
            cls.model.chunk_num,
            cls.model.progress,
            cls.model.progress_msg,
            cls.model.process_begin_at,
            cls.model.process_duration,
            cls.model.meta_fields,
            cls.model.suffix,
            cls.model.run,
            cls.model.status,
            cls.model.create_time,
            cls.model.create_date,
            cls.model.update_time,
            cls.model.update_date,
        ]

    @classmethod
    def get_list(
            cls,
            db: Session,
            kb_id,
            page_number: int,
            items_per_page: int,
            orderby: str,
            desc: bool,
            keywords: str = None,
            id: int = None,
            name: str = None,
            suffix: list = None,
            run: list = None,
            doc_ids: list = None
    ):
        # 1) 需要返回的列 —— 等价于 Peewee 的 select(*fields)
        #    确保 get_cls_model_fields() 返回的是 Column/ColumnElement 列对象，而不是字符串
        fields: list = cls.get_cls_model_fields()

        # 2) 基础查询（含 join）
        base = (
            select(*fields, UserCanvas.title)
            .select_from(cls.model)
            .join(File2Document, File2Document.document_id == cls.model.id)
            .join(File, File.id == File2Document.file_id)
            .outerjoin(UserCanvas, and_(
                cls.model.pipeline_id == UserCanvas.id,
                UserCanvas.canvas_category == CanvasCategory.DataFlow.value
            ))
            .where(cls.model.kb_id == kb_id)
        )

        # 3) 过滤
        if id is not None:
            base = base.where(cls.model.id == id)
        if name:
            base = base.where(cls.model.name == name)
        if keywords:
            # 等价于 lower(name) like %lower(keywords)%
            base = base.where(func.lower(cls.model.name).contains(keywords.lower()))

            # ilike（更直观，也能走索引策略更好）：
            # base = base.where(cls.model.name.ilike(f"%{keywords}%"))
        
        if suffix:
            base = base.where(cls.model.suffix.in_(suffix))
        if run:
            base = base.where(cls.model.run.in_(run))
        if doc_ids:
            base = base.where(cls.model.id.in_(doc_ids))

        # 4) 排序（避免与 sqlalchemy.desc 重名）
        order_col = getattr(cls.model, orderby)
        if desc:
            base = base.order_by(sa_desc(order_col))
        else:
            base = base.order_by(asc(order_col))

        # 5) 总数（不受分页影响）
        total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()

        # 6) 分页
        stmt = base.offset((page_number - 1) * items_per_page).limit(items_per_page)

        # 7) 执行并返回"字典行"（等价 Peewee 的 .dicts()）
        rows = db.execute(stmt).mappings().all()
        return [dict(r) for r in rows], total

    @classmethod
    def check_doc_health(cls, db: Session, tenant_id: str, filename):
        import os
        MAX_FILE_NUM_PER_USER = int(os.environ.get("MAX_FILE_NUM_PER_USER", 0))
        if 0 < MAX_FILE_NUM_PER_USER <= DocumentService.get_doc_count(db, tenant_id):
            raise RuntimeError("Exceed the maximum file number of a free user!")
        if len(filename.encode("utf-8")) > FILE_NAME_LEN_LIMIT:
            raise RuntimeError("Exceed the maximum length of file name!")
        return True

    @classmethod
    def get_by_kb_id(cls, db: Session, kb_id: str, page_number: int, items_per_page: int,
                     orderby: str, desc: bool, keywords: str | None,
                     run_status: list | None = None, types: list | None = None, suffix: list = None,
                     doc_ids: list | None = None) -> tuple[list[dict], int]:
        if suffix is None:
            suffix = []
        fields = cls.get_cls_model_fields()
        
        # 使用 select() 构建查询（SQLAlchemy 2.0 风格）
        base = (
            select(*fields, UserCanvas.title.label("pipeline_name"), User.nickname)
            .select_from(cls.model)
            .join(File2Document, File2Document.document_id == cls.model.id)
            .join(File, File.id == File2Document.file_id)
            .outerjoin(UserCanvas, cls.model.pipeline_id == UserCanvas.id)
            .outerjoin(User, cls.model.created_by == User.id)
            .where(cls.model.kb_id == kb_id)
        )
        
        if keywords:
            base = base.where(func.lower(cls.model.name).contains(keywords.lower()))

        if run_status:
            base = base.where(cls.model.run.in_(run_status))

        if types:
            base = base.where(cls.model.type.in_(types))

        if suffix:
            base = base.where(cls.model.suffix.in_(suffix))

        if doc_ids:
            base = base.where(cls.model.id.in_(doc_ids))

        # 计算总数
        count = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()

        # 排序
        order_col = getattr(cls.model, orderby)
        if desc:
            base = base.order_by(sa_desc(order_col))
        else:
            base = base.order_by(asc(order_col))

        # 分页
        if page_number and items_per_page:
            stmt = base.offset((page_number - 1) * items_per_page).limit(items_per_page)
        else:
            stmt = base

        # 执行并返回字典行（自动获取列名，无需手动维护）
        rows = db.execute(stmt).mappings().all()
        return [dict(r) for r in rows], count

    @classmethod
    def get_filter_by_kb_id(cls, db: Session, kb_id, keywords, run_status, types, suffix):
        """
        优化版本：使用数据库聚合查询提高性能

        returns:
        {
            "suffix": {
                "ppt": 1,
                "doxc": 2
            },
            "run_status": {
             "1": 2,
             "2": 2
            },
            "metadata": {
                "key1": {
                 "key1_value1": 1,
                 "key1_value2": 2,
                },
                "key2": {
                 "key2_value1": 2,
                 "key2_value2": 1,
                },
            }
        }, total
        where "1" => RUNNING, "2" => CANCEL
        """
        # 构建基础查询条件
        filters = [cls.model.kb_id == kb_id]

        # 添加关键词过滤
        if keywords:
            filters.append(func.lower(cls.model.name).contains(keywords.lower()))

        # 添加运行状态过滤
        if run_status:
            filters.append(cls.model.run.in_(run_status))

        # 添加类型过滤
        if types:
            filters.append(cls.model.type.in_(types))

        # 添加后缀过滤
        if suffix:
            filters.append(cls.model.suffix.in_(suffix))

        # 2) 构造“已 join”的基础 FROM（关键最小改动：select_from + join + 复用 filters）
        base_join = (
            select(cls.model.id)
            .select_from(cls.model)
            .join(File2Document, File2Document.document_id == cls.model.id)
            .join(File, File.id == File2Document.file_id)
            .where(*filters)
        )

        # 3) total：按文档去重计数，避免一文档多文件被重复计算
        total_stmt = (
            select(func.count(func.distinct(cls.model.id)))
            .select_from(cls.model)
            .join(File2Document, File2Document.document_id == cls.model.id)
            .join(File, File.id == File2Document.file_id)
            .where(*filters)
        )
        total = db.execute(total_stmt).scalar()

        # 4) suffix 分布：同理对 Document.id 去重计数
        suffix_stmt = (
            select(
                cls.model.suffix,
                func.count(func.distinct(cls.model.id)).label("count")
            )
            .select_from(cls.model)
            .join(File2Document, File2Document.document_id == cls.model.id)
            .join(File, File.id == File2Document.file_id)
            .where(*filters)
            .group_by(cls.model.suffix)
        )
        suffix_stats = db.execute(suffix_stmt).all()

        # 5) run_status 分布：同理
        run_status_stmt = (
            select(
                cls.model.run,
                func.count(func.distinct(cls.model.id)).label("count")
            )
            .select_from(cls.model)
            .join(File2Document, File2Document.document_id == cls.model.id)
            .join(File, File.id == File2Document.file_id)
            .where(*filters)
            .group_by(cls.model.run)
        )
        run_status_stats = db.execute(run_status_stmt).all()

        # 6) metadata 分布：遍历文档的 meta_fields 字段进行统计
        # 先获取符合条件的唯一文档 ID（避免对 JSON 字段使用 DISTINCT）
        doc_ids_subquery = (
            select(cls.model.id)
            .select_from(cls.model)
            .join(File2Document, File2Document.document_id == cls.model.id)
            .join(File, File.id == File2Document.file_id)
            .where(*filters)
            .distinct()
        )
        # 然后获取这些文档的 meta_fields
        meta_stmt = select(cls.model.meta_fields).where(cls.model.id.in_(doc_ids_subquery))
        meta_rows = db.scalars(meta_stmt).all()

        metadata_counter = {}
        for meta_fields in meta_rows:
            meta_fields = meta_fields or {}
            if isinstance(meta_fields, str):
                try:
                    meta_fields = json.loads(meta_fields)
                except Exception:
                    meta_fields = {}
            if not isinstance(meta_fields, dict):
                continue
            for key, value in meta_fields.items():
                values = value if isinstance(value, list) else [value]
                for vv in values:
                    if vv is None:
                        continue
                    if isinstance(vv, str) and not vv.strip():
                        continue
                    sv = str(vv)
                    if key not in metadata_counter:
                        metadata_counter[key] = {}
                    metadata_counter[key][sv] = metadata_counter[key].get(sv, 0) + 1

        # 7) 组装返回
        suffix_counter = {row.suffix: row.count for row in suffix_stats}
        run_status_counter = {str(row.run): row.count for row in run_status_stats}

        return {
            "suffix": suffix_counter,
            "run_status": run_status_counter,
            "metadata": metadata_counter,
        }, total

    @classmethod
    def count_by_kb_id(cls, db: Session, kb_id: str, keywords: str | None = None,
                       run_status: list | None = None, types: list | None = None) -> int:
        """
        根据知识库ID统计文档数量。

        参数:
        - db: 数据库会话对象，用于执行数据库查询操作。
        - kb_id: 知识库ID。
        - keywords: 可选的关键词，用于按文档名称进行模糊匹配。
        - run_status: 可选的运行状态列表，用于筛选特定运行状态的文档。
        - types: 可选的文档类型列表，用于筛选特定类型的文档。

        返回:
        - 符合条件的文档数量。
        """
        query = db.query(cls.model).filter_by(kb_id=kb_id)

        if keywords:
            query = query.filter(func.lower(cls.model.name).contains(keywords.lower()))

        if run_status:
            query = query.filter(cls.model.run.in_(run_status))

        if types:
            query = query.filter(cls.model.type.in_(types))

        count = query.count()

        return count

    @classmethod
    def get_total_size_by_kb_id(cls, db: Session, kb_id: str, keywords: str | None = None,
                               run_status: list | None = None, types: list | None = None) -> int:
        """
        根据知识库ID统计文档总大小。

        参数:
        - db: 数据库会话对象，用于执行数据库查询操作。
        - kb_id: 知识库ID。
        - keywords: 可选的关键词，用于按文档名称进行模糊匹配。
        - run_status: 可选的运行状态列表，用于筛选特定运行状态的文档。
        - types: 可选的文档类型列表，用于筛选特定类型的文档。

        返回:
        - 符合条件的文档总大小（字节）。
        """
        query = db.query(func.coalesce(func.sum(cls.model.size), 0)).filter_by(kb_id=kb_id)

        if keywords:
            query = query.filter(func.lower(cls.model.name).contains(keywords.lower()))

        if run_status:
            query = query.filter(cls.model.run.in_(run_status))

        if types:
            query = query.filter(cls.model.type.in_(types))

        return int(query.scalar()) or 0

    @classmethod
    def get_all_doc_ids_by_kb_ids(cls, db: Session, kb_ids: list[str]) -> list[dict]:
        """根据知识库ID列表批量查询所有文档ID，使用分页避免内存溢出"""
        stmt = (
            select(cls.model.id)
            .where(cls.model.kb_id.in_(kb_ids))
            .order_by(cls.model.create_time.asc())
        )

        # maybe cause slow query by deep paginate, optimize later
        offset, limit = 0, 100
        res = []

        while True:
            try:
                doc_batch = db.execute(
                    stmt.offset(offset).limit(limit)
                ).scalars().all()

                if not doc_batch:
                    break

                res.extend([{"id": doc_id} for doc_id in doc_batch])
                offset += limit
            except Exception:
                logging.exception("Failed to get document IDs for kb_ids at offset %d", offset)
                break

        return res

    @classmethod
    def get_all_docs_by_creator_id(cls, db: Session, creator_id: str) -> list[dict]:
        """根据创建者ID批量查询所有文档信息，使用分页避免内存溢出"""
        stmt = (
            select(
                cls.model.id,
                cls.model.kb_id,
                cls.model.token_num,
                cls.model.chunk_num,
                Knowledgebase.tenant_id,
                Knowledgebase.name.label('kb_name')
            )
            .join(Knowledgebase, Knowledgebase.id == cls.model.kb_id)
            .where(cls.model.created_by == creator_id)
            .order_by(cls.model.create_time.asc())
        )

        # maybe cause slow query by deep paginate, optimize later
        offset, limit = 0, 100
        res = []

        while True:
            try:
                doc_batch = db.execute(
                    stmt.offset(offset).limit(limit)
                ).all()

                if not doc_batch:
                    break

                res.extend([
                    {
                        "id": doc.id,
                        "kb_id": doc.kb_id,
                        "token_num": doc.token_num,
                        "chunk_num": doc.chunk_num,
                        "tenant_id": doc.tenant_id,
                        "kb_name": doc.kb_name
                    }
                    for doc in doc_batch
                ])
                offset += limit
            except Exception:
                logging.exception("Failed to get documents for creator_id=%s at offset %d", creator_id, offset)
                break

        return res

    @classmethod
    def preview_document_chunks(
        cls,
        db: Session,
        doc_id: str,
        parser_config_override: dict | None = None,
        limit: int | None = None,
        override_parser_id: str | None = None,
    ) -> list[dict | str]:
        """
        仅执行文档切片，不进行向量化/入库，返回切片列表（可能包含元数据）。

        - 根据文档的 parser_id 与 parser_config，调用对应 parser 的 chunk() 实现
        - 返回格式：若 parser 返回 dict 列表（包含页码等元数据），则保留完整结构；否则返回字符串列表
        - 不修改数据库状态，不写入向量库
        """
        # 基础校验
        doc = cls.get_by_id(db, doc_id)
        if not doc:
            raise LookupError("Document not found")

        # 读取租户/语言/解析配置
        chunking_cfg = cls.get_chunking_config(db, doc_id)
        if not chunking_cfg:
            raise LookupError("Chunking config not found")

        tenant_id = chunking_cfg.get("tenant_id")
        language = chunking_cfg.get("language") or "Chinese"
        parser_id = doc.parser_id
        filename = doc.name

        # 读取文件二进制
        from api.db.services.file2document_service import File2DocumentService
        bucket, name = File2DocumentService.get_storage_address(db, doc_id=doc_id)
        file_bin = settings.STORAGE_IMPL.get(bucket, name)

        # 合并解析配置
        from api.utils.api_utils import get_parser_config

        base_cfg = get_parser_config(parser_id, doc.parser_config)
        if parser_config_override:
            # 递归合并，以覆盖为主
            def _deep_merge(a: dict, b: dict) -> dict:
                for k, v in (b or {}).items():
                    if isinstance(v, dict) and isinstance(a.get(k), dict):
                        _deep_merge(a[k], v)
                    else:
                        a[k] = v
                return a

            base_cfg = _deep_merge(base_cfg or {}, parser_config_override or {})

        # 解析页区间（优先 parser_config），兼容 pdf/table/文本等
        effective_from = 0
        effective_to = 100000
        try:
            if isinstance(base_cfg, dict):
                if "from_page" in base_cfg:
                    effective_from = int(base_cfg.get("from_page", 0))
                if "to_page" in base_cfg:
                    effective_to = int(base_cfg.get("to_page", 100000))
                # 若提供 pages 列表，则按其最小/最大范围覆盖
                pages = base_cfg.get("pages")
                if isinstance(pages, list) and pages:
                    try:
                        starts = [int(p[0]) for p in pages if isinstance(p, (list, tuple)) and len(p) >= 1]
                        ends = [int(p[1]) for p in pages if isinstance(p, (list, tuple)) and len(p) >= 2]
                        if starts:
                            effective_from = min(effective_from, min(starts) - 1)
                        if ends:
                            effective_to = max(effective_to, max(ends))
                    except Exception:
                        pass
        except Exception:
            effective_from, effective_to = 0, 100000

        # 选择解析器模块（支持用户覆盖，且校验文件类型允许列表）
        module, parser_id = cls._resolve_parser_for_filename(filename, override_parser_id or parser_id)

        # 空回调
        def _noop(prog=None, msg=""):
            return None

        # 执行切片
        result = module.chunk(
            doc.name,
            binary=file_bin,
            from_page=effective_from,
            to_page=effective_to,
            lang=language,
            callback=_noop,
            parser_config=base_cfg or {},
            tenant_id=tenant_id,
        )

        # 统一为chunk列表（保留元数据）
        chunks_list: list[dict | str] = []
        if isinstance(result, list):
            if not result:
                chunks_list = []
            else:
                first = result[0]
                if isinstance(first, dict) and "content_with_weight" in first:
                    # 保留完整的chunk对象，包含页码等元数据
                    chunks_list = result
                elif isinstance(first, str):
                    chunks_list = result
                else:
                    chunks_list = [str(x) for x in result]
        else:
            chunks_list = [str(result)]

        if isinstance(limit, int) and limit > 0:
            chunks_list = chunks_list[:limit]
        return chunks_list

    @classmethod
    async def preview_document_chunks_batched(
        cls,
        db: Session,
        doc_id: str | None = None,
        parser_config_override: dict | None = None,
        batch_size: int = 50,
        batch_id: str | None = None,
        session_ttl: int = 1800,
        override_parser_id: str | None = None,
        batch_index: int | None = None,
        tenant_id: str | None = None,
    ) -> dict:
        """
        仅切片预览的批次化接口：
        - 首次调用（无 batch_id）：计算切片、创建预览会话，返回首批数据与 batch_id。
        - 后续调用（带 batch_id）：从会话中读取下一批数据，直到结束删除会话。
        - 会话存储于 Redis，TTL 默认 30 分钟。
        返回：{"batch_id", "chunks", "count", "total", "has_more"}
        """
        import json

        # 优先：若提供了 batch_id，则尝试恢复会话（支持仅凭 batch_id 续取）
        session_key = None
        session = None
        if batch_id:
            session_key = f"preview:session:{batch_id}"
            try:
                payload = REDIS_CONN.get(session_key)
                if payload:
                    session = json.loads(payload)
            except Exception:
                session = None

        # 如会话存在且未提供 doc_id，则直接使用会话续取
        if session and not doc_id:
            # 规范 batch_size：优先使用请求值，其次会话内保存的 batch_size，最后默认 50
            try:
                bs = int(batch_size) if batch_size is not None else int(session.get("batch_size", 50))
                if bs <= 0:
                    bs = int(session.get("batch_size", 50)) or 50
            except Exception:
                bs = int(session.get("batch_size", 50)) if session.get("batch_size") else 50

            start = int(session.get("offset", 0))
            # 支持渐进式解析
            current_chunks = session.get("chunks", [])
            current_total = len(current_chunks)
            parsing_status = session.get("status", "completed")
            estimated_total = int(session.get("estimated_total", current_total))
            
            if isinstance(batch_index, int) and batch_index >= 0:
                start = min(batch_index * bs, current_total)
            end = min(start + bs, current_total)
            batch = current_chunks[start:end]
            
            # 关键修复：如果batch为空且还在解析中，等待直到有新数据
            if not batch and parsing_status == "parsing":
                import asyncio
                import logging
                logging.info(f"[preview_chunks] {batch_id} - offset已达当前总数，等待后台解析新数据...")
                
                check_interval = 0.5
                while True:
                    await asyncio.sleep(check_interval)
                    
                    try:
                        payload = REDIS_CONN.get(session_key)
                        if not payload:
                            logging.warning(f"[preview_chunks] {batch_id} - 会话丢失")
                            break
                        
                        temp_session = json.loads(payload)
                        temp_chunks = temp_session.get("chunks", [])
                        temp_total = len(temp_chunks)
                        temp_status = temp_session.get("status", "completed")
                        
                        # 如果有新数据，更新session并退出等待
                        if temp_total > current_total:
                            session = temp_session
                            current_chunks = temp_chunks
                            current_total = temp_total
                            parsing_status = temp_status
                            batch = current_chunks[start:min(start + bs, current_total)]
                            logging.info(f"[preview_chunks] {batch_id} - 新数据就绪，从{start}返回{len(batch)}个chunks")
                            break
                        
                        # 如果解析完成但仍无新数据，退出
                        if temp_status == "completed":
                            session = temp_session
                            parsing_status = temp_status
                            logging.info(f"[preview_chunks] {batch_id} - 解析完成，无更多数据")
                            break
                        
                        # 如果解析失败，退出
                        if temp_status == "error":
                            session = temp_session
                            parsing_status = temp_status
                            logging.error(f"[preview_chunks] {batch_id} - 解析失败")
                            break
                    except Exception as e:
                        logging.error(f"[preview_chunks] {batch_id} - 等待新数据失败: {e}")
                        await asyncio.sleep(1)
                
                # 重新计算end（可能有新数据了）
                end = min(start + bs, current_total)
                batch = current_chunks[start:end]
            
            # 提取纯文本（用户只需要文本，不需要元数据）
            batch_texts = []
            for chunk in batch:
                if isinstance(chunk, dict):
                    batch_texts.append(chunk.get("content_with_weight", ""))
                elif isinstance(chunk, str):
                    batch_texts.append(chunk)
                else:
                    batch_texts.append(str(chunk))
            
            has_more = (end < current_total) or (parsing_status == "parsing")
            current_batch_index = (start // bs) if bs > 0 else 0
            
            if parsing_status == "completed":
                total_batches = (current_total + bs - 1) // bs if bs > 0 else 0
            else:
                total_batches = (estimated_total + bs - 1) // bs if bs > 0 and estimated_total > 0 else None

            # 顺序模式推进 offset；并发批次模式在最后一批时清理会话
            if batch_index is None:
                # 关键修复：无论status是什么，只要有更多数据，就更新offset
                # 避免用户晚点续取时跳过中间chunks
                if has_more:
                    session["offset"] = end
                    # 确保会话保存 batch_size 以便后续续取沿用
                    try:
                        session["batch_size"] = bs
                    except Exception:
                        pass
                    REDIS_CONN.set_obj(session_key, session, exp=session_ttl)
                else:
                    # 没有更多数据了，删除会话（无论status）
                    REDIS_CONN.delete(session_key)
            else:
                # 并发模式：最后一批时删除会话
                if not has_more:
                    REDIS_CONN.delete(session_key)

            # 简化字段：只保留必要的
            return {
                "batch_id": batch_id,
                "chunks": batch_texts,  # 返回纯文本数组
                "count": len(batch_texts),  # 当前批次数量
                "total": current_total,  # 当前实际总数（简化，不再区分预估/实际）
                "has_more": has_more,
                "batch_index": current_batch_index,
                "total_batches": (current_total + bs - 1) // bs if bs > 0 else 0,  # 基于当前实际数量计算
                "status": parsing_status,  # parsing | completed | error
                "progress": session.get("progress", 1.0 if parsing_status == "completed" else 0.0),  # 解析进度 0.0-1.0
                "parsed_page_range": session.get("parsed_page_range", ""),  # 已解析的页面范围（如"0-24"）
                "total_pages": session.get("total_pages", 0),  # 总页数（PDF专用）
            }

        # 若未能从会话恢复，则必须提供 doc_id 以初始化新会话
        if not doc_id:
            raise LookupError("Preview session not found or expired. Please provide doc_id to initialize a new session.")

        # 生成会话摘要，确保不同配置/区间/文档变化对应不同会话
        doc = cls.get_by_id(db, doc_id)
        if not doc:
            raise LookupError("Document not found")

        # 使用已有方法获取合并后的解析配置以计算摘要
        chunking_cfg = cls.get_chunking_config(db, doc_id) or {}
        merged_cfg = chunking_cfg.get("parser_config") or {}
        if parser_config_override:
            def _deep_merge(a: dict, b: dict) -> dict:
                for k, v in (b or {}).items():
                    if isinstance(v, dict) and isinstance(a.get(k), dict):
                        _deep_merge(a[k], v)
                    else:
                        a[k] = v
                return a
            merged_cfg = _deep_merge(json.loads(json.dumps(merged_cfg)), parser_config_override or {})

        # 解析有效页区间
        effective_from = int(merged_cfg.get("from_page", 0)) if isinstance(merged_cfg, dict) else 0
        effective_to = int(merged_cfg.get("to_page", 100000)) if isinstance(merged_cfg, dict) else 100000
        if isinstance(merged_cfg, dict):
            pages = merged_cfg.get("pages")
            if isinstance(pages, list) and pages:
                try:
                    starts = [int(p[0]) for p in pages if isinstance(p, (list, tuple)) and len(p) >= 1]
                    ends = [int(p[1]) for p in pages if isinstance(p, (list, tuple)) and len(p) >= 2]
                    if starts:
                        effective_from = min(effective_from, min(starts) - 1)
                    if ends:
                        effective_to = max(effective_to, max(ends))
                except Exception:
                    pass

        hasher = xxhash.xxh64()
        hasher.update(str(doc_id).encode("utf-8"))
        hasher.update(str(effective_from).encode("utf-8"))
        hasher.update(str(effective_to).encode("utf-8"))
        # 解析器选择（合入摘要）
        try:
            filename = doc.name
            _, eff_parser = cls._resolve_parser_for_filename(filename, override_parser_id or doc.parser_id)
            hasher.update(str(eff_parser).encode("utf-8"))
        except Exception:
            pass
        try:
            hasher.update(json.dumps(merged_cfg, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        except Exception:
            hasher.update(str(merged_cfg).encode("utf-8"))
        # 加入文档更新时间，确保文档变化会生成新会话
        hasher.update(str(getattr(doc, "update_time", "")).encode("utf-8"))
        digest = hasher.hexdigest()

        # 规范 batch_size（若已有会话对象，尽量沿用其中的 batch_size）
        try:
            bs = int(batch_size)
            if bs <= 0:
                bs = int(session.get("batch_size", 50)) if session else 50
        except Exception:
            bs = int(session.get("batch_size", 50)) if session else 50

        # 如果无会话或摘要不匹配，则创建新会话
        if not session or session.get("digest") != digest:
            import re
            batch_id = get_uuid()
            session_key = f"preview:session:{batch_id}"
            
            # 判断是否是PDF文档（需要渐进式解析）
            filename = doc.name
            is_pdf = re.search(r"\.pdf$", filename or "", re.IGNORECASE) is not None
            
            if is_pdf:
                # ===== PDF文档：渐进式解析 =====
                import threading
                from deepdoc.parser import PdfParser
                from api.db.services.file2document_service import File2DocumentService
                
                # 读取文件
                bucket, name = File2DocumentService.get_storage_address(db, doc_id=doc_id)
                file_bin = settings.STORAGE_IMPL.get(bucket, name)
                
                # 获取PDF总页数
                try:
                    total_pages = PdfParser.total_page_number(filename, file_bin)
                    if total_pages is None:
                        total_pages = 0
                except Exception:
                    total_pages = 0
                
                effective_to = min(effective_to, total_pages)
                estimated_chunks = (effective_to - effective_from) * 4
                
                # 创建会话（状态：parsing）
                session = {
                    "digest": digest,
                    "doc_id": doc_id,
                    "from": effective_from,
                    "to": effective_to,
                    "total": 0,
                    "estimated_total": estimated_chunks,
                    "offset": 0,
                    "chunks": [],
                    "batch_size": bs,
                    "status": "parsing",
                    "progress": 0.0,
                    "parsed_page_range": f"{effective_from}-{effective_from}",  # 初始范围
                    "total_pages": effective_to,
                }
                REDIS_CONN.set_obj(session_key, session, exp=session_ttl)
                
                # 后台线程：分批解析
                def progressive_parse_pdf_doc():
                    import logging
                    logging.info(f"[preview_chunks] {batch_id} - 开始渐进式解析PDF文档 {doc_id}，共{effective_to - effective_from}页")
                    
                    # 定义每批解析的页数（遵循原生逻辑）
                    page_batch_size = merged_cfg.get("task_page_size") or 12
                    if override_parser_id == "paper" or doc.parser_id == "paper":
                        page_batch_size = merged_cfg.get("task_page_size") or 22
                    # 如果是特殊解析器或非DeepDOC布局，不分批（一次性解析）
                    do_layout = merged_cfg.get("layout_recognize", "DeepDOC")
                    if override_parser_id in ["one", "knowledge_graph"] or doc.parser_id in ["one", "knowledge_graph"] or do_layout != "DeepDOC":
                        page_batch_size = 10**9  # 相当于全部一次性解析
                    
                    current_from = effective_from
                    
                    try:
                        while current_from < effective_to:
                            current_to = min(current_from + page_batch_size, effective_to)
                            
                            # 创建临时配置
                            temp_cfg_override = parser_config_override.copy() if parser_config_override else {}
                            temp_cfg_override["from_page"] = current_from
                            temp_cfg_override["to_page"] = current_to
                            
                            # 解析这批页面
                            logging.info(f"[preview_chunks] {batch_id} - 解析文档第 {current_from}-{current_to} 页")
                            batch_chunks = cls.preview_document_chunks(
                                db,
                                doc_id=doc_id,
                                parser_config_override=temp_cfg_override,
                                limit=None,
                                override_parser_id=override_parser_id,
                            )
                            
                            # 更新Redis会话
                            try:
                                payload = REDIS_CONN.get(session_key)
                                if not payload:
                                    logging.warning(f"[preview_chunks] {batch_id} - 会话已过期，停止解析")
                                    break
                                
                                current_session = json.loads(payload)
                                current_session["chunks"].extend(batch_chunks)
                                current_session["total"] = len(current_session["chunks"])
                                current_session["parsed_page_range"] = f"{effective_from}-{current_to}"  # 已解析的页面范围
                                current_session["progress"] = (current_to - effective_from) / (effective_to - effective_from)
                                
                                REDIS_CONN.set_obj(session_key, current_session, exp=session_ttl)
                                logging.info(f"[preview_chunks] {batch_id} - 已解析页面{effective_from}-{current_to}，累计{len(current_session['chunks'])}个chunks")
                            except Exception as e:
                                logging.error(f"[preview_chunks] {batch_id} - 更新Redis失败: {e}")
                                break
                            
                            current_from = current_to
                        
                        # 解析完成
                        try:
                            payload = REDIS_CONN.get(session_key)
                            if payload:
                                final_session = json.loads(payload)
                                final_session["status"] = "completed"
                                final_session["progress"] = 1.0
                                final_session["parsed_page_range"] = f"{effective_from}-{effective_to}"  # 最终页面范围
                                REDIS_CONN.set_obj(session_key, final_session, exp=session_ttl)
                                logging.info(f"[preview_chunks] {batch_id} - PDF文档解析完成（页面{effective_from}-{effective_to}），共{len(final_session['chunks'])}个chunks")
                        except Exception as e:
                            logging.error(f"[preview_chunks] {batch_id} - 更新最终状态失败: {e}")
                    
                    except Exception as e:
                        logging.error(f"[preview_chunks] {batch_id} - 解析失败: {e}", exc_info=True)
                        try:
                            payload = REDIS_CONN.get(session_key)
                            if payload:
                                error_session = json.loads(payload)
                                error_session["status"] = "error"
                                error_session["error"] = str(e)
                                REDIS_CONN.set_obj(session_key, error_session, exp=session_ttl)
                        except:
                            pass
                
                # 启动后台线程
                parse_thread = threading.Thread(target=progressive_parse_pdf_doc, daemon=True, name=f"pdf-doc-parse-{batch_id[:8]}")
                parse_thread.start()
                
                # 等待直到有数据再返回（避免返回空数组破坏调用者逻辑）
                # 不设置超时，无论多久都要等到真实数据
                import asyncio
                import logging
                check_interval = 0.5  # 每500ms检查一次
                
                logging.info(f"[preview_chunks] {batch_id} - 等待首批解析结果...")
                
                while True:
                    await asyncio.sleep(check_interval)
                    
                    try:
                        payload = REDIS_CONN.get(session_key)
                        if not payload:
                            # 会话已被删除或过期，可能是后台线程异常退出
                            logging.warning(f"[preview_chunks] {batch_id} - 会话丢失，停止等待")
                            break
                        
                        temp_session = json.loads(payload)
                        
                        # 如果已有数据，立即返回
                        if temp_session.get("chunks") and len(temp_session["chunks"]) > 0:
                            session = temp_session
                            logging.info(f"[preview_chunks] {batch_id} - 首批数据就绪，共{len(temp_session['chunks'])}个chunks")
                            break
                        
                        # 如果解析失败，也立即返回
                        if temp_session.get("status") == "error":
                            session = temp_session
                            logging.error(f"[preview_chunks] {batch_id} - 解析失败: {temp_session.get('error')}")
                            break
                        
                        # 如果已完成但没有数据（空文档），也返回
                        if temp_session.get("status") == "completed":
                            session = temp_session
                            logging.info(f"[preview_chunks] {batch_id} - 解析完成（空文档）")
                            break
                    except Exception as e:
                        logging.error(f"[preview_chunks] {batch_id} - 检查会话状态失败: {e}")
                        await asyncio.sleep(1)  # 出错时等待更长时间
            
            else:
                # ===== 非PDF文档：原有逻辑 =====
                all_chunks = cls.preview_document_chunks(
                    db,
                    doc_id=doc_id,
                    parser_config_override=parser_config_override,
                    limit=None,
                    override_parser_id=override_parser_id,
                )
                session = {
                    "digest": digest,
                    "doc_id": doc_id,
                    "from": effective_from,
                    "to": effective_to,
                    "total": len(all_chunks),
                    "estimated_total": len(all_chunks),
                    "offset": 0,
                    "chunks": all_chunks,
                    "batch_size": bs,
                    "status": "completed",
                    "progress": 1.0,
                }
                REDIS_CONN.set_obj(session_key, session, exp=session_ttl)

        # 计算返回批次
        start = int(session.get("offset", 0))
        current_chunks = session.get("chunks", [])
        current_total = len(current_chunks)
        parsing_status = session.get("status", "completed")
        estimated_total = int(session.get("estimated_total", current_total))
        
        if isinstance(batch_index, int) and batch_index >= 0:
            start = min(batch_index * bs, current_total)
        end = min(start + bs, current_total)
        batch = current_chunks[start:end]
        
        # 提取纯文本（用户只需要文本，不需要元数据）
        batch_texts = []
        for chunk in batch:
            if isinstance(chunk, dict):
                batch_texts.append(chunk.get("content_with_weight", ""))
            elif isinstance(chunk, str):
                batch_texts.append(chunk)
            else:
                batch_texts.append(str(chunk))
        
        has_more = (end < current_total) or (parsing_status == "parsing")
        current_batch_index = (start // bs) if bs > 0 else 0
        
        if parsing_status == "completed":
            total_batches = (current_total + bs - 1) // bs if bs > 0 else 0
        else:
            total_batches = (estimated_total + bs - 1) // bs if bs > 0 and estimated_total > 0 else None

        # 更新或删除会话
        if batch_index is None:
            # 关键修复：无论status是什么，只要有更多数据，就更新offset
            # 避免用户晚点续取时跳过中间chunks
            if has_more:
                session["offset"] = end
                REDIS_CONN.set_obj(session_key, session, exp=session_ttl)
            else:
                # 没有更多数据了，删除会话（无论status）
                REDIS_CONN.delete(session_key)
        else:
            # 并发模式：最后一批时删除会话
            if not has_more:
                REDIS_CONN.delete(session_key)

        return {
            "batch_id": batch_id,
            "chunks": batch_texts,  # 返回纯文本数组
            "count": len(batch_texts),  # 当前批次数量
            "total": current_total,  # 当前已解析的chunks总数（动态增长）
            "has_more": has_more,
            "batch_index": current_batch_index,
            "total_batches": (current_total + bs - 1) // bs if bs > 0 else 0,  # 基于当前total计算
            "status": parsing_status,  # parsing | completed | error
            "progress": session.get("progress", 1.0 if parsing_status == "completed" else 0.0),  # 解析进度 0.0-1.0
            "parsed_page_range": session.get("parsed_page_range", ""),  # 已解析的页面范围（如"0-24"）
            "total_pages": session.get("total_pages", 0),  # 总页数（PDF专用）
        }

    @classmethod
    def _get_allowed_parsers_for_filename(cls, filename: str) -> set[str]:
        from common.constants import ParserType
        import re
        f = (filename or "").lower()
        if re.search(r"\.pdf$", f):
            return {
                ParserType.NAIVE.value, ParserType.MANUAL.value, ParserType.PAPER.value,
                ParserType.BOOK.value, ParserType.LAWS.value, ParserType.PRESENTATION.value,
                ParserType.ONE.value, ParserType.QA.value
            }
        if re.search(r"\.(doc|docx)$", f):
            return {
                ParserType.NAIVE.value, ParserType.BOOK.value, ParserType.LAWS.value,
                ParserType.ONE.value, ParserType.QA.value, ParserType.MANUAL.value
            }
        if re.search(r"\.(xlsx?|xls)$", f):
            return {ParserType.NAIVE.value, ParserType.QA.value, ParserType.TABLE.value, ParserType.ONE.value}
        if re.search(r"\.(ppt|pptx)$", f):
            return {ParserType.PRESENTATION.value}
        if re.search(r"\.(jpg|jpeg|png|gif|bmp|tif|tiff|webp|svg|ico)$", f):
            return {ParserType.PICTURE.value}
        if re.search(r"\.txt$", f):
            return {ParserType.NAIVE.value, ParserType.BOOK.value, ParserType.LAWS.value, ParserType.ONE.value, ParserType.QA.value, ParserType.TABLE.value}
        if re.search(r"\.csv$", f):
            return {ParserType.NAIVE.value, ParserType.BOOK.value, ParserType.LAWS.value, ParserType.ONE.value, ParserType.QA.value, ParserType.TABLE.value}
        if re.search(r"\.(md|markdown)$", f):
            return {ParserType.NAIVE.value, ParserType.QA.value}
        if re.search(r"\.(json|jsonl|ldjson)$", f):
            return {ParserType.NAIVE.value}
        if re.search(r"\.eml$", f):
            return {ParserType.EMAIL.value}
        if re.search(r"\.(mp3|wav|aac|flac|ogg|aiff|au|midi|wma|da|wave|realaudio|vqf|oggvorbis|ape)$", f):
            return {ParserType.AUDIO.value}
        from common.constants import ParserType as PT
        return {PT.NAIVE.value}

    @classmethod
    def _get_module_by_parser_id(cls, parser_id: str):
        from common.constants import ParserType
        from core.app import (
            naive,
            paper,
            book,
            presentation,
            manual,
            laws,
            qa,
            table,
            resume,
            picture,
            one,
            audio,
            email,
            tag,
        )
        PARSER_FACTORY = {
            "general": naive,
            ParserType.NAIVE.value: naive,
            ParserType.PAPER.value: paper,
            ParserType.BOOK.value: book,
            ParserType.PRESENTATION.value: presentation,
            ParserType.MANUAL.value: manual,
            ParserType.LAWS.value: laws,
            ParserType.QA.value: qa,
            ParserType.TABLE.value: table,
            ParserType.RESUME.value: resume,
            ParserType.PICTURE.value: picture,
            ParserType.ONE.value: one,
            ParserType.AUDIO.value: audio,
            ParserType.EMAIL.value: email,
            ParserType.KG.value: naive,
            ParserType.TAG.value: tag,
        }
        return PARSER_FACTORY.get(parser_id, naive)

    @classmethod
    def _resolve_parser_for_filename(cls, filename: str, requested_parser_id: str | None):
        allowed = cls._get_allowed_parsers_for_filename(filename)
        if requested_parser_id:
            if requested_parser_id not in allowed:
                raise ValueError(f"Unsupported parser_id '{requested_parser_id}' for file '{filename}'. Allowed: {sorted(list(allowed))}")
            return cls._get_module_by_parser_id(requested_parser_id), requested_parser_id
        # 默认按扩展名推断
        import re
        f = (filename or "").lower()
        from common.constants import ParserType
        if re.search(r"\.(ppt|pptx)$", f):
            return cls._get_module_by_parser_id(ParserType.PRESENTATION.value), ParserType.PRESENTATION.value
        if re.search(r"\.(csv|xlsx?|xls)$", f):
            return cls._get_module_by_parser_id(ParserType.TABLE.value), ParserType.TABLE.value
        if re.search(r"\.(jpg|jpeg|png|gif|bmp|tif|tiff|webp|svg|ico)$", f):
            return cls._get_module_by_parser_id(ParserType.PICTURE.value), ParserType.PICTURE.value
        if re.search(r"\.eml$", f):
            return cls._get_module_by_parser_id(ParserType.EMAIL.value), ParserType.EMAIL.value
        if re.search(r"\.(mp3|wav|aac|flac|ogg|aiff|au|midi|wma|da|wave|realaudio|vqf|oggvorbis|ape)$", f):
            return cls._get_module_by_parser_id(ParserType.AUDIO.value), ParserType.AUDIO.value
        if re.search(r"\.(mp4|mov|avi|flv|mpeg|mpg|webm|wmv|3gp|3gpp|mkv)$", f):
            return cls._get_module_by_parser_id(ParserType.PICTURE.value), ParserType.PICTURE.value
        return cls._get_module_by_parser_id(ParserType.NAIVE.value), ParserType.NAIVE.value

    @classmethod
    def preview_file_chunks(
        cls,
        db: Session,
        filename: str,
        file_bytes: bytes,
        parser_config_override: dict | None = None,
        override_parser_id: str | None = None,
        language: str | None = None,
        tenant_id: str | None = None,
    ) -> list[str]:
        """
        仅对上传文件执行切片预览，不落库、不向量化。
        """
        language = language or "Chinese"

        module, method = cls._resolve_parser_for_filename(filename, override_parser_id)

        from api.utils.api_utils import get_parser_config

        base_cfg = get_parser_config(method, {})
        if parser_config_override:
            def _deep_merge(a: dict, b: dict) -> dict:
                for k, v in (b or {}).items():
                    if isinstance(v, dict) and isinstance(a.get(k), dict):
                        _deep_merge(a[k], v)
                    else:
                        a[k] = v
                return a
            base_cfg = _deep_merge(base_cfg or {}, parser_config_override or {})

        # 解析页区间（仅对部分解析器有效）
        effective_from = 0
        effective_to = 100000
        try:
            if isinstance(base_cfg, dict):
                if "from_page" in base_cfg:
                    effective_from = int(base_cfg.get("from_page", 0))
                if "to_page" in base_cfg:
                    effective_to = int(base_cfg.get("to_page", 100000))
                pages = base_cfg.get("pages")
                if isinstance(pages, list) and pages:
                    try:
                        starts = [int(p[0]) for p in pages if isinstance(p, (list, tuple)) and len(p) >= 1]
                        ends = [int(p[1]) for p in pages if isinstance(p, (list, tuple)) and len(p) >= 2]
                        if starts:
                            effective_from = min(effective_from, min(starts) - 1)
                        if ends:
                            effective_to = max(effective_to, max(ends))
                    except Exception:
                        pass
        except Exception:
            effective_from, effective_to = 0, 100000

        def _noop(prog=None, msg=""):
            return None

        result = module.chunk(
            filename,
            binary=file_bytes,
            from_page=effective_from,
            to_page=effective_to,
            lang=language,
            callback=_noop,
            parser_config=base_cfg or {},
            tenant_id=tenant_id,
        )

        # 统一为文本列表（只提取文本内容）
        chunks_text: list[str] = []
        if isinstance(result, list):
            if not result:
                chunks_text = []
            else:
                first = result[0]
                if isinstance(first, dict) and "content_with_weight" in first:
                    # 提取文本内容（保证顺序）
                    chunks_text = [d.get("content_with_weight", "") for d in result]
                elif isinstance(first, str):
                    chunks_text = result
                else:
                    chunks_text = [str(x) for x in result]
        else:
            chunks_text = [str(result)]
        return chunks_text

    # 类级别的信号量：限制并发解析数量
    _pdf_parse_semaphore = None
    _semaphore_lock = None
    
    @classmethod
    def _get_pdf_parse_semaphore(cls):
        """获取PDF解析信号量（单例模式）"""
        if cls._pdf_parse_semaphore is None:
            import threading
            if cls._semaphore_lock is None:
                cls._semaphore_lock = threading.Lock()
            with cls._semaphore_lock:
                if cls._pdf_parse_semaphore is None:
                    import os
                    # 从环境变量读取，默认最多5个PDF并发解析
                    max_concurrent = int(os.getenv("MAX_CONCURRENT_PDF_PARSE", "5"))
                    import asyncio
                    cls._pdf_parse_semaphore = asyncio.Semaphore(max_concurrent)
        return cls._pdf_parse_semaphore
    
    @classmethod
    async def preview_file_chunks_batched(
        cls,
        db: Session,
        filename: str | None = None,
        file_bytes: bytes | None = None,
        parser_config_override: dict | None = None,
        batch_size: int = 50,
        batch_id: str | None = None,
        session_ttl: int = 1800,
        override_parser_id: str | None = None,
        language: str | None = None,
        batch_index: int | None = None,
        tenant_id: str | None = None,
    ) -> dict:
        import json
        import re

        language = language or "Chinese"

        # 会话恢复优先（支持仅 batch_id 续取）
        session_key = None
        session = None
        if batch_id:
            session_key = f"preview:file_session:{batch_id}"
            try:
                payload = REDIS_CONN.get(session_key)
                if payload:
                    session = json.loads(payload)
            except Exception:
                session = None

        # 如果有会话且未提供文件，则直接续取
        if session and (filename is None or file_bytes is None):
            if tenant_id and session.get("tenant_id") not in (None, tenant_id):
                raise LookupError("Preview file session not found or expired for current tenant.")
            try:
                bs = int(batch_size) if batch_size is not None else int(session.get("batch_size", 50))
                if bs <= 0:
                    bs = int(session.get("batch_size", 50)) or 50
            except Exception:
                bs = int(session.get("batch_size", 50)) if session.get("batch_size") else 50

            start = int(session.get("offset", 0))
            # 获取当前已解析的chunk总数
            current_chunks = session.get("chunks", [])
            current_total = len(current_chunks)
            
            # 检查解析状态
            parsing_status = session.get("status", "completed")  # parsing | completed | error
            estimated_total = int(session.get("estimated_total", current_total))
            
            if isinstance(batch_index, int) and batch_index >= 0:
                start = min(batch_index * bs, current_total)
            end = min(start + bs, current_total)
            batch = current_chunks[start:end]
            
            # 关键修复：如果batch为空且还在解析中，等待直到有新数据
            if not batch and parsing_status == "parsing":
                import asyncio
                import logging
                logging.info(f"[preview_chunks] {batch_id} - offset已达当前总数，等待后台解析新数据...")
                
                check_interval = 0.5
                while True:
                    await asyncio.sleep(check_interval)
                    
                    try:
                        payload = REDIS_CONN.get(session_key)
                        if not payload:
                            logging.warning(f"[preview_chunks] {batch_id} - 会话丢失")
                            break
                        
                        temp_session = json.loads(payload)
                        temp_chunks = temp_session.get("chunks", [])
                        temp_total = len(temp_chunks)
                        temp_status = temp_session.get("status", "completed")
                        
                        # 如果有新数据，更新session并退出等待
                        if temp_total > current_total:
                            session = temp_session
                            current_chunks = temp_chunks
                            current_total = temp_total
                            parsing_status = temp_status
                            batch = current_chunks[start:min(start + bs, current_total)]
                            logging.info(f"[preview_chunks] {batch_id} - 新数据就绪，从{start}返回{len(batch)}个chunks")
                            break
                        
                        # 如果解析完成但仍无新数据，退出
                        if temp_status == "completed":
                            session = temp_session
                            parsing_status = temp_status
                            logging.info(f"[preview_chunks] {batch_id} - 解析完成，无更多数据")
                            break
                        
                        # 如果解析失败，退出
                        if temp_status == "error":
                            session = temp_session
                            parsing_status = temp_status
                            logging.error(f"[preview_chunks] {batch_id} - 解析失败")
                            break
                    except Exception as e:
                        logging.error(f"[preview_chunks] {batch_id} - 等待新数据失败: {e}")
                        await asyncio.sleep(1)
                
                # 重新计算end（可能有新数据了）
                end = min(start + bs, current_total)
                batch = current_chunks[start:end]
            
            # 提取纯文本（用户只需要文本，不需要元数据）
            batch_texts = []
            for chunk in batch:
                if isinstance(chunk, dict):
                    batch_texts.append(chunk.get("content_with_weight", ""))
                elif isinstance(chunk, str):
                    batch_texts.append(chunk)
                else:
                    batch_texts.append(str(chunk))
            
            # 如果还在解析中，或者还有未读取的chunks，则has_more=True
            has_more = (end < current_total) or (parsing_status == "parsing")
            current_batch_index = (start // bs) if bs > 0 else 0
            
            # 计算总批次数
            if parsing_status == "completed":
                total_batches = (current_total + bs - 1) // bs if bs > 0 else 0
            else:
                # 解析中，基于预估值计算
                total_batches = (estimated_total + bs - 1) // bs if bs > 0 and estimated_total > 0 else None

            if batch_index is None:
                # 关键修复：无论status是什么，只要有更多数据，就更新offset
                # 避免用户晚点续取时跳过中间chunks
                if has_more:
                    session["offset"] = end
                    try:
                        session["batch_size"] = bs
                    except Exception:
                        pass
                    REDIS_CONN.set_obj(session_key, session, exp=session_ttl)
                else:
                    # 没有更多数据了，删除会话（无论status）
                    REDIS_CONN.delete(session_key)

            # 简化字段：只保留必要的
            return {
                "batch_id": batch_id,
                "chunks": batch_texts,  # 返回纯文本数组
                "count": len(batch_texts),  # 当前批次数量
                "total": current_total,  # 当前实际总数（简化，不再区分预估/实际）
                "has_more": has_more,
                "batch_index": current_batch_index,
                "total_batches": (current_total + bs - 1) // bs if bs > 0 else 0,  # 基于当前实际数量计算
                "status": parsing_status,  # parsing | completed | error
                "progress": session.get("progress", 1.0 if parsing_status == "completed" else 0.0),  # 解析进度 0.0-1.0
                "parsed_page_range": session.get("parsed_page_range", ""),  # 已解析的页面范围（如"0-24"）
                "total_pages": session.get("total_pages", 0),  # 总页数（PDF专用）
            }

        # 否则需要文件以初始化/重建会话
        if filename is None or file_bytes is None:
            raise LookupError("Preview file session not found or expired. Please upload file again to initialize.")

        # 合并配置并计算摘要
        module, method = cls._resolve_parser_for_filename(filename, override_parser_id)
        from api.utils.api_utils import get_parser_config
        base_cfg = get_parser_config(method, {})
        if parser_config_override:
            def _deep_merge(a: dict, b: dict) -> dict:
                for k, v in (b or {}).items():
                    if isinstance(v, dict) and isinstance(a.get(k), dict):
                        _deep_merge(a[k], v)
                    else:
                        a[k] = v
                return a
            base_cfg = _deep_merge(base_cfg or {}, parser_config_override or {})

        hasher = xxhash.xxh64()
        hasher.update((filename or "").encode("utf-8"))
        hasher.update(file_bytes)
        try:
            hasher.update(json.dumps(base_cfg, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        except Exception:
            hasher.update(str(base_cfg).encode("utf-8"))
        hasher.update((language or "").encode("utf-8"))
        hasher.update((method or "").encode("utf-8"))
        if tenant_id:
            hasher.update(str(tenant_id).encode("utf-8"))
        digest = hasher.hexdigest()

        # 规范 batch_size（新建会话时保存）
        try:
            bs = int(batch_size)
            if bs <= 0:
                bs = 50
        except Exception:
            bs = 50

        # 检查是否需要创建新会话
        if not session or session.get("digest") != digest:
            batch_id = get_uuid()
            session_key = f"preview:file_session:{batch_id}"
            
            # 判断是否是PDF文件（需要特殊处理）
            is_pdf = re.search(r"\.pdf$", filename or "", re.IGNORECASE) is not None
            
            if is_pdf:
                # ===== PDF文件：渐进式解析 =====
                import threading
                from deepdoc.parser import PdfParser
                
                # 获取PDF总页数
                try:
                    total_pages = PdfParser.total_page_number(filename, file_bytes)
                    if total_pages is None:
                        total_pages = 0
                except Exception:
                    total_pages = 0
                
                # 解析配置中的页面范围
                effective_from = int(base_cfg.get("from_page", 0)) if isinstance(base_cfg, dict) else 0
                effective_to = int(base_cfg.get("to_page", total_pages)) if isinstance(base_cfg, dict) else total_pages
                effective_to = min(effective_to, total_pages)
                
                # 首批只解析前N页（快速返回）
                initial_pages = min(15, effective_to - effective_from)  # 先解析15页
                
                # 预估总chunk数（假设每页平均3-5个chunks）
                estimated_chunks = (effective_to - effective_from) * 4
                
                # 创建会话（状态：parsing）
                session = {
                    "digest": digest,
                    "filename": filename,
                    "total": 0,  # 初始为0
                    "estimated_total": estimated_chunks,
                    "offset": 0,
                    "chunks": [],
                    "batch_size": bs,
                    "tenant_id": tenant_id,
                    "status": "parsing",
                    "progress": 0.0,
                    "current_page": effective_from,
                    "total_pages": effective_to,
                    "from_page": effective_from,
                    "to_page": effective_to,
                }
                REDIS_CONN.set_obj(session_key, session, exp=session_ttl)
                
                # 后台线程：分批解析PDF
                def progressive_parse_pdf():
                    import logging
                    logging.info(f"[preview_chunks] {batch_id} - 开始渐进式解析PDF，共{effective_to - effective_from}页")
                    
                    # 定义每批解析的页数（遵循原生逻辑）
                    page_batch_size = base_cfg.get("task_page_size") or 12
                    if override_parser_id == "paper" or method == "paper":
                        page_batch_size = base_cfg.get("task_page_size") or 22
                    # 如果是特殊解析器或非DeepDOC布局，不分批（一次性解析）
                    do_layout = base_cfg.get("layout_recognize", "DeepDOC")
                    if override_parser_id in ["one", "knowledge_graph"] or do_layout != "DeepDOC":
                        page_batch_size = 10**9  # 相当于全部一次性解析
                    
                    current_from = effective_from
                    
                    try:
                        while current_from < effective_to:
                            current_to = min(current_from + page_batch_size, effective_to)
                            
                            # 创建临时配置，限制解析范围
                            temp_cfg = base_cfg.copy() if base_cfg else {}
                            temp_cfg["from_page"] = current_from
                            temp_cfg["to_page"] = current_to
                            
                            # 调用原有的chunk方法解析这一批页面
                            logging.info(f"[preview_chunks] {batch_id} - 解析第 {current_from}-{current_to} 页")
                            batch_chunks = cls.preview_file_chunks(
                                db,
                                filename,
                                file_bytes,
                                parser_config_override=temp_cfg,
                                override_parser_id=override_parser_id,
                                language=language,
                                tenant_id=tenant_id,
                            )
                            
                            # 更新Redis会话：追加新chunks
                            try:
                                payload = REDIS_CONN.get(session_key)
                                if not payload:
                                    logging.warning(f"[preview_chunks] {batch_id} - 会话已过期，停止解析")
                                    break
                                
                                current_session = json.loads(payload)
                                current_session["chunks"].extend(batch_chunks)
                                current_session["total"] = len(current_session["chunks"])
                                current_session["parsed_page_range"] = f"{effective_from}-{current_to}"  # 已解析的页面范围
                                current_session["progress"] = (current_to - effective_from) / (effective_to - effective_from)
                                
                                REDIS_CONN.set_obj(session_key, current_session, exp=session_ttl)
                                logging.info(f"[preview_chunks] {batch_id} - 已解析页面{effective_from}-{current_to}，累计{len(current_session['chunks'])}个chunks")
                            except Exception as e:
                                logging.error(f"[preview_chunks] {batch_id} - 更新Redis失败: {e}")
                                break
                            
                            current_from = current_to
                        
                        # 解析完成，更新最终状态
                        try:
                            payload = REDIS_CONN.get(session_key)
                            if payload:
                                final_session = json.loads(payload)
                                final_session["status"] = "completed"
                                final_session["progress"] = 1.0
                                final_session["parsed_page_range"] = f"{effective_from}-{effective_to}"  # 最终页面范围
                                REDIS_CONN.set_obj(session_key, final_session, exp=session_ttl)
                                logging.info(f"[preview_chunks] {batch_id} - PDF解析完成（页面{effective_from}-{effective_to}），共{len(final_session['chunks'])}个chunks")
                        except Exception as e:
                            logging.error(f"[preview_chunks] {batch_id} - 更新最终状态失败: {e}")
                    
                    except Exception as e:
                        # 解析出错
                        logging.error(f"[preview_chunks] {batch_id} - 解析失败: {e}", exc_info=True)
                        try:
                            payload = REDIS_CONN.get(session_key)
                            if payload:
                                error_session = json.loads(payload)
                                error_session["status"] = "error"
                                error_session["error"] = str(e)
                                REDIS_CONN.set_obj(session_key, error_session, exp=session_ttl)
                        except:
                            pass
                
                # 获取信号量，限制并发解析数量
                semaphore = cls._get_pdf_parse_semaphore()
                
                # 启动后台解析线程
                parse_thread = threading.Thread(target=progressive_parse_pdf, daemon=True, name=f"pdf-parse-{batch_id[:8]}")
                parse_thread.start()
                
                # 等待直到有数据再返回（避免返回空数组破坏调用者逻辑）
                # 使用信号量控制并发，避免系统过载
                import asyncio
                import logging
                check_interval = 0.5  # 每500ms检查一次
                
                async with semaphore:  # ← 限制并发数
                    logging.info(f"[preview_chunks] {batch_id} - 等待首批解析结果...")
                    
                    while True:
                        await asyncio.sleep(check_interval)
                        
                        try:
                            payload = REDIS_CONN.get(session_key)
                            if not payload:
                                # 会话已被删除或过期，可能是后台线程异常退出
                                logging.warning(f"[preview_chunks] {batch_id} - 会话丢失，停止等待")
                                break
                            
                            temp_session = json.loads(payload)
                            
                            # 如果已有数据，立即返回
                            if temp_session.get("chunks") and len(temp_session["chunks"]) > 0:
                                session = temp_session
                                logging.info(f"[preview_chunks] {batch_id} - 首批数据就绪，共{len(temp_session['chunks'])}个chunks")
                                break
                            
                            # 如果解析失败，也立即返回
                            if temp_session.get("status") == "error":
                                session = temp_session
                                logging.error(f"[preview_chunks] {batch_id} - 解析失败: {temp_session.get('error')}")
                                break
                            
                            # 如果已完成但没有数据（空文档），也返回
                            if temp_session.get("status") == "completed":
                                session = temp_session
                                logging.info(f"[preview_chunks] {batch_id} - 解析完成（空文档）")
                                break
                        except Exception as e:
                            logging.error(f"[preview_chunks] {batch_id} - 检查会话状态失败: {e}")
                            await asyncio.sleep(1)  # 出错时等待更长时间
            
            else:
                # ===== 非PDF文件：原有逻辑（一次性解析） =====
                all_chunks = cls.preview_file_chunks(
                    db,
                    filename,
                    file_bytes,
                    parser_config_override=base_cfg,
                    override_parser_id=override_parser_id,
                    language=language,
                    tenant_id=tenant_id,
                )
                session = {
                    "digest": digest,
                    "filename": filename,
                    "total": len(all_chunks),
                    "estimated_total": len(all_chunks),
                    "offset": 0,
                    "chunks": all_chunks,
                    "batch_size": bs,
                    "tenant_id": tenant_id,
                    "status": "completed",
                    "progress": 1.0,
                }
                REDIS_CONN.set_obj(session_key, session, exp=session_ttl)

        # 返回当前批次数据
        start = int(session.get("offset", 0))
        current_chunks = session.get("chunks", [])
        current_total = len(current_chunks)
        parsing_status = session.get("status", "completed")
        estimated_total = int(session.get("estimated_total", current_total))
        
        if isinstance(batch_index, int) and batch_index >= 0:
            start = min(batch_index * bs, current_total)
        end = min(start + bs, current_total)
        batch = current_chunks[start:end]
        
        # 提取纯文本（用户只需要文本，不需要元数据）
        batch_texts = []
        for chunk in batch:
            if isinstance(chunk, dict):
                batch_texts.append(chunk.get("content_with_weight", ""))
            elif isinstance(chunk, str):
                batch_texts.append(chunk)
            else:
                batch_texts.append(str(chunk))
        
        has_more = (end < current_total) or (parsing_status == "parsing")
        current_batch_index = (start // bs) if bs > 0 else 0
        
        if parsing_status == "completed":
            total_batches = (current_total + bs - 1) // bs if bs > 0 else 0
        else:
            total_batches = (estimated_total + bs - 1) // bs if bs > 0 and estimated_total > 0 else None

        # 顺序/并发模式会话推进
        if batch_index is None:
            # 关键修复：无论status是什么，只要有更多数据，就更新offset
            # 避免用户晚点续取时跳过中间chunks
            if has_more:
                session["offset"] = end
                REDIS_CONN.set_obj(session_key, session, exp=session_ttl)
            else:
                # 没有更多数据了，删除会话（无论status）
                REDIS_CONN.delete(session_key)

        return {
            "batch_id": batch_id,
            "chunks": batch_texts,  # 返回纯文本数组
            "count": len(batch_texts),  # 当前批次数量
            "total": current_total,  # 当前已解析的chunks总数（动态增长）
            "has_more": has_more,
            "batch_index": current_batch_index,
            "total_batches": (current_total + bs - 1) // bs if bs > 0 else 0,  # 基于当前total计算
            "status": parsing_status,  # parsing | completed | error
            "progress": session.get("progress", 1.0 if parsing_status == "completed" else 0.0),  # 解析进度 0.0-1.0
            "parsed_page_range": session.get("parsed_page_range", ""),  # 已解析的页面范围（如"0-24"）
            "total_pages": session.get("total_pages", 0),  # 总页数（PDF专用）
        }

    @classmethod
    def get_by_doc_id(cls, db: Session, doc_id: str) -> dict | None:
        """
        通过文档ID获取文档信息。

        参数:
        - db: 数据库会话对象，用于执行数据库查询和更新操作。
        - doc_id: 需要查询的文档的ID。

        返回:
        - 返回包含文档信息的字典，如果未找到文档，则返回None。
        """
        doc = db.query(cls.model).filter_by(id=doc_id).first()
        return doc.to_dict() if doc else None

    @classmethod
    def list_documents_in_dataset(cls, db: Session, dataset_id: str, offset: int, count: int,
                                  order_by: str, descend: bool, keywords: str | None = None) -> (list[dict], int):
        query = db.query(cls.model).filter_by(kb_id=dataset_id)
        if keywords:
            query = query.filter(func.lower(cls.model.name).contains(keywords.lower()))

        total = query.count()

        if descend:
            query = query.order_by(getattr(cls.model, order_by).desc())
        else:
            query = query.order_by(getattr(cls.model, order_by).asc())

        docs = query.all()

        if offset < 0 or offset > len(docs):
            raise IndexError("Offset is out of the valid range.")

        if count == -1:
            return [doc.to_dict() for doc in docs[offset:]], total

        return [doc.to_dict() for doc in docs[offset:offset + count]], total

    @classmethod
    def insert(cls, db: Session, doc: dict):
        if not cls.save(db, **doc):
            raise RuntimeError("Database error (Document)!")
        if not KnowledgebaseService.atomic_increase_doc_num_by_id(db, doc["kb_id"]):
            raise RuntimeError("Database error (dataset)!")
        return Document(**doc)

    @classmethod
    def remove_document(cls, db: Session, doc: Document, tenant_id: str):
        from api.db.services.task_service import TaskService
        # 在删除文档前先保存需要的属性
        doc_id = doc.id
        cls.clear_chunk_num(db, doc.id)

        document = DocumentService.get_by_doc_id(db, doc.id)
        kb = KnowledgebaseService.get_by_id(db, document["kb_id"])
        # 构建 Milvus 集合名称
        collection_name = search.index_name_one(tenant_id, kb.name)

        TaskService.filter_delete(db, [Task.doc_id == doc.id])
        page = 0
        page_size = 1000
        all_chunk_ids = []
        while True:
            chunks = settings.docStoreConn.search(["img_id"], [], {"doc_id": doc.id}, [], OrderByExpr(),
                                                  page * page_size, page_size, collection_name,
                                                  [doc.kb_id])
            chunk_ids = settings.docStoreConn.get_chunk_ids(chunks)
            if not chunk_ids:
                break
            all_chunk_ids.extend(chunk_ids)
            page += 1
        for cid in all_chunk_ids:
            if settings.STORAGE_IMPL.obj_exist(doc.kb_id, cid):
                settings.STORAGE_IMPL.rm(doc.kb_id, cid)
        if doc.thumbnail and not doc.thumbnail.startswith(IMG_BASE64_PREFIX):
            if settings.STORAGE_IMPL.obj_exist(doc.kb_id, doc.thumbnail):
                settings.STORAGE_IMPL.rm(doc.kb_id, doc.thumbnail)

        try:
            # 检查集合是否存在并删除向量数据库中的数据
            if settings.docStoreConn.has_collection(collection_name):
                db_type = settings.docStoreConn.dbType()
                if db_type == "milvus":
                    settings.docStoreConn.delete(
                        collection_name=collection_name,
                        filter=f"doc_id == '{doc_id}'"
                    )
                else:
                    settings.docStoreConn.delete(
                        condition={"doc_id": doc_id},
                        indexName=collection_name,
                        knowledgebaseId=doc.kb_id
                    )
            # todo 待测试【settings.docStoreConn.delete等】，测试成功则替换上面的方法 优先级较高，不然graphrag玩不转
            # kb_id = document["kb_id"]  # 使用从数据库重新获取的kb_id
            # graph_source = settings.docStoreConn.get_fields(
            #     settings.docStoreConn.search(["source_id"], [], {"kb_id": kb_id, "knowledge_graph_kwd": ["graph"]}, [], OrderByExpr(), 0, 1, search.index_name(tenant_id, [kb.name]), [kb_id]), ["source_id"]
            # )
            # if len(graph_source) > 0 and doc_id in list(graph_source.values())[0]["source_id"]:
            #     settings.docStoreConn.update({"kb_id": kb_id, "knowledge_graph_kwd": ["entity", "relation", "graph", "subgraph", "community_report"], "source_id": doc_id},
            #                                 {"remove": {"source_id": doc_id}},
            #                                 search.index_name(tenant_id, [kb.name]), kb_id)
            #     settings.docStoreConn.update({"kb_id": kb_id, "knowledge_graph_kwd": ["graph"]},
            #                                 {"removed_kwd": "Y"},
            #                                 search.index_name(tenant_id, [kb.name]), kb_id)
            #     settings.docStoreConn.delete({"kb_id": kb_id, "knowledge_graph_kwd": ["entity", "relation", "graph", "subgraph", "community_report"], "must_not": {"exists": "source_id"}},
            #                                 search.index_name(tenant_id, [kb.name]), kb_id)
        except Exception as e:
            return e
        return cls.delete_by_id(db, doc_id)

    @classmethod
    def get_newly_uploaded(cls, db: Session):
        query = db.query(
            cls.model.id, cls.model.kb_id, cls.model.parser_id, cls.model.parser_config, cls.model.name,
            cls.model.type, cls.model.location, cls.model.size, Knowledgebase.tenant_id, Tenant.embd_id,
            Tenant.img2txt_id, Tenant.asr_id, cls.model.update_time
        ).join(Knowledgebase, cls.model.kb_id == Knowledgebase.id
               ).join(Tenant, Knowledgebase.tenant_id == Tenant.id
                      ).filter(
            cls.model.status == StatusEnum.VALID.value,
            cls.model.type != FileType.VIRTUAL.value,
            cls.model.progress == 0,
            cls.model.update_time >= current_timestamp() - 1000 * 600,
            cls.model.run == TaskStatus.RUNNING.value
        ).order_by(cls.model.update_time.asc())
        return query.all()

    @classmethod
    def get_unfinished_docs(cls, db: Session):
        # Subquery to get doc_ids with unfinished tasks
        unfinished_task_query = db.query(Task.doc_id).filter(
            Task.progress >= 0,
            Task.progress < 1
        ).scalar_subquery()

        query = db.query(
            cls.model.id, cls.model.process_begin_at, cls.model.parser_config,
            cls.model.progress_msg, cls.model.run, cls.model.parser_id
        ).filter(
            cls.model.status == StatusEnum.VALID.value,
            cls.model.type != FileType.VIRTUAL.value,
            or_(
                and_(cls.model.progress < 1, cls.model.progress > 0),
                cls.model.id.in_(unfinished_task_query)  # including unfinished tasks like GraphRAG, RAPTOR and Mindmap
            )
        )
        rows = query.all()
        return [dict(row._mapping) for row in rows]

    @classmethod
    def increment_chunk_num(cls, db: Session, doc_id, kb_id, token_num, chunk_num, duration):
        """
        更新文档和知识库的片段数量、令牌数量和处理时长（SQLAlchemy 2.0 Core 风格）。

        本方法通过查询指定ID的文档和知识库，在数据库中更新它们的令牌数量、片段数量和处理时长。
        如果文档未找到，则抛出LookupError异常。

        注意：此方法故意不更新 update_time/update_date。
        这是因为文档解析过程中频繁增加 chunk 数量不应该刷新"最后修改时间"，
        用户通常认为修改文件名或解析配置才算修改，而后台处理进度不算。

        参数:
        - db: 数据库会话对象，用于执行数据库查询和更新操作。
        - doc_id: 需要更新的文档的ID。
        - kb_id: 需要更新的知识库的ID。
        - token_num: 需要增加的令牌数量。
        - chunk_num: 需要增加的片段数量。
        - duration: 需要增加的处理时长。

        返回:
        - kb_update: 知识库更新的影响行数。
        """
        # 更新文档的令牌数量、片段数量和处理时长
        doc_stmt = update(cls.model).where(cls.model.id == doc_id).values({
            cls.model.token_num: cls.model.token_num + token_num,
            cls.model.chunk_num: cls.model.chunk_num + chunk_num,
            cls.model.process_duration: cls.model.process_duration + duration
        })
        doc_result = db.execute(doc_stmt)

        # 如果文档更新影响行数为0，表示未找到文档，抛出异常
        if doc_result.rowcount == 0:
            logging.warning("Document not found which is supposed to be there")

        # 更新知识库的令牌数量和片段数量
        kb_stmt = update(Knowledgebase).where(Knowledgebase.id == kb_id).values({
            Knowledgebase.token_num: Knowledgebase.token_num + token_num,
            Knowledgebase.chunk_num: Knowledgebase.chunk_num + chunk_num
        })
        kb_result = db.execute(kb_stmt)
        db.commit()
        return kb_result.rowcount

    @classmethod
    def decrement_chunk_num(cls, db: Session, doc_id: str, kb_id: str, token_num: int, chunk_num: int, duration: int):
        """
        减少文档和知识库的片段数量、令牌数量和处理时长（SQLAlchemy 2.0 Core 风格）。

        本方法通过查询指定ID的文档和知识库，在数据库中更新它们的令牌数量、片段数量和处理时长。
        如果文档未找到，则抛出LookupError异常。

        注意：此方法故意不更新 update_time/update_date

        参数:
        - db: 数据库会话对象，用于执行数据库查询和更新操作。
        - doc_id: 需要更新的文档的ID。
        - kb_id: 需要更新的知识库的ID。
        - token_num: 需要减少的令牌数量。
        - chunk_num: 需要减少的片段数量。
        - duration: 需要增加的处理时长。

        返回:
        - kb_update: 知识库更新的影响行数。
        """
        # 更新文档的令牌数量、片段数量和处理时长
        doc_stmt = update(cls.model).where(cls.model.id == doc_id).values({
            cls.model.token_num: cls.model.token_num - token_num,
            cls.model.chunk_num: cls.model.chunk_num - chunk_num,
            cls.model.process_duration: cls.model.process_duration + duration
        })
        doc_result = db.execute(doc_stmt)

        # 如果文档更新影响行数为0，表示未找到文档，抛出异常
        if doc_result.rowcount == 0:
            raise LookupError("Document not found which is supposed to be there")

        # 更新知识库的令牌数量和片段数量
        kb_stmt = update(Knowledgebase).where(Knowledgebase.id == kb_id).values({
            Knowledgebase.token_num: Knowledgebase.token_num - token_num,
            Knowledgebase.chunk_num: Knowledgebase.chunk_num - chunk_num
        })
        kb_result = db.execute(kb_stmt)
        db.commit()
        return kb_result.rowcount

    @classmethod
    def clear_chunk_num(cls, db: Session, doc_id: str, max_retries=3):
        """
        清除文档的 chunk 数量并更新知识库统计（SQLAlchemy 2.0 Core 风格）。
        """
        doc = cls.get_by_id(db, doc_id)
        if not doc:
            raise LookupError("Can't find document in database.")
        retries = 0
        while retries < max_retries:
            try:
                # 读取数据（使用 session.get() 主键直取）
                kb_record = db.get(Knowledgebase, doc.kb_id)

                # 检查数据是否存在，进行更新
                if kb_record:
                    kb_update_stmt = update(Knowledgebase).where(Knowledgebase.id == doc.kb_id).values({
                        Knowledgebase.token_num: Knowledgebase.token_num - doc.token_num,
                        Knowledgebase.chunk_num: Knowledgebase.chunk_num - doc.chunk_num,
                        Knowledgebase.doc_num: Knowledgebase.doc_num - 1
                    })
                    result = db.execute(kb_update_stmt)
                    db.commit()

                    return result.rowcount

            except OperationalError as e:
                # 如果检测到锁冲突（例如数据库锁定），可以选择重试
                db.rollback()
                retries += 1
                wait_time = 2 ** retries  # 使用指数退避策略
                time.sleep(wait_time)
                if retries >= max_retries:
                    raise e  # 达到最大重试次数后抛出异常

            except NoResultFound:
                db.rollback()
                raise LookupError("Knowledgebase entry not found.")

            except Exception as e:
                db.rollback()  # 回滚事务以防止不一致性
                raise e

        return None

    @classmethod
    def clear_chunk_num_when_rerun(cls, db: Session, doc_id):
        """
        重新运行时清除 chunk 数量（SQLAlchemy 2.0 推荐的 session.get() 方式）。
        """
        # 获取文档（使用 session.get() 主键直取）
        doc = db.get(cls.model, doc_id)
        assert doc, "Can't find document in database."

        # 更新知识库统计
        kb_stmt = update(Knowledgebase).where(Knowledgebase.id == doc.kb_id).values({
            Knowledgebase.token_num: Knowledgebase.token_num - doc.token_num,
            Knowledgebase.chunk_num: Knowledgebase.chunk_num - doc.chunk_num,
        })
        result = db.execute(kb_stmt)

        # 提交事务
        db.commit()

        return result.rowcount


    @classmethod
    def get_tenant_id(cls, db: Session, doc_id: str):
        """
        获取文档所属的租户 ID（SQLAlchemy 2.0 Core 风格）。
        """
        # 使用 aliased 创建表别名
        KnowledgebaseAlias = aliased(Knowledgebase)
        DocumentAlias = aliased(Document)
        stmt = (
            select(KnowledgebaseAlias.tenant_id)
            .select_from(DocumentAlias)
            .join(KnowledgebaseAlias, DocumentAlias.kb_id == KnowledgebaseAlias.id)
            .where(DocumentAlias.id == doc_id, KnowledgebaseAlias.status == StatusEnum.VALID.value)
        )
        result = db.execute(stmt).first()
        return result.tenant_id if result else None

    @classmethod
    def get_knowledgebase_id(cls, db: Session, doc_id: str):
        """
        获取文档所属的知识库 ID（SQLAlchemy 2.0 Core 风格）。
        """
        stmt = select(cls.model.kb_id).where(cls.model.id == doc_id)
        result = db.execute(stmt).first()
        return result.kb_id if result else None

    @classmethod
    def get_tenant_id_by_name(cls, db: Session, name: str):
        query = db.query(Knowledgebase.tenant_id).join(Knowledgebase, cls.model.kb_id == Knowledgebase.id
                                                       ).filter(
            cls.model.name == name,
            Knowledgebase.status == StatusEnum.VALID.value
        ).first()
        return query.tenant_id if query else None

    @classmethod
    def accessible(cls, db: Session, doc_id, user_id):
        # 使用 SQLAlchemy 查询文档是否可访问
        docs = db.query(cls.model.id).join(
            Knowledgebase, cls.model.kb_id == Knowledgebase.id
        ).join(
            UserTenant, UserTenant.tenant_id == Knowledgebase.tenant_id
        ).filter(
            cls.model.id == doc_id,
            UserTenant.user_id == user_id
        ).limit(1).all()

        # 如果没有找到文档则返回 False
        if not docs:
            return False
        return True

    @classmethod
    def accessible4deletion(cls, db: Session, doc_id, user_id):
        # 构造查询：join Knowledgebase，再 join UserTenant
        q = (
            db.query(cls.model.id)
            .join(Knowledgebase, cls.model.kb_id == Knowledgebase.id)
            .join(
                UserTenant,
                and_(
                    UserTenant.tenant_id == Knowledgebase.created_by,
                    UserTenant.user_id == user_id
                )
            )
            .filter(
                cls.model.id == doc_id,
                UserTenant.status == StatusEnum.VALID.value,
                or_(
                    UserTenant.role == UserTenantRole.NORMAL,
                    UserTenant.role == UserTenantRole.OWNER
                )
            )
        )

        # 只取一条，存在即可删除
        exists = q.first()
        return exists is not None

    @classmethod
    def get_embd_id(cls, db: Session, doc_id: str):
        query = db.query(cls.model, Knowledgebase.embd_id).join(
            Knowledgebase, cls.model.kb_id == Knowledgebase.id
        ).filter(
            cls.model.id == doc_id,
            Knowledgebase.status == StatusEnum.VALID.value
        ).first()
        return query.embd_id if query else None

    @classmethod
    def get_chunking_config(cls, db: Session, doc_id: str) -> dict | None:
        """
        获取文档的分块配置信息。
        """
        # 检查 model 是否为有效的 SQLAlchemy 模型
        if not hasattr(cls, "model") or not hasattr(cls.model, "id"):
            raise AttributeError("cls.model 必须是一个 SQLAlchemy 模型类，并且定义了 'id' 字段。")

        # 定义别名
        TenantAlias = aliased(Tenant)
        KnowledgebaseAlias = aliased(Knowledgebase)

        # 构建查询
        query = (
            db.query(
                cls.model.id.label("id"),
                cls.model.kb_id.label("kb_id"),
                cls.model.parser_id.label("parser_id"),
                cls.model.parser_config.label("parser_config"),
                KnowledgebaseAlias.language.label("language"),
                KnowledgebaseAlias.embd_id.label("embd_id"),
                KnowledgebaseAlias.name.label("name"),
                TenantAlias.id.label("tenant_id"),
                TenantAlias.img2txt_id.label("img2txt_id"),
                TenantAlias.asr_id.label("asr_id"),
                TenantAlias.llm_id.label("llm_id"),
            )
            .join(KnowledgebaseAlias, cls.model.kb_id == KnowledgebaseAlias.id)
            .join(TenantAlias, KnowledgebaseAlias.tenant_id == TenantAlias.id)
            .filter(cls.model.id == doc_id)
        )

        # 执行查询
        configs = query.all()

        # 如果无结果，返回 None
        if not configs:
            return None

        # 将结果转换为字典
        result = [dict(row._mapping) for row in configs]
        return result[0]

    @classmethod
    def get_doc_id_by_doc_name(cls, db: Session, doc_name: str):
        query = db.query(cls.model.id).filter_by(name=doc_name).first()
        return query.id if query else None

    @classmethod
    def get_doc_ids_by_doc_names(cls, db: Session, doc_names: list[str]) -> list[str]:
        """
        Get document IDs by document names

        Args:
            db: Database session
            doc_names: List of document names

        Returns:
            List of document IDs
        """
        if not doc_names:
            return []

        query = db.query(cls.model.id).filter(cls.model.name.in_(doc_names))
        results = query.all()
        return [result.id for result in results]

    @classmethod
    def get_thumbnails(cls, db: Session, doc_ids: list[str]):
        query = db.query(cls.model.id, cls.model.kb_id, cls.model.thumbnail).filter(cls.model.id.in_(doc_ids))
        rows = query.all()
        return [
            {
                "id": row.id,
                "kb_id": row.kb_id,
                "thumbnail": row.thumbnail
            }
            for row in rows
        ]

    @classmethod
    def update_parser_config(cls, db: Session, id: str, config: dict):
        if not config:
            return
        doc = cls.get_by_id(db, id)
        if not doc:
            raise LookupError(f"Document({id}) not found.")

        def dfs_update(old, new):
            for k, v in new.items():
                if k not in old:
                    old[k] = v
                    continue
                if isinstance(v, dict):
                    assert isinstance(old[k], dict)
                    dfs_update(old[k], v)
                else:
                    old[k] = v

        dfs_update(doc.parser_config, config)
        if not config.get("raptor") and doc.parser_config.get("raptor"):
            del doc.parser_config["raptor"]
        cls.update_by_id(db, id, {"parser_config": doc.parser_config})

    @classmethod
    def get_doc_count(cls, db: Session, tenant_id: str):
        query = db.query(cls.model.id).join(Knowledgebase, Knowledgebase.id == cls.model.kb_id
                                            ).filter(Knowledgebase.tenant_id == tenant_id)
        return query.count()

    @classmethod
    def begin2parse(cls, db: Session, doc_id: str, keep_progress: bool = False):
        info = {
            "progress_msg": "Task is queued...",
            "process_begin_at": get_format_time(),
        }
        if not keep_progress:
            info["progress"] = random.random() * 1 / 100.
            info["run"] = TaskStatus.RUNNING.value
            # keep the doc in DONE state when keep_progress=True for GraphRAG, RAPTOR and Mindmap tasks

        cls.update_by_id(db, doc_id, info)

    @classmethod
    def update_meta_fields(cls, db: Session, doc_id, meta_fields):
        return cls.update_by_id(db, doc_id, {"meta_fields": meta_fields})

    @classmethod
    def get_meta_by_kbs(cls, db: Session, kb_ids):
        stmt = (
            select(cls.model.id, cls.model.meta_fields)
            .where(cls.model.kb_id.in_(kb_ids))
        )

        meta = {}
        for row in db.execute(stmt).mappings():
            doc_id = row["id"]
            fields = row.get("meta_fields") or {}
            if not isinstance(fields, dict):
                continue
            for key, value in fields.items():
                value_str = str(value)
                meta.setdefault(key, {}).setdefault(value_str, []).append(doc_id)

        return meta

    @classmethod
    def get_flatted_meta_by_kbs(cls, db: Session, kb_ids: list[str]) -> dict:
        """
        获取知识库文档的扁平化元数据。

        - 解析字符串化的 JSON meta_fields，跳过非字典或无法解析的值
        - 将列表值展开为单独的条目
          示例: {"tags": ["foo","bar"], "author": "alice"} ->
            meta["tags"]["foo"] = [doc_id], meta["tags"]["bar"] = [doc_id], meta["author"]["alice"] = [doc_id]
        适用于 metadata_condition 过滤和需要遵循列表语义的场景。
        """
        stmt = select(cls.model.id, cls.model.meta_fields).where(cls.model.kb_id.in_(kb_ids))

        meta: dict = {}
        for row in db.execute(stmt).mappings():
            doc_id = row["id"]
            meta_fields = row.get("meta_fields") or {}
            if isinstance(meta_fields, str):
                try:
                    meta_fields = json.loads(meta_fields)
                except Exception:
                    continue
            if not isinstance(meta_fields, dict):
                continue
            for k, v in meta_fields.items():
                if k not in meta:
                    meta[k] = {}
                values = v if isinstance(v, list) else [v]
                for vv in values:
                    if vv is None:
                        continue
                    sv = str(vv)
                    if sv not in meta[k]:
                        meta[k][sv] = []
                    meta[k][sv].append(doc_id)
        return meta

    @classmethod
    def get_metadata_summary(cls, db: Session, kb_id: str) -> dict:
        """
        获取知识库中文档元数据的汇总统计。

        返回: {key: [(value, count), ...], ...}，按计数降序排列
        """
        stmt = select(cls.model.id, cls.model.meta_fields).where(cls.model.kb_id == kb_id)

        summary: dict = {}
        for row in db.execute(stmt).mappings():
            meta_fields = row.get("meta_fields") or {}
            if isinstance(meta_fields, str):
                try:
                    meta_fields = json.loads(meta_fields)
                except Exception:
                    continue
            if not isinstance(meta_fields, dict):
                continue
            for k, v in meta_fields.items():
                values = v if isinstance(v, list) else [v]
                for vv in values:
                    if not vv:
                        continue
                    sv = str(vv)
                    if k not in summary:
                        summary[k] = {}
                    summary[k][sv] = summary[k].get(sv, 0) + 1
        return {
            k: sorted([(val, cnt) for val, cnt in v.items()], key=lambda x: x[1], reverse=True)
            for k, v in summary.items()
        }

    @classmethod
    def batch_update_metadata(cls, db: Session, kb_id: str, doc_ids: list[str],
                              updates: list[dict] | None = None,
                              deletes: list[dict] | None = None) -> int:
        """
        批量更新文档元数据。

        Args:
            db: 数据库会话
            kb_id: 知识库ID
            doc_ids: 要更新的文档ID列表
            updates: 更新操作列表，每个元素包含 {"key": str, "value": any, "match": any (optional)}
            deletes: 删除操作列表，每个元素包含 {"key": str, "value": any (optional)}

        Returns:
            更新的文档数量
        """
        updates = updates or []
        deletes = deletes or []
        if not doc_ids:
            return 0

        def _normalize_meta(meta):
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    return {}
            if not isinstance(meta, dict):
                return {}
            return deepcopy(meta)

        def _str_equal(a, b):
            return str(a) == str(b)

        def _apply_updates(meta: dict) -> bool:
            changed = False
            for upd in updates:
                key = upd.get("key")
                if not key or key not in meta:
                    continue

                new_value = upd.get("value")
                match_provided = "match" in upd
                if isinstance(meta[key], list):
                    if not match_provided:
                        meta[key] = new_value
                        changed = True
                    else:
                        match_value = upd.get("match")
                        replaced = False
                        new_list = []
                        for item in meta[key]:
                            if _str_equal(item, match_value):
                                new_list.append(new_value)
                                replaced = True
                            else:
                                new_list.append(item)
                        if replaced:
                            meta[key] = new_list
                            changed = True
                else:
                    if not match_provided:
                        meta[key] = new_value
                        changed = True
                    else:
                        match_value = upd.get("match")
                        if _str_equal(meta[key], match_value):
                            meta[key] = new_value
                            changed = True
            return changed

        def _apply_deletes(meta: dict) -> bool:
            changed = False
            for d in deletes:
                key = d.get("key")
                if not key or key not in meta:
                    continue
                value = d.get("value", None)
                if isinstance(meta[key], list):
                    if value is None:
                        del meta[key]
                        changed = True
                        continue
                    new_list = [item for item in meta[key] if not _str_equal(item, value)]
                    if len(new_list) != len(meta[key]):
                        if new_list:
                            meta[key] = new_list
                        else:
                            del meta[key]
                        changed = True
                else:
                    if value is None or _str_equal(meta[key], value):
                        del meta[key]
                        changed = True
            return changed

        updated_docs = 0
        stmt = select(cls.model.id, cls.model.meta_fields).where(
            cls.model.id.in_(doc_ids),
            cls.model.kb_id == kb_id
        )
        rows = list(db.execute(stmt).mappings())

        for r in rows:
            meta = _normalize_meta(r.get("meta_fields") or {})
            original_meta = deepcopy(meta)
            changed = _apply_updates(meta)
            changed = _apply_deletes(meta) or changed
            if changed and meta != original_meta:
                db.execute(
                    update(cls.model)
                    .where(cls.model.id == r["id"])
                    .values(
                        meta_fields=meta,
                        update_time=current_timestamp(),
                        update_date=get_format_time()
                    )
                )
                updated_docs += 1

        db.commit()
        return updated_docs

    @classmethod
    def update_progress(cls, db: Session):
        docs = cls.get_unfinished_docs(db)

        cls._sync_progress(db, docs)

    @classmethod
    def update_progress_immediately(cls, db: Session, docs: list[dict]):
        if not docs:
            return

        cls._sync_progress(db, docs)

    @classmethod
    def _sync_progress(cls, db: Session, docs: list[dict]):
        from api.db.services.task_service import TaskService

        for d in docs:
            try:
                # 从元组中提取文档ID
                doc_id = d[0] if isinstance(d, tuple) else d["id"]
                tsks = TaskService.query(db, doc_id=doc_id, order_by="create_time")
                if not tsks:
                    continue
                msg = []
                prg = 0
                finished = True
                bad = 0
                doc = DocumentService.get_by_id(db, doc_id)
                status = doc.run  # TaskStatus.RUNNING.value
                doc_progress = doc.progress if doc and doc.progress else 0.0
                special_task_running = False
                priority = 0

                for t in tsks:
                    task_type = (t.task_type or "").lower()
                    if task_type in PIPELINE_SPECIAL_PROGRESS_FREEZE_TASK_TYPES:
                        special_task_running = True
                    if 0 <= t.progress < 1:
                        finished = False

                    if t.progress == -1:
                        bad += 1
                    prg += t.progress if t.progress >= 0 else 0
                    if t.progress_msg.strip():
                        msg.append(t.progress_msg)
                    priority = max(priority, t.priority)

                prg /= len(tsks)
                if finished and bad:
                    prg = -1
                    status = TaskStatus.FAIL.value
                elif finished:
                    prg = 1
                    status = TaskStatus.DONE.value

                # only for special task and parsed docs and unfinished
                freeze_progress = special_task_running and doc_progress >= 1 and not finished
                msg = "\n".join(sorted(msg))
                begin_at = d.get("process_begin_at")
                if not begin_at:
                    begin_at = datetime.now()
                    # fallback
                    cls.update_by_id(d["id"], {"process_begin_at": begin_at})

                info = {
                    "process_duration": max(datetime.timestamp(datetime.now()) - begin_at.timestamp(), 0),
                    "run": status
                }
                if prg != 0 and not freeze_progress:
                    info["progress"] = prg
                if msg:
                    info["progress_msg"] = msg
                    if msg.endswith("created task graphrag") or msg.endswith("created task raptor") or msg.endswith("created task mindmap"):
                        info["progress_msg"] += "\n%d tasks are ahead in the queue..."%get_queue_length(priority)
                else:
                    info["progress_msg"] = "%d tasks are ahead in the queue..."%get_queue_length(priority)
                cls.update_by_id(db, d["id"], info)
            except Exception as e:
                if str(e).find("'0'") < 0:
                    logging.exception("fetch task exception")

    @classmethod
    def get_kb_doc_count(cls, db: Session, kb_id: str):
        query = db.query(cls.model.id).filter_by(kb_id=kb_id)
        return query.count()

    @classmethod
    def get_all_kb_doc_count(cls, db: Session):
        """
        获取所有知识库的文档数量统计。

        :param db: 数据库会话对象。
        :return: 字典，键为知识库ID，值为对应的文档数量。
        """
        result = {}
        rows = db.query(
            cls.model.kb_id,
            func.count(cls.model.id).label('count')
        ).group_by(cls.model.kb_id).all()
        
        for row in rows:
            result[row.kb_id] = row.count
        return result

    @classmethod
    def parse_web_by_provider(
        cls,
        provider: str,
        url: str,
        options: dict[str, Any] | None,
        credentials: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        统一的网页解析服务入口，根据 provider 调用不同适配器。

        返回统一结构：
        {
          "provider": str,
          "url": str,
          "texts": list[str],
          "raw": dict[str, Any]   # 原始响应，用于排障/扩展
        }
        """
        provider_lc = (provider or "").strip().lower()
        if provider_lc == "tavily":
            from core.utils.tavily_conn import Tavily

            if not credentials or not credentials.get("api_key"):
                raise ValueError("Tavily requires credentials.api_key")

            client = Tavily(api_key=credentials["api_key"])

            # 透传 options 到 tavily extract。Thin 封装已在 conn 层生效
            kwargs: dict[str, Any] = {"urls": url}
            if options:
                # 映射兼容：如果上层传了 format/extract_depth/include_images/include_favicon/timeout
                for k in ["include_images", "extract_depth", "format", "timeout", "include_favicon"]:
                    if k in options:
                        kwargs[k] = options[k]

            raw = client.extract(**kwargs)
            texts: list[str] = []
            for item in raw.get("results", []) or []:
                txt = item.get("raw_content") or ""
                if txt:
                    texts.append(txt)

            # 图片去噪处理
            filtered_images: list[str] | None = None
            image_cleaning_stats: dict | None = None

            clean_images = options.get("clean_images") if options else None
            if clean_images:
                from common.image_utils import ImageFilter

                filter_mode = options.get("image_filter_mode", "strict")

                # 处理每个 result 的文本和图片
                all_dropped_details = []
                total_images_count = 0
                kept_images_set = set()

                # 清洗文本中的图片引用
                cleaned_texts = []
                for idx, item in enumerate(raw.get("results", []) or []):
                    raw_content = item.get("raw_content") or ""
                    if raw_content:
                        cleaned_text, dropped_from_text, total_in_text = ImageFilter.clean_markdown_images(
                            raw_content, mode=filter_mode, base_url=url
                        )
                        cleaned_texts.append(cleaned_text)
                        all_dropped_details.extend(dropped_from_text)

                # 过滤图片 URL 列表
                for item in raw.get("results", []) or []:
                    original_images = item.get("images") or []
                    total_images_count += len(original_images)

                    kept_urls, dropped_from_array = ImageFilter.filter_image_urls(
                        original_images, mode=filter_mode, base_url=url
                    )
                    kept_images_set.update(kept_urls)
                    all_dropped_details.extend(dropped_from_array)

                # 去重 dropped_details（基于 URL）
                seen_urls = set()
                unique_dropped = []
                for detail in all_dropped_details:
                    if detail["url"] not in seen_urls:
                        seen_urls.add(detail["url"])
                        unique_dropped.append(detail)

                # 更新 texts 和构建返回数据
                if cleaned_texts:
                    texts = cleaned_texts

                # 从保留列表中移除所有被删除的图片（修复文本清洗与URL过滤的矛盾）
                dropped_urls = {detail["url"] for detail in unique_dropped}
                filtered_images = sorted([url for url in kept_images_set if url not in dropped_urls])

                dropped_count = len(unique_dropped)
                kept_count = total_images_count - dropped_count

                image_cleaning_stats = {
                    "enabled": True,
                    "mode": filter_mode,
                    "total_images": total_images_count,
                    "dropped_count": dropped_count,
                    "kept_count": kept_count,
                    "dropped_details": unique_dropped,
                }

            # 构建返回数据
            result = {"provider": provider_lc, "url": url, "texts": texts, "raw": raw}
            if filtered_images is not None:
                result["filtered_images"] = filtered_images
            if image_cleaning_stats is not None:
                result["image_cleaning_stats"] = image_cleaning_stats

            return result

        elif provider_lc == "jinareader":
            # 预留：未来在 core/utils/jina_conn.py 中实现后在此对接
            # 暂时抛出明确错误，提示尚未集成
            raise NotImplementedError("Provider 'jinareader' is not integrated yet. Please add core/utils/jina_conn.py and wire here.")

        else:
            raise ValueError(f"Unsupported provider: {provider}")

    @classmethod
    def do_cancel(cls, db: Session, doc_id):
        try:
            _, doc = DocumentService.get_by_id(db, doc_id)
            return doc.run == TaskStatus.CANCEL.value or doc.progress < 0
        except Exception as e:
            pass
        return False

    @classmethod
    def knowledgebase_basic_info(cls, db: Session, kb_id: str) -> dict[str, int]:
        """
        获取知识库的文档处理基本信息统计
        
        Args:
            db: SQLAlchemy Session
            kb_id: 知识库ID
            
        Returns:
            dict: 包含 processing, finished, failed, cancelled 数量的字典
        """
        from sqlalchemy import case
        
        # cancelled: run == "2" (TaskStatus.CANCEL)
        cancelled_query = select(func.count()).select_from(cls.model).where(
            and_(
                cls.model.kb_id == kb_id,
                cls.model.run == TaskStatus.CANCEL
            )
        )
        cancelled = db.execute(cancelled_query).scalar() or 0

        # downloaded: source_type != "local" (从外部数据源同步的文档)
        downloaded_query = select(func.count()).select_from(cls.model).where(
            and_(
                cls.model.kb_id == kb_id,
                cls.model.source_type != "local"
            )
        )
        downloaded = db.execute(downloaded_query).scalar() or 0

        # 统计其他状态的文档
        stats_query = select(
            # finished: progress == 1
            func.coalesce(
                func.sum(case((cls.model.progress == 1, 1), else_=0)),
                0
            ).label("finished"),
            
            # failed: progress == -1
            func.coalesce(
                func.sum(case((cls.model.progress == -1, 1), else_=0)),
                0
            ).label("failed"),
            
            # processing: 0 <= progress < 1
            func.coalesce(
                func.sum(
                    case(
                        (
                            or_(
                                cls.model.progress == 0,
                                and_(cls.model.progress > 0, cls.model.progress < 1)
                            ),
                            1
                        ),
                        else_=0
                    )
                ),
                0
            ).label("processing"),
        ).select_from(cls.model).where(
            and_(
                cls.model.kb_id == kb_id,
                or_(
                    cls.model.run.is_(None),
                    cls.model.run != TaskStatus.CANCEL
                )
            )
        )
        
        result = db.execute(stats_query).first()

        return {
            "processing": int(result.processing) if result else 0,
            "finished": int(result.finished) if result else 0,
            "failed": int(result.failed) if result else 0,
            "cancelled": int(cancelled),
            "downloaded": int(downloaded),
        }


    @classmethod
    def run(cls, db: Session, tenant_id: str, doc: dict, kb_table_num_map: dict):
        from api.db.services.task_service import queue_dataflow, queue_tasks
        from api.db.services.file2document_service import File2DocumentService

        doc["tenant_id"] = tenant_id
        doc_parser = doc.get("parser_id", ParserType.NAIVE)
        if doc_parser == ParserType.TABLE:
            kb_id = doc.get("kb_id")
            if not kb_id:
                return
            if kb_id not in kb_table_num_map:
                count = DocumentService.count_by_kb_id(db, kb_id=kb_id, keywords="", run_status=[TaskStatus.DONE], types=[])
                kb_table_num_map[kb_id] = count
                if kb_table_num_map[kb_id] <= 0:
                    KnowledgebaseService.delete_field_map(db, kb_id)
        if doc.get("pipeline_id", ""):
            queue_dataflow(db, tenant_id, flow_id=doc["pipeline_id"], task_id=get_uuid(), doc_id=doc["id"])
        else:
            bucket, name = File2DocumentService.get_storage_address(db, doc_id=doc["id"])
            queue_tasks(db, doc, bucket, name, 0)


def queue_raptor_o_graphrag_tasks(db, sample_doc_id, ty, priority, fake_doc_id="", doc_ids=[]):
    """
    You can provide a fake_doc_id to bypass the restriction of tasks at the knowledgebase level.
    Optionally, specify a list of doc_ids to determine which documents participate in the task.
    """
    assert ty in ["graphrag", "raptor", "mindmap"], "type should be graphrag, raptor or mindmap"

    chunking_config = DocumentService.get_chunking_config(db, sample_doc_id["id"])
    hasher = xxhash.xxh64()
    for field in sorted(chunking_config.keys()):
        hasher.update(str(chunking_config[field]).encode("utf-8"))

    def new_task():
        nonlocal sample_doc_id
        return {
            "id": get_uuid(),
            "doc_id": sample_doc_id["id"],
            "from_page": 100000000,
            "to_page": 100000000,
            "task_type": ty,
            "progress_msg":  datetime.now().strftime("%H:%M:%S") + " created task " + ty,
            "begin_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    task = new_task()
    for field in ["doc_id", "from_page", "to_page"]:
        hasher.update(str(task.get(field, "")).encode("utf-8"))
    hasher.update(ty.encode("utf-8"))
    task["digest"] = hasher.hexdigest()
    bulk_insert_into_db(db, Task, [task], True)

    task["doc_id"] = fake_doc_id
    task["doc_ids"] = doc_ids
    DocumentService.begin2parse(db, sample_doc_id["id"], keep_progress=True)
    assert REDIS_CONN.queue_product(settings.get_svr_queue_name(priority), message=task), "Can't access Redis. Please check the Redis' status."
    return task["id"]


async def queue_analyze_v2_task(db, doc_id, kb_id, config, user_id, file=None, priority=0):
    """
    创建 analyze_v2 任务并加入队列
    
    参考: queue_raptor_o_graphrag_tasks
    
    Args:
        db: 数据库会话
        doc_id: 文档ID（可选）
        kb_id: 知识库ID（可选）
        config: 分析配置字典
        user_id: 用户ID
        file: 上传的文件对象（可选）
        priority: 任务优先级
        
    Returns:
        task_id: 任务ID
    """
    
    task_id = get_uuid()
    
    # 处理直传文件
    temp_file_path = None
    if file:
        # 保存文件到临时位置，供 task_executor 读取
        if hasattr(file, 'filename'):
            fname = file.filename
        else:
            raise ValueError("File must have filename attribute")
        
        # 读取文件内容（FastAPI 的 UploadFile.read() 是异步的）
        if hasattr(file, 'read'):
            file_content = await file.read()
        else:
            raise ValueError("File must be readable")
        
        # 保存到 MinIO 临时位置
        temp_location = f"temp/analyze_v2/{task_id}/{fname}"
        settings.STORAGE_IMPL.put("multirag-temp", temp_location, file_content)
        
        temp_file_path = temp_location
        logging.info(f"Saved temp file to MinIO: {temp_location}")
    
    # 构建任务数据
    # 注意：doc_id 字段最大长度为 32 个字符，如果没有提供 doc_id，使用 task_id（32字符）
    # 而不是 f"temp-{task_id}" 会超过长度限制
    task_data = {
        "id": task_id,
        "doc_id": doc_id or task_id,  # 直接使用 task_id，不加前缀
        "task_type": "analyze_v2",
        "begin_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "progress": 0,
        "progress_msg": f"{datetime.now().strftime('%H:%M:%S')} 任务已创建",
        "priority": priority,
        "from_page": 0,
        "to_page": 100000000,
        # 使用 chunk_ids 存储配置
        "chunk_ids": json.dumps({
            "config": config,
            "kb_id": kb_id,
            "user_id": user_id,
            "enable_sse": config.get("enable_sse", False),
            "temp_file_path": temp_file_path,
            "is_temp_file": file is not None,  # 标记是否是临时文件
            "result": None
        }, ensure_ascii=False)
    }
    
    # 创建 Task 记录（传递字典而不是 ORM 对象）
    bulk_insert_into_db(db, Task, [task_data], True)
    
    # 发送到 Redis 队列
    queue_message = {
        "id": task_id,
        "task_type": "analyze_v2",
        "doc_id": task_data["doc_id"],
        "kb_id": kb_id,
        "priority": priority
    }
    
    success = REDIS_CONN.queue_product(settings.get_svr_queue_name(priority), message=queue_message)
    
    if not success:
        logging.error(f"Failed to send task {task_id} to Redis queue")
        raise Exception("Can't access Redis. Please check the Redis' status.")
    
    logging.info(f"Created and queued analyze_v2 task: {task_id}, enable_sse: {config.get('enable_sse')}")
    
    return task_id


def get_queue_length(priority):
    group_info = REDIS_CONN.queue_info(settings.get_svr_queue_name(priority), SVR_CONSUMER_GROUP_NAME)
    if not group_info:
        return 0
    return int(group_info.get("lag", 0) or 0)


def doc_upload_and_parse(db, conversation_id, file_objs, user_id):
    from api.db.services.api_service import API4ConversationService
    from api.db.services.conversation_service import ConversationService
    from api.db.services.dialog_service import DialogService
    from api.db.services.file_service import FileService
    from api.db.services.llm_service import LLMBundle
    from api.db.services.user_service import TenantService
    from core.app import audio, email, naive, picture, presentation

    conv = ConversationService.get_by_id(db, conversation_id)
    if not conv:
        conv = API4ConversationService.get_by_id(db, conversation_id)
    assert conv, "Conversation not found!"

    dia = DialogService.get_by_id(db, conv.dialog_id)
    if not dia.kb_ids:
        raise LookupError("No dataset associated with this conversation. "
                          "Please add a dataset before uploading documents")
    kb_id = dia.kb_ids[0]
    kb = KnowledgebaseService.get_by_id(db, kb_id)
    if not kb:
        raise LookupError("Can't find this dataset!")

    embd_mdl = LLMBundle(db, kb.tenant_id, LLMType.EMBEDDING, llm_name=kb.embd_id, lang=kb.language)

    err, files = FileService.upload_document(db, kb, file_objs, user_id)
    assert not err, "\n".join(err)

    def dummy(prog=None, msg=""):
        pass

    FACTORY = {
        ParserType.PRESENTATION.value: presentation,
        ParserType.PICTURE.value: picture,
        ParserType.AUDIO.value: audio,
        ParserType.EMAIL.value: email
    }
    parser_config = {"chunk_token_num": 4096, "delimiter": "\n!?;。；！？", "layout_recognize": "Plain Text", "table_context_size": 0, "image_context_size": 0}
    exe = ThreadPoolExecutor(max_workers=12)
    threads = []
    doc_nm = {}
    for d, blob in files:
        doc_nm[d["id"]] = d["name"]
    for d, blob in files:
        kwargs = {
            "callback": dummy,
            "parser_config": parser_config,
            "from_page": 0,
            "to_page": 100000,
            "tenant_id": kb.tenant_id,
            "lang": kb.language
        }
        threads.append(exe.submit(FACTORY.get(d["parser_id"], naive).chunk, d["name"], blob, **kwargs))

    for (docinfo, _), th in zip(files, threads):
        docs = []
        doc = {
            "doc_id": docinfo["id"],
            "kb_id": [kb.id]
        }
        for ck in th.result():
            d = deepcopy(doc)
            d.update(ck)
            d["id"] = xxhash.xxh64((ck["content_with_weight"] + str(d["doc_id"])).encode("utf-8")).hexdigest()
            d["create_time"] = str(datetime.now()).replace("T", " ")[:19]
            d["create_timestamp_flt"] = datetime.now().timestamp()
            if not d.get("image"):
                docs.append(d)
                continue

            output_buffer = BytesIO()
            if isinstance(d["image"], bytes):
                output_buffer = BytesIO(d["image"])
            else:
                d["image"].save(output_buffer, format='JPEG')

            settings.STORAGE_IMPL.put(kb.id, d["id"], output_buffer.getvalue())
            d["img_id"] = "{}-{}".format(kb.id, d["id"])
            d.pop("image", None)
            docs.append(d)

    parser_ids = {d["id"]: d["parser_id"] for d, _ in files}
    docids = [d["id"] for d, _ in files]
    chunk_counts = {id: 0 for id in docids}
    token_counts = {id: 0 for id in docids}
    es_bulk_size = 64

    def embedding(doc_id, cnts, batch_size=16):
        nonlocal embd_mdl, chunk_counts, token_counts
        vectors = []
        for i in range(0, len(cnts), batch_size):
            vts, c = embd_mdl.encode(cnts[i: i + batch_size])
            vectors.extend(vts.tolist())
            chunk_counts[doc_id] += len(cnts[i:i + batch_size])
            token_counts[doc_id] += c
        return vectors

    idxnm = search.index_name(kb.tenant_id, [kb.name])
    try_create_idx = True

    tenant = TenantService.get_by_id(db, kb.tenant_id)
    llm_bdl = LLMBundle(db, kb.tenant_id, LLMType.CHAT, tenant.llm_id)
    for doc_id in docids:
        cks = [c for c in docs if c["doc_id"] == doc_id]

        if parser_ids[doc_id] != ParserType.PICTURE.value:
            from graphrag.general.mind_map_extractor import MindMapExtractor
            mindmap = MindMapExtractor(llm_bdl)
            try:
                mind_map = asyncio.run(mindmap([c["content_with_weight"] for c in docs if c["doc_id"] == doc_id]))
                mind_map = json.dumps(mind_map.output, ensure_ascii=False, indent=2)
                if len(mind_map) < 32:
                    raise Exception("Few content: " + mind_map)
                cks.append({
                    "id": get_uuid(),
                    "doc_id": doc_id,
                    "kb_id": [kb.id],
                    "docnm_kwd": doc_nm[doc_id],
                    "title_tks": rag_tokenizer.tokenize(re.sub(r"\.[a-zA-Z]+$", "", doc_nm[doc_id])),
                    "content_ltks": rag_tokenizer.tokenize("summary summarize 总结 概况 file 文件 概括"),
                    "content_with_weight": mind_map,
                    "knowledge_graph_kwd": "mind_map"
                })
            except Exception:
                logging.exception("Mind map generation error")

        vectors = embedding(doc_id, [c["content_with_weight"] for c in cks])
        assert len(cks) == len(vectors)
        for i, d in enumerate(cks):
            v = vectors[i]
            d["q_%d_vec" % len(v)] = v
        for b in range(0, len(cks), es_bulk_size):
            if try_create_idx:
                if not settings.docStoreConn.indexExist(idxnm, kb_id):
                    settings.docStoreConn.createIdx(idxnm, kb_id, len(vectors[0]))
                try_create_idx = False
            settings.docStoreConn.insert(cks[b:b + es_bulk_size], idxnm, kb_id)

        DocumentService.increment_chunk_num(
            db, doc_id, kb.id, token_counts[doc_id], chunk_counts[doc_id], 0)

    return [d["id"] for d, _ in files]
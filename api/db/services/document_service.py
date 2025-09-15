# coding=utf-8
"""
@project: multirag
@Author：龙
@file： document_service.py
@date：2024/8/14 11:00
@desc:
"""
import logging
import random
import time
from datetime import datetime
from typing import Any

import trio
import xxhash
from pymilvus import MilvusException
from sqlalchemy.exc import NoResultFound, OperationalError
from sqlalchemy.orm import Session, aliased
from sqlalchemy import func, asc, and_, or_, select, desc as sa_desc

from api.constants import IMG_BASE64_PREFIX, FILE_NAME_LEN_LIMIT
from api.db import FileType, TaskStatus, StatusEnum, UserTenantRole
from api.db.db_models import Document, Knowledgebase, Tenant, Task, UserTenant, db_connection, File2Document, File
from api.db.services.common_service import CommonService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils import current_timestamp, get_format_time, get_uuid
from api.utils.db_utils import bulk_insert_into_db
# from api.settings import docStoreConn
from api import settings
from core.settings import get_svr_queue_name, SVR_CONSUMER_GROUP_NAME
from core.nlp import search, rag_tokenizer
from core.utils.storage_factory import STORAGE_IMPL
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
            name: str = None
    ):
        # 1) 需要返回的列 —— 等价于 Peewee 的 select(*fields)
        #    确保 get_cls_model_fields() 返回的是 Column/ColumnElement 列对象，而不是字符串
        fields: list = cls.get_cls_model_fields()

        # 2) 基础查询（含 join）
        base = (
            select(*fields)
            .select_from(cls.model)
            .join(File2Document, File2Document.document_id == cls.model.id)
            .join(File, File.id == File2Document.file_id)
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

        # 7) 执行并返回“字典行”（等价 Peewee 的 .dicts()）
        rows = db.execute(stmt).mappings().all()
        return [dict(r) for r in rows], total

    @classmethod
    def check_doc_health(cls, db: Session, tenant_id: str, filename):
        import os
        MAX_FILE_NUM_PER_USER = int(os.environ.get("MAX_FILE_NUM_PER_USER", 0))
        if MAX_FILE_NUM_PER_USER > 0 and DocumentService.get_doc_count(db, tenant_id) >= MAX_FILE_NUM_PER_USER:
            raise RuntimeError("Exceed the maximum file number of a free user!")
        if len(filename.encode("utf-8")) > FILE_NAME_LEN_LIMIT:
            raise RuntimeError("Exceed the maximum length of file name!")
        return True

    @classmethod
    def get_by_kb_id(cls, db: Session, kb_id: str, page_number: int, items_per_page: int,
                     orderby: str, desc: bool, keywords: str | None,
                     run_status: list | None = None, types: list | None = None, suffix: list = None) -> tuple[list[dict], int]:
        if suffix is None:
            suffix = []
        fields = cls.get_cls_model_fields()
        query = (
            db.query(*fields)
            .select_from(cls.model)
            .join(File2Document, File2Document.document_id == cls.model.id)
            .join(File, File.id == File2Document.file_id)
            .filter(cls.model.kb_id == kb_id)
        )
        if keywords:
            query = query.filter(func.lower(cls.model.name).contains(keywords.lower()))

        if run_status:
            query = query.filter(cls.model.run.in_(run_status))

        if types:
            query = query.filter(cls.model.type.in_(types))

        if suffix:
            query = query.filter(cls.model.suffix.in_(suffix))

        count = query.count()

        if desc:
            query = query.order_by(getattr(cls.model, orderby).desc())
        else:
            query = query.order_by(getattr(cls.model, orderby).asc())

        if page_number and items_per_page:
            docs = query.offset((page_number - 1) * items_per_page).limit(items_per_page).all()
        else:
            docs = query.all()

        col_names = [getattr(c, "key", getattr(c, "name", None)) for c in fields]

        return [dict(zip(col_names, row)) for row in docs], count

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
        base_from = (
            db.query(cls.model.id)  # 这里只取 id 作为锚点
            .select_from(cls.model)
            .join(File2Document, File2Document.document_id == cls.model.id)
            .join(File, File.id == File2Document.file_id)
            .filter(*filters)
        )

        # 3) total：按文档去重计数，避免一文档多文件被重复计算
        total = (
            db.query(func.count(func.distinct(cls.model.id)))
            .select_from(cls.model)
            .join(File2Document, File2Document.document_id == cls.model.id)
            .join(File, File.id == File2Document.file_id)
            .filter(*filters)
            .scalar()
        )

        # 4) suffix 分布：同理对 Document.id 去重计数
        suffix_stats = (
            db.query(
                cls.model.suffix,
                func.count(func.distinct(cls.model.id)).label("count")
            )
            .select_from(cls.model)
            .join(File2Document, File2Document.document_id == cls.model.id)
            .join(File, File.id == File2Document.file_id)
            .filter(*filters)
            .group_by(cls.model.suffix)
            .all()
        )

        # 5) run_status 分布：同理
        run_status_stats = (
            db.query(
                cls.model.run,
                func.count(func.distinct(cls.model.id)).label("count")
            )
            .select_from(cls.model)
            .join(File2Document, File2Document.document_id == cls.model.id)
            .join(File, File.id == File2Document.file_id)
            .filter(*filters)
            .group_by(cls.model.run)
            .all()
        )

        # 6) 组装返回
        suffix_counter = {row.suffix: row.count for row in suffix_stats}
        run_status_counter = {str(row.run): row.count for row in run_status_stats}

        return {
            "suffix": suffix_counter,
            "run_status": run_status_counter
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
    def preview_document_chunks(
        cls,
        db: Session,
        doc_id: str,
        parser_config_override: dict | None = None,
        limit: int | None = None,
        override_parser_id: str | None = None,
    ) -> list[str]:
        """
        仅执行文档切片，不进行向量化/入库，返回切片后的纯文本列表。

        - 根据文档的 parser_id 与 parser_config，调用对应 parser 的 chunk() 实现
        - 统一转为文本列表返回：若 parser 返回 dict 列表，则提取 content_with_weight
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
        file_bin = STORAGE_IMPL.get(bucket, name)

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

        # 统一为文本列表
        chunks_text: list[str] = []
        if isinstance(result, list):
            if not result:
                chunks_text = []
            else:
                first = result[0]
                if isinstance(first, dict) and "content_with_weight" in first:
                    chunks_text = [d.get("content_with_weight", "") for d in result]
                elif isinstance(first, str):
                    chunks_text = result
                else:
                    chunks_text = [str(x) for x in result]
        else:
            chunks_text = [str(result)]

        if isinstance(limit, int) and limit > 0:
            chunks_text = chunks_text[:limit]
        return chunks_text

    @classmethod
    def preview_document_chunks_batched(
        cls,
        db: Session,
        doc_id: str,
        parser_config_override: dict | None = None,
        batch_size: int = 50,
        batch_id: str | None = None,
        session_ttl: int = 1800,
        override_parser_id: str | None = None,
        batch_index: int | None = None,
    ) -> dict:
        """
        仅切片预览的批次化接口：
        - 首次调用（无 batch_id）：计算切片、创建预览会话，返回首批数据与 batch_id。
        - 后续调用（带 batch_id）：从会话中读取下一批数据，直到结束删除会话。
        - 会话存储于 Redis，TTL 默认 30 分钟。
        返回：{"batch_id", "chunks", "count", "total", "has_more"}
        """
        import json

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

        # 规范 batch_size
        try:
            bs = int(batch_size)
            if bs <= 0:
                bs = 50
        except Exception:
            bs = 50

        # 会话 key
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

        # 如果无会话或摘要不匹配，则创建新会话
        if not session or session.get("digest") != digest:
            # 重新切片
            all_chunks = cls.preview_document_chunks(
                db,
                doc_id=doc_id,
                parser_config_override=parser_config_override,
                limit=None,
                override_parser_id=override_parser_id,
            )

            batch_id = get_uuid()
            session_key = f"preview:session:{batch_id}"
            session = {
                "digest": digest,
                "doc_id": doc_id,
                "from": effective_from,
                "to": effective_to,
                "total": len(all_chunks),
                "offset": 0,
                "chunks": all_chunks,
            }
            REDIS_CONN.set_obj(session_key, session, exp=session_ttl)

        # 计算返回批次
        start = int(session.get("offset", 0))
        total = int(session.get("total", 0))
        if isinstance(batch_index, int) and batch_index >= 0:
            start = min(batch_index * bs, total)
        end = min(start + bs, total)
        batch = session.get("chunks", [])[start:end]
        has_more = end < total
        current_batch_index = (start // bs) if bs > 0 else 0
        total_batches = (total + bs - 1) // bs if bs > 0 else 0

        # 更新或删除会话
        if batch_index is None:
            # 顺序模式：按 offset 推进；最后一批删除会话
            if has_more:
                session["offset"] = end
                REDIS_CONN.set_obj(session_key, session, exp=session_ttl)
            else:
                REDIS_CONN.delete(session_key)
        else:
            # 并发批次模式：如果已经取到最后一批（has_more=false），立即删除会话
            if not has_more:
                REDIS_CONN.delete(session_key)

        return {
            "batch_id": batch_id,
            "chunks": batch,
            "count": len(batch),
            "total": total,
            "has_more": has_more,
            "batch_index": current_batch_index,
            "total_batches": total_batches,
        }

    @classmethod
    def _get_allowed_parsers_for_filename(cls, filename: str) -> set[str]:
        from api.db import ParserType
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
        from api.db import ParserType as PT
        return {PT.NAIVE.value}

    @classmethod
    def _get_module_by_parser_id(cls, parser_id: str):
        from api.db import ParserType
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
        from api.db import ParserType
        if re.search(r"\.(ppt|pptx)$", f):
            return cls._get_module_by_parser_id(ParserType.PRESENTATION.value), ParserType.PRESENTATION.value
        if re.search(r"\.(csv|xlsx?|xls)$", f):
            return cls._get_module_by_parser_id(ParserType.TABLE.value), ParserType.TABLE.value
        if re.search(r"\.(jpg|jpeg|png|gif|bmp|tif|tiff|webp|svg|ico)$", f):
            return cls._get_module_by_parser_id(ParserType.PICTURE.value), ParserType.PICTURE.value
        if re.search(r"\.eml$", f):
            return cls._get_module_by_parser_id(ParserType.EMAIL.value), ParserType.EMAIL.value
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
            # 不传 tenant_id，解析器内部会降级跳过视觉增强等
        )

        # 统一为文本列表
        chunks_text: list[str] = []
        if isinstance(result, list):
            if not result:
                chunks_text = []
            else:
                first = result[0]
                if isinstance(first, dict) and "content_with_weight" in first:
                    chunks_text = [d.get("content_with_weight", "") for d in result]
                elif isinstance(first, str):
                    chunks_text = result
                else:
                    chunks_text = [str(x) for x in result]
        else:
            chunks_text = [str(result)]
        return chunks_text

    @classmethod
    def preview_file_chunks_batched(
        cls,
        db: Session,
        filename: str,
        file_bytes: bytes,
        parser_config_override: dict | None = None,
        batch_size: int = 50,
        batch_id: str | None = None,
        session_ttl: int = 1800,
        override_parser_id: str | None = None,
        language: str | None = None,
        batch_index: int | None = None,
    ) -> dict:
        import json

        language = language or "Chinese"

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
        digest = hasher.hexdigest()

        # 规范 batch_size
        try:
            bs = int(batch_size)
            if bs <= 0:
                bs = 50
        except Exception:
            bs = 50

        # 会话恢复
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

        if not session or session.get("digest") != digest:
            all_chunks = cls.preview_file_chunks(
                db,
                filename,
                file_bytes,
                parser_config_override=base_cfg,
                override_parser_id=override_parser_id,
                language=language,
            )
            batch_id = get_uuid()
            session_key = f"preview:file_session:{batch_id}"
            session = {
                "digest": digest,
                "filename": filename,
                "total": len(all_chunks),
                "offset": 0,
                "chunks": all_chunks,
            }
            REDIS_CONN.set_obj(session_key, session, exp=session_ttl)

        start = int(session.get("offset", 0))
        total = int(session.get("total", 0))
        if isinstance(batch_index, int) and batch_index >= 0:
            start = min(batch_index * bs, total)
        end = min(start + bs, total)
        batch = session.get("chunks", [])[start:end]
        has_more = end < total
        current_batch_index = (start // bs) if bs > 0 else 0
        total_batches = (total + bs - 1) // bs if bs > 0 else 0

        # 同样：顺序模式推进 offset；并发批次模式在最后一批时清理会话
        if batch_index is None:
            if has_more:
                session["offset"] = end
                REDIS_CONN.set_obj(session_key, session, exp=session_ttl)
            else:
                REDIS_CONN.delete(session_key)
        else:
            if not has_more:
                REDIS_CONN.delete(session_key)

        return {
            "batch_id": batch_id,
            "chunks": batch,
            "count": len(batch),
            "total": total,
            "has_more": has_more,
            "batch_index": current_batch_index,
            "total_batches": total_batches,
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
            raise RuntimeError("Database error (Knowledgebase)!")
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
            chunk_ids = settings.docStoreConn.getChunkIds(chunks)
            if not chunk_ids:
                break
            all_chunk_ids.extend(chunk_ids)
            page += 1
        for cid in all_chunk_ids:
            if STORAGE_IMPL.obj_exist(doc.kb_id, cid):
                STORAGE_IMPL.rm(doc.kb_id, cid)
        if doc.thumbnail and not doc.thumbnail.startswith(IMG_BASE64_PREFIX):
            if STORAGE_IMPL.obj_exist(doc.kb_id, doc.thumbnail):
                STORAGE_IMPL.rm(doc.kb_id, doc.thumbnail)

        try:
            # 检查集合是否存在并删除 Milvus 中的数据
            if settings.docStoreConn.has_collection(collection_name):
                settings.docStoreConn.delete(
                    collection_name=collection_name,
                    filter=f"doc_id == '{doc_id}'"
                )
            # todo 待测试【settings.docStoreConn.delete等】，测试成功则替换上面的方法 优先级较高，不然graphrag玩不转
            # kb_id = document["kb_id"]  # 使用从数据库重新获取的kb_id
            # graph_source = settings.docStoreConn.getFields(
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
        except MilvusException as e:
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
        query = db.query(
            cls.model.id, cls.model.process_begin_at, cls.model.parser_config,
            cls.model.progress_msg, cls.model.run, cls.model.parser_id
        ).filter(
            cls.model.status == StatusEnum.VALID.value,
            cls.model.type != FileType.VIRTUAL.value,
            cls.model.progress < 1,
            cls.model.progress > 0
        )
        rows = query.all()
        return [dict(row._mapping) for row in rows]

    @classmethod
    def increment_chunk_num(cls, db: Session, doc_id, kb_id, token_num, chunk_num, duration):
        """
        更新文档和知识库的片段数量、令牌数量和处理时长。

        本方法通过查询指定ID的文档和知识库，在数据库中更新它们的令牌数量、片段数量和处理时长。
        如果文档未找到，则抛出LookupError异常。

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
        doc_update = db.query(cls.model).filter_by(id=doc_id).update({
            cls.model.token_num: cls.model.token_num + token_num,
            cls.model.chunk_num: cls.model.chunk_num + chunk_num,
            cls.model.process_duration: cls.model.process_duration + duration
        })

        # 如果文档更新影响行数为0，表示未找到文档，抛出异常
        if doc_update == 0:
            raise LookupError("Document not found which is supposed to be there")

        # 更新知识库的令牌数量和片段数量
        kb_update = db.query(Knowledgebase).filter_by(id=kb_id).update({
            Knowledgebase.token_num: Knowledgebase.token_num + token_num,
            Knowledgebase.chunk_num: Knowledgebase.chunk_num + chunk_num
        })
        db.commit()
        return kb_update

    @classmethod
    def decrement_chunk_num(cls, db: Session, doc_id: str, kb_id: str, token_num: int, chunk_num: int, duration: int):
        """
        减少文档和知识库的片段数量、令牌数量和处理时长。

        本方法通过查询指定ID的文档和知识库，在数据库中更新它们的令牌数量、片段数量和处理时长。
        如果文档未找到，则抛出LookupError异常。

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
        doc_update = db.query(cls.model).filter_by(id=doc_id).update({
            cls.model.token_num: cls.model.token_num - token_num,
            cls.model.chunk_num: cls.model.chunk_num - chunk_num,
            cls.model.process_duration: cls.model.process_duration + duration
        })

        # 如果文档更新影响行数为0，表示未找到文档，抛出异常
        if doc_update == 0:
            raise LookupError("Document not found which is supposed to be there")

        # 更新知识库的令牌数量和片段数量
        kb_update = db.query(Knowledgebase).filter_by(id=kb_id).update({
            Knowledgebase.token_num: Knowledgebase.token_num - token_num,
            Knowledgebase.chunk_num: Knowledgebase.chunk_num - chunk_num
        })
        db.commit()
        return kb_update

    @classmethod
    def clear_chunk_num(cls, db: Session, doc_id: str, max_retries=3):
        doc = cls.get_by_id(db, doc_id)
        if not doc:
            raise LookupError("Can't find document in database.")
        retries = 0
        while retries < max_retries:
            try:
                # 读取数据
                kb_record = db.query(Knowledgebase).filter_by(id=doc.kb_id).first()

                # 检查数据是否存在，进行更新
                if kb_record:
                    kb_update = db.query(Knowledgebase).filter_by(id=doc.kb_id).update({
                        Knowledgebase.token_num: Knowledgebase.token_num - doc.token_num,
                        Knowledgebase.chunk_num: Knowledgebase.chunk_num - doc.chunk_num,
                        Knowledgebase.doc_num: Knowledgebase.doc_num - 1
                    })
                    db.commit()

                    return kb_update

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
        # 获取文档
        doc = db.query(cls.model).filter(cls.model.id == doc_id).first()
        assert doc, "Can't find document in database."

        # 更新知识库统计
        num = (
            db.query(Knowledgebase)
            .filter(Knowledgebase.id == doc.kb_id)
            .update({
                Knowledgebase.token_num: Knowledgebase.token_num - doc.token_num,
                Knowledgebase.chunk_num: Knowledgebase.chunk_num - doc.chunk_num,
            })
        )

        # 提交事务
        db.commit()

        return num


    @classmethod
    def get_tenant_id(cls, db: Session, doc_id: str):
        # 使用 aliased 创建表别名
        KnowledgebaseAlias = aliased(Knowledgebase)
        DocumentAlias = aliased(Document)
        query = db.query(KnowledgebaseAlias.tenant_id) \
            .select_from(DocumentAlias) \
            .join(KnowledgebaseAlias, DocumentAlias.kb_id == KnowledgebaseAlias.id) \
            .filter(DocumentAlias.id == doc_id, KnowledgebaseAlias.status == StatusEnum.VALID.value) \
            .first()
        # query = db.query(Knowledgebase.tenant_id).join(Knowledgebase, cls.model.kb_id == Knowledgebase.id
        #                                                ).filter(
        #     cls.model.id == doc_id,
        #     Knowledgebase.status == StatusEnum.VALID.value
        # ).first()
        return query.tenant_id if query else None

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
    def begin2parse(cls, db: Session, doc_id: str):
        cls.update_by_id(db, doc_id, {
            "progress": random.random() * 1 / 100.,
            "progress_msg": "Task is queued...",
            "process_begin_at": get_format_time()
        })

    @classmethod
    def update_meta_fields(cls, db: Session, doc_id, meta_fields):
        return cls.update_by_id(db, doc_id, {"meta_fields": meta_fields})

    @classmethod
    def update_progress(cls, db: Session):
        docs = cls.get_unfinished_docs(db)
        for d in docs:
            try:
                # 从元组中提取文档ID
                doc_id = d[0] if isinstance(d, tuple) else d["id"]
                tsks = db.query(Task).filter_by(doc_id=doc_id).order_by(Task.create_time).all()
                if not tsks:
                    continue
                msg = []
                prg = 0
                finished = True
                bad = 0
                has_raptor = False
                has_graphrag = False
                doc = DocumentService.get_by_id(db, doc_id)
                status = doc.run  # TaskStatus.RUNNING.value
                priority = 0

                # 安全获取parser_config
                parser_config = getattr(doc, 'parser_config', {})

                for t in tsks:
                    if 0 <= t.progress < 1:
                        finished = False

                    if t.progress == -1:
                        bad += 1
                    prg += t.progress if t.progress >= 0 else 0
                    if t.progress_msg.strip():
                        msg.append(t.progress_msg)
                    if t.task_type == "raptor":
                        has_raptor = True
                    elif t.task_type == "graphrag":
                        has_graphrag = True
                    priority = max(priority, t.priority)

                prg /= len(tsks)
                if finished and bad:
                    prg = -1
                    status = TaskStatus.FAIL.value
                elif finished:
                    if (d["parser_config"].get("raptor") or {}).get("use_raptor") and not has_raptor:
                        queue_raptor_o_graphrag_tasks(db, d, "raptor", priority)
                        prg = 0.98 * len(tsks) / (len(tsks) + 1)
                    elif (d["parser_config"].get("graphrag") or {}).get("use_graphrag") and not has_graphrag:
                        queue_raptor_o_graphrag_tasks(db, d, "graphrag", priority)
                        prg = 0.98 * len(tsks) / (len(tsks) + 1)
                    else:
                        status = TaskStatus.DONE.value

                msg = "\n".join(sorted(msg))
                info = {
                    "process_duration": datetime.timestamp(datetime.now()) - d["process_begin_at"].timestamp(),
                    "run": status
                }
                if prg != 0:
                    info["progress"] = prg
                if msg:
                    info["progress_msg"] = msg
                    if msg.endswith("created task graphrag") or msg.endswith("created task raptor"):
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
            return {"provider": provider_lc, "url": url, "texts": texts, "raw": raw}

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


def queue_raptor_o_graphrag_tasks(db, doc, ty, priority):
    chunking_config = DocumentService.get_chunking_config(db, doc["id"])
    hasher = xxhash.xxh64()
    for field in sorted(chunking_config.keys()):
        hasher.update(str(chunking_config[field]).encode("utf-8"))

    def new_task():
        nonlocal doc
        return {
            "id": get_uuid(),
            "doc_id": doc["id"],
            "from_page": 100000000,
            "to_page": 100000000,
            "task_type": ty,
            "progress_msg":  datetime.now().strftime("%H:%M:%S") + " created task " + ty
        }

    task = new_task()
    for field in ["doc_id", "from_page", "to_page"]:
        hasher.update(str(task.get(field, "")).encode("utf-8"))
    hasher.update(ty.encode("utf-8"))
    task["digest"] = hasher.hexdigest()
    bulk_insert_into_db(db, Task, [task], True)
    assert REDIS_CONN.queue_product(get_svr_queue_name(priority), message=task), "Can't access Redis. Please check the Redis' status."


def get_queue_length(priority):
    group_info = REDIS_CONN.queue_info(get_svr_queue_name(priority), SVR_CONSUMER_GROUP_NAME)
    return int(group_info.get("lag", 0) or 0)


# def doc_upload_and_parse(conversation_id, file_objs, user_id):
#     from core.app import presentation, picture, naive, audio, email
#     from api.db.services.dialog_service import ConversationService, DialogService
#     from api.db.services.file_service import FileService
#     from api.db.services.llm_service import LLMBundle
#     from api.db.services.user_service import TenantService
#     from api.db.services.api_service import API4ConversationService
#
#     e, conv = ConversationService.get_by_id(conversation_id)
#     if not e:
#         e, conv = API4ConversationService.get_by_id(conversation_id)
#     assert e, "Conversation not found!"
#
#     e, dia = DialogService.get_by_id(conv.dialog_id)
#     kb_id = dia.kb_ids[0]
#     e, kb = KnowledgebaseService.get_by_id(kb_id)
#     if not e:
#         raise LookupError("Can't find this knowledgebase!")
#
#     idxnm = search.index_name(kb.tenant_id)
#     if not ELASTICSEARCH.indexExist(idxnm):
#         ELASTICSEARCH.createIdx(idxnm, json.load(
#             open(os.path.join(get_project_base_directory(), "conf", "mapping.json"), "r")))
#
#     embd_mdl = LLMBundle(kb.tenant_id, LLMType.EMBEDDING, llm_name=kb.embd_id, lang=kb.language)
#
#     err, files = FileService.upload_document(kb, file_objs, user_id)
#     assert not err, "\n".join(err)
#
#     def dummy(prog=None, msg=""):
#         pass
#
#     FACTORY = {
#         ParserType.PRESENTATION.value: presentation,
#         ParserType.PICTURE.value: picture,
#         ParserType.AUDIO.value: audio,
#         ParserType.EMAIL.value: email
#     }
#     parser_config = {"chunk_token_num": 4096, "delimiter": "\n!?;。；！？", "layout_recognize": False}
#     exe = ThreadPoolExecutor(max_workers=12)
#     threads = []
#     for d, blob in files:
#         kwargs = {
#             "callback": dummy,
#             "parser_config": parser_config,
#             "from_page": 0,
#             "to_page": 100000,
#             "tenant_id": kb.tenant_id,
#             "lang": kb.language
#         }
#         threads.append(exe.submit(FACTORY.get(d["parser_id"], naive).chunk, d["name"], blob, **kwargs))
#
#     for (docinfo, _), th in zip(files, threads):
#         docs = []
#         doc = {
#             "doc_id": docinfo["id"],
#             "kb_id": [kb.id]
#         }
#         for ck in th.result():
#             d = deepcopy(doc)
#             d.update(ck)
#             md5 = hashlib.md5()
#             md5.update((ck["content_with_weight"] +
#                         str(d["doc_id"])).encode("utf-8"))
#             d["_id"] = md5.hexdigest()
#             d["create_time"] = str(datetime.now()).replace("T", " ")[:19]
#             d["create_timestamp_flt"] = datetime.now().timestamp()
#             if not d.get("image"):
#                 docs.append(d)
#                 continue
#
#             output_buffer = BytesIO()
#             if isinstance(d["image"], bytes):
#                 output_buffer = BytesIO(d["image"])
#             else:
#                 d["image"].save(output_buffer, format='JPEG')
#
#             MINIO.put(kb.id, d["_id"], output_buffer.getvalue())
#             d["img_id"] = "{}-{}".format(kb.id, d["_id"])
#             d.pop("image", None)
#             docs.append(d)
#
#     parser_ids = {d["id"]: d["parser_id"] for d, _ in files}
#     docids = [d["id"] for d, _ in files]
#     chunk_counts = {id: 0 for id in docids}
#     token_counts = {id: 0 for id in docids}
#     es_bulk_size = 64
#
#     def embedding(doc_id, cnts, batch_size=16):
#         nonlocal embd_mdl, chunk_counts, token_counts
#         vects = []
#         for i in range(0, len(cnts), batch_size):
#             vts, c = embd_mdl.encode(cnts[i: i + batch_size])
#             vects.extend(vts.tolist())
#             chunk_counts[doc_id] += len(cnts[i:i + batch_size])
#             token_counts[doc_id] += c
#         return vects
#
#     _, tenant = TenantService.get_by_id(kb.tenant_id)
#     llm_bdl = LLMBundle(kb.tenant_id, LLMType.CHAT, tenant.llm_id)
#     for doc_id in docids:
#         cks = [c for c in docs if c["doc_id"] == doc_id]
#
#         if parser_ids[doc_id] != ParserType.PICTURE.value:
#             mindmap = MindMapExtractor(llm_bdl)
#             try:
#                 mind_map = json.dumps(mindmap([c["content_with_weight"] for c in docs if c["doc_id"] == doc_id]).output,
#                                       ensure_ascii=False, indent=2)
#                 if len(mind_map) < 32: raise Exception("Few content: " + mind_map)
#                 cks.append({
#                     "id": get_uuid(),
#                     "doc_id": doc_id,
#                     "kb_id": [kb.id],
#                     "content_with_weight": mind_map,
#                     "knowledge_graph_kwd": "mind_map"
#                 })
#             except Exception:
#                 logging.exception("Mind map generation error")
#
#         vects = embedding(doc_id, [c["content_with_weight"] for c in cks])
#         assert len(cks) == len(vects)
#         for i, d in enumerate(cks):
#             v = vects[i]
#             d["q_%d_vec" % len(v)] = v
#         for b in range(0, len(cks), es_bulk_size):
#             ELASTICSEARCH.bulk(cks[b:b + es_bulk_size], idxnm)
#
#         DocumentService.increment_chunk_num(
#             doc_id, kb.id, token_counts[doc_id], chunk_counts[doc_id], 0)
#
#     return [d["id"] for d,_ in files]

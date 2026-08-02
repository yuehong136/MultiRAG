"""
@project: multirag
@Author：龙
@file： connector_service.py
@date：2024/7/9 9:00
@desc: 数据源连接器相关服务
"""

import logging
import os
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import case, cast, delete, func, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import INTERVAL as Interval
from sqlalchemy.orm import Session
from sqlalchemy.sql import desc as sa_desc

from api.db import InputType
from api.db.db_models import Connector, Connector2Kb, Knowledgebase, SyncLogs
from api.db.services.common_service import CommonService
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from api.utils.common import hash128
from common.constants import TaskStatus
from common.misc_utils import get_uuid

logger = logging.getLogger(__name__)


def _to_utc(value: datetime | None) -> datetime | None:
    """Normalize connector timestamps at the persistence boundary.

    A few upstream connectors still return naive datetimes.  RAGFlow treats
    those values as UTC, so we keep that compatibility in one explicit place
    while ensuring every value persisted by the sync service is timezone-aware.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class ConnectorService(CommonService):
    """
    数据源连接器服务类，提供连接器的CRUD操作。
    """

    model = Connector

    @classmethod
    def accessible(cls, db: Session, connector_id: str, user_id: str) -> bool:
        """判断用户能否访问该连接器。

        连接器的 config 里存着数据源凭证（Google/Box 的 OAuth token 等），按 id 直取
        必须先过这一关，否则任意登录用户都能读走别人的凭证。

        Args:
            db: 数据库会话
            connector_id: 连接器ID
            user_id: 当前用户ID

        Returns:
            属于本人租户或本人已加入该租户时为 True
        """
        connector = cls.get_by_id(db, connector_id)
        if not connector:
            logger.warning("connector access denied: connector not found connector_id=%s user_id=%s", connector_id, user_id)
            return False

        if connector.tenant_id == user_id:
            return True

        from api.db.services.user_service import UserTenantService

        role = UserTenantService.get_role_in_tenant(db, user_id=user_id, tenant_id=connector.tenant_id)
        has_access = UserTenantService.can_access_tenant_resources(role)
        if not has_access:
            logger.warning(
                "connector access denied: tenant mismatch connector_id=%s user_id=%s tenant_id=%s",
                connector_id,
                user_id,
                connector.tenant_id,
            )
        return has_access

    @classmethod
    def resume(cls, db: Session, connector_id: str, status: str):
        """
        恢复连接器任务状态

        Args:
            db: 数据库会话
            connector_id: 连接器ID
            status: 目标状态
        """
        c2k_list = Connector2KbService.query(db, connector_id=connector_id)
        for c2k in c2k_list:
            task = SyncLogsService.get_latest_task(db, connector_id, c2k.kb_id)
            if not task:
                if status == TaskStatus.SCHEDULE:
                    SyncLogsService.schedule(db, connector_id, c2k.kb_id)
                    cls.update_by_id(db, connector_id, {"status": status})
                    return
                continue

            if task.status == TaskStatus.DONE:
                if status == TaskStatus.SCHEDULE:
                    SyncLogsService.schedule(db, connector_id, c2k.kb_id, poll_range_start=task.poll_range_end, total_docs_indexed=task.total_docs_indexed)
                    cls.update_by_id(db, connector_id, {"status": status})
                    return

            task_dict = task.to_dict()
            task_dict["status"] = status
            SyncLogsService.update_by_id(db, task_dict["id"], task_dict)

        cls.update_by_id(db, connector_id, {"status": status})

    @classmethod
    def list(cls, db: Session, tenant_id: str) -> list[dict]:
        """
        获取租户的连接器列表

        Args:
            db: 数据库会话
            tenant_id: 租户ID

        Returns:
            连接器列表
        """
        stmt = select(cls.model.id, cls.model.name, cls.model.source, cls.model.status).where(cls.model.tenant_id == tenant_id)
        rows = db.execute(stmt).mappings().all()
        return [dict(row) for row in rows]

    @classmethod
    def rebuild(cls, db: Session, connector_id: str, kb_id: str, tenant_id: str) -> str | None:
        """
        重建连接器与知识库的关联

        Args:
            db: 数据库会话
            connector_id: 连接器ID
            kb_id: 知识库ID
            tenant_id: 租户ID

        Returns:
            错误信息（如果有）
        """
        conn = cls.get_by_id(db, connector_id)
        if not conn:
            return "连接器不存在"
        SyncLogsService.filter_delete(db, [SyncLogs.connector_id == connector_id, SyncLogs.kb_id == kb_id])
        docs = DocumentService.query(db, source_type=f"{conn.source}/{conn.id}", kb_id=kb_id)
        err = FileService.delete_docs(db, [d.id for d in docs], tenant_id)
        SyncLogsService.schedule(db, connector_id, kb_id, reindex=True)
        return err

    @classmethod
    def cleanup_stale_documents_for_task(
        cls,
        db: Session,
        task_id: str,
        connector_id: str,
        kb_id: str,
        tenant_id: str,
        file_list,
        delete_batch_size: int = 100,
    ):
        """
        删除源端已不存在、但本地仍保留的连接器文档。
        """
        if not Connector2KbService.query(db, connector_id=connector_id, kb_id=kb_id):
            return 0, []

        conn = cls.get_by_id(db, connector_id)
        if not conn:
            return 0, []

        source_type = f"{conn.source}/{conn.id}"
        retain_doc_ids = {hash128(file.id) for file in file_list}
        existing_docs = DocumentService.list_doc_headers_by_kb_and_source_type(
            db,
            kb_id,
            source_type,
        )
        stale_doc_ids = [doc["id"] for doc in existing_docs if doc["id"] not in retain_doc_ids]
        if not stale_doc_ids:
            return 0, []

        stale_doc_id_set = set(stale_doc_ids)
        errors = []
        for offset in range(0, len(stale_doc_ids), delete_batch_size):
            err = FileService.delete_docs(
                db,
                stale_doc_ids[offset : offset + delete_batch_size],
                tenant_id,
            )
            if err:
                errors.append(err)

        remaining_doc_ids = {
            doc["id"]
            for doc in DocumentService.list_doc_headers_by_kb_and_source_type(
                db,
                kb_id,
                source_type,
            )
            if doc["id"] in stale_doc_id_set
        }
        removed_count = len(stale_doc_id_set) - len(remaining_doc_ids)
        SyncLogsService.increase_removed_docs(
            db,
            task_id,
            removed_count,
            "\n".join(errors),
            len(errors),
        )
        return removed_count, errors


class SyncLogsService(CommonService):
    """
    同步日志服务类，提供同步任务的管理操作。
    """

    model = SyncLogs

    @classmethod
    def list_sync_tasks(cls, db: Session, connector_id: str | None = None, page_number: int | None = None, items_per_page: int = 15) -> tuple[list[dict], int]:
        """
        获取同步任务列表

        Args:
            db: 数据库会话
            connector_id: 连接器ID（可选）
            page_number: 页码（可选）
            items_per_page: 每页数量

        Returns:
            同步任务列表
        """
        # 基础查询字段
        columns = [
            cls.model.id,
            cls.model.connector_id,
            cls.model.kb_id,
            cls.model.update_date,
            cls.model.update_time,  # 添加 update_time，用于 ORDER BY（PostgreSQL DISTINCT 要求）
            cls.model.poll_range_start,
            cls.model.poll_range_end,
            cls.model.new_docs_indexed,
            cls.model.total_docs_indexed,
            cls.model.error_msg,
            cls.model.full_exception_trace,
            cls.model.error_count,
            Connector.name,
            Connector.source,
            Connector.tenant_id,
            Connector.timeout_secs,
            Knowledgebase.name.label("kb_name"),
            Knowledgebase.avatar.label("kb_avatar"),
            Connector2Kb.auto_parse,
            cls.model.from_beginning.label("reindex"),
            cls.model.status,
        ]
        if not connector_id:
            columns.append(Connector.config)

        # 构建基础查询 (SQLAlchemy 2.0 style)
        stmt = (
            select(*columns)
            .select_from(cls.model)
            .join(Connector, cls.model.connector_id == Connector.id)
            .join(Connector2Kb, (cls.model.kb_id == Connector2Kb.kb_id) & (cls.model.connector_id == Connector2Kb.connector_id))
            .join(Knowledgebase, cls.model.kb_id == Knowledgebase.id)
        )

        if connector_id:
            stmt = stmt.where(cls.model.connector_id == connector_id)
        else:
            # 计算时间间隔，查询需要执行的定时任务
            # 根据数据库类型选择正确的 INTERVAL 语法
            database_type = os.getenv("DB_TYPE", "postgresql")
            if "postgres" in database_type.lower():
                # PostgreSQL 使用 INTERVAL 表达式
                # 构造: NOW() - (refresh_freq || ' minutes')::INTERVAL
                interval_expr = func.now() - cast(Connector.refresh_freq.concat(literal_column("' minutes'")), Interval)
                time_condition = cls.model.update_date < interval_expr
            else:
                # MySQL 使用 TIMESTAMPDIFF 函数
                # TIMESTAMPDIFF(MINUTE, update_date, NOW()) > refresh_freq
                time_condition = func.timestampdiff(text("MINUTE"), cls.model.update_date, func.now()) > Connector.refresh_freq
            stmt = stmt.where(Connector.input_type == InputType.POLL, Connector.status == TaskStatus.SCHEDULE, cls.model.status == TaskStatus.SCHEDULE, time_condition)

        stmt = stmt.distinct().order_by(sa_desc(cls.model.update_time))
        total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar()

        if page_number:
            offset = (page_number - 1) * items_per_page
            stmt = stmt.offset(offset).limit(items_per_page)

        rows = db.execute(stmt).mappings().all()
        return [dict(row) for row in rows], total

    @classmethod
    def start(cls, db: Session, task_id: str, connector_id: str):
        """
        开始同步任务

        Args:
            db: 数据库会话
            task_id: 任务ID
            connector_id: 连接器ID
        """
        now = datetime.now(UTC)
        timestamp = cls.current_timestamp()
        db.execute(update(cls.model).where(cls.model.id == task_id).values(status=TaskStatus.RUNNING, time_started=now, update_date=now, update_time=timestamp))
        db.execute(update(Connector).where(Connector.id == connector_id).values(status=TaskStatus.RUNNING, update_date=now, update_time=timestamp))
        db.commit()

    @classmethod
    def done(cls, db: Session, task_id: str, connector_id: str):
        """
        完成同步任务

        Args:
            db: 数据库会话
            task_id: 任务ID
            connector_id: 连接器ID
        """
        now = datetime.now(UTC)
        timestamp = cls.current_timestamp()
        db.execute(update(cls.model).where(cls.model.id == task_id).values(status=TaskStatus.DONE, update_date=now, update_time=timestamp))
        db.execute(update(Connector).where(Connector.id == connector_id).values(status=TaskStatus.DONE, update_date=now, update_time=timestamp))
        db.commit()

    @classmethod
    def fail(cls, db: Session, task_id: str, connector_id: str, error_msg: str, full_exception_trace: str = "") -> None:
        """Fail a sync task and its connector in one transaction."""
        now = datetime.now(UTC)
        timestamp = cls.current_timestamp()
        db.execute(
            update(cls.model)
            .where(cls.model.id == task_id)
            .values(
                status=TaskStatus.FAIL,
                error_msg=error_msg,
                full_exception_trace=full_exception_trace,
                update_date=now,
                update_time=timestamp,
            )
        )
        db.execute(update(Connector).where(Connector.id == connector_id).values(status=TaskStatus.FAIL, update_date=now, update_time=timestamp))
        db.commit()

    @classmethod
    def schedule(
        cls,
        db: Session,
        connector_id: str,
        kb_id: str,
        poll_range_start: datetime | None = None,
        reindex: bool = False,
        total_docs_indexed: int = 0,
    ) -> SyncLogs | None:
        """
        调度同步任务

        Args:
            db: 数据库会话
            connector_id: 连接器ID
            kb_id: 知识库ID
            poll_range_start: 轮询起始时间
            reindex: 是否重新索引
            total_docs_indexed: 已索引文档总数
        """
        poll_range_start = _to_utc(poll_range_start)
        log_count = db.scalar(select(func.count()).select_from(cls.model).where(cls.model.kb_id == kb_id, cls.model.connector_id == connector_id)) or 0
        if log_count > 100:
            old_ids = list(db.scalars(select(cls.model.id).where(cls.model.kb_id == kb_id, cls.model.connector_id == connector_id).order_by(cls.model.update_time.asc()).limit(70)))
            if old_ids:
                deleted = db.execute(delete(cls.model).where(cls.model.id.in_(old_ids))).rowcount
                logger.info("[SyncLogService] Cleaned %s old logs.", deleted)

        existing = db.scalar(
            select(cls.model.id).where(
                cls.model.kb_id == kb_id,
                cls.model.connector_id == connector_id,
                cls.model.status == TaskStatus.SCHEDULE,
            )
        )
        if existing:
            logger.warning("%s--%s already has a scheduled sync task.", kb_id, connector_id)
            db.rollback()
            return None

        now = datetime.now(UTC)
        timestamp = cls.current_timestamp()
        task = cls.model(
            id=get_uuid(),
            kb_id=kb_id,
            status=TaskStatus.SCHEDULE,
            connector_id=connector_id,
            poll_range_start=poll_range_start,
            from_beginning="1" if reindex else "0",
            total_docs_indexed=total_docs_indexed,
        )
        db.add(task)
        db.execute(update(Connector).where(Connector.id == connector_id).values(status=TaskStatus.SCHEDULE, update_date=now, update_time=timestamp))
        db.commit()
        db.refresh(task)
        return task

    @classmethod
    def complete_and_schedule_next(
        cls,
        db: Session,
        task_id: str,
        connector_id: str,
        kb_id: str,
        checkpoint: datetime,
    ) -> str:
        """Commit task completion, its checkpoint, and the next task atomically."""
        checkpoint = _to_utc(checkpoint)
        if checkpoint is None:
            raise ValueError("A completed sync task requires a checkpoint")

        current = db.scalar(select(cls.model).where(cls.model.id == task_id).with_for_update())
        if current is None:
            raise RuntimeError(f"Sync task {task_id} no longer exists")

        now = datetime.now(UTC)
        timestamp = cls.current_timestamp()
        current.status = TaskStatus.DONE
        current.poll_range_end = checkpoint
        current.update_date = now
        current.update_time = timestamp

        next_task = cls.model(
            id=get_uuid(),
            kb_id=kb_id,
            status=TaskStatus.SCHEDULE,
            connector_id=connector_id,
            poll_range_start=checkpoint,
            from_beginning="0",
            total_docs_indexed=current.total_docs_indexed,
        )
        db.add(next_task)
        db.execute(update(Connector).where(Connector.id == connector_id).values(status=TaskStatus.SCHEDULE, update_date=now, update_time=timestamp))
        db.commit()
        return next_task.id

    @classmethod
    def increase_docs(cls, db: Session, task_id: str, max_update: datetime, doc_num: int, err_msg: str = "", error_count: int = 0) -> int:
        """
        增加已索引文档数量

        Args:
            db: 数据库会话
            task_id: 任务ID
            max_update: 最大更新时间
            doc_num: 文档数量
            err_msg: 错误消息
            error_count: 错误计数
        """
        max_update = _to_utc(max_update)
        if max_update is None:
            raise ValueError("A synchronized document batch requires max_update")

        monotonic_start = case(
            (cls.model.poll_range_start.is_(None), max_update),
            (cls.model.poll_range_start < max_update, max_update),
            else_=cls.model.poll_range_start,
        )
        monotonic_end = case(
            (cls.model.poll_range_end.is_(None), max_update),
            (cls.model.poll_range_end < max_update, max_update),
            else_=cls.model.poll_range_end,
        )
        result = db.execute(
            update(cls.model)
            .where(cls.model.id == task_id)
            .values(
                new_docs_indexed=cls.model.new_docs_indexed + doc_num,
                total_docs_indexed=cls.model.total_docs_indexed + doc_num,
                poll_range_start=monotonic_start,
                poll_range_end=monotonic_end,
                error_msg=func.coalesce(cls.model.error_msg, "") + err_msg,
                error_count=cls.model.error_count + error_count,
                update_time=cls.current_timestamp(),
                update_date=datetime.now(UTC),
            )
        )
        db.commit()
        return result.rowcount

    @classmethod
    def increase_removed_docs(
        cls,
        db: Session,
        task_id: str,
        removed_count: int,
        err_msg: str = "",
        error_count: int = 0,
    ) -> int:
        """
        增加从索引中移除的文档数量。
        """
        result = db.execute(
            update(cls.model)
            .where(cls.model.id == task_id)
            .values(
                docs_removed_from_index=cls.model.docs_removed_from_index + removed_count,
                error_msg=func.coalesce(cls.model.error_msg, "") + err_msg,
                error_count=cls.model.error_count + error_count,
                update_time=cls.current_timestamp(),
                update_date=datetime.now(UTC),
            )
        )
        db.commit()
        return result.rowcount

    @classmethod
    def duplicate_and_parse(cls, db: Session, kb, docs: list, tenant_id, src: str, auto_parse=True):
        """
        复制并解析文档

        Args:
            db: 数据库会话
            kb: 知识库对象
            docs: 文档列表
            tenant_id: 当前租户id
            src: 来源
            auto_parse: 是否自动解析

        Returns:
            (错误列表, 文档ID列表)
        """
        if not docs:
            return [], []

        class FileObj(BaseModel):
            id: str
            filename: str
            blob: bytes

            def read(self) -> bytes:
                return self.blob

        # 将文档转换为 FileObj 对象，携带 id 以支持重复检测
        files = [FileObj(id=d["id"], filename=d["semantic_identifier"] + (f"{d['extension']}" if d["semantic_identifier"][::-1].find(d["extension"][::-1]) < 0 else ""), blob=d["blob"]) for d in docs]

        # Create a mapping from filename to metadata for later use
        metadata_map = {}
        for d in docs:
            if d.get("metadata"):
                filename = d["semantic_identifier"] + (f"{d['extension']}" if d["semantic_identifier"][::-1].find(d["extension"][::-1]) < 0 else "")
                metadata_map[filename] = d["metadata"]

        doc_ids = []
        errs, doc_blob_pairs = FileService.upload_document(db, kb, files, tenant_id, None, src)
        kb_table_num_map = {}
        for doc, _ in doc_blob_pairs:
            doc_ids.append(doc["id"])

            # Set metadata if available for this document
            if doc["name"] in metadata_map:
                DocMetadataService.update_document_metadata(db, doc["id"], metadata_map[doc["name"]])

            if not auto_parse or auto_parse == "0":
                continue
            DocumentService.run(db, tenant_id, doc, kb_table_num_map)

        return errs, doc_ids

    @classmethod
    def get_latest_task(cls, db: Session, connector_id: str, kb_id: str) -> SyncLogs | None:
        """
        获取最新的同步任务

        Args:
            db: 数据库会话
            connector_id: 连接器ID
            kb_id: 知识库ID

        Returns:
            最新的同步任务，如果没有则返回 None
        """
        stmt = select(cls.model).where(cls.model.connector_id == connector_id, cls.model.kb_id == kb_id).order_by(sa_desc(cls.model.update_time)).limit(1)
        return db.execute(stmt).scalar_one_or_none()


class Connector2KbService(CommonService):
    """
    连接器与知识库关联服务类
    """

    model = Connector2Kb

    @classmethod
    def link_connectors(cls, db: Session, kb_id: str, connectors: list[dict], tenant_id: str) -> str:
        """
        关联连接器到知识库

        Args:
            db: 数据库会话
            kb_id: 知识库ID
            connectors: 连接器列表，每个元素包含 id 和可选的 auto_parse 字段
            tenant_id: 租户ID

        Returns:
            错误信息（如果有）
        """
        # 获取现有关联
        old_conn_ids = [a.connector_id for a in cls.query(db, kb_id=kb_id)]

        # 添加或更新传入的关联
        connector_ids = []
        for conn in connectors:
            conn_id = conn["id"]
            connector_ids.append(conn_id)
            cls.link_connector(db, kb_id, conn_id, conn.get("auto_parse", "1"))

        # 删除不再需要的关联
        for conn_id in old_conn_ids:
            if conn_id in connector_ids:
                continue
            cls.unlink_connector(db, kb_id, conn_id)

        return ""

    @classmethod
    def link_connector(cls, db: Session, kb_id: str, connector_id: str, auto_parse: str = "1") -> None:
        """关联单个连接器到知识库；已关联时只更新 auto_parse（幂等）。

        Args:
            db: 数据库会话
            kb_id: 知识库ID
            connector_id: 连接器ID
            auto_parse: 是否自动解析（"0" / "1"）
        """
        if cls.query(db, kb_id=kb_id, connector_id=connector_id):
            cls.filter_update(db, [cls.model.connector_id == connector_id, cls.model.kb_id == kb_id], {"auto_parse": auto_parse})
            return

        cls.insert(db, **{"id": get_uuid(), "connector_id": connector_id, "kb_id": kb_id, "auto_parse": auto_parse})
        SyncLogsService.schedule(db, connector_id, kb_id, reindex=True)

    @classmethod
    def unlink_connector(cls, db: Session, kb_id: str, connector_id: str) -> None:
        """解除单个连接器与知识库的关联。

        取消调度中/运行中的同步任务，但不删除已同步入库的文档。

        Args:
            db: 数据库会话
            kb_id: 知识库ID
            connector_id: 连接器ID
        """
        cls.filter_delete(db, [cls.model.kb_id == kb_id, cls.model.connector_id == connector_id])

        if not ConnectorService.get_by_id(db, connector_id):
            return

        SyncLogsService.filter_update(
            db,
            [SyncLogs.connector_id == connector_id, SyncLogs.kb_id == kb_id, SyncLogs.status.in_([TaskStatus.SCHEDULE, TaskStatus.RUNNING])],
            {"status": TaskStatus.CANCEL},
        )

    @classmethod
    def list_connectors(cls, db: Session, kb_id: str) -> list[dict]:
        """
        列出知识库关联的连接器

        Args:
            db: 数据库会话
            kb_id: 知识库ID

        Returns:
            连接器列表
        """
        stmt = (
            select(Connector.id, Connector.source, Connector.name, cls.model.auto_parse, Connector.status)
            .select_from(cls.model)
            .join(Connector, cls.model.connector_id == Connector.id)
            .where(cls.model.kb_id == kb_id)
        )
        rows = db.execute(stmt).mappings().all()
        return [dict(row) for row in rows]

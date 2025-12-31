# coding=utf-8
"""
@project: multirag
@Author：龙
@file： connector_service.py
@date：2024/7/9 9:00
@desc: 数据源连接器相关服务
"""
import logging
import os
from datetime import datetime

from sqlalchemy import select, func, text
from sqlalchemy.sql import desc as sa_desc
from sqlalchemy.orm import Session

from api.db import InputType
from common.constants import TaskStatus
from api.db.db_models import Connector, SyncLogs, Connector2Kb, Knowledgebase
from api.db.services.common_service import CommonService
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from common.misc_utils import get_uuid

logger = logging.getLogger(__name__)


class ConnectorService(CommonService):
    """
    数据源连接器服务类，提供连接器的CRUD操作。
    """
    model = Connector

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
                    SyncLogsService.schedule(
                        db, connector_id, c2k.kb_id,
                        poll_range_start=task.poll_range_end,
                        total_docs_indexed=task.total_docs_indexed
                    )
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
        stmt = (
            select(
                cls.model.id,
                cls.model.name,
                cls.model.source,
                cls.model.status
            )
            .where(cls.model.tenant_id == tenant_id)
        )
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


class SyncLogsService(CommonService):
    """
    同步日志服务类，提供同步任务的管理操作。
    """
    model = SyncLogs

    @classmethod
    def list_sync_tasks(
        cls,
        db: Session,
        connector_id: str | None = None,
        page_number: int | None = None,
        items_per_page: int = 15
    ) -> tuple[list[dict], int]:
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
            cls.model.status
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
                # PostgreSQL 使用 make_interval 函数
                interval_expr = func.now() - func.make_interval(mins=Connector.refresh_freq)
                time_condition = cls.model.update_date < interval_expr
            else:
                # MySQL 使用 TIMESTAMPDIFF 函数
                # TIMESTAMPDIFF(MINUTE, update_date, NOW()) > refresh_freq
                time_condition = func.timestampdiff(text("MINUTE"), cls.model.update_date, func.now()) > Connector.refresh_freq
            stmt = stmt.where(
                Connector.input_type == InputType.POLL,
                Connector.status == TaskStatus.SCHEDULE,
                cls.model.status == TaskStatus.SCHEDULE,
                time_condition
            )

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
        cls.update_by_id(db, task_id, {
            "status": TaskStatus.RUNNING,
            "time_started": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
        ConnectorService.update_by_id(db, connector_id, {"status": TaskStatus.RUNNING})

    @classmethod
    def done(cls, db: Session, task_id: str, connector_id: str):
        """
        完成同步任务

        Args:
            db: 数据库会话
            task_id: 任务ID
            connector_id: 连接器ID
        """
        cls.update_by_id(db, task_id, {"status": TaskStatus.DONE})
        ConnectorService.update_by_id(db, connector_id, {"status": TaskStatus.DONE})

    @classmethod
    def schedule(
        cls,
        db: Session,
        connector_id: str,
        kb_id: str,
        poll_range_start: str | None = None,
        reindex: bool = False,
        total_docs_indexed: int = 0
    ):
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
        try:
            if cls.query_count(db, kb_id=kb_id, connector_id=connector_id) > 100:
                rm_objs = cls.query(db, kb_id=kb_id, connector_id=connector_id, order_by="update_time", desc=False, limit=70)
                rm_ids = [m.id for m in rm_objs]
                deleted = cls.delete_by_ids(db, rm_ids)
                logger.info(f"[SyncLogService] Cleaned {deleted} old logs.")
        except Exception as e:
            logger.exception(e)

        try:
            # 检查是否已有调度中的任务
            existing = cls.query(db, kb_id=kb_id, connector_id=connector_id, status=TaskStatus.SCHEDULE)
            if existing:
                logger.warning(f"{kb_id}--{connector_id} 已有调度中的同步任务，这是异常情况。")
                return None

            reindex_flag = "1" if reindex else "0"
            ConnectorService.update_by_id(db, connector_id, {"status": TaskStatus.SCHEDULE})
            return cls.insert(db, **{
                "id": get_uuid(),
                "kb_id": kb_id,
                "status": TaskStatus.SCHEDULE,
                "connector_id": connector_id,
                "poll_range_start": poll_range_start,
                "from_beginning": reindex_flag,
                "total_docs_indexed": total_docs_indexed
            })
        except Exception as e:
            logger.exception(f"调度同步任务失败: {e}")
            task = cls.get_latest_task(db, connector_id, kb_id)
            if task:
                # 更新已有任务的状态
                update_data = {
                    "status": TaskStatus.SCHEDULE,
                    "poll_range_start": poll_range_start,
                }
                # 追加错误信息
                if task.error_msg:
                    update_data["error_msg"] = task.error_msg + str(e)
                else:
                    update_data["error_msg"] = str(e)
                if task.full_exception_trace:
                    update_data["full_exception_trace"] = task.full_exception_trace + str(e)
                else:
                    update_data["full_exception_trace"] = str(e)

                cls.filter_update(db, [cls.model.id == task.id], update_data)
                ConnectorService.update_by_id(db, connector_id, {"status": TaskStatus.SCHEDULE})

    @classmethod
    def increase_docs(
        cls,
        db: Session,
        task_id: str,
        min_update: str,
        max_update: str,
        doc_num: int,
        err_msg: str = "",
        error_count: int = 0
    ):
        """
        增加已索引文档数量

        Args:
            db: 数据库会话
            task_id: 任务ID
            min_update: 最小更新时间
            max_update: 最大更新时间
            doc_num: 文档数量
            err_msg: 错误消息
            error_count: 错误计数
        """
        # 获取当前任务
        task = cls.get_by_id(db, task_id)
        if not task:
            return None

        # 计算新的 poll_range_start
        if task.poll_range_start:
            new_poll_range_start = min(task.poll_range_start, min_update) if min_update else task.poll_range_start
        else:
            new_poll_range_start = min_update

        # 计算新的 poll_range_end
        if task.poll_range_end:
            new_poll_range_end = max(task.poll_range_end, max_update) if max_update else task.poll_range_end
        else:
            new_poll_range_end = max_update

        update_data = {
            "new_docs_indexed": task.new_docs_indexed + doc_num,
            "total_docs_indexed": task.total_docs_indexed + doc_num,
            "poll_range_start": new_poll_range_start,
            "poll_range_end": new_poll_range_end,
            "error_count": task.error_count + error_count,
        }

        # 追加错误信息
        if err_msg:
            update_data["error_msg"] = (task.error_msg or "") + err_msg

        cls.update_by_id(db, task_id, update_data)

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

        # 将文档转换为 (blob, filename) 元组格式
        files = [
            (d["blob"], d["semantic_identifier"] + (f"{d['extension']}" if d["semantic_identifier"][::-1].find(d['extension'][::-1]) < 0 else ""))
            for d in docs
        ]

        doc_ids = []
        errs, doc_blob_pairs = FileService.upload_document(db, kb, files, tenant_id, None, src)
        kb_table_num_map = {}
        for doc, _ in doc_blob_pairs:
            doc_ids.append(doc["id"])
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
        stmt = (
            select(cls.model)
            .where(
                cls.model.connector_id == connector_id,
                cls.model.kb_id == kb_id
            )
            .order_by(sa_desc(cls.model.update_time))
            .limit(1)
        )
        return db.execute(stmt).scalar_one_or_none()


class Connector2KbService(CommonService):
    """
    连接器与知识库关联服务类
    """
    model = Connector2Kb

    @classmethod
    def link_connectors(cls, db: Session, kb_id: str, connectors: list[dict], tenant_id: str) -> str:
        """
        关联连接器到知识库（与 link_kb 相反的方向）

        Args:
            db: 数据库会话
            kb_id: 知识库ID
            connectors: 连接器列表，每个元素包含 id 和可选的 auto_parse 字段
            tenant_id: 租户ID

        Returns:
            错误信息（如果有）
        """
        # 获取现有关联
        arr = cls.query(db, kb_id=kb_id)
        old_conn_ids = [a.connector_id for a in arr]

        # 添加新关联
        connector_ids = []
        for conn in connectors:
            conn_id = conn["id"]
            connector_ids.append(conn_id)
            if conn_id in old_conn_ids:
                cls.filter_update(db, [cls.model.connector_id==conn_id, cls.model.kb_id==kb_id], {"auto_parse": conn.get("auto_parse", "1")})
                continue
            cls.insert(db, **{
                "id": get_uuid(),
                "connector_id": conn_id,
                "kb_id": kb_id,
                "auto_parse": conn.get("auto_parse", "1")
            })
            SyncLogsService.schedule(db, conn_id, kb_id, reindex=True)

        # 删除不再需要的关联
        errs = []
        for conn_id in old_conn_ids:
            if conn_id in connector_ids:
                continue

            # 删除关联
            cls.filter_delete(db, [cls.model.kb_id == kb_id, cls.model.connector_id == conn_id])

            # 获取连接器信息
            conn = ConnectorService.get_by_id(db, conn_id)
            if not conn:
                continue

            # 取消调度中或运行中的同步任务（不删除已同步的文档）
            SyncLogsService.filter_update(
                db,
                [
                    SyncLogs.connector_id == conn_id,
                    SyncLogs.kb_id == kb_id,
                    SyncLogs.status.in_([TaskStatus.SCHEDULE, TaskStatus.RUNNING])
                ],
                {"status": TaskStatus.CANCEL}
            )

        return "\n".join(errs)

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
            select(
                Connector.id,
                Connector.source,
                Connector.name,
                cls.model.auto_parse,
                Connector.status
            )
            .select_from(cls.model)
            .join(Connector, cls.model.connector_id == Connector.id)
            .where(cls.model.kb_id == kb_id)
        )
        rows = db.execute(stmt).mappings().all()
        return [dict(row) for row in rows]

    @classmethod
    def link_kb(cls, db: Session, conn_id: str, kb_ids: list[str], tenant_id: str) -> str:
        """
        关联连接器与知识库

        Args:
            db: 数据库会话
            conn_id: 连接器ID
            kb_ids: 知识库ID列表
            tenant_id: 租户ID

        Returns:
            错误信息（如果有）
        """
        # 获取现有关联
        arr = cls.query(db, connector_id=conn_id)
        old_kb_ids = [a.kb_id for a in arr]

        # 添加新关联
        for kb_id in kb_ids:
            if kb_id in old_kb_ids:
                continue
            cls.insert(db, **{
                "id": get_uuid(),
                "connector_id": conn_id,
                "kb_id": kb_id
            })
            SyncLogsService.schedule(db, conn_id, kb_id, reindex=True)

        # 删除不再需要的关联
        errs = []
        conn = ConnectorService.get_by_id(db, conn_id)
        if not conn:
            return "连接器不存在"

        for kb_id in old_kb_ids:
            if kb_id in kb_ids:
                continue

            # 删除关联
            cls.filter_delete(db, [cls.model.kb_id == kb_id, cls.model.connector_id == conn_id])

            # 取消调度中的同步任务
            SyncLogsService.filter_update(
                db,
                [
                    SyncLogs.connector_id == conn_id,
                    SyncLogs.kb_id == kb_id,
                    SyncLogs.status == TaskStatus.SCHEDULE
                ],
                {"status": TaskStatus.CANCEL}
            )

            # 删除相关文档
            docs = DocumentService.query(db, source_type=f"{conn.source}/{conn.id}")
            err = FileService.delete_docs(db, [d.id for d in docs], tenant_id)
            if err:
                errs.append(err)

        return "\n".join(errs)

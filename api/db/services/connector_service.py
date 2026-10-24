"""
@project: multirag
@Author：龙
@file： connector_service.py
@date：2024/7/9 9:00
@desc: 数据源连接器相关服务
"""

from __future__ import annotations

import logging
import math
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import case, delete, func, literal_column, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql import desc as sa_desc
from sqlalchemy.sql.elements import ColumnElement

from api.db import InputType
from api.db.db_models import Connector, Connector2Kb, Document, Knowledgebase, SyncLogs
from api.db.services.common_service import CommonService
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_service import DocumentDeletePrecondition, DocumentService
from api.db.services.file_service import FileService
from api.utils.common import hash128
from common.constants import ConnectorTaskType, TaskStatus
from common.data_source.imap_connector import imap_scope_signature
from common.data_source.models import SlimDocument
from common.data_source.rdbms_connector import RDBMSConnector, rdbms_scope_signature
from common.misc_utils import get_uuid

logger = logging.getLogger(__name__)


class SyncTaskCancelled(RuntimeError):
    """A persisted pause/cancellation must survive worker completion or failure."""


class ConnectorAuthorizationError(PermissionError):
    """The caller cannot rebuild the requested connector and dataset."""


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


def _schedule_time_condition(dialect: str, now: ColumnElement[datetime] | None = None, task_type: str = ConnectorTaskType.SYNC) -> ColumnElement[bool]:
    """Compare UTC wall-clock timestamps without rounding elapsed minutes."""
    frequency = Connector.prune_freq if task_type == ConnectorTaskType.PRUNE else Connector.refresh_freq
    if dialect == "postgresql":
        utc_now = func.timezone("UTC", func.now()) if now is None else now
        cutoff = utc_now - func.make_interval(0, 0, 0, 0, 0, frequency)
    elif dialect in {"mysql", "mariadb"}:
        utc_now = func.utc_timestamp(6) if now is None else now
        cutoff = func.timestampadd(literal_column("MINUTE"), -frequency, utc_now)
    else:
        raise ValueError(f"Connector scheduling does not support database dialect {dialect}")
    return SyncLogs.update_date < cutoff


def connector_doc_id_candidates(kb_id: str, connector_id: str, external_id: str) -> tuple[str, str, str]:
    """Historical IDs and the current KB-scoped ID for a source document."""
    return hash128(external_id), hash128(f"{connector_id}:{external_id}"), hash128(f"{kb_id}:{connector_id}:{external_id}")


def resolve_connector_doc_id(kb_id: str, connector_id: str, external_id: str, owned_doc_ids: Collection[str]) -> str:
    """Preserve IDs already owned by this KB; scope all new document IDs."""
    *legacy_ids, scoped_id = connector_doc_id_candidates(kb_id, connector_id, external_id)
    return next((candidate for candidate in legacy_ids if candidate in owned_doc_ids), scoped_id)


class ConnectorService(CommonService):
    """
    数据源连接器服务类，提供连接器的CRUD操作。
    """

    model = Connector

    @classmethod
    def ensure_imap_sync_scope(
        cls,
        db: Session,
        connector_id: str,
        kb_id: str,
        task_id: str,
        signature: str,
        initial_start: float,
        *,
        reindex: bool,
        incremental: bool,
    ) -> float:
        """Persist the initial inventory scope independently for every KB.

        Retry the same task with its original lower bound. Configuration saves
        share this row lock, so concurrent KB starts do not overwrite state.
        """
        connector = db.scalar(select(cls.model).where(cls.model.id == connector_id).with_for_update().execution_options(populate_existing=True))
        if connector is None or connector.source != "imap":
            raise RuntimeError("IMAP connector is unavailable")
        config = connector.config or {}
        if imap_scope_signature(config) != signature:
            raise RuntimeError("IMAP configuration changed before synchronization; retry")
        scopes = config.get("imap_sync_scopes", {})
        if not isinstance(scopes, dict):
            raise ValueError("Invalid persisted IMAP synchronization scopes")
        previous = scopes.get(kb_id)
        if previous is not None:
            if not isinstance(previous, dict) or isinstance(previous.get("start"), bool):
                raise ValueError("Invalid persisted IMAP initial range")
            start = float(previous["start"])
            if not math.isfinite(start) or start < 0:
                raise ValueError("Invalid persisted IMAP initial range")
            if previous.get("scope") != signature and not reindex:
                raise RuntimeError("IMAP account/mailboxes changed; rebuild this knowledge base before syncing")
            if previous.get("scope") == signature and (not reindex or previous.get("initial_task") == task_id):
                return start
        # Old incremental tasks without persisted state get a conservative
        # inventory superset; their unknown original lower bound is never guessed.
        start = 0.0 if incremental and not reindex else initial_start
        if not math.isfinite(start) or start < 0:
            raise ValueError("Invalid IMAP initial range")
        connector.config = {**config, "imap_sync_scopes": {**scopes, kb_id: {"start": start, "scope": signature, "initial_task": task_id}}}
        db.commit()
        return start

    @classmethod
    def stage_rdbms_sync_state(cls, db: Session, connector_id: str, kb_id: str, state: dict[str, Any]) -> None:
        """Stage a per-KB cursor in the task completion transaction, without commit."""
        connector = db.scalar(select(cls.model).where(cls.model.id == connector_id).with_for_update().execution_options(populate_existing=True))
        if connector is None or connector.source not in {"mysql", "postgresql"} or state.get("source") != connector.source:
            raise RuntimeError("RDBMS connector is unavailable")
        config = connector.config or {}
        signature = rdbms_scope_signature(config, connector.source)
        if state.get("scope") != signature:
            raise RuntimeError("RDBMS configuration changed during sync; retry/rebuild")
        scopes = config.get("rdbms_sync_scopes", {})
        if not isinstance(scopes, dict):
            raise ValueError("Invalid persisted RDBMS scopes")
        cursor = RDBMSConnector.serialize_cursor_value(RDBMSConnector.deserialize_cursor_value(state["cursor"]))
        connector.config = {**config, "rdbms_sync_scopes": {**scopes, kb_id: {"scope": signature, "cursor": cursor}}}
        db.flush()

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
    def lock(cls, db: Session, connector_id: str) -> Connector | None:
        return db.scalar(select(cls.model).where(cls.model.id == connector_id).with_for_update().execution_options(populate_existing=True))

    @staticmethod
    def prune_enabled(connector: Connector) -> bool:
        return (connector.config or {}).get("sync_deleted_files") is True and connector.prune_freq > 0

    @classmethod
    def cancel_tasks(cls, db: Session, connector_id: str, *, commit: bool = True) -> None:
        connector = cls.lock(db, connector_id)
        if connector is None:
            return
        db.execute(update(SyncLogs).where(SyncLogs.connector_id == connector_id, SyncLogs.status.in_([TaskStatus.SCHEDULE, TaskStatus.RUNNING])).values(status=TaskStatus.CANCEL))
        connector.status = TaskStatus.CANCEL
        db.flush()
        if commit:
            db.commit()

    @classmethod
    def schedule_tasks(cls, db: Session, connector_id: str, *, commit: bool = True) -> None:
        """Schedule every binding/type once; retries retain their original window."""
        connector = cls.lock(db, connector_id)
        if connector is None:
            return
        connector.status = TaskStatus.SCHEDULE
        db.flush()
        for binding in Connector2KbService.query(db, connector_id=connector_id):
            for task_type in ConnectorTaskType:
                if task_type == ConnectorTaskType.PRUNE and not cls.prune_enabled(connector):
                    continue
                latest = SyncLogsService.get_latest_task(db, connector_id, binding.kb_id, task_type)
                checkpoint = latest.poll_range_end if latest and latest.status == TaskStatus.DONE else latest.poll_range_start if latest else None
                SyncLogsService.schedule(
                    db,
                    connector_id,
                    binding.kb_id,
                    checkpoint,
                    reindex=bool(latest and latest.status != TaskStatus.DONE and latest.from_beginning == "1"),
                    total_docs_indexed=latest.total_docs_indexed if latest else 0,
                    task_type=task_type,
                    run_immediately=True,
                    commit=False,
                )
        running = db.scalar(select(SyncLogs.id).where(SyncLogs.connector_id == connector_id, SyncLogs.status == TaskStatus.RUNNING).limit(1))
        if running:
            connector.status = TaskStatus.RUNNING
        if commit:
            db.commit()

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
        """Rebuild an accessible, persisted connector/dataset binding.

        ``tenant_id`` is the requesting user's ID in this legacy service. Check
        both resources and their binding before any log/document deletion or
        scheduling, including when called without an HTTP route's guard.
        """
        from api.db.services.knowledgebase_service import KnowledgebaseService

        if not cls.accessible(db, connector_id, tenant_id) or not KnowledgebaseService.accessible(db, kb_id, tenant_id):
            logger.warning("connector rebuild denied: resource access connector_id=%s kb_id=%s user_id=%s", connector_id, kb_id, tenant_id)
            raise ConnectorAuthorizationError("No authorization.")
        if not Connector2KbService.query(db, connector_id=connector_id, kb_id=kb_id):
            logger.warning("connector rebuild denied: missing binding connector_id=%s kb_id=%s user_id=%s", connector_id, kb_id, tenant_id)
            raise ConnectorAuthorizationError("No authorization.")

        conn = cls.get_by_id(db, connector_id)
        if not conn:
            return "连接器不存在"
        SyncLogsService.filter_delete(db, [SyncLogs.connector_id == connector_id, SyncLogs.kb_id == kb_id])
        docs = DocumentService.query(db, source_type=f"{conn.source}/{conn.id}", kb_id=kb_id)
        err = FileService.delete_docs(db, [d.id for d in docs], tenant_id)
        if err:
            return err
        SyncLogsService.schedule(db, connector_id, kb_id, reindex=True, run_immediately=True)
        if cls.prune_enabled(conn):
            SyncLogsService.schedule(db, connector_id, kb_id, task_type=ConnectorTaskType.PRUNE, run_immediately=True)
        return err

    @classmethod
    def prune_candidates(cls, db: Session, kb_id: str, source_type: str) -> dict[str, tuple[int | None, datetime | None, str | None]]:
        return {
            identifier: (updated, date, fingerprint)
            for identifier, updated, date, fingerprint in db.execute(
                select(Document.id, Document.update_time, Document.update_date, Document.content_hash).where(Document.kb_id == kb_id, Document.source_type == source_type)
            )
        }

    @classmethod
    def cleanup_stale_documents_for_task(
        cls,
        db: Session,
        task_id: str,
        connector_id: str,
        kb_id: str,
        tenant_id: str,
        file_list: Sequence[SlimDocument],
        delete_batch_size: int = 100,
        *,
        candidates: dict[str, tuple[int | None, datetime | None, str | None]] | None = None,
    ) -> tuple[int, list[str]]:
        """
        删除源端已不存在、但本地仍保留的连接器文档。
        """
        if delete_batch_size <= 0:
            raise ValueError("delete_batch_size must be positive")
        if any(not file.id.strip() for file in file_list):
            raise ValueError("Deleted-file snapshot contains an empty source ID")
        if not Connector2KbService.query(db, connector_id=connector_id, kb_id=kb_id):
            return 0, []

        conn = cls.get_by_id(db, connector_id)
        if not conn:
            return 0, []

        source_type = f"{conn.source}/{conn.id}"
        retain_doc_ids = {doc_id for file in file_list for doc_id in connector_doc_id_candidates(kb_id, connector_id, file.id)}
        existing_docs = DocumentService.list_doc_headers_by_kb_and_source_type(
            db,
            kb_id,
            source_type,
        )
        stale_doc_ids = [doc["id"] for doc in existing_docs if doc["id"] not in retain_doc_ids and (candidates is None or doc["id"] in candidates)]
        if not stale_doc_ids:
            return 0, []

        stale_doc_id_set = set(stale_doc_ids)
        errors = []
        cancellation: SyncTaskCancelled | None = None
        # The actual delete transaction checks each captured document revision.
        if candidates is not None:
            delete_batch_size = 1
        for offset in range(0, len(stale_doc_ids), delete_batch_size):
            try:
                SyncLogsService.raise_if_cancelled(db, task_id)
            except SyncTaskCancelled as exc:
                cancellation = exc
                break
            batch = stale_doc_ids[offset : offset + delete_batch_size]
            if candidates is not None:
                identifier = batch[0]
                unchanged = db.scalar(
                    select(Document.id).where(
                        Document.id == identifier,
                        Document.kb_id == kb_id,
                        Document.source_type == source_type,
                        Document.update_time == candidates[identifier][0],
                        Document.update_date == candidates[identifier][1],
                        Document.content_hash == candidates[identifier][2],
                    )
                )
                if unchanged is None:
                    stale_doc_id_set.discard(identifier)
                    continue
            if candidates is None:
                err = FileService.delete_docs(db, batch, tenant_id)
            else:
                preconditions = {identifier: DocumentDeletePrecondition(kb_id, source_type, *candidates[identifier]) for identifier in batch}
                err = FileService.delete_docs(db, batch, tenant_id, delete_preconditions=preconditions)
            if err:
                errors.append(err)

        if candidates is None:
            remaining_doc_ids = {doc["id"] for doc in DocumentService.list_doc_headers_by_kb_and_source_type(db, kb_id, source_type) if doc["id"] in stale_doc_id_set}
        else:
            current = cls.prune_candidates(db, kb_id, source_type)
            # A conditional delete may have preserved a concurrent update. It is
            # neither a removed document nor an undeleted stale document/error.
            stale_doc_id_set.difference_update(identifier for identifier, revision in current.items() if identifier in candidates and revision != candidates[identifier])
            remaining_doc_ids = current.keys() & stale_doc_id_set
        removed_count = len(stale_doc_id_set) - len(remaining_doc_ids)
        if remaining_doc_ids and not errors and cancellation is None:
            errors.append(f"{len(remaining_doc_ids)} stale document(s) remain after deletion")
        SyncLogsService.increase_removed_docs(
            db,
            task_id,
            removed_count,
            "\n".join(errors),
            len(errors),
        )
        if cancellation is not None:
            raise cancellation
        return removed_count, errors


class SyncLogsService(CommonService):
    """
    同步日志服务类，提供同步任务的管理操作。
    """

    model = SyncLogs

    @classmethod
    def raise_if_cancelled(cls, db: Session, task_id: str) -> None:
        status = db.scalar(select(cls.model.status).where(cls.model.id == task_id))
        if status is None or status == TaskStatus.CANCEL:
            raise SyncTaskCancelled(f"Sync task {task_id} was cancelled or removed")

    @classmethod
    def _task_query(cls) -> Any:
        return (
            select(
                cls.model.id,
                cls.model.connector_id,
                cls.model.task_type,
                cls.model.kb_id,
                cls.model.update_date,
                cls.model.create_date,
                cls.model.update_time,
                cls.model.poll_range_start,
                cls.model.poll_range_end,
                cls.model.new_docs_indexed,
                cls.model.total_docs_indexed,
                cls.model.docs_removed_from_index,
                cls.model.error_msg,
                cls.model.full_exception_trace,
                cls.model.error_count,
                cls.model.time_started,
                Connector.name,
                Connector.source,
                Connector.tenant_id,
                Connector.timeout_secs,
                Connector.refresh_freq,
                Connector.prune_freq,
                Knowledgebase.name.label("kb_name"),
                Knowledgebase.avatar.label("kb_avatar"),
                Connector2Kb.auto_parse,
                cls.model.from_beginning.label("reindex"),
                cls.model.status,
            )
            .select_from(cls.model)
            .join(Connector, cls.model.connector_id == Connector.id)
            .join(Connector2Kb, (cls.model.kb_id == Connector2Kb.kb_id) & (cls.model.connector_id == Connector2Kb.connector_id))
            .join(Knowledgebase, cls.model.kb_id == Knowledgebase.id)
        )

    @staticmethod
    def _task_dicts(db: Session, statement: Any) -> list[dict[str, Any]]:
        tasks = [dict(row) for row in db.execute(statement).mappings()]
        for task in tasks:
            for field in ("update_date", "poll_range_start", "poll_range_end", "time_started", "create_date"):
                task[field] = _to_utc(task[field])
        return tasks

    @classmethod
    def list_sync_tasks(cls, db: Session, connector_id: str | None = None, page_number: int | None = None, items_per_page: int = 15) -> tuple[list[dict], int]:
        """Dashboard history; the dispatcher uses separate unpaginated due queries."""
        statement = cls._task_query()
        if connector_id:
            statement = statement.where(cls.model.connector_id == connector_id)
        statement = statement.distinct().order_by(sa_desc(cls.model.update_time), sa_desc(cls.model.id))
        total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
        if page_number:
            statement = statement.offset((page_number - 1) * items_per_page).limit(items_per_page)
        return cls._task_dicts(db, statement), total

    @classmethod
    def _list_due_tasks(cls, db: Session, task_type: ConnectorTaskType) -> list[dict[str, Any]]:
        statement = (
            cls._task_query()
            .add_columns(Connector.config)
            .where(
                Connector.input_type == InputType.POLL,
                Connector.status.in_([TaskStatus.SCHEDULE, TaskStatus.RUNNING]),
                cls.model.status == TaskStatus.SCHEDULE,
                cls.model.task_type == task_type,
                _schedule_time_condition(db.get_bind().dialect.name, task_type=task_type),
            )
            .distinct()
            .order_by(sa_desc(cls.model.update_time))
        )
        tasks = cls._task_dicts(db, statement)
        if task_type == ConnectorTaskType.PRUNE:
            tasks = [task for task in tasks if (task["config"] or {}).get("sync_deleted_files") is True and task["prune_freq"] > 0]
        return tasks

    @classmethod
    def list_due_sync_tasks(cls, db: Session) -> list[dict[str, Any]]:
        return cls._list_due_tasks(db, ConnectorTaskType.SYNC)

    @classmethod
    def list_due_prune_tasks(cls, db: Session) -> list[dict[str, Any]]:
        return cls._list_due_tasks(db, ConnectorTaskType.PRUNE)

    @classmethod
    def start(cls, db: Session, task_id: str, connector_id: str) -> None:
        connector = ConnectorService.lock(db, connector_id)
        current = db.scalar(select(cls.model).where(cls.model.id == task_id).with_for_update().execution_options(populate_existing=True))
        if connector is None or connector.status in (TaskStatus.CANCEL, TaskStatus.FAIL) or current is None or current.connector_id != connector_id or current.status != TaskStatus.SCHEDULE:
            raise SyncTaskCancelled(f"Sync task {task_id} is no longer scheduled")
        if current.task_type == ConnectorTaskType.PRUNE and not ConnectorService.prune_enabled(connector):
            current.status = TaskStatus.CANCEL
            db.commit()
            raise SyncTaskCancelled(f"Pruning is disabled for {connector_id}")
        current.status = TaskStatus.RUNNING
        current.time_started = datetime.now(UTC)
        connector.status = TaskStatus.RUNNING
        db.commit()

    @classmethod
    def fail(cls, db: Session, task_id: str, connector_id: str, error_msg: str, full_exception_trace: str = "", poll_range_start: datetime | None = None) -> None:
        """Fail atomically and retain the original window for a complete retry."""
        connector = ConnectorService.lock(db, connector_id)
        current = db.scalar(select(cls.model).where(cls.model.id == task_id).with_for_update().execution_options(populate_existing=True))
        if connector is None or connector.status == TaskStatus.CANCEL or current is None or current.connector_id != connector_id or current.status != TaskStatus.RUNNING:
            return
        current.status = TaskStatus.FAIL
        current.error_msg = error_msg
        current.full_exception_trace = full_exception_trace
        current.poll_range_start = _to_utc(poll_range_start)
        connector.status = TaskStatus.FAIL
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
        *,
        task_type: str = ConnectorTaskType.SYNC,
        run_immediately: bool = False,
        commit: bool = True,
    ) -> SyncLogs | None:
        """Stage one task per binding/type under the connector lock."""
        task_type = ConnectorTaskType(task_type)
        connector = ConnectorService.lock(db, connector_id)
        if connector is None:
            raise RuntimeError("Connector is unavailable")
        existing = db.scalar(
            select(cls.model).where(cls.model.kb_id == kb_id, cls.model.connector_id == connector_id, cls.model.task_type == task_type, cls.model.status.in_([TaskStatus.SCHEDULE, TaskStatus.RUNNING]))
        )
        if existing:
            return None
        terminal = (
            select(cls.model.id)
            .where(cls.model.kb_id == kb_id, cls.model.connector_id == connector_id, cls.model.status.in_([TaskStatus.DONE, TaskStatus.FAIL, TaskStatus.CANCEL]))
            .order_by(sa_desc(cls.model.update_time))
            .offset(100)
        )
        old_ids = list(db.scalars(terminal))
        if old_ids:
            db.execute(delete(cls.model).where(cls.model.id.in_(old_ids)))
        task = cls.model(
            id=get_uuid(),
            kb_id=kb_id,
            status=TaskStatus.SCHEDULE,
            connector_id=connector_id,
            task_type=task_type,
            poll_range_start=_to_utc(poll_range_start),
            from_beginning="1" if reindex else "0",
            total_docs_indexed=total_docs_indexed,
        )
        db.add(task)
        if connector.status not in (TaskStatus.RUNNING, TaskStatus.FAIL):
            connector.status = TaskStatus.SCHEDULE
        db.flush()
        if run_immediately:
            # Insert hooks stamp update_date; adjust only the scheduling clock
            # after flush, in this transaction, without corrupting log ordering.
            db.execute(update(cls.model).where(cls.model.id == task.id).values(update_date=datetime(1970, 1, 1, tzinfo=UTC)))
        if commit:
            db.commit()
        return task

    @classmethod
    def complete_and_schedule_next(
        cls,
        db: Session,
        task_id: str,
        connector_id: str,
        kb_id: str,
        checkpoint: datetime | None,
        *,
        rdbms_state: dict[str, Any] | None = None,
    ) -> str | None:
        """Commit completion/checkpoint and the next task of the same type atomically."""
        connector = ConnectorService.lock(db, connector_id)
        current = db.scalar(select(cls.model).where(cls.model.id == task_id).with_for_update().execution_options(populate_existing=True))
        if connector is None or connector.status == TaskStatus.CANCEL or current is None or current.connector_id != connector_id or current.kb_id != kb_id or current.status != TaskStatus.RUNNING:
            raise SyncTaskCancelled(f"Sync task {task_id} was cancelled or removed")
        if not Connector2KbService.query(db, connector_id=connector_id, kb_id=kb_id):
            raise SyncTaskCancelled("Connector binding was removed")
        if current.task_type == ConnectorTaskType.SYNC:
            checkpoint = _to_utc(checkpoint)
            if checkpoint is None:
                raise ValueError("A completed sync task requires a checkpoint")
            if rdbms_state is not None:
                ConnectorService.stage_rdbms_sync_state(db, connector_id, kb_id, rdbms_state)
            current.poll_range_end = checkpoint
        current.status = TaskStatus.DONE
        db.flush()
        next_task = None
        if current.task_type == ConnectorTaskType.SYNC or ConnectorService.prune_enabled(connector):
            next_task = cls.schedule(db, connector_id, kb_id, checkpoint, total_docs_indexed=current.total_docs_indexed, task_type=current.task_type, commit=False)
        if connector.status != TaskStatus.FAIL:
            running = db.scalar(select(cls.model.id).where(cls.model.connector_id == connector_id, cls.model.status == TaskStatus.RUNNING).limit(1))
            connector.status = TaskStatus.RUNNING if running else TaskStatus.SCHEDULE
        db.commit()
        return next_task.id if next_task else None

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
    def duplicate_and_parse(cls, db: Session, kb: Knowledgebase, docs: list[dict[str, Any]], tenant_id: str, src: str, auto_parse: bool | str = True) -> tuple[list[str], list[str]]:
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
            fingerprint: str | None = None

            def read(self) -> bytes:
                return self.blob

        # 将文档转换为 FileObj 对象，携带 id 以支持重复检测
        files = [
            FileObj(
                id=d["id"],
                filename=d["semantic_identifier"] + (f"{d['extension']}" if d["semantic_identifier"][::-1].find(d["extension"][::-1]) < 0 else ""),
                blob=d["blob"],
                fingerprint=d.get("fingerprint"),
            )
            for d in docs
        ]

        # Distinct source rows can share a title/filename. Match by the exact
        # resolved document identity used by upload, never by display name.
        metadata_map = {d["id"]: d["metadata"] for d in docs if d.get("metadata")}

        doc_ids = []
        errs, doc_blob_pairs = FileService.upload_document(db, kb, files, tenant_id, None, src)
        kb_table_num_map = {}
        for doc, _ in doc_blob_pairs:
            doc_ids.append(doc["id"])

            # Set metadata if available for this document
            if doc["id"] in metadata_map:
                if not DocMetadataService.update_document_metadata(db, doc["id"], metadata_map[doc["id"]]):
                    raise RuntimeError("Failed to persist connector document metadata")

            if not auto_parse or auto_parse == "0":
                continue
            DocumentService.run(db, tenant_id, doc, kb_table_num_map)

        return errs, doc_ids

    @classmethod
    def get_latest_task(cls, db: Session, connector_id: str, kb_id: str, task_type: str = ConnectorTaskType.SYNC) -> SyncLogs | None:
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
            .where(cls.model.connector_id == connector_id, cls.model.kb_id == kb_id, cls.model.task_type == task_type)
            .order_by(sa_desc(cls.model.update_time), sa_desc(cls.model.id))
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
        SyncLogsService.schedule(db, connector_id, kb_id, reindex=True, run_immediately=True)
        connector = ConnectorService.get_by_id(db, connector_id)
        if connector and ConnectorService.prune_enabled(connector):
            SyncLogsService.schedule(db, connector_id, kb_id, task_type=ConnectorTaskType.PRUNE, run_immediately=True)

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

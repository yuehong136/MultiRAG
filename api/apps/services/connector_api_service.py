"""
@project: multirag
@file: connector_api_service.py
@desc: Connector API 业务逻辑层 - 数据源连接器的增删改查与调度控制。

从 ``api/apps/connector_app.py`` 抽出，供两个网关层共用：
    - ``api/apps/restful_apis/connector_api.py``（正典 ``/api/v1/connectors/*``）
    - ``api/apps/connector_app.py``（deprecated ``/v1/connector/*``，前端过渡期仍在用）

两个路由模块由 ``register_page`` 各自以 ``spec_from_file_location`` 加载，互相 import
会二次加载出两份模块对象，因此共享逻辑只能落在本层。

约定（与 ``file_api_service.py`` 一致）：本层只吃 ``db: Session``、不 import fastapi、
不返回 HTTP 响应对象；可能带不同错误码的操作统一返回三元组
``(success, result_or_message, retcode)``。

鉴权：按 id 定位单个连接器的操作一律先过 ``ConnectorService.accessible``——连接器的
``config`` 里存着数据源凭证，缺这一关等于任意登录用户可读写他人凭证。
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db import InputType
from api.db.db_models import Connector, SyncLogs
from api.db.services.connector_service import Connector2KbService, ConnectorService, SyncLogsService
from common.constants import ConnectorTaskType, FileSource, RetCode, TaskStatus
from common.data_source.azure_blob_connector import AzureBlobConnector
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError
from common.data_source.microsoft_graph import graph_batch_size
from common.data_source.onedrive_connector import OneDriveConnector
from common.data_source.outlook_connector import OutlookConnector
from common.data_source.rest_api_connector import RestAPIConnector
from common.data_source.salesforce_connector import SalesforceConnector
from common.data_source.teams_connector import TeamsConnector
from common.misc_utils import get_uuid

# 连接器写入后回读前的等待，沿用移植前的行为（上游同样有一秒 sleep）。
WRITE_SETTLE_SECS = 1

_UPDATABLE_FIELDS = ("prune_freq", "refresh_freq", "config", "timeout_secs")

AUTH_ERROR = "No authorization."

_WORKER_STATE_FIELDS = {"imap": "imap_sync_scopes", "mysql": "rdbms_sync_scopes", "postgresql": "rdbms_sync_scopes"}


def _validate_schedule(req: dict[str, Any]) -> str | None:
    for name, minimum in (("refresh_freq", 0), ("prune_freq", 0), ("timeout_secs", 1)):
        if name in req and (type(req[name]) is not int or req[name] < minimum):
            return f"{name} must be an integer >= {minimum}"
    if isinstance(req.get("config"), dict) and "sync_deleted_files" in req["config"] and type(req["config"]["sync_deleted_files"]) is not bool:
        return "sync_deleted_files must be a boolean"
    if "reschedule" in req and type(req["reschedule"]) is not bool:
        return "reschedule must be a boolean"
    if "status" in req and req["status"] not in (TaskStatus.CANCEL, TaskStatus.SCHEDULE):
        return "status must be cancel (2) or schedule (5)"
    if req.get("reschedule") and req.get("status") == TaskStatus.CANCEL:
        return "reschedule cannot be combined with cancel"
    return None


def accessible(db: Session, connector_id: str, user_id: str) -> bool:
    return ConnectorService.accessible(db, connector_id, user_id)


def list_connectors(db: Session, tenant_id: str) -> list[dict]:
    return ConnectorService.list(db, tenant_id)


def get_connector(db: Session, connector_id: str) -> tuple[bool, Any, RetCode | None]:
    connector = ConnectorService.get_by_id(db, connector_id)
    if not connector:
        return False, "Can't find this Connector!", RetCode.DATA_ERROR
    return True, connector.to_dict(), None


def create_connector(db: Session, req: dict[str, Any], tenant_id: str) -> tuple[bool, Any, RetCode | None]:
    """建连接器，成功时返回新建的 id（回读由网关在等待后单独发起）。"""
    if not req.get("name") or not req.get("source"):
        return False, "创建连接器时 name 和 source 为必填项", RetCode.DATA_ERROR

    if error := _validate_schedule(req):
        return False, error, RetCode.ARGUMENT_ERROR
    config = dict(req.get("config") or {})
    if state_key := _WORKER_STATE_FIELDS.get(req["source"]):
        config.pop(state_key, None)
    if req["source"] == FileSource.AZURE_BLOB:
        try:
            AzureBlobConnector.build_connector(config)
        except (ConnectorValidationError, ConnectorMissingCredentialError) as exc:
            return False, str(exc), RetCode.DATA_ERROR
    if req["source"] == FileSource.SALESFORCE:
        try:
            SalesforceConnector.build_connector(config)
        except (ConnectorValidationError, ConnectorMissingCredentialError) as exc:
            return False, str(exc), RetCode.DATA_ERROR
    if req["source"] == FileSource.REST_API:
        try:
            _validate_rest_settings(config)
        except (ConnectorValidationError, ConnectorMissingCredentialError) as exc:
            return False, str(exc), RetCode.DATA_ERROR
    if req["source"] == FileSource.OUTLOOK:
        try:
            _validate_outlook_settings(config)
        except (ConnectorValidationError, ConnectorMissingCredentialError) as exc:
            return False, str(exc), RetCode.DATA_ERROR
    connector_id = get_uuid()
    ConnectorService.insert(
        db,
        id=connector_id,
        tenant_id=tenant_id,
        name=req["name"],
        source=req["source"],
        input_type=InputType.POLL,
        config=config,
        refresh_freq=req.get("refresh_freq", 5),
        prune_freq=req.get("prune_freq", 5),
        timeout_secs=req.get("timeout_secs", 60 * 29),
        status=TaskStatus.UNSTART,
    )
    return True, connector_id, None


def update_connector(db: Session, connector_id: str, req: dict[str, Any]) -> tuple[bool, Any, RetCode | None]:
    """改连接器的调度配置，成功时返回 id（回读同上，由网关发起）。"""
    if error := _validate_schedule(req):
        return False, error, RetCode.ARGUMENT_ERROR
    connector = ConnectorService.lock(db, connector_id)
    if connector is None:
        return False, "Can't find this Connector!", RetCode.DATA_ERROR

    changes = {field: req[field] for field in _UPDATABLE_FIELDS if field in req}
    if "config" in changes and connector.source == FileSource.AZURE_BLOB:
        try:
            AzureBlobConnector.build_connector(changes["config"])
        except (ConnectorValidationError, ConnectorMissingCredentialError) as exc:
            return False, str(exc), RetCode.DATA_ERROR
    if "config" in changes and connector.source == FileSource.SALESFORCE:
        try:
            SalesforceConnector.build_connector(changes["config"])
        except (ConnectorValidationError, ConnectorMissingCredentialError) as exc:
            return False, str(exc), RetCode.DATA_ERROR
    if "config" in changes and (state_key := _WORKER_STATE_FIELDS.get(connector.source)):
        connector = db.scalar(select(Connector).where(Connector.id == connector_id).with_for_update().execution_options(populate_existing=True))
        if connector is None:
            return False, "Can't find this Connector!", RetCode.DATA_ERROR
        config = dict(changes["config"])
        config.pop(state_key, None)
        if state_key in (connector.config or {}):
            config[state_key] = connector.config[state_key]
        changes["config"] = config
    if "config" in changes and connector.source == FileSource.REST_API:
        try:
            _validate_rest_settings(changes["config"])
        except (ConnectorValidationError, ConnectorMissingCredentialError) as exc:
            return False, str(exc), RetCode.DATA_ERROR
    if "config" in changes and connector.source == FileSource.OUTLOOK:
        try:
            _validate_outlook_settings(changes["config"])
        except (ConnectorValidationError, ConnectorMissingCredentialError) as exc:
            return False, str(exc), RetCode.DATA_ERROR
    if changes:
        for field, value in changes.items():
            setattr(connector, field, value)
        db.flush()
        if not ConnectorService.prune_enabled(connector):
            db.execute(
                update(SyncLogs)
                .where(SyncLogs.connector_id == connector_id, SyncLogs.task_type == ConnectorTaskType.PRUNE, SyncLogs.status.in_([TaskStatus.SCHEDULE, TaskStatus.RUNNING]))
                .values(status=TaskStatus.CANCEL)
            )
    if req.get("reschedule"):
        ConnectorService.cancel_tasks(db, connector_id, commit=False)
        ConnectorService.schedule_tasks(db, connector_id, commit=False)
    elif req.get("status") == TaskStatus.CANCEL:
        ConnectorService.cancel_tasks(db, connector_id, commit=False)
    elif req.get("status") == TaskStatus.SCHEDULE:
        ConnectorService.schedule_tasks(db, connector_id, commit=False)
    db.commit()
    return True, connector_id, None


def list_logs(db: Session, connector_id: str, page: int, page_size: int) -> dict:
    logs, total = SyncLogsService.list_sync_tasks(db, connector_id, page, page_size)
    return {"total": total, "logs": logs}


def rebuild_connector(db: Session, connector_id: str, kb_id: str, tenant_id: str) -> tuple[bool, Any, RetCode | None]:
    err = ConnectorService.rebuild(db, connector_id, kb_id, tenant_id)
    if err:
        return False, err, RetCode.SERVER_ERROR
    return True, True, None


def remove_connector(db: Session, connector_id: str) -> bool:
    ConnectorService.cancel_tasks(db, connector_id)
    ConnectorService.delete_by_id(db, connector_id)
    return True


# ==================== 连接器 ↔ 知识库关联 ====================
#
# 整集写入走数据集更新端点的 ``connectors`` 字段（``dataset_api_service``）；下面是按单个
# 连接器操作的入口，供"知识库设置里逐条勾选/解绑/切自动解析"的界面使用。


def list_dataset_connectors(db: Session, dataset_id: str) -> list[dict]:
    return Connector2KbService.list_connectors(db, dataset_id)


def link_dataset_connector(db: Session, dataset_id: str, connector_id: str, auto_parse: bool) -> bool:
    Connector2KbService.link_connector(db, dataset_id, connector_id, "1" if auto_parse else "0")
    return True


def unlink_dataset_connector(db: Session, dataset_id: str, connector_id: str) -> bool:
    Connector2KbService.unlink_connector(db, dataset_id, connector_id)
    return True


def _validate_rest_settings(config: dict[str, Any]) -> None:
    """Schema and credential checks only: a normal save never probes a vendor."""
    connector = RestAPIConnector.from_parsed_config(RestAPIConnector.parse_storage_config(config))
    connector.load_credentials(config.get("credentials") or {})


def _validate_outlook_settings(config: dict[str, Any]) -> None:
    if config.get("sync_deleted_files", False) is not False:
        raise ConnectorValidationError("Outlook deletion synchronization is not supported")
    adapter = OutlookConnector(batch_size=graph_batch_size(config), folder=config.get("folder", "inbox"), user_ids=config.get("user_ids"))
    adapter.load_credentials(config.get("credentials"))


class _ConnectorProbeGuard:
    def __init__(self) -> None:
        self.stopped = threading.Event()
        self.deadline = time.monotonic() + 30.0

    def __call__(self) -> None:
        if self.stopped.is_set() or time.monotonic() >= self.deadline:
            raise TimeoutError("Microsoft Graph connection test timed out")

    def remaining_seconds(self) -> float:
        self()
        return max(0.001, self.deadline - time.monotonic())


async def test_connector(db: AsyncSession, connector_id: str) -> tuple[bool, Any, RetCode | None]:
    """Probe saved config after the route's tenant guard, without any writes."""
    connector = await db.scalar(select(Connector).where(Connector.id == connector_id))
    if connector is None:
        return False, "Can't find this Connector!", RetCode.DATA_ERROR
    if connector.source not in {FileSource.REST_API, FileSource.TEAMS, FileSource.ONEDRIVE, FileSource.OUTLOOK}:
        return False, "Connection testing is not supported for this source.", RetCode.DATA_ERROR
    config = dict(connector.config or {})
    if connector.source == FileSource.ONEDRIVE and config.get("drive_ids") == []:
        return False, "OneDrive connection testing requires at least one configured drive ID.", RetCode.DATA_ERROR
    if connector.source == FileSource.OUTLOOK and config.get("user_ids") == []:
        return False, "Outlook connection testing requires at least one configured mailbox ID.", RetCode.DATA_ERROR
    try:
        if connector.source == FileSource.REST_API:
            await asyncio.to_thread(RestAPIConnector.validate_config, config, config.get("credentials") or {})
        else:
            source = connector.source
            check_active = _ConnectorProbeGuard()

            def probe() -> None:
                adapter: TeamsConnector | OneDriveConnector | OutlookConnector
                batch_size = graph_batch_size(config)
                if source == FileSource.TEAMS:
                    adapter = TeamsConnector(batch_size=batch_size, check_active=check_active)
                elif source == FileSource.OUTLOOK:
                    _validate_outlook_settings(config)
                    adapter = OutlookConnector(batch_size=batch_size, user_ids=config.get("user_ids"), folder=config.get("folder", "inbox"), check_active=check_active)
                else:
                    adapter = OneDriveConnector(batch_size=batch_size, drive_ids=config.get("drive_ids"), folder_path=config.get("folder_path"), check_active=check_active)
                adapter.load_credentials(config.get("credentials") or {})
                adapter.validate_connector_settings()
                check_active()

            worker = asyncio.create_task(asyncio.to_thread(probe))
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=30.0)
            finally:
                check_active.stopped.set()
                # Wait for the finite transport to settle; no detached probe may
                # keep reading credentials/network after the route completes.
                if not worker.done():
                    try:
                        await worker
                    except Exception:
                        pass

    except (ConnectorValidationError, ConnectorMissingCredentialError, TimeoutError) as exc:
        return False, str(exc), RetCode.DATA_ERROR
    return True, {"message": "Connection successful."}, None

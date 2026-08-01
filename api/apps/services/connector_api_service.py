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

from typing import Any

from sqlalchemy.orm import Session

from api.db import InputType
from api.db.services.connector_service import Connector2KbService, ConnectorService, SyncLogsService
from common.constants import RetCode, TaskStatus
from common.misc_utils import get_uuid

# 连接器写入后回读前的等待，沿用移植前的行为（上游同样有一秒 sleep）。
WRITE_SETTLE_SECS = 1

_UPDATABLE_FIELDS = ("prune_freq", "refresh_freq", "config", "timeout_secs")

AUTH_ERROR = "No authorization."


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

    connector_id = get_uuid()
    ConnectorService.insert(
        db,
        id=connector_id,
        tenant_id=tenant_id,
        name=req["name"],
        source=req["source"],
        input_type=InputType.POLL,
        config=req.get("config", {}),
        refresh_freq=int(req.get("refresh_freq", 5)),
        prune_freq=int(req.get("prune_freq", 720)),
        timeout_secs=int(req.get("timeout_secs", 60 * 29)),
        status=TaskStatus.SCHEDULE,
    )
    return True, connector_id, None


def update_connector(db: Session, connector_id: str, req: dict[str, Any]) -> tuple[bool, Any, RetCode | None]:
    """改连接器的调度配置，成功时返回 id（回读同上，由网关发起）。"""
    if not ConnectorService.get_by_id(db, connector_id):
        return False, "Can't find this Connector!", RetCode.DATA_ERROR

    changes = {field: req[field] for field in _UPDATABLE_FIELDS if field in req}
    if changes:
        ConnectorService.update_by_id(db, connector_id, changes)
    return True, connector_id, None


def list_logs(db: Session, connector_id: str, page: int, page_size: int) -> dict:
    logs, total = SyncLogsService.list_sync_tasks(db, connector_id, page, page_size)
    return {"total": total, "logs": logs}


def resume_connector(db: Session, connector_id: str, resume: bool) -> bool:
    ConnectorService.resume(db, connector_id, TaskStatus.SCHEDULE if resume else TaskStatus.CANCEL)
    return True


def rebuild_connector(db: Session, connector_id: str, kb_id: str, tenant_id: str) -> tuple[bool, Any, RetCode | None]:
    err = ConnectorService.rebuild(db, connector_id, kb_id, tenant_id)
    if err:
        return False, err, RetCode.SERVER_ERROR
    return True, True, None


def remove_connector(db: Session, connector_id: str) -> bool:
    ConnectorService.resume(db, connector_id, TaskStatus.CANCEL)
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

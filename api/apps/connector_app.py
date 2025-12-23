# coding=utf-8
"""
@project: multirag
@Author：龙
@file： connector_app.py
@date：2025/12/19 16:00
@desc: 数据源连接器管理接口
"""
import time

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api import settings
from api.apps import manager
from api.db import InputType
from common.constants import TaskStatus
from api.db.db_models import get_db
from api.db.services.connector_service import ConnectorService, Connector2KbService, SyncLogsService
from api.utils.api_utils import get_json_result, get_data_error_result, server_error_response
from common.misc_utils import get_uuid
from common.constants import RetCode

router = APIRouter()


# ==================== 请求体模型定义 ====================

class SetConnectorRequest(BaseModel):
    """创建或更新连接器请求"""
    id: str | None = None
    name: str | None = None
    source: str | None = None
    config: dict | None = None
    refresh_freq: int | None = 60  # 刷新频率（分钟）
    prune_freq: int | None = 0  # 修剪频率（分钟）
    timeout_secs: int | None = 3600  # 超时时间（秒）


class ResumeConnectorRequest(BaseModel):
    """恢复/暂停连接器请求"""
    resume: bool = True  # True: 恢复调度, False: 取消调度


class LinkKbRequest(BaseModel):
    """关联知识库请求"""
    kb_ids: list[str]


# ==================== 接口定义 ====================

@router.post("/set", summary="创建或更新连接器", response_description="连接器信息")
def set_connector(
    request: SetConnectorRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### POST `/set` 创建或更新连接器

    **功能描述**:
    创建新的数据源连接器或更新现有连接器的配置。

    ---

    ### 请求体 (Request Body)

    | 字段          | 类型   | 必填 | 描述                           |
    |---------------|--------|------|--------------------------------|
    | `id`          | string | 否   | 连接器ID，不传则创建新连接器   |
    | `name`        | string | 是*  | 连接器名称（创建时必填）       |
    | `source`      | string | 是*  | 数据源类型（创建时必填）       |
    | `config`      | object | 是   | 数据源配置信息                 |
    | `refresh_freq`| int    | 否   | 刷新频率（分钟），默认60       |
    | `prune_freq`  | int    | 否   | 修剪频率（分钟），默认0        |
    | `timeout_secs`| int    | 否   | 超时时间（秒），默认3600       |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "id": "connector_id",
            "name": "My Connector",
            "source": "notion",
            ...
        }
    }
    ```
    """
    try:
        req = request.model_dump(exclude_none=True)

        if req.get("id"):
            # 更新现有连接器
            conn = {
                fld: req[fld]
                for fld in ["prune_freq", "refresh_freq", "config", "timeout_secs"]
                if fld in req
            }
            ConnectorService.update_by_id(db, req["id"], conn)
        else:
            # 创建新连接器
            if not req.get("name") or not req.get("source"):
                return get_data_error_result(retmsg="创建连接器时 name 和 source 为必填项")

            req["id"] = get_uuid()
            conn = {
                "id": req["id"],
                "tenant_id": user.id,
                "name": req["name"],
                "source": req["source"],
                "input_type": InputType.POLL,
                "config": req.get("config", {}),
                "refresh_freq": int(req.get("refresh_freq", 60)),
                "prune_freq": int(req.get("prune_freq", 0)),
                "timeout_secs": int(req.get("timeout_secs", 3600)),
                "status": TaskStatus.SCHEDULE
            }
            ConnectorService.insert(db, **conn)

        time.sleep(1)  # 等待数据库写入完成
        connector = ConnectorService.get_by_id(db, req["id"])
        if not connector:
            return get_data_error_result(retmsg="创建连接器失败")

        return get_json_result(data=connector.to_dict())
    except Exception as e:
        return server_error_response(e)


@router.get("/list", summary="获取连接器列表", response_description="连接器列表")
def list_connector(
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### GET `/list` 获取连接器列表

    **功能描述**:
    获取当前用户的所有数据源连接器列表。

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": [
            {
                "id": "connector_id",
                "name": "My Connector",
                "source": "notion",
                "status": "schedule"
            },
            ...
        ]
    }
    ```
    """
    try:
        connectors = ConnectorService.list(db, user.id)
        return get_json_result(data=connectors)
    except Exception as e:
        return server_error_response(e)


@router.get("/{connector_id}", summary="获取连接器详情", response_description="连接器详情")
def get_connector(
    connector_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### GET `/{connector_id}` 获取连接器详情

    **功能描述**:
    根据连接器ID获取连接器的详细信息。

    ---

    ### 路径参数

    | 参数          | 类型   | 描述       |
    |---------------|--------|------------|
    | `connector_id`| string | 连接器ID   |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "id": "connector_id",
            "name": "My Connector",
            "source": "notion",
            "config": {...},
            ...
        }
    }
    ```
    """
    try:
        connector = ConnectorService.get_by_id(db, connector_id)
        if not connector:
            return get_data_error_result(retmsg="找不到该连接器")
        return get_json_result(data=connector.to_dict())
    except Exception as e:
        return server_error_response(e)


@router.get("/{connector_id}/logs", summary="获取同步日志", response_description="同步日志列表")
def list_logs(
    connector_id: str,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(15, ge=1, le=100, description="每页数量"),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### GET `/{connector_id}/logs` 获取同步日志

    **功能描述**:
    获取指定连接器的同步任务日志列表。

    ---

    ### 路径参数

    | 参数          | 类型   | 描述       |
    |---------------|--------|------------|
    | `connector_id`| string | 连接器ID   |

    ### 查询参数

    | 参数       | 类型 | 必填 | 描述             |
    |------------|------|------|------------------|
    | `page`     | int  | 否   | 页码，默认1      |
    | `page_size`| int  | 否   | 每页数量，默认15 |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": [
            {
                "id": "log_id",
                "connector_id": "connector_id",
                "status": "done",
                "new_docs_indexed": 10,
                ...
            },
            ...
        ]
    }
    ```
    """
    try:
        logs = SyncLogsService.list_sync_tasks(db, connector_id, page, page_size)
        return get_json_result(data=logs)
    except Exception as e:
        return server_error_response(e)


@router.put("/{connector_id}/resume", summary="恢复或暂停连接器", response_description="操作结果")
def resume(
    connector_id: str,
    request: ResumeConnectorRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### PUT `/{connector_id}/resume` 恢复或暂停连接器

    **功能描述**:
    恢复或暂停连接器的同步调度任务。

    ---

    ### 路径参数

    | 参数          | 类型   | 描述       |
    |---------------|--------|------------|
    | `connector_id`| string | 连接器ID   |

    ### 请求体 (Request Body)

    | 字段     | 类型    | 必填 | 描述                              |
    |----------|---------|------|-----------------------------------|
    | `resume` | boolean | 否   | true=恢复调度，false=暂停，默认true |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": true
    }
    ```
    """
    try:
        req = request.model_dump()
        if req.get("resume"):
            ConnectorService.resume(db, connector_id, TaskStatus.SCHEDULE)
        else:
            ConnectorService.resume(db, connector_id, TaskStatus.CANCEL)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post("/{connector_id}/link", summary="关联知识库", response_description="操作结果")
def link_kb(
    connector_id: str,
    request: LinkKbRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### POST `/{connector_id}/link` 关联知识库

    **功能描述**:
    将连接器与一个或多个知识库进行关联，同步的文档将被导入到关联的知识库中。

    ---

    ### 路径参数

    | 参数          | 类型   | 描述       |
    |---------------|--------|------------|
    | `connector_id`| string | 连接器ID   |

    ### 请求体 (Request Body)

    | 字段     | 类型          | 必填 | 描述               |
    |----------|---------------|------|--------------------|
    | `kb_ids` | list[string]  | 是   | 要关联的知识库ID列表 |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": true
    }
    ```

    #### 错误响应 (500)
    ```json
    {
        "retcode": 500,
        "retmsg": "关联过程中的错误信息",
        "data": false
    }
    ```
    """
    try:
        req = request.model_dump()
        errors = Connector2KbService.link_kb(db, connector_id, req["kb_ids"], user.id)
        if errors:
            return get_json_result(data=False, retmsg=errors, retcode=RetCode.SERVER_ERROR)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post("/{connector_id}/rm", summary="删除连接器", response_description="操作结果")
def rm_connector(
    connector_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### POST `/{connector_id}/rm` 删除连接器

    **功能描述**:
    删除指定的数据源连接器。删除前会先取消所有相关的同步任务。

    ---

    ### 路径参数

    | 参数          | 类型   | 描述       |
    |---------------|--------|------------|
    | `connector_id`| string | 连接器ID   |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": true
    }
    ```

    ---

    ### 注意事项

    - 删除连接器会取消所有相关的调度任务
    - 已同步的文档不会被删除，需要手动清理
    """
    try:
        # 先取消所有相关任务
        ConnectorService.resume(db, connector_id, TaskStatus.CANCEL)
        # 删除连接器
        ConnectorService.delete_by_id(db, connector_id)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)

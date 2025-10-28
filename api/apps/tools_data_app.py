# coding=utf-8
"""
@project: multirag
@Author：龙
@file： tools_data_app.py
@date：2025/9/23 10:00
@desc: MCP工具数据接口
"""
import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.db.db_models import get_db
from api.db.services.tools_data_service import ToolsDataService
from api.utils.api_utils import get_error_data_result, get_json_result, server_error_response


router = APIRouter()


def _normalize_payload(data: Any) -> str | None:
    """将请求体中的复杂结构转换为字符串存储。"""
    if data is None:
        return None
    if isinstance(data, str):
        return data
    try:
        return json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(data)


def _deserialize_payload(text_data: str | None) -> Any:
    """尝试将存储的文本转换回原始结构。"""
    if text_data is None:
        return None
    try:
        return json.loads(text_data)
    except (json.JSONDecodeError, TypeError):
        return text_data


class ToolsDataKey(BaseModel):
    flow_id: str = Field(..., description="数据对应的流程ID")
    user_id: str | None = Field(None, description="用户ID，不传则默认当前登录用户")


class CreateToolsDataRequest(ToolsDataKey):
    meta_data: Any | None = Field(None, description="工具运行的参数或中间数据")
    final_data: Any | None = Field(None, description="工具最终输出数据")


@router.get('/detail', summary="查询工具数据", response_description="查询成功返回数据详情")
def get_tools_data(
    flow_id: str = Query(..., description="数据对应的流程ID"),
    user_id: str | None = Query(None, description="用户ID，不传则默认当前登录用户"),
    db: Session = Depends(get_db)
):
    """
    ### GET `/detail` 查询工具数据

    **功能描述**:
    根据 `flow_id` 及可选的 `user_id` 查询 MCP 工具数据表中对应的一条记录。
    若未提供 `user_id`，将以 `null` 匹配记录。

    ---
    ### 查询参数 (Query Parameters)
    | 参数      | 类型     | 必填 | 默认值 | 描述                               |
    |-----------|----------|------|--------|------------------------------------|
    | `flow_id` | `string` | 是   | 无     | 工具数据对应的流程 ID               |
    | `user_id` | `string` | 否   | `null` | 工具数据对应的用户 ID（可为空） |

    **请求示例**:
    ```
    GET /detail?flow_id=rag-flow-001&user_id=user-123
    ```

    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "data": {
            "flow_id": "rag-flow-001",
            "user_id": "user-123",
            "meta_data": "{\"step\":\"collect\"}",
            "final_data": {"answer": "ok"}
        }
    }
    ```
    """
    try:
        resolved_user_id = user_id
        record = ToolsDataService.get_by_flow_id_user_id(db, flow_id=flow_id, user_id=resolved_user_id)
        if not record:
            return get_error_data_result(retmsg="Record not found")

        data = record.to_dict()
        # data["meta_data"] = _deserialize_payload(data.get("meta_data"))
        data["final_data"] = _deserialize_payload(data.get("final_data"))
        return get_json_result(data=data)
    except Exception as e:
        db.rollback()
        return server_error_response(e)


@router.post('/create', summary="新增或更新工具数据", response_description="成功创建或更新数据")
def create_tools_data(
    request: CreateToolsDataRequest,
    db: Session = Depends(get_db)
):
    """
    ### POST `/create` 新增或更新工具数据

    **功能描述**:
    基于 `flow_id` 与 `user_id` 组合键写入工具数据，可传入 `meta_data`、`final_data` 字段。
    若 `user_id` 为空，将以 `null` 存储。

    ---
    ### 请求体 (Request Body)
    | 字段         | 类型     | 必填 | 描述                                      |
    |--------------|----------|------|-------------------------------------------|
    | `flow_id`    | `string` | 是   | 流程 ID                                   |
    | `user_id`    | `string` | 否   | 用户 ID，可为空                           |
    | `meta_data`  | `any`    | 否   | 工具运行过程中的中间信息（将转存为文本）   |
    | `final_data` | `any`    | 否   | 工具最终输出结果（将转存为文本或 JSON）   |

    **请求示例**:
    ```json
    {
        "flow_id": "rag-flow-001",
        "user_id": "user-123",
        "meta_data": {"inputs": ["a", "b"]},
        "final_data": {"result": "ok"}
    }
    ```

    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "data": {
            "flow_id": "rag-flow-001",
            "user_id": "user-123",
            "meta_data": {"inputs": ["a", "b"]},
            "final_data": {"result": "ok"}
        }
    }
    ```
    """
    try:
        payload = request.model_dump()
        resolved_user_id = payload.get("user_id")

        record = ToolsDataService.create_or_update(
            db,
            flow_id=payload["flow_id"],
            user_id=resolved_user_id,
            meta_data=_normalize_payload(payload.get("meta_data")),
            final_data=_normalize_payload(payload.get("final_data")),
        )

        data = record.to_dict()
        data["meta_data"] = _deserialize_payload(data.get("meta_data"))
        data["final_data"] = _deserialize_payload(data.get("final_data"))
        return get_json_result(data=data)
    except Exception as e:
        db.rollback()
        return server_error_response(e)


@router.delete('/remove', summary="删除工具数据", response_description="成功删除返回True")
def delete_tools_data(
    request: ToolsDataKey,
    db: Session = Depends(get_db)
):
    """
    ### DELETE `/remove` 删除工具数据

    **功能描述**:
    删除工具数据表中指定 `flow_id`、`user_id` 的记录。
    `user_id` 为空时以 `null` 匹配。

    ---
    ### 请求体 (Request Body)
    | 字段      | 类型     | 必填 | 描述                         |
    |-----------|----------|------|------------------------------|
    | `flow_id` | `string` | 是   | 流程 ID                      |
    | `user_id` | `string` | 否   | 用户 ID，可为空             |

    **请求示例**:
    ```json
    {
        "flow_id": "rag-flow-001",
        "user_id": "user-123"
    }
    ```

    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "data": true
    }
    ```
    """
    try:
        payload = request.model_dump()
        resolved_user_id = payload.get("user_id")

        deleted = ToolsDataService.delete_by_flow_id_user_id(
            db,
            flow_id=payload["flow_id"],
            user_id=resolved_user_id,
        )

        if not deleted:
            return get_error_data_result(retmsg="Record not found")

        return get_json_result(data=True)
    except Exception as e:
        db.rollback()
        return server_error_response(e)

# coding=utf-8
"""
@project: multirag
@Author：龙
@file： mcp_server_app.py
@date：2025/7/16 10:00
@desc: MCP服务器应用接口
"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api import settings
from api.db import VALID_MCP_SERVER_TYPES
from api.db.db_models import get_db, MCPServer
from api.db.services.mcp_server_service import MCPServerService
from api.db.services.user_service import TenantService
from api.utils import get_uuid
from api.utils.api_utils import get_data_error_result, get_json_result, server_error_response
from api.utils.web_utils import safe_json_parse
from api.apps import manager

router = APIRouter()


class CreateMCPServerRequest(BaseModel):
    name: str
    server_type: str
    url: str
    description: str | None = None
    variables: dict | None = None
    headers: dict | None = None


class UpdateMCPServerRequest(BaseModel):
    id: str
    name: str | None = None
    server_type: str | None = None
    url: str | None = None
    description: str | None = None
    variables: dict | None = None
    headers: dict | None = None


class GetMultipleMCPServerRequest(BaseModel):
    id_list: list[str]


class ListMCPServerRequest(BaseModel):
    mcp_ids: list[str] = []


class RemoveMCPServerRequest(BaseModel):
    mcp_ids: list[str]


class ImportMCPServerRequest(BaseModel):
    mcpServers: dict


class ExportMCPServerRequest(BaseModel):
    mcp_ids: list[str]


@router.post('/list', summary="获取MCP服务器列表", response_description="成功获取MCP服务器列表")
def list_mcp(
    request: ListMCPServerRequest,
    keywords: str = Query("", description="关键词搜索"),
    page: int = Query(0, description="页码"),
    page_size: int = Query(0, description="每页数量"),
    orderby: str = Query("create_time", description="排序字段"),
    desc: bool = Query(True, description="是否降序"),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### POST `/list` 获取MCP服务器列表
    
    **功能描述**:
    此接口用于获取MCP服务器列表，支持分页查询、关键词搜索和排序功能。
    用户可以查看自己有权限访问的所有MCP服务器，或指定特定的MCP服务器。
    支持按名称关键词过滤，并提供灵活的排序和分页选项。
    
    ---
    ### 请求体 (Request Body)
    | 字段      | 类型          | 必填 | 描述                         |
    |-----------|---------------|------|------------------------------|
    | `mcp_ids` | `list[string]` | 否   | 指定MCP服务器ID列表，为空时查询所有可访问的服务器 |
    
    ### 查询参数 (Query Parameters)
    | 参数        | 类型      | 必填 | 默认值      | 描述                         |
    |-------------|-----------|------|-------------|------------------------------|
    | `keywords`  | `string`  | 否   | ""          | 搜索关键词，用于模糊匹配服务器名称 |
    | `page`      | `integer` | 否   | 0           | 页码，0表示不分页            |
    | `page_size` | `integer` | 否   | 0           | 每页显示的记录数，0表示不分页 |
    | `orderby`   | `string`  | 否   | "create_time" | 排序字段               |
    | `desc`      | `boolean` | 否   | true        | 是否降序排列             |
    
    **请求示例**:
    ```json
    {
        "mcp_ids": ["uuid-server-id-1", "uuid-server-id-2"]
    }
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "mcp_servers": [
                {
                    "id": "uuid-server-id-1",
                    "name": "智能客服MCP服务器",
                    "server_type": "http",
                    "url": "https://api.example.com/mcp",
                    "description": "用于客服场景的MCP服务器",
                    "variables": {
                        "timeout": 30
                    },
                    "update_date": "2024-07-16T10:00:00"
                }
            ],
            "total": 1
        }
    }
    ```
    """
    req_data = request.model_dump()
    mcp_ids = req_data.get("mcp_ids", [])
    
    try:
        servers = MCPServerService.get_servers(
            db, user.id, mcp_ids, page, page_size, orderby, desc, keywords
        ) or []

        return get_json_result(data={"mcp_servers": servers, "total": len(servers)})
    except Exception as e:
        return server_error_response(e)


@router.get('/detail', summary="获取MCP服务器详情", response_description="成功获取MCP服务器详情")
def detail(
    mcp_id: str = Query(..., description="MCP服务器ID"),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### GET `/detail` 获取MCP服务器详情
    
    **功能描述**:
    此接口用于获取指定MCP服务器的详细信息。系统会验证用户对该MCP服务器的访问权限，
    确保用户只能查看有权限的MCP服务器详情，包括服务器的基本信息、配置参数和元数据。
    
    ---
    ### 请求参数 (Query Parameters)
    | 参数      | 类型     | 必填 | 描述               |
    |-----------|----------|------|--------------------|
    | `mcp_id`  | `string` | 是   | MCP服务器的唯一标识符 |
    
    **请求示例**:
    ```
    GET /detail?mcp_id=uuid-server-id
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "id": "uuid-server-id",
            "name": "智能客服MCP服务器",
            "server_type": "http",
            "url": "https://api.example.com/mcp",
            "description": "用于客服场景的MCP服务器",
            "variables": {
                "api_key": "your-api-key",
                "timeout": 30
            },
            "headers": {
                "Content-Type": "application/json"
            },
            "tenant_id": "tenant-uuid",
            "create_time": "2024-07-16T10:00:00",
            "update_time": "2024-07-16T10:00:00"
        }
    }
    ```
    
    #### 错误响应
    - **MCP服务器不存在**:
    ```json
    {
        "retcode": 404,
        "retmsg": "NOT_FOUND",
        "data": null
    }
    ```
    """
    try:
        mcp_server = MCPServerService.get_or_none(db, id=mcp_id, tenant_id=user.id)

        if mcp_server is None:
            return get_json_result(retcode=settings.RetCode.NOT_FOUND, data=None)

        return get_json_result(data=mcp_server.to_dict())
    except Exception as e:
        return server_error_response(e)


@router.post('/create', summary="创建MCP服务器", response_description="成功创建MCP服务器")
def create(request: CreateMCPServerRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/create` 创建MCP服务器
    
    **功能描述**:
    此接口用于创建新的MCP服务器配置。用户可以通过提供服务器名称、类型、URL和其他配置信息来创建MCP服务器。
    系统会自动检查名称重复性、验证用户权限，并为MCP服务器分配唯一标识符。
    
    ---
    ### 请求体 (Request Body)
    | 字段          | 类型     | 必填 | 描述                                               |
    |---------------|----------|------|----------------------------------------------------|
    | `name`        | `string` | 是   | MCP服务器名称，不能为空，长度不能超过255字节        |
    | `server_type` | `string` | 是   | MCP服务器类型，必须是有效的MCP服务器类型           |
    | `url`         | `string` | 是   | MCP服务器的访问地址                                |
    | `description` | `string` | 否   | MCP服务器描述信息，用于说明服务器的用途和功能        |
    | `variables`   | `object` | 否   | MCP服务器变量配置，JSON格式                        |
    | `headers`     | `object` | 否   | 额外的HTTP请求头配置，JSON格式                      |
    
    **请求示例**:
    ```json
    {
        "name": "智能客服MCP服务器",
        "server_type": "http",
        "url": "https://api.example.com/mcp",
        "description": "用于客服场景的MCP服务器",
        "variables": {
            "api_key": "your-api-key",
            "timeout": 30
        },
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer token"
        }
    }
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "id": "uuid-generated-id"
        }
    }
    ```
    
    #### 错误响应
    - **不支持的服务器类型**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Unsupported MCP server type.",
        "data": null
    }
    ```
    - **无效的服务器名称**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Invalid MCP name or length is xxx which is large than 255.",
        "data": null
    }
    ```
    - **租户未找到**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Tenant not found.",
        "data": null
    }
    ```
    """
    req_data = request.model_dump()

    server_type = req_data.get("server_type", "")
    if server_type not in VALID_MCP_SERVER_TYPES:
        return get_data_error_result(retmsg="Unsupported MCP server type.")

    server_name = req_data.get("name", "")
    if not server_name or len(server_name.encode("utf-8")) > 255:
        return get_data_error_result(retmsg=f"Invalid MCP name or length is {len(server_name)} which is large than 255.")

    # 使用safe_json_parse处理JSON字段
    req_data["headers"] = safe_json_parse(req_data.get("headers", {}))
    req_data["variables"] = safe_json_parse(req_data.get("variables", {}))

    try:
        req_data["id"] = get_uuid()
        req_data["tenant_id"] = user.id

        # 验证租户存在
        tenant = TenantService.get_by_id(db, user.id)
        if not tenant:
            return get_data_error_result(retmsg="Tenant not found.")

        # 创建MCP服务器
        if not MCPServerService.insert(db, **req_data):
            return get_data_error_result()

        return get_json_result(data={"id": req_data["id"]})
    except Exception as e:
        return server_error_response(e)


@router.post('/update', summary="更新MCP服务器", response_description="成功更新MCP服务器")
def update(request: UpdateMCPServerRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/update` 更新MCP服务器
    
    **功能描述**:
    此接口用于更新现有的MCP服务器配置信息。支持更新服务器名称、类型、URL、描述和配置参数。
    系统会验证用户权限、检查服务器类型有效性，并合并配置参数，保留现有配置的同时应用新的设置。
    
    ---
    ### 请求体 (Request Body)
    | 字段          | 类型     | 必填 | 描述                                               |
    |---------------|----------|------|----------------------------------------------------|
    | `id`          | `string` | 是   | 要更新的MCP服务器ID                                |
    | `name`        | `string` | 否   | 新的MCP服务器名称                                  |
    | `server_type` | `string` | 否   | 新的MCP服务器类型                                  |
    | `url`         | `string` | 否   | 新的MCP服务器URL                                   |
    | `description` | `string` | 否   | 新的MCP服务器描述                                  |
    | `variables`   | `object` | 否   | 新的变量配置，会与现有配置合并                      |
    | `headers`     | `object` | 否   | 新的请求头配置，会与现有配置合并                    |
    
    **请求示例**:
    ```json
    {
        "id": "uuid-server-id",
        "name": "增强版智能客服MCP服务器",
        "server_type": "websocket",
        "url": "wss://api.example.com/mcp",
        "description": "升级版的客服MCP服务器，支持WebSocket",
        "variables": {
            "api_key": "new-api-key",
            "timeout": 60
        },
        "headers": {
            "User-Agent": "MultiRAG/1.0"
        }
    }
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "id": "uuid-server-id",
            "name": "增强版智能客服MCP服务器",
            "server_type": "websocket",
            "url": "wss://api.example.com/mcp",
            "description": "升级版的客服MCP服务器，支持WebSocket",
            "variables": {
                "api_key": "new-api-key",
                "timeout": 60
            },
            "headers": {
                "Content-Type": "application/json",
                "User-Agent": "MultiRAG/1.0"
            },
            "tenant_id": "tenant-uuid",
            "create_time": "2024-07-16T10:00:00",
            "update_time": "2024-07-16T11:30:00"
        }
    }
    ```
    
    #### 错误响应
    - **更新失败**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Failed to update MCP server.",
        "data": null
    }
    ```
    - **服务器不存在**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Failed to fetch updated MCP server.",
        "data": null
    }
    ```
    """
    req_data = request.model_dump()

    server_type = req_data.get("server_type", "")
    if server_type and server_type not in VALID_MCP_SERVER_TYPES:
        return get_data_error_result(retmsg="Unsupported MCP server type.")
    
    server_name = req_data.get("name", "")
    if server_name and len(server_name.encode("utf-8")) > 255:
        return get_data_error_result(retmsg=f"Invalid MCP name or length is {len(server_name)} which is large than 255.")

    # 使用safe_json_parse处理JSON字段
    if "headers" in req_data and req_data["headers"] is not None:
        req_data["headers"] = safe_json_parse(req_data["headers"])
    if "variables" in req_data and req_data["variables"] is not None:
        req_data["variables"] = safe_json_parse(req_data["variables"])

    try:
        server_id = req_data["id"]
        req_data["tenant_id"] = user.id
        
        # 移除id字段，避免更新时冲突
        req_data.pop("id", None)

        # 更新MCP服务器
        if not MCPServerService.filter_update(db, [MCPServer.id == server_id, MCPServer.tenant_id == user.id], req_data):
            return get_data_error_result(retmsg="Failed to update MCP server.")

        # 获取更新后的MCP服务器
        updated_mcp = MCPServerService.get_by_id(db, server_id)
        if not updated_mcp:
            return get_data_error_result(retmsg="Failed to fetch updated MCP server.")

        return get_json_result(data=updated_mcp.to_dict())
    except Exception as e:
        return server_error_response(e)


@router.post('/get_multiple', summary="批量获取MCP服务器", response_description="成功批量获取MCP服务器")
def get_multiple(request: GetMultipleMCPServerRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/get_multiple` 批量获取MCP服务器
    
    **功能描述**:
    此接口用于根据ID列表批量获取MCP服务器详情。用户可以通过提供MCP服务器ID列表，
    一次性获取多个MCP服务器的详细信息。系统会验证用户对每个MCP服务器的访问权限。
    
    ---
    ### 请求体 (Request Body)
    | 字段      | 类型          | 必填 | 描述                     |
    |-----------|---------------|------|--------------------------|
    | `id_list` | `list[string]` | 是   | MCP服务器ID列表           |
    
    **请求示例**:
    ```json
    {
        "id_list": ["uuid-server-id-1", "uuid-server-id-2", "uuid-server-id-3"]
    }
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": [
            {
                "id": "uuid-server-id-1",
                "name": "智能客服MCP服务器",
                "server_type": "http",
                "url": "https://api.example.com/mcp",
                "description": "用于客服场景的MCP服务器",
                "variables": {
                    "timeout": 30
                },
                "update_date": "2024-07-16T10:00:00"
            }
        ]
    }
    ```
    """
    req_data = request.model_dump()
    id_list = req_data.get("id_list", [])
    
    if not id_list:
        return get_json_result(data=[])
    
    try:
        # 使用get_servers方法，传入id_list
        servers = MCPServerService.get_servers(
            db, user.id, id_list, 0, 0, "create_time", True, ""
        ) or []
        
        return get_json_result(data=servers)
    except Exception as e:
        return server_error_response(e)


@router.post('/rm', summary="删除MCP服务器", response_description="成功删除MCP服务器")
def rm(request: RemoveMCPServerRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/rm` 删除MCP服务器
    
    **功能描述**:
    此接口用于删除指定的MCP服务器。执行的是软删除操作，即将MCP服务器状态标记为已删除，
    而不是物理删除数据。系统会验证用户对该MCP服务器的删除权限，确保只有授权用户才能执行删除操作。
    
    ---
    ### 请求体 (Request Body)
    | 字段      | 类型          | 必填 | 描述                     |
    |-----------|---------------|------|--------------------------|
    | `mcp_ids` | `list[string]` | 是   | 要删除的MCP服务器ID列表   |
    
    **请求示例**:
    ```json
    {
        "mcp_ids": ["uuid-server-id-1", "uuid-server-id-2"]
    }
    ```
    
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
    
    #### 错误响应
    - **删除失败**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Failed to delete MCP servers ['uuid-server-id-1', 'uuid-server-id-2']",
        "data": null
    }
    ```
    """
    req_data = request.model_dump()
    mcp_ids = req_data.get("mcp_ids", [])

    try:
        # 批量删除MCP服务器
        if not MCPServerService.delete_by_ids(db, mcp_ids):
            return get_data_error_result(retmsg=f"Failed to delete MCP servers {mcp_ids}")

        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post('/import', summary="批量导入MCP服务器", response_description="成功批量导入MCP服务器")
def import_multiple(request: ImportMCPServerRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/import` 批量导入MCP服务器
    
    **功能描述**:
    此接口用于批量导入多个MCP服务器配置。支持从外部配置文件或其他系统导入MCP服务器设置。
    系统会自动处理重名冲突，确保每个导入的服务器都有唯一的名称。
    
    ---
    ### 请求体 (Request Body)
    | 字段         | 类型     | 必填 | 描述                             |
    |--------------|----------|------|----------------------------------|
    | `mcpServers` | `object` | 是   | MCP服务器配置对象，键为服务器名称 |
    
    **请求示例**:
    ```json
    {
        "mcpServers": {
            "客服MCP服务器": {
                "type": "http",
                "url": "https://api.example.com/mcp",
                "authorization_token": "token123",
                "tool_configuration": {
                    "timeout": 30
                }
            },
            "知识库MCP服务器": {
                "type": "websocket",
                "url": "wss://kb.example.com/mcp",
                "authorization_token": "token456",
                "tool_configuration": {
                    "max_connections": 10
                }
            }
        }
    }
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "results": [
                {
                    "server": "客服MCP服务器",
                    "success": true,
                    "action": "created",
                    "id": "uuid-generated-id-1",
                    "new_name": "客服MCP服务器"
                },
                {
                    "server": "知识库MCP服务器",
                    "success": true,
                    "action": "created",
                    "id": "uuid-generated-id-2",
                    "new_name": "知识库MCP服务器_0",
                    "message": "Renamed from '知识库MCP服务器' to avoid duplication"
                }
            ]
        }
    }
    ```
    """
    req_data = request.model_dump()
    servers = req_data.get("mcpServers", {})

    if not servers:
        return get_data_error_result(retmsg="No MCP servers provided.")

    results = []
    try:
        for server_name, config in servers.items():
            if not all(key in config for key in ["type", "url"]):
                results.append({"server": server_name, "success": False, "message": "Missing required fields (type or url)"})
                continue

            base_name = server_name
            new_name = base_name
            counter = 0

            # 检查名称冲突并自动重命名
            while True:
                existing, _ = MCPServerService.get_by_name_and_tenant(db, name=new_name, tenant_id=user.id)
                if not existing:
                    break
                new_name = f"{base_name}_{counter}"
                counter += 1

            create_data = {
                "id": get_uuid(),
                "tenant_id": user.id,
                "name": new_name,
                "url": config["url"],
                "server_type": config["type"],
                "variables": {
                    "authorization_token": config.get("authorization_token", ""),
                    "tool_configuration": config.get("tool_configuration", {})
                },
            }

            if MCPServerService.insert(db, **create_data):
                result = {"server": server_name, "success": True, "action": "created", "id": create_data["id"], "new_name": new_name}
                if new_name != base_name:
                    result["message"] = f"Renamed from '{base_name}' to avoid duplication"

                results.append(result)
            else:
                results.append({"server": server_name, "success": False, "message": "Failed to create MCP server."})

        return get_json_result(data={"results": results})
    except Exception as e:
        return server_error_response(e)


@router.post('/export', summary="批量导出MCP服务器", response_description="成功批量导出MCP服务器")
def export_multiple(request: ExportMCPServerRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/export` 批量导出MCP服务器
    
    **功能描述**:
    此接口用于批量导出指定的MCP服务器配置。用户可以将MCP服务器配置导出为标准格式，
    便于备份、迁移或与其他系统集成。导出的配置包含服务器的所有关键信息。
    
    ---
    ### 请求体 (Request Body)
    | 字段      | 类型          | 必填 | 描述                     |
    |-----------|---------------|------|--------------------------|
    | `mcp_ids` | `list[string]` | 是   | 要导出的MCP服务器ID列表   |
    
    **请求示例**:
    ```json
    {
        "mcp_ids": ["uuid-server-id-1", "uuid-server-id-2"]
    }
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "mcpServers": {
                "智能客服MCP服务器": {
                    "type": "http",
                    "url": "https://api.example.com/mcp",
                    "name": "智能客服MCP服务器",
                    "authorization_token": "token123",
                    "tool_configuration": {
                        "timeout": 30
                    }
                },
                "知识库MCP服务器": {
                    "type": "websocket",
                    "url": "wss://kb.example.com/mcp",
                    "name": "知识库MCP服务器",
                    "authorization_token": "token456",
                    "tool_configuration": {
                        "max_connections": 10
                    }
                }
            }
        }
    }
    ```
    
    #### 错误响应
    - **无MCP服务器ID**:
    ```json
    {
        "retcode": 400,
        "retmsg": "No MCP server IDs provided.",
        "data": null
    }
    ```
    """
    req_data = request.model_dump()
    mcp_ids = req_data.get("mcp_ids", [])

    if not mcp_ids:
        return get_data_error_result(retmsg="No MCP server IDs provided.")

    try:
        exported_servers = {}

        for mcp_id in mcp_ids:
            mcp_server = MCPServerService.get_by_id(db, mcp_id)

            if mcp_server and mcp_server.tenant_id == user.id:
                server_key = mcp_server.name

                exported_servers[server_key] = {
                    "type": mcp_server.server_type,
                    "url": mcp_server.url,
                    "name": mcp_server.name,
                    "authorization_token": mcp_server.variables.get("authorization_token", ""),
                    "tool_configuration": mcp_server.variables.get("tool_configuration", {}),
                }

        return get_json_result(data={"mcpServers": exported_servers})
    except Exception as e:
        return server_error_response(e)
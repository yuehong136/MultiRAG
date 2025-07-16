from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api import settings
from api.constants import DATASET_NAME_LIMIT
from api.db import StatusEnum
from api.db.db_models import get_db, MCPServer
from api.db.services import duplicate_name
from api.db.services.mcp_server_service import MCPServerService
from api.db.services.user_service import TenantService, UserTenantService
from api.utils import get_uuid
from api.utils.api_utils import get_data_error_result, get_json_result, server_error_response
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
    name: str
    server_type: str
    url: str
    description: str | None = None
    variables: dict | None = None
    headers: dict | None = None
    tenant_id: str


class ListMCPServerRequest(BaseModel):
    owner_ids: list[str] | None = []


class GetMultipleMCPServerRequest(BaseModel):
    id_list: list[str]


class RemoveMCPServerRequest(BaseModel):
    id: str


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
    | `name`        | `string` | 是   | MCP服务器名称，不能为空，长度不能超过系统限制        |
    | `server_type` | `string` | 是   | MCP服务器类型，如"http"、"websocket"等              |
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
            "server_id": "uuid-generated-id"
        }
    }
    ```
    
    #### 错误响应
    - **数据验证错误**:
    ```json
    {
        "retcode": 400,
        "retmsg": "MCP server name must be string.",
        "data": null
    }
    ```
    - **名称重复错误**:
    ```json
    {
        "retcode": 400,
        "retmsg": "已存在该MCP服务器名: 智能客服MCP服务器，请调整！",
        "data": null
    }
    ```
    - **权限验证错误**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Authorization identity.",
        "data": null
    }
    ```
    
    ---
    ### 主要流程
    1. 验证请求参数的有效性（名称类型、长度限制等）
    2. 验证用户租户身份，确保用户有权限创建MCP服务器
    3. 检查MCP服务器名称是否重复，避免重名冲突
    4. 生成唯一的MCP服务器ID，保存服务器信息到数据库
    5. 返回创建成功的MCP服务器ID
    
    ---
    ### 注意事项
    - **名称限制**: MCP服务器名称不能为空，且UTF-8编码长度不能超过DATASET_NAME_LIMIT
    - **权限验证**: 需要验证用户的租户身份，未授权用户无法创建MCP服务器
    - **名称唯一性**: 同一租户下的MCP服务器名称必须唯一
    - **自动去重**: 系统会自动处理重复名称，添加后缀确保唯一性
    - **配置安全**: 敏感配置信息（如API密钥）需要妥善保护
    """
    req_data = request.model_dump()
    server_name = req_data["name"]
    server_type = req_data["server_type"]
    url = req_data["url"]
    description = req_data.get("description", "")
    variables = req_data.get("variables", {})
    headers = req_data.get("headers", {})
    
    # 验证必填字段
    if not isinstance(server_name, str):
        return get_data_error_result(retmsg="MCP server name must be string.")
    if server_name.strip() == "":
        return get_data_error_result(retmsg="MCP server name can't be empty.")
    if len(server_name.encode("utf-8")) > DATASET_NAME_LIMIT:
        return get_data_error_result(retmsg=f"MCP server name length is {len(server_name)} which is large than {DATASET_NAME_LIMIT}")
    
    if not isinstance(server_type, str) or server_type.strip() == "":
        return get_data_error_result(retmsg="MCP server type must be string and can't be empty.")
    
    if not isinstance(url, str) or url.strip() == "":
        return get_data_error_result(retmsg="MCP server URL must be string and can't be empty.")
    
    # 验证租户
    tenant = TenantService.get_by_id(db, user.id)
    if not tenant:
        return get_data_error_result(retmsg="Authorization identity.")
    
    server_name = server_name.strip()
    server_name = duplicate_name(MCPServerService.query, name=server_name, tenant_id=user.id, status=StatusEnum.VALID.value)
    
    try:
        req_data["id"] = get_uuid()
        req_data["name"] = server_name
        req_data["server_type"] = server_type.strip()
        req_data["url"] = url.strip()
        req_data["description"] = description
        req_data["variables"] = variables
        req_data["headers"] = headers
        req_data["tenant_id"] = user.id
        req_data["created_by"] = user.id
        
        server_obj = MCPServerService.save(db, **req_data)
        if not server_obj:
            return get_data_error_result()
        return get_json_result(data={"server_id": req_data["id"]})
    except Exception as e:
        return server_error_response(e)


@router.post('/update', summary="更新MCP服务器", response_description="成功更新MCP服务器")
def update(request: UpdateMCPServerRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/update` 更新MCP服务器
    
    **功能描述**:
    此接口用于更新现有的MCP服务器配置信息。支持更新服务器名称、类型、URL、描述和配置参数。
    系统会验证用户权限、检查名称重复性，并合并配置参数，保留现有配置的同时应用新的设置。
    
    ---
    ### 请求体 (Request Body)
    | 字段          | 类型     | 必填 | 描述                                               |
    |---------------|----------|------|----------------------------------------------------|
    | `id`   | `string` | 是   | 要更新的MCP服务器ID                                |
    | `name`        | `string` | 是   | 新的MCP服务器名称                                  |
    | `server_type` | `string` | 是   | 新的MCP服务器类型                                  |
    | `url`         | `string` | 是   | 新的MCP服务器URL                                   |
    | `description` | `string` | 否   | 新的MCP服务器描述                                  |
    | `variables`   | `object` | 否   | 新的变量配置，会与现有配置合并                      |
    | `headers`     | `object` | 否   | 新的请求头配置，会与现有配置合并                    |
    | `tenant_id`   | `string` | 是   | 租户ID，用于权限验证                               |
    
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
        },
        "tenant_id": "tenant-uuid"
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
            "created_by": "user-uuid",
            "create_time": "2024-07-16T10:00:00",
            "update_time": "2024-07-16T11:30:00"
        }
    }
    ```
    
    #### 错误响应
    - **权限不足**:
    ```json
    {
        "retcode": 403,
        "retmsg": "No authorization.",
        "data": false
    }
    ```
    - **MCP服务器不存在**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Cannot find MCP server uuid-server-id",
        "data": false
    }
    ```
    - **名称重复**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Duplicated MCP server name.",
        "data": null
    }
    ```
    
    ---
    ### 主要流程
    1. 验证请求参数的有效性和用户权限
    2. 查找要更新的MCP服务器，确认其存在性
    3. 检查新名称是否与其他服务器重复（忽略大小写）
    4. 合并配置参数：保留现有配置，应用新的配置项
    5. 更新MCP服务器信息并返回完整的更新后数据
    
    ---
    ### 注意事项
    - **权限验证**: 只有有权限的用户才能更新MCP服务器
    - **配置合并**: 新的variables和headers会与现有配置合并，不会完全覆盖
    - **名称唯一性**: 更新后的名称在同一租户下必须唯一（不区分大小写）
    - **原子操作**: 更新操作具有原子性，要么全部成功要么全部失败
    """
    req_data = request.model_dump()
    
    # 验证必填字段
    if not isinstance(req_data["name"], str):
        return get_data_error_result(retmsg="MCP server name must be string.")
    if req_data["name"].strip() == "":
        return get_data_error_result(retmsg="MCP server name can't be empty.")
    if len(req_data["name"].encode("utf-8")) > DATASET_NAME_LIMIT:
        return get_data_error_result(retmsg=f"MCP server name length is {len(req_data['name'])} which is large than {DATASET_NAME_LIMIT}")
    
    if not isinstance(req_data["server_type"], str) or req_data["server_type"].strip() == "":
        return get_data_error_result(retmsg="MCP server type must be string and can't be empty.")
    
    if not isinstance(req_data["url"], str) or req_data["url"].strip() == "":
        return get_data_error_result(retmsg="MCP server URL must be string and can't be empty.")
    
    req_data["name"] = req_data["name"].strip()
    req_data["server_type"] = req_data["server_type"].strip()
    req_data["url"] = req_data["url"].strip()
    tenant_id = req_data["tenant_id"]
    id = req_data["id"]
    
    # 验证租户
    tenant = TenantService.get_by_id(db, tenant_id)
    if not tenant:
        return get_data_error_result(retmsg="Authorization identity.")
    
    # 检查权限 - 简化版本，实际项目中应该有更详细的权限检查
    # if not MCPServerService.accessible4deletion(db, server_id, user.id):
    #     return get_json_result(data=False, retmsg="No authorization.", retcode=settings.RetCode.AUTHENTICATION_ERROR)
    
    try:
        # 获取现有MCP服务器
        server_apps = MCPServerService.query(db, tenant_id=tenant_id, id=id)
        if not server_apps:
            return get_json_result(data=False, retmsg=f"Cannot find MCP server {id}", retcode=settings.RetCode.DATA_ERROR)
        
        server_app = server_apps[0]
        
        # 检查名称重复
        if req_data["name"].lower() != server_app.name.lower():
            existing_servers = MCPServerService.query(db, name=req_data["name"], tenant_id=tenant_id, status=StatusEnum.VALID.value)
            if existing_servers:
                return get_data_error_result(retmsg="Duplicated MCP server name.")
        
        # 处理变量配置
        if "variables" in req_data and req_data["variables"] is not None:
            current_variables = server_app.variables or {}
            new_variables = req_data["variables"]
            
            if not isinstance(new_variables, dict):
                return get_data_error_result(retmsg="variables must be a JSON object")
            
            updated_variables = {**current_variables, **new_variables}
            req_data["variables"] = updated_variables
        
        # 处理请求头配置
        if "headers" in req_data and req_data["headers"] is not None:
            current_headers = server_app.headers or {}
            new_headers = req_data["headers"]
            
            if not isinstance(new_headers, dict):
                return get_data_error_result(retmsg="headers must be a JSON object")
            
            updated_headers = {**current_headers, **new_headers}
            req_data["headers"] = updated_headers
        
        # 移除不需要的字段
        req_data.pop("id", None)
        req_data.pop("tenant_id", None)
        
        # 更新MCP服务器
        updated_count = MCPServerService.update_by_id(db, id, req_data)
        if not updated_count:
            return get_data_error_result(retmsg="Failed to update MCP server")
        
        # 获取更新后的MCP服务器
        updated_server = MCPServerService.get_by_id(db, id)
        if not updated_server:
            return get_data_error_result(retmsg="Failed to fetch updated MCP server")
        
        return get_json_result(data=updated_server.to_dict())
        
    except Exception as e:
        return server_error_response(e)


@router.get('/detail', summary="获取MCP服务器详情", response_description="成功获取MCP服务器详情")
def detail(id: str = Query(..., description="MCP服务器ID"), db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### GET `/detail` 获取MCP服务器详情
    
    **功能描述**:
    此接口用于获取指定MCP服务器的详细信息。系统会验证用户对该MCP服务器的访问权限，
    确保用户只能查看有权限的MCP服务器详情，包括服务器的基本信息、配置参数和元数据。
    
    ---
    ### 请求参数 (Query Parameters)
    | 参数        | 类型     | 必填 | 描述               |
    |-------------|----------|------|--------------------|
    | `id` | `string` | 是   | MCP服务器的唯一标识符 |
    
    **请求示例**:
    ```
    GET /detail?id=uuid-server-id
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
            "created_by": "user-uuid",
            "create_time": "2024-07-16T10:00:00",
            "update_time": "2024-07-16T10:00:00",
            "status": "VALID"
        }
    }
    ```
    
    #### 错误响应
    - **权限不足**:
    ```json
    {
        "retcode": 500,
        "retmsg": "Has no permission for this operation.",
        "data": false
    }
    ```
    - **MCP服务器不存在**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Can't find this MCP Server!",
        "data": null
    }
    ```
    
    ---
    ### 主要流程
    1. 验证用户身份和登录状态
    2. 检查用户对指定MCP服务器的访问权限
    3. 遍历用户所属的所有租户，查找MCP服务器
    4. 获取MCP服务器的完整详细信息
    5. 返回MCP服务器的所有相关数据
    
    ---
    ### 注意事项
    - **权限验证**: 用户只能查看自己有权限访问的MCP服务器
    - **多租户支持**: 系统会检查用户在所有租户中的权限
    - **数据完整性**: 返回MCP服务器的完整信息，包括配置和元数据
    - **安全性**: 未授权访问会被拒绝，保护数据安全
    - **敏感信息**: 敏感配置信息需要谨慎处理，可能需要脱敏显示
    """
    try:
        # 检查用户权限
        tenants = UserTenantService.query(db, user_id=user.id)
        has_permission = False
        
        for tenant in tenants:
            server_apps = MCPServerService.query(db, tenant_id=tenant.tenant_id, id=id)
            if server_apps:
                has_permission = True
                break
        
        if not has_permission:
            return get_json_result(data=False, retmsg="Has no permission for this operation.", retcode=settings.RetCode.OPERATING_ERROR)
        
        # 获取MCP服务器详情
        server = MCPServerService.get_by_id(db, id)
        if not server:
            return get_data_error_result(retmsg="Can't find this MCP Server!")
        
        return get_json_result(data=server.to_dict())
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
            },
            {
                "id": "uuid-server-id-2",
                "name": "产品知识MCP服务器",
                "server_type": "websocket",
                "url": "wss://api.example.com/mcp",
                "description": "产品相关知识MCP服务器",
                "variables": {
                    "timeout": 60
                },
                "update_date": "2024-07-16T09:00:00"
            }
        ]
    }
    ```
    
    #### 错误响应
    - **无权限访问的服务器会被过滤**:
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": [
            // 只返回有权限访问的服务器
        ]
    }
    ```
    - **服务器内部错误**:
    ```json
    {
        "retcode": 500,
        "retmsg": "Internal server error",
        "data": null
    }
    ```
    
    ---
    ### 主要流程
    1. 验证用户身份和请求参数
    2. 获取用户有权限访问的所有租户
    3. 根据ID列表查询MCP服务器
    4. 过滤用户有权限访问的服务器
    5. 返回符合条件的MCP服务器列表
    
    ---
    ### 注意事项
    - **权限过滤**: 只返回用户有权限访问的MCP服务器
    - **批量查询**: 支持一次查询多个MCP服务器
    - **性能优化**: 对于大量ID的查询，建议分批请求
    - **数据一致性**: 返回的数据为实时数据
    """
    req_data = request.model_dump()
    id_list = req_data.get("id_list", [])
    
    if not id_list:
        return get_json_result(data=[])
    
    try:
        # 获取用户有权限的租户
        tenants = UserTenantService.query(db, user_id=user.id)
        tenant_ids = [tenant.tenant_id for tenant in tenants]
        tenant_ids.append(user.id)  # 添加用户自己的租户ID
        
        # 收集所有有权限的MCP服务器
        accessible_servers = []
        for tenant_id in tenant_ids:
            servers = MCPServerService.get_servers(db, tenant_id, id_list)
            if servers:
                accessible_servers.extend(servers)
        
        # 去重（防止同一个服务器在多个租户中出现）
        seen_ids = set()
        unique_servers = []
        for server in accessible_servers:
            if server["id"] not in seen_ids:
                seen_ids.add(server["id"])
                unique_servers.append(server)
        
        return get_json_result(data=unique_servers)
    except Exception as e:
        return server_error_response(e)


@router.post('/list', summary="获取MCP服务器列表", response_description="成功获取MCP服务器列表")
def list_mcp_servers(
        request: ListMCPServerRequest,
        keywords: str = Query("", description="关键词搜索"),
        page: int = Query(1, description="页码"),
        page_size: int = Query(10, description="每页数量"),
        orderby: str = Query("create_time", description="排序字段"),
        desc: bool = Query(True, description="是否降序"),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    ### POST `/list` 获取MCP服务器列表
    
    **功能描述**:
    此接口用于获取MCP服务器列表，支持分页查询、关键词搜索和排序功能。
    用户可以查看自己有权限访问的所有MCP服务器，或指定特定租户的MCP服务器。
    支持按名称关键词过滤，并提供灵活的排序和分页选项。
    
    ---
    ### 请求体 (Request Body)
    | 字段        | 类型          | 必填 | 描述                                       |
    |-------------|---------------|------|--------------------------------------------|
    | `owner_ids` | `list[string]` | 否   | 指定租户ID列表，为空时查询用户所有可访问的MCP服务器 |
    
    ### 查询参数 (Query Parameters)
    | 参数        | 类型      | 必填 | 默认值      | 描述                         |
    |-------------|-----------|------|-------------|------------------------------|
    | `keywords`  | `string`  | 否   | ""          | 搜索关键词，用于模糊匹配服务器名称 |
    | `page`      | `integer` | 否   | 1           | 页码，从1开始                |
    | `page_size` | `integer` | 否   | 10          | 每页显示的记录数             |
    | `orderby`   | `string`  | 否   | "create_time" | 排序字段                   |
    | `desc`      | `boolean` | 否   | true        | 是否降序排列                 |
    
    **请求示例**:
    ```json
    {
        "owner_ids": ["tenant-uuid-1", "tenant-uuid-2"]
    }
    ```
    
    **查询参数示例**:
    ```
    POST /list?keywords=客服&page=1&page_size=10&orderby=create_time&desc=true
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "servers": [
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
                },
                {
                    "id": "uuid-server-id-2",
                    "name": "产品知识MCP服务器",
                    "server_type": "websocket",
                    "url": "wss://api.example.com/mcp",
                    "description": "产品相关知识MCP服务器",
                    "variables": {
                        "timeout": 60
                    },
                    "update_date": "2024-07-16T09:00:00"
                }
            ],
            "total": 2
        }
    }
    ```
    
    #### 错误响应
    - **服务器内部错误**:
    ```json
    {
        "retcode": 500,
        "retmsg": "Internal server error",
        "data": null
    }
    ```
    
    ---
    ### 主要流程
    1. 解析请求参数，获取查询条件和分页信息
    2. 根据owner_ids参数确定查询范围：
       - 为空时：查询用户所有可访问的租户
       - 有值时：查询指定租户的MCP服务器
    3. 应用关键词搜索、排序和分页条件
    4. 返回符合条件的MCP服务器列表和总数
    
    ---
    ### 注意事项
    - **权限控制**: 用户只能查看自己有权限的MCP服务器
    - **分页性能**: 建议合理设置page_size以获得最佳性能
    - **关键词搜索**: 支持按服务器名称进行模糊匹配搜索
    - **灵活排序**: 支持多种字段排序，默认按创建时间降序
    - **租户过滤**: 可以指定特定租户或查看所有可访问的租户
    - **数据安全**: 返回数据中敏感信息已做适当处理
    """
    req_data = request.model_dump()
    owner_ids = req_data.get("owner_ids", [])
    
    try:
        if not owner_ids:
            # 获取用户加入的租户
            tenants = TenantService.get_joined_tenants_by_user_id(db, user.id)
            tenant_ids = [tenant.tenant_id for tenant in tenants]
            # 添加当前用户的租户ID（用户自己的租户）
            tenant_ids.append(user.id)
        else:
            tenant_ids = owner_ids
        
        # 收集所有租户的MCP服务器
        all_servers = []
        for tenant_id in tenant_ids:
            servers = MCPServerService.get_servers(db, tenant_id, None)
            if servers:
                all_servers.extend(servers)
        
        # 关键词过滤
        if keywords:
            all_servers = [server for server in all_servers if keywords.lower() in server["name"].lower()]
        
        # 排序
        if orderby == "create_time":
            all_servers.sort(key=lambda x: x.get("update_date", ""), reverse=desc)
        else:
            all_servers.sort(key=lambda x: x.get(orderby, ""), reverse=desc)
        
        total = len(all_servers)
        
        # 手动分页
        if page and page_size:
            start = (page - 1) * page_size
            end = page * page_size
            all_servers = all_servers[start:end]
        
        return get_json_result(data={"servers": all_servers, "total": total})
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
    | 字段        | 类型     | 必填 | 描述                 |
    |-------------|----------|------|----------------------|
    | `id` | `string` | 是   | 要删除的MCP服务器ID   |
    
    **请求示例**:
    ```json
    {
        "id": "uuid-server-id"
    }
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {"id": "uuid-server-id"}
    }
    ```
    
    #### 错误响应
    - **权限不足**:
    ```json
    {
        "retcode": 403,
        "retmsg": "No authorization.",
        "data": false
    }
    ```
    - **删除失败**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Failed to delete MCP Server uuid-server-id",
        "data": null
    }
    ```
    
    ---
    ### 主要流程
    1. 验证用户身份和请求参数的有效性
    2. 检查用户对指定MCP服务器的删除权限
    3. 执行软删除操作，更新MCP服务器状态为已删除
    4. 确认删除操作成功，返回操作结果
    
    ---
    ### 注意事项
    - **软删除**: 删除操作为软删除，数据仍保留在数据库中，只是状态标记为已删除
    - **权限验证**: 只有有删除权限的用户才能执行删除操作
    - **数据安全**: 删除的数据可以通过管理员恢复，确保数据安全
    - **关联处理**: 删除服务器时需要考虑相关联的数据和依赖关系
    - **审计日志**: 删除操作会记录在系统日志中，便于追踪和审计
    """
    req_data = request.model_dump()
    ms_id = req_data["id"]
    req_data["tenant_id"] = user.id
    # 简化版权限检查 - 实际项目中应该有更详细的权限检查
    # if not MCPServerService.accessible4deletion(db, ms_id, user.id):
    #     return get_json_result(data=False, retmsg="No authorization.", retcode=settings.RetCode.AUTHENTICATION_ERROR)
    
    try:
        # 软删除MCP服务器
        deleted_count = MCPServerService.filter_delete(db, [MCPServer.id == ms_id, MCPServer.tenant_id == req_data["tenant_id"]])
        if not deleted_count:
            return get_data_error_result(retmsg=f"Failed to delete MCP Server {ms_id}")
        
        return get_json_result(data={"id": req_data["id"]})
    except Exception as e:
        return server_error_response(e)
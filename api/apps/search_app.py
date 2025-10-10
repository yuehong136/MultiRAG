# coding=utf-8
"""
@project: multirag
@Author：龙
@file： search_app.py
@date：2025/7/15 16:20
@desc: 搜索应用接口
"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api import settings
from api.constants import DATASET_NAME_LIMIT
from api.db import StatusEnum
from api.db.db_models import get_db
from api.db.services import duplicate_name
from api.db.services.search_service import SearchService
from api.db.services.user_service import TenantService, UserTenantService
from api.utils import get_uuid
from api.utils.api_utils import get_data_error_result, get_json_result, server_error_response
from api.apps import manager

router = APIRouter()


class CreateSearchRequest(BaseModel):
    name: str
    description: str | None = None
    search_config: dict | None = None


class UpdateSearchRequest(BaseModel):
    search_id: str
    name: str
    description: str | None = None
    search_config: dict | None = None
    tenant_id: str


class SearchDetailRequest(BaseModel):
    search_id: str


class ListSearchRequest(BaseModel):
    owner_ids: list[str] | None = []


class RemoveSearchRequest(BaseModel):
    search_id: str


@router.post('/create', summary="创建搜索应用", response_description="成功创建搜索应用")
def create(request: CreateSearchRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/create` 创建搜索应用

    **功能描述**:
    此接口用于创建新的搜索应用。用户可以通过提供应用名称、描述和搜索配置来创建个性化的搜索应用。
    系统会自动检查名称重复性、验证用户权限，并为搜索应用分配唯一标识符。

    ---
    ### 请求体 (Request Body)
    | 字段            | 类型     | 必填 | 描述                                                   |
    |-----------------|----------|------|--------------------------------------------------------|
    | `name`          | `string` | 是   | 搜索应用名称，不能为空，长度不能超过系统限制            |
    | `description`   | `string` | 否   | 搜索应用描述信息，用于说明应用的用途和功能              |
    | `search_config` | `object` | 否   | 搜索配置对象，包含搜索相关的参数设置                   |

    **请求示例**:
    ```json
    {
        "name": "智能客服搜索",
        "description": "用于客服场景的智能搜索应用",
        "search_config": {
            "max_results": 10,
            "similarity_threshold": 0.8
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
            "search_id": "uuid-generated-id"
        }
    }
    ```

    #### 错误响应
    - **数据验证错误**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Search name must be string.",
        "data": null
    }
    ```
    - **名称重复错误**:
    ```json
    {
        "retcode": 400,
        "retmsg": "已存在该搜索应用名: 智能客服搜索，请调整！",
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
    2. 验证用户租户身份，确保用户有权限创建搜索应用
    3. 检查搜索应用名称是否重复，避免重名冲突
    4. 生成唯一的搜索应用ID，保存搜索应用信息到数据库
    5. 返回创建成功的搜索应用ID

    ---
    ### 注意事项
    - **名称限制**: 搜索应用名称不能为空，且UTF-8编码长度不能超过DATASET_NAME_LIMIT
    - **权限验证**: 需要验证用户的租户身份，未授权用户无法创建搜索应用
    - **名称唯一性**: 同一租户下的搜索应用名称必须唯一
    - **自动去重**: 系统会自动处理重复名称，添加后缀确保唯一性
    """
    req_data = request.model_dump()
    search_name = req_data["name"]
    description = req_data.get("description", "")

    if not isinstance(search_name, str):
        return get_data_error_result(retmsg="Search name must be string.")
    if search_name.strip() == "":
        return get_data_error_result(retmsg="Search name can't be empty.")
    if len(search_name.encode("utf-8")) > 255:
        return get_data_error_result(retmsg=f"Search name length is {len(search_name)} which is large than 255.")

    # 验证租户
    tenant = TenantService.get_by_id(db, user.id)
    if not tenant:
        return get_data_error_result(retmsg="Authorization identity.")

    search_name = search_name.strip()
    search_name = duplicate_name(SearchService.query, db=db, name=search_name, tenant_id=user.id, status=StatusEnum.VALID.value)

    # # 检查重复名称
    # existing_search = SearchService.query(
    #     db=db,
    #     name=search_name,
    #     tenant_id=user.id,
    #     status=StatusEnum.VALID.value
    # )
    # if existing_search:
    #     return get_data_error_result(retmsg=f"已存在该搜索应用名: {existing_search[0].name}，请调整！")

    try:
        req_data["id"] = get_uuid()
        req_data["name"] = search_name
        req_data["description"] = description
        req_data["tenant_id"] = user.id
        req_data["created_by"] = user.id

        search_obj = SearchService.save(db, **req_data)
        if not search_obj:
            return get_data_error_result()
        return get_json_result(data={"search_id": req_data["id"]})
    except Exception as e:
        return server_error_response(e)


@router.post('/update', summary="更新搜索应用", response_description="成功更新搜索应用")
def update(request: UpdateSearchRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/update` 更新搜索应用

    **功能描述**:
    此接口用于更新现有的搜索应用信息。支持更新应用名称、描述和搜索配置。
    系统会验证用户权限、检查名称重复性，并合并搜索配置参数，保留现有配置的同时应用新的设置。

    ---
    ### 请求体 (Request Body)
    | 字段            | 类型     | 必填 | 描述                                                   |
    |-----------------|----------|------|--------------------------------------------------------|
    | `search_id`     | `string` | 是   | 要更新的搜索应用ID                                     |
    | `name`          | `string` | 是   | 新的搜索应用名称                                       |
    | `description`   | `string` | 否   | 新的搜索应用描述                                       |
    | `search_config` | `object` | 否   | 新的搜索配置，会与现有配置合并                         |
    | `tenant_id`     | `string` | 是   | 租户ID，用于权限验证                                   |

    **请求示例**:
    ```json
    {
        "search_id": "uuid-search-id",
        "name": "增强版智能客服搜索",
        "description": "升级版的客服搜索应用，支持更多功能",
        "search_config": {
            "max_results": 20,
            "enable_semantic_search": true
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
            "id": "uuid-search-id",
            "name": "增强版智能客服搜索",
            "description": "升级版的客服搜索应用，支持更多功能",
            "search_config": {
                "max_results": 20,
                "similarity_threshold": 0.8,
                "enable_semantic_search": true
            },
            "tenant_id": "tenant-uuid",
            "created_by": "user-uuid",
            "create_time": "2024-07-15T16:20:00",
            "update_time": "2024-07-15T17:30:00"
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
    - **搜索应用不存在**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Cannot find search uuid-search-id",
        "data": false
    }
    ```
    - **名称重复**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Duplicated search name.",
        "data": null
    }
    ```

    ---
    ### 主要流程
    1. 验证请求参数的有效性和用户权限
    2. 查找要更新的搜索应用，确认其存在性
    3. 检查新名称是否与其他应用重复（忽略大小写）
    4. 合并搜索配置：保留现有配置，应用新的配置项
    5. 更新搜索应用信息并返回完整的更新后数据

    ---
    ### 注意事项
    - **权限验证**: 只有有权限的用户才能更新搜索应用
    - **配置合并**: 新的search_config会与现有配置合并，不会完全覆盖
    - **名称唯一性**: 更新后的名称在同一租户下必须唯一（不区分大小写）
    - **原子操作**: 更新操作具有原子性，要么全部成功要么全部失败
    """
    req_data = request.model_dump()

    if not isinstance(req_data["name"], str):
        return get_data_error_result(retmsg="Search name must be string.")
    if req_data["name"].strip() == "":
        return get_data_error_result(retmsg="Search name can't be empty.")
    if len(req_data["name"].encode("utf-8")) > DATASET_NAME_LIMIT:
        return get_data_error_result(retmsg=f"Search name length is {len(req_data['name'])} which is large than {DATASET_NAME_LIMIT}")

    req_data["name"] = req_data["name"].strip()
    tenant_id = req_data["tenant_id"]
    search_id = req_data["search_id"]

    # 验证租户
    tenant = TenantService.get_by_id(db, tenant_id)
    if not tenant:
        return get_data_error_result(retmsg="Authorization identity.")

    # 检查权限
    if not SearchService.accessible4deletion(db, search_id, user.id):
        return get_json_result(data=False, retmsg="No authorization.", retcode=settings.RetCode.AUTHENTICATION_ERROR)

    try:
        # 获取现有搜索应用
        search_apps = SearchService.query(db, tenant_id=tenant_id, id=search_id)
        if not search_apps:
            return get_json_result(data=False, retmsg=f"Cannot find search {search_id}", retcode=settings.RetCode.DATA_ERROR)

        search_app = search_apps[0]

        # 检查名称重复
        if req_data["name"].lower() != search_app.name.lower():
            existing_searches = SearchService.query(db, name=req_data["name"], tenant_id=tenant_id, status=StatusEnum.VALID.value)
            if existing_searches:
                return get_data_error_result(retmsg="Duplicated search name.")

        # 处理搜索配置
        if "search_config" in req_data and req_data["search_config"] is not None:
            current_config = search_app.search_config or {}
            new_config = req_data["search_config"]

            if not isinstance(new_config, dict):
                return get_data_error_result(retmsg="search_config must be a JSON object")

            updated_config = {**current_config, **new_config}
            req_data["search_config"] = updated_config

        # 移除不需要的字段
        req_data.pop("search_id", None)
        req_data.pop("tenant_id", None)

        # 更新搜索应用
        updated_count = SearchService.update_by_id(db, search_id, req_data)
        if not updated_count:
            return get_data_error_result(retmsg="Failed to update search")

        # 获取更新后的搜索应用
        updated_search = SearchService.get_by_id(db, search_id)
        if not updated_search:
            return get_data_error_result(retmsg="Failed to fetch updated search")

        return get_json_result(data=updated_search.to_dict())

    except Exception as e:
        return server_error_response(e)


@router.get('/detail', summary="获取搜索应用详情", response_description="成功获取搜索应用详情")
def detail(search_id: str = Query(..., description="搜索应用ID"), db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### GET `/detail` 获取搜索应用详情

    **功能描述**:
    此接口用于获取指定搜索应用的详细信息。系统会验证用户对该搜索应用的访问权限，
    确保用户只能查看有权限的搜索应用详情，包括应用的基本信息、配置参数和元数据。

    ---
    ### 请求参数 (Query Parameters)
    | 参数        | 类型     | 必填 | 描述           |
    |-------------|----------|------|----------------|
    | `search_id` | `string` | 是   | 搜索应用的唯一标识符 |

    **请求示例**:
    ```
    GET /detail?search_id=uuid-search-id
    ```

    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "id": "uuid-search-id",
            "name": "智能客服搜索",
            "description": "用于客服场景的智能搜索应用",
            "search_config": {
                "max_results": 10,
                "similarity_threshold": 0.8
            },
            "tenant_id": "tenant-uuid",
            "created_by": "user-uuid",
            "create_time": "2024-07-15T16:20:00",
            "update_time": "2024-07-15T16:20:00",
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
    - **搜索应用不存在**:
    ```json
    {
        "retcode": 400,
        "retmsg": "Can't find this Search App!",
        "data": null
    }
    ```

    ---
    ### 主要流程
    1. 验证用户身份和登录状态
    2. 检查用户对指定搜索应用的访问权限
    3. 遍历用户所属的所有租户，查找搜索应用
    4. 获取搜索应用的完整详细信息
    5. 返回搜索应用的所有相关数据

    ---
    ### 注意事项
    - **权限验证**: 用户只能查看自己有权限访问的搜索应用
    - **多租户支持**: 系统会检查用户在所有租户中的权限
    - **数据完整性**: 返回搜索应用的完整信息，包括配置和元数据
    - **安全性**: 未授权访问会被拒绝，保护数据安全
    """
    try:
        # 检查用户权限
        tenants = UserTenantService.query(db, user_id=user.id)
        has_permission = False

        for tenant in tenants:
            search_apps = SearchService.query(db, tenant_id=tenant.tenant_id, id=search_id)
            if search_apps:
                has_permission = True
                break

        if not has_permission:
            return get_json_result(data=False, retmsg="Has no permission for this operation.", retcode=settings.RetCode.OPERATING_ERROR)

        # 获取搜索详情
        search = SearchService.get_detail(db, search_id)
        if not search:
            return get_data_error_result(retmsg="Can't find this Search App!")

        return get_json_result(data=search)
    except Exception as e:
        return server_error_response(e)


@router.post('/list', summary="获取搜索应用列表", response_description="成功获取搜索应用列表")
def list_search_app(
        request: ListSearchRequest,
        keywords: str = Query("", description="关键词搜索"),
        page: int = Query(1, description="页码"),
        page_size: int = Query(10, description="每页数量"),
        orderby: str = Query("create_time", description="排序字段"),
        desc: bool = Query(True, description="是否降序"),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    ### POST `/list` 获取搜索应用列表

    **功能描述**:
    此接口用于获取搜索应用列表，支持分页查询、关键词搜索和排序功能。
    用户可以查看自己有权限访问的所有搜索应用，或指定特定租户的搜索应用。
    支持按名称关键词过滤，并提供灵活的排序和分页选项。

    ---
    ### 请求体 (Request Body)
    | 字段        | 类型          | 必填 | 描述                                   |
    |-------------|---------------|------|----------------------------------------|
    | `owner_ids` | `list[string]` | 否   | 指定租户ID列表，为空时查询用户所有可访问的搜索应用 |

    ### 查询参数 (Query Parameters)
    | 参数        | 类型      | 必填 | 默认值      | 描述                     |
    |-------------|-----------|------|-------------|--------------------------|
    | `keywords`  | `string`  | 否   | ""          | 搜索关键词，用于模糊匹配应用名称 |
    | `page`      | `integer` | 否   | 1           | 页码，从1开始            |
    | `page_size` | `integer` | 否   | 10          | 每页显示的记录数         |
    | `orderby`   | `string`  | 否   | "create_time" | 排序字段               |
    | `desc`      | `boolean` | 否   | true        | 是否降序排列             |

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
            "search_apps": [
                {
                    "id": "uuid-search-id-1",
                    "name": "智能客服搜索",
                    "description": "用于客服场景的智能搜索应用",
                    "tenant_id": "tenant-uuid-1",
                    "created_by": "user-uuid",
                    "create_time": "2024-07-15T16:20:00",
                    "update_time": "2024-07-15T16:20:00"
                },
                {
                    "id": "uuid-search-id-2",
                    "name": "产品知识搜索",
                    "description": "产品相关知识搜索应用",
                    "tenant_id": "tenant-uuid-1",
                    "created_by": "user-uuid",
                    "create_time": "2024-07-15T15:10:00",
                    "update_time": "2024-07-15T15:10:00"
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
       - 有值时：查询指定租户的搜索应用
    3. 应用关键词搜索、排序和分页条件
    4. 返回符合条件的搜索应用列表和总数

    ---
    ### 注意事项
    - **权限控制**: 用户只能查看自己有权限的搜索应用
    - **分页性能**: 建议合理设置page_size以获得最佳性能
    - **关键词搜索**: 支持按应用名称进行模糊匹配搜索
    - **灵活排序**: 支持多种字段排序，默认按创建时间降序
    - **租户过滤**: 可以指定特定租户或查看所有可访问的租户
    """
    req_data = request.model_dump()
    owner_ids = req_data.get("owner_ids", [])

    try:
        if not owner_ids:
            # 获取用户加入的租户
            tenants = TenantService.get_joined_tenants_by_user_id(db, user.id)
            tenants = [m["tenant_id"] for m in tenants]
            search_apps, total = SearchService.get_by_tenant_ids(
                db, tenants, user.id, page, page_size, orderby, desc, keywords
            )
        else:
            tenants = owner_ids
            search_apps, total = SearchService.get_by_tenant_ids(
                db, tenants, user.id, 0, 0, orderby, desc, keywords
            )
            # 过滤只显示指定租户的搜索应用
            search_apps = [search_app for search_app in search_apps if search_app["tenant_id"] in tenants]
            total = len(search_apps)

            # 手动分页
            if page and page_size:
                start = (page - 1) * page_size
                end = page * page_size
                search_apps = search_apps[start:end]

        return get_json_result(data={"search_apps": search_apps, "total": total})
    except Exception as e:
        return server_error_response(e)


@router.post('/rm', summary="删除搜索应用", response_description="成功删除搜索应用")
def rm(request: RemoveSearchRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/rm` 删除搜索应用

    **功能描述**:
    此接口用于删除指定的搜索应用。执行的是软删除操作，即将搜索应用状态标记为已删除，
    而不是物理删除数据。系统会验证用户对该搜索应用的删除权限，确保只有授权用户才能执行删除操作。

    ---
    ### 请求体 (Request Body)
    | 字段        | 类型     | 必填 | 描述               |
    |-------------|----------|------|--------------------|
    | `search_id` | `string` | 是   | 要删除的搜索应用ID |

    **请求示例**:
    ```json
    {
        "search_id": "uuid-search-id"
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
        "retmsg": "Failed to delete search App uuid-search-id",
        "data": null
    }
    ```

    ---
    ### 主要流程
    1. 验证用户身份和请求参数的有效性
    2. 检查用户对指定搜索应用的删除权限
    3. 执行软删除操作，更新搜索应用状态为已删除
    4. 确认删除操作成功，返回操作结果

    ---
    ### 注意事项
    - **软删除**: 删除操作为软删除，数据仍保留在数据库中，只是状态标记为已删除
    - **权限验证**: 只有有删除权限的用户才能执行删除操作
    - **数据安全**: 删除的数据可以通过管理员恢复，确保数据安全
    - **关联处理**: 删除应用时需要考虑相关联的数据和依赖关系
    - **审计日志**: 删除操作会记录在系统日志中，便于追踪和审计
    """
    req_data = request.model_dump()
    search_id = req_data["search_id"]

    # 检查权限
    if not SearchService.accessible4deletion(db, search_id, user.id):
        return get_json_result(data=False, retmsg="No authorization.", retcode=settings.RetCode.AUTHENTICATION_ERROR)

    try:
        # 软删除搜索应用
        deleted_count = SearchService.delete_by_id(db, search_id)
        if not deleted_count:
            return get_data_error_result(retmsg=f"Failed to delete search App {search_id}")

        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)
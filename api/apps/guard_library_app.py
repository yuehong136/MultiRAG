"""
@project: multirag
@Author：龙
@file： guard_library_app.py
@date：2025/01/11 18:10
@desc: AI安全护栏词库管理接口
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.db.services.guard_library_item_service import GuardLibraryItemService
from api.db.services.guard_library_service import GuardLibraryService
from api.utils.api_utils import get_data_error_result, get_json_result, server_error_response

router = APIRouter()


class CreateLibraryRequest(BaseModel):
    """创建词库请求模型"""

    library_type: str = Field("custom", description="词库类型: blacklist/whitelist/reply/pattern/custom")
    name: str = Field(..., description="词库名称")
    description: str | None = Field(None, description="词库描述")
    category: str | None = Field(None, description="词库分类")
    tags: list[str] = Field(default_factory=list, description="标签列表")
    config: dict = Field(default_factory=dict, description="词库配置")


class UpdateLibraryRequest(BaseModel):
    """更新词库请求模型"""

    library_id: str = Field(..., description="词库ID")
    name: str | None = Field(None, description="词库名称")
    description: str | None = Field(None, description="词库描述")
    category: str | None = Field(None, description="词库分类")
    tags: list[str] | None = Field(None, description="标签列表")
    config: dict | None = Field(None, description="词库配置")


class CreateLibraryItemRequest(BaseModel):
    """创建词库项请求模型"""

    library_id: str = Field(..., description="词库ID")
    content: str = Field(..., description="内容")
    content_type: str = Field("text", description="内容类型")
    item_metadata: dict = Field(default_factory=dict, description="元数据")
    sort_order: int = Field(0, description="排序")


class BatchCreateItemsRequest(BaseModel):
    """批量创建词库项请求模型"""

    library_id: str = Field(..., description="词库ID")
    contents: list[str] = Field(..., description="内容列表")
    content_type: str = Field("text", description="内容类型")
    item_metadata: dict = Field(default_factory=dict, description="元数据")


class UpdateLibraryItemByHashRequest(BaseModel):
    """根据哈希更新词库项请求模型"""

    content: str | None = Field(None, description="内容")
    content_type: str | None = Field(None, description="内容类型")
    item_metadata: dict | None = Field(None, description="元数据")
    sort_order: int | None = Field(None, description="排序")


class BatchGetItemsRequest(BaseModel):
    """批量获取词库项请求模型"""

    item_ids: list[str] = Field(..., description="词库项ID列表")


class DeleteItemsRequest(BaseModel):
    """删除词库项请求模型"""

    item_ids: list[str] = Field(..., description="词库项ID列表")


class UpdateItemsStatusRequest(BaseModel):
    """更新词库项状态请求模型"""

    item_ids: list[str] = Field(..., description="词库项ID列表")
    status: str = Field(..., description="状态值: 1-启用, 0-禁用")


class UpdateItemByIdRequest(BaseModel):
    """根据ID更新词库项请求模型"""

    content: str | None = Field(None, description="内容")
    content_type: str | None = Field(None, description="内容类型")
    item_metadata: dict | None = Field(None, description="元数据")
    sort_order: int | None = Field(None, description="排序")


# 词库管理接口
@router.post("/create", summary="创建词库")
def create_library(request: CreateLibraryRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### POST `/create` 创建词库

    **功能描述**:
    此接口用于创建新的AI安全护栏词库，支持创建黑名单、白名单、代答库、模式库等不同类型的词库。
    用户可以指定词库的基本信息、分类、标签和配置参数。

    ---
    ### 请求体 (Request Body)
    | 字段           | 类型         | 必填 | 描述                                                    |
    |----------------|-------------|------|--------------------------------------------------------|
    | `library_type` | `string`    | 是   | 词库类型: blacklist/whitelist/reply/pattern/custom      |
    | `name`         | `string`    | 是   | 词库名称                                                |
    | `description`  | `string`    | 否   | 词库描述                                                |
    | `category`     | `string`    | 否   | 词库分类                                                |
    | `tags`         | `list[string]` | 否   | 标签列表                                             |
    | `config`       | `object`    | 否   | 词库配置参数                                            |

    **请求示例**:
    ```json
    {
        "library_type": "blacklist",
        "name": "色情内容黑名单",
        "description": "包含色情相关敏感词汇",
        "category": "内容合规",
        "tags": ["色情", "敏感"],
        "config": {
            "match_mode": "exact",
            "case_sensitive": false
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
            "library_id": "uuid-library-id-here"
        }
    }
    ```

    #### 失败响应 (400)
    ```json
    {
        "retcode": -1,
        "retmsg": "创建词库失败",
        "data": null
    }
    ```
    """
    try:
        library_id = GuardLibraryService.create_library(
            db=db,
            library_type=request.library_type,
            name=request.name,
            description=request.description,
            tenant_id=user.id,
            created_by=user.id,
            category=request.category,
            tags=request.tags,
            config=request.config,
        )

        if library_id:
            return get_json_result(data={"library_id": library_id})
        else:
            return get_data_error_result(retmsg="创建词库失败")

    except Exception as e:
        return server_error_response(e)


@router.get("/list", summary="获取词库列表")
def list_libraries(library_type: str | None = None, category: str | None = None, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### GET `/list` 获取词库列表

    **功能描述**:
    此接口用于获取用户有权限访问的AI安全护栏词库列表。
    支持按词库类型和分类进行过滤，返回词库的完整信息包括配置、统计数据等。

    ---
    ### 查询参数 (Query Parameters)
    | 参数           | 类型     | 必填 | 默认值 | 描述                                      |
    |----------------|----------|------|--------|-------------------------------------------|
    | `library_type` | `string` | 否   | null   | 词库类型过滤: blacklist/whitelist/reply/pattern/custom |
    | `category`     | `string` | 否   | null   | 词库分类过滤                              |

    **请求示例**:
    ```bash
    GET /list?library_type=blacklist&category=内容合规
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
                "id": "uuid-library-id-1",
                "library_type": "blacklist",
                "name": "色情内容黑名单",
                "description": "包含色情相关敏感词汇",
                "category": "内容合规",
                "tags": ["色情", "敏感"],
                "config": {
                    "match_mode": "exact",
                    "case_sensitive": false
                },
                "item_count": 150,
                "hit_count": 1200,
                "version": 1,
                "create_time": "2024-07-16T10:00:00",
                "update_time": "2024-07-16T12:00:00"
            }
        ]
    }
    ```
    """
    try:
        libraries = GuardLibraryService.get_libraries_by_tenant(db, user.id, library_type, category)

        return get_json_result(data=[lib.to_dict() for lib in libraries])

    except Exception as e:
        return server_error_response(e)


@router.put("/update", summary="更新词库")
def update_library(request: UpdateLibraryRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### PUT `/update` 更新词库

    **功能描述**:
    此接口用于更新已存在的AI安全护栏词库信息。
    支持部分更新，只更新传入的字段，未传入的字段保持不变。

    ---
    ### 请求体 (Request Body)
    | 字段           | 类型         | 必填 | 描述                                                |
    |----------------|-------------|------|-----------------------------------------------------|
    | `library_id`   | `string`    | 是   | 词库ID                                              |
    | `name`         | `string`    | 否   | 词库名称                                            |
    | `description`  | `string`    | 否   | 词库描述                                            |
    | `category`     | `string`    | 否   | 词库分类                                            |
    | `tags`         | `list[string]` | 否   | 标签列表                                         |
    | `config`       | `object`    | 否   | 词库配置参数                                        |

    **请求示例**:
    ```json
    {
        "library_id": "uuid-library-id-here",
        "name": "更新后的词库名称",
        "description": "更新后的描述信息",
        "tags": ["更新", "标签"]
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

    #### 失败响应 (400)
    ```json
    {
        "retcode": -1,
        "retmsg": "更新词库失败",
        "data": null
    }
    ```
    """
    try:
        update_data = {k: v for k, v in request.model_dump().items() if v is not None and k != "library_id"}

        success = GuardLibraryService.update_library(db, request.library_id, update_data)

        if success:
            return get_json_result(data=True)
        else:
            return get_data_error_result(retmsg="更新词库失败")

    except Exception as e:
        return server_error_response(e)


@router.delete("/{library_id}", summary="删除词库")
def delete_library(library_id: str, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### DELETE `/{library_id}` 删除词库

    **功能描述**:
    此接口用于删除指定的AI安全护栏词库（软删除）。
    删除词库不会物理删除数据，而是将状态标记为删除，保留历史记录。
    删除词库时会同时删除该词库下的所有词库项。

    ---
    ### 路径参数 (Path Parameters)
    | 参数         | 类型     | 必填 | 描述       |
    |--------------|----------|------|------------|
    | `library_id` | `string` | 是   | 词库ID     |

    **请求示例**:
    ```bash
    DELETE /uuid-library-id-here
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

    #### 失败响应 (400)
    ```json
    {
        "retcode": -1,
        "retmsg": "删除词库失败",
        "data": null
    }
    ```
    """
    try:
        success = GuardLibraryService.delete_library(db, library_id)

        if success:
            return get_json_result(data=True)
        else:
            return get_data_error_result(retmsg="删除词库失败")

    except Exception as e:
        return server_error_response(e)


# 词库项管理接口
@router.post("/{library_id}/items/create", summary="创建词库项")
def create_library_item(library_id: str, request: CreateLibraryItemRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### POST `/{library_id}/items/create` 创建词库项

    **功能描述**:
    此接口用于在指定词库中创建新的词库项。
    支持创建文本、正则表达式、模板等不同类型的内容，适用于黑名单、白名单、代答库等场景。
    创建时会自动计算内容哈希值，防止重复添加相同内容。

    ---
    ### 路径参数 (Path Parameters)
    | 参数         | 类型     | 必填 | 描述       |
    |--------------|----------|------|------------|
    | `library_id` | `string` | 是   | 词库ID     |

    ### 请求体 (Request Body)
    | 字段            | 类型     | 必填 | 默认值 | 描述                                    |
    |-----------------|----------|------|--------|-----------------------------------------|
    | `library_id`    | `string` | 是   | -      | 词库ID                                  |
    | `content`       | `string` | 是   | -      | 词库项内容                              |
    | `content_type`  | `string` | 否   | "text" | 内容类型: text/regex/template           |
    | `item_metadata` | `object` | 否   | {}     | 元数据信息                              |
    | `sort_order`    | `integer`| 否   | 0      | 排序权重                                |

    **请求示例**:
    ```json
    {
        "library_id": "uuid-library-id-here",
        "content": "敏感词汇",
        "content_type": "text",
        "item_metadata": {
            "category": "政治",
            "level": 4
        },
        "sort_order": 10
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
            "item_id": "uuid-item-id-here"
        }
    }
    ```

    #### 失败响应 (400)
    ```json
    {
        "retcode": -1,
        "retmsg": "创建词库项失败",
        "data": null
    }
    ```
    """
    try:
        # 先验证词库的所有权
        library = GuardLibraryService.get_by_id(db, library_id)
        if not library:
            return get_data_error_result(retmsg="词库不存在")

        if library.tenant_id != user.id:
            return get_data_error_result(retmsg="无权访问此词库")

        item_id = GuardLibraryItemService.create_item(
            db=db, library_id=library_id, content=request.content, content_type=request.content_type, item_metadata=request.item_metadata, tenant_id=user.id, sort_order=request.sort_order
        )

        if item_id:
            return get_json_result(data={"item_id": item_id})
        else:
            return get_data_error_result(retmsg="创建词库项失败")

    except Exception as e:
        return server_error_response(e)


@router.get("/{library_id}/items", summary="获取词库项列表")
def get_library_items(library_id: str, page: int = 1, page_size: int = 50, keyword: str | None = None, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### GET `/{library_id}/items` 获取词库项列表

    **功能描述**:
    此接口用于获取指定词库下的词库项列表，支持分页查询和关键词搜索。
    返回词库项的完整信息包括内容、类型、元数据、命中统计等。
    支持按内容关键词进行模糊搜索。

    ---
    ### 路径参数 (Path Parameters)
    | 参数         | 类型     | 必填 | 描述       |
    |--------------|----------|------|------------|
    | `library_id` | `string` | 是   | 词库ID     |

    ### 查询参数 (Query Parameters)
    | 参数        | 类型      | 必填 | 默认值 | 描述                         |
    |-------------|-----------|------|--------|------------------------------|
    | `page`      | `integer` | 否   | 1      | 页码，从1开始                |
    | `page_size` | `integer` | 否   | 50     | 每页显示的记录数             |
    | `keyword`   | `string`  | 否   | null   | 搜索关键词，用于模糊匹配内容 |

    **请求示例**:
    ```bash
    GET /uuid-library-id-here/items?page=1&page_size=20&keyword=敏感
    ```

    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "items": [
                {
                    "id": "uuid-item-id-1",
                    "library_id": "uuid-library-id-here",
                    "content": "敏感词汇",
                    "content_hash": "5d41402abc4b2a76b9719d911017c592",
                    "content_type": "text",
                    "metadata": {
                        "category": "政治",
                        "level": 4
                    },
                    "hit_count": 15,
                    "sort_order": 10,
                    "create_time": "2024-07-16T10:00:00",
                    "update_time": "2024-07-16T10:00:00"
                }
            ],
            "total": 150,
            "page": 1,
            "page_size": 20,
            "total_pages": 8
        }
    }
    ```
    """
    try:
        # 先验证词库的所有权
        library = GuardLibraryService.get_by_id(db, library_id)
        if not library:
            return get_data_error_result(retmsg="词库不存在")

        if library.tenant_id != user.id:
            return get_data_error_result(retmsg="无权访问此词库")

        if keyword:
            result = GuardLibraryItemService.search_items(db, library_id, keyword, page, page_size)
        else:
            result = GuardLibraryItemService.get_items_by_library(db, library_id, page, page_size)

        # 转换为字典格式
        items_data = []
        for item in result["items"]:
            items_data.append(item.to_dict())

        result["items"] = items_data
        return get_json_result(data=result)

    except Exception as e:
        return server_error_response(e)


@router.post("/{library_id}/items/batch", summary="批量创建词库项")
def batch_create_items(library_id: str, request: BatchCreateItemsRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### POST `/{library_id}/items/batch` 批量创建词库项

    **功能描述**:
    此接口用于在指定词库中批量创建多个词库项。
    适用于导入大量敏感词、白名单词汇等批量操作场景。
    会自动去重，避免添加重复内容，并返回详细的创建统计结果。

    ---
    ### 路径参数 (Path Parameters)
    | 参数         | 类型     | 必填 | 描述       |
    |--------------|----------|------|------------|
    | `library_id` | `string` | 是   | 词库ID     |

    ### 请求体 (Request Body)
    | 字段            | 类型         | 必填 | 默认值 | 描述                                    |
    |-----------------|-------------|------|--------|-----------------------------------------|
    | `library_id`    | `string`    | 是   | -      | 词库ID                                  |
    | `contents`      | `list[string]` | 是 | -      | 内容列表                                |
    | `content_type`  | `string`    | 否   | "text" | 内容类型: text/regex/template           |
    | `item_metadata` | `object`    | 否   | {}     | 统一的元数据信息                        |

    **请求示例**:
    ```json
    {
        "library_id": "uuid-library-id-here",
        "contents": [
            "敏感词汇1",
            "敏感词汇2",
            "敏感词汇3"
        ],
        "content_type": "text",
        "item_metadata": {
            "category": "政治",
            "level": 4
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
            "success_count": 2,
            "failed_count": 1,
            "failed_contents": ["重复内容"]
        }
    }
    ```
    """
    try:
        # 先验证词库的所有权
        library = GuardLibraryService.get_by_id(db, library_id)
        if not library:
            return get_data_error_result(retmsg="词库不存在")

        if library.tenant_id != user.id:
            return get_data_error_result(retmsg="无权访问此词库")

        result = GuardLibraryItemService.batch_create_items(
            db=db, library_id=library_id, contents=request.contents, content_type=request.content_type, tenant_id=user.id, item_metadata=request.item_metadata
        )

        return get_json_result(data=result)

    except Exception as e:
        return server_error_response(e)


@router.post("/items/delete", summary="批量删除词库项")
def delete_library_items(request: DeleteItemsRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### POST `/items/delete` 批量删除词库项

    **功能描述**:
    此接口用于批量删除词库项（硬删除，物理删除）。
    支持单个或多个词库项删除，使用数组形式传入ID列表。
    删除后会自动更新相关词库的项目计数。

    ---
    ### 请求体 (Request Body)
    | 字段       | 类型         | 必填 | 描述           |
    |------------|-------------|------|----------------|
    | `item_ids` | `list[string]` | 是   | 词库项ID列表   |

    **请求示例**:
    ```json
    {
        "item_ids": [
            "uuid-item-id-1",
            "uuid-item-id-2"
        ]
    }
    ```

    **单个删除示例**:
    ```json
    {
        "item_ids": ["uuid-item-id-1"]
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
            "success_count": 2,
            "failed_count": 0,
            "total": 2
        }
    }
    ```

    #### 部分失败响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "success_count": 1,
            "failed_count": 1,
            "total": 2
        }
    }
    ```
    """
    try:
        if not request.item_ids:
            return get_data_error_result(retmsg="词库项ID列表不能为空")

        result = GuardLibraryItemService.delete_items(db, request.item_ids, user.id)

        return get_json_result(data=result)

    except Exception as e:
        return server_error_response(e)


@router.put("/items/status", summary="批量更新词库项状态")
def update_items_status(request: UpdateItemsStatusRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### PUT `/items/status` 批量更新词库项状态

    **功能描述**:
    此接口用于批量更新词库项的启用/禁用状态。
    支持单个或多个词库项状态更新，使用数组形式传入ID列表。
    状态值："1"表示启用，"0"表示禁用。

    ---
    ### 请求体 (Request Body)
    | 字段       | 类型         | 必填 | 描述                          |
    |------------|-------------|------|-------------------------------|
    | `item_ids` | `list[string]` | 是   | 词库项ID列表                  |
    | `status`   | `string`    | 是   | 状态值: "1"-启用, "0"-禁用     |

    **启用示例**:
    ```json
    {
        "item_ids": [
            "uuid-item-id-1",
            "uuid-item-id-2"
        ],
        "status": "1"
    }
    ```

    **禁用示例**:
    ```json
    {
        "item_ids": ["uuid-item-id-1"],
        "status": "0"
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
            "success_count": 2,
            "failed_count": 0,
            "total": 2
        }
    }
    ```

    #### 部分失败响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "success_count": 1,
            "failed_count": 1,
            "total": 2
        }
    }
    ```
    """
    try:
        if not request.item_ids:
            return get_data_error_result(retmsg="词库项ID列表不能为空")

        if request.status not in ["0", "1"]:
            return get_data_error_result(retmsg="状态值必须是 '0' 或 '1'")

        result = GuardLibraryItemService.update_items_status(db, request.item_ids, request.status, user.id)

        return get_json_result(data=result)

    except Exception as e:
        return server_error_response(e)


@router.put("/items/{item_id}", summary="根据ID更新词库项")
def update_library_item_by_id(item_id: str, request: UpdateItemByIdRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### PUT `/items/{item_id}` 根据ID更新词库项

    **功能描述**:
    此接口用于根据词库项ID更新指定的词库项。
    支持部分更新，只更新传入的字段。
    如果更新内容字段，会重新计算哈希值。

    ---
    ### 路径参数 (Path Parameters)
    | 参数      | 类型     | 必填 | 描述       |
    |-----------|----------|------|------------|
    | `item_id` | `string` | 是   | 词库项ID   |

    ### 请求体 (Request Body)
    | 字段            | 类型     | 必填 | 描述                                    |
    |-----------------|----------|------|-----------------------------------------|
    | `content`       | `string` | 否   | 更新后的内容                            |
    | `content_type`  | `string` | 否   | 更新后的内容类型                        |
    | `item_metadata` | `object` | 否   | 更新后的元数据                          |
    | `sort_order`    | `integer`| 否   | 更新后的排序权重                        |

    **请求示例**:
    ```json
    {
        "content": "更新后的敏感词汇",
        "item_metadata": {
            "category": "更新后的分类",
            "level": 5
        },
        "sort_order": 20
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

    #### 失败响应 (400)
    ```json
    {
        "retcode": -1,
        "retmsg": "更新词库项失败",
        "data": null
    }
    ```
    """
    try:
        # 构建更新数据，只包含非None的字段
        update_data = {}
        if request.content is not None:
            update_data["content"] = request.content
        if request.content_type is not None:
            update_data["content_type"] = request.content_type
        if request.item_metadata is not None:
            update_data["item_metadata"] = request.item_metadata
        if request.sort_order is not None:
            update_data["sort_order"] = request.sort_order

        if not update_data:
            return get_data_error_result(retmsg="没有提供更新数据")

        success = GuardLibraryItemService.update_item_by_id(db, item_id, update_data, user.id)

        if success:
            return get_json_result(data=True)
        else:
            return get_data_error_result(retmsg="更新词库项失败")

    except Exception as e:
        return server_error_response(e)


@router.put("/{library_id}/items/hash/{content_hash}", summary="根据哈希更新词库项")
def update_library_item_by_hash(library_id: str, content_hash: str, request: UpdateLibraryItemByHashRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### PUT `/{library_id}/items/hash/{content_hash}` 根据哈希更新词库项

    **功能描述**:
    此接口用于根据词库ID和内容哈希精确更新指定的词库项。
    支持部分更新，只更新传入的字段。适用于已知内容哈希时的精确修改操作。
    如果更新内容字段，会重新计算哈希值。

    ---
    ### 路径参数 (Path Parameters)
    | 参数           | 类型     | 必填 | 描述       |
    |----------------|----------|------|------------|
    | `library_id`   | `string` | 是   | 词库ID     |
    | `content_hash` | `string` | 是   | 内容哈希值 |

    ### 请求体 (Request Body)
    | 字段            | 类型     | 必填 | 描述                                    |
    |-----------------|----------|------|-----------------------------------------|
    | `content`       | `string` | 否   | 更新后的内容                            |
    | `content_type`  | `string` | 否   | 更新后的内容类型                        |
    | `item_metadata` | `object` | 否   | 更新后的元数据                          |
    | `sort_order`    | `integer`| 否   | 更新后的排序权重                        |

    **请求示例**:
    ```json
    {
        "content": "更新后的敏感词汇",
        "item_metadata": {
            "category": "更新后的分类",
            "level": 5
        },
        "sort_order": 20
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

    #### 失败响应 (400)
    ```json
    {
        "retcode": -1,
        "retmsg": "更新词库项失败",
        "data": null
    }
    ```
    """
    try:
        # 构建更新数据，只包含非None的字段
        update_data = {}
        if request.content is not None:
            update_data["content"] = request.content
        if request.content_type is not None:
            update_data["content_type"] = request.content_type
        if request.item_metadata is not None:
            update_data["item_metadata"] = request.item_metadata
        if request.sort_order is not None:
            update_data["sort_order"] = request.sort_order

        if not update_data:
            return get_data_error_result(retmsg="没有提供更新数据")

        success = GuardLibraryItemService.update_item_by_hash(db, library_id, content_hash, update_data)

        if success:
            return get_json_result(data=True)
        else:
            return get_data_error_result(retmsg="更新词库项失败")

    except Exception as e:
        return server_error_response(e)


@router.delete("/{library_id}/items/hash/{content_hash}", summary="根据哈希删除词库项")
def delete_library_item_by_hash(library_id: str, content_hash: str, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### DELETE `/{library_id}/items/hash/{content_hash}` 根据哈希删除词库项

    **功能描述**:
    此接口用于根据词库ID和内容哈希精确删除指定的词库项（硬删除，物理删除）。
    适用于已知内容哈希时的精确删除操作，常用于去重或批量清理场景。
    删除后会自动更新词库的项目计数。

    ---
    ### 路径参数 (Path Parameters)
    | 参数           | 类型     | 必填 | 描述       |
    |----------------|----------|------|------------|
    | `library_id`   | `string` | 是   | 词库ID     |
    | `content_hash` | `string` | 是   | 内容哈希值 |

    **请求示例**:
    ```bash
    DELETE /uuid-library-id-here/items/hash/5d41402abc4b2a76b9719d911017c592
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

    #### 失败响应 (400)
    ```json
    {
        "retcode": -1,
        "retmsg": "删除词库项失败",
        "data": null
    }
    ```
    """
    try:
        success = GuardLibraryItemService.delete_item_by_hash(db, library_id, content_hash)

        if success:
            return get_json_result(data=True)
        else:
            return get_data_error_result(retmsg="删除词库项失败")

    except Exception as e:
        return server_error_response(e)


@router.get("/{library_id}/items/export", summary="导出词库所有项")
def export_library_items(library_id: str, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### GET `/{library_id}/items/export` 导出词库所有项

    **功能描述**:
    此接口用于导出指定词库下的所有词库项，不分页返回全部数据。
    适用于词库数据的完整导出、备份等场景。
    返回的数据格式与分页接口相同，但包含所有词库项。

    ---
    ### 路径参数 (Path Parameters)
    | 参数         | 类型     | 必填 | 描述       |
    |--------------|----------|------|------------|
    | `library_id` | `string` | 是   | 词库ID     |

    **请求示例**:
    ```bash
    GET /uuid-library-id-here/items/export
    ```

    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "items": [
                {
                    "id": "uuid-item-id-1",
                    "library_id": "uuid-library-id-here",
                    "content": "敏感词汇1",
                    "content_hash": "5d41402abc4b2a76b9719d911017c592",
                    "content_type": "text",
                    "item_metadata": {
                        "category": "政治",
                        "level": 4
                    },
                    "hit_count": 15,
                    "sort_order": 10,
                    "create_time": "2024-07-16T10:00:00",
                    "update_time": "2024-07-16T10:00:00"
                }
            ],
            "total": 1500
        }
    }
    ```
    """
    try:
        # 先验证词库的所有权
        library = GuardLibraryService.get_by_id(db, library_id)
        if not library:
            return get_data_error_result(retmsg="词库不存在")

        if library.tenant_id != user.id:
            return get_data_error_result(retmsg="无权访问此词库")

        items = GuardLibraryItemService.get_all_items_by_library(db, library_id)

        # 转换为字典格式
        items_data = []
        for item in items:
            items_data.append(item.to_dict())

        return get_json_result(data={"items": items_data, "total": len(items_data)})

    except Exception as e:
        return server_error_response(e)


@router.post("/items/batch-get", summary="批量获取词库项")
def batch_get_items(request: BatchGetItemsRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### POST `/items/batch-get` 批量获取词库项

    **功能描述**:
    此接口用于根据词库项ID数组批量获取指定的词库项。
    适用于选择性导出、批量操作前的数据确认等场景。
    会自动过滤掉不存在或无权限访问的词库项。

    ---
    ### 请求体 (Request Body)
    | 字段       | 类型         | 必填 | 描述           |
    |------------|-------------|------|----------------|
    | `item_ids` | `list[string]` | 是   | 词库项ID列表   |

    **请求示例**:
    ```json
    {
        "item_ids": [
            "uuid-item-id-1",
            "uuid-item-id-2",
            "uuid-item-id-3"
        ]
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
            "items": [
                {
                    "id": "uuid-item-id-1",
                    "library_id": "uuid-library-id-here",
                    "content": "敏感词汇1",
                    "content_hash": "5d41402abc4b2a76b9719d911017c592",
                    "content_type": "text",
                    "metadata": {
                        "category": "政治",
                        "level": 4
                    },
                    "hit_count": 15,
                    "sort_order": 10,
                    "create_time": "2024-07-16T10:00:00",
                    "update_time": "2024-07-16T10:00:00"
                }
            ],
            "total": 3,
            "found_count": 2,
            "not_found_count": 1
        }
    }
    ```
    """
    try:
        if not request.item_ids:
            return get_data_error_result(retmsg="词库项ID列表不能为空")

        items = GuardLibraryItemService.get_items_by_ids(db, request.item_ids, user.id)

        # 转换为字典格式
        items_data = []
        for item in items:
            items_data.append(item.to_dict())

        # 统计信息
        found_count = len(items_data)
        not_found_count = len(request.item_ids) - found_count

        return get_json_result(data={"items": items_data, "total": len(request.item_ids), "found_count": found_count, "not_found_count": not_found_count})

    except Exception as e:
        return server_error_response(e)


@router.get("/stats", summary="获取词库统计")
def get_library_stats(db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### GET `/stats` 获取词库统计信息

    **功能描述**:
    此接口用于获取用户租户下所有词库的统计信息。
    包括词库总数、词库项总数、总命中次数，以及按类型的详细统计。
    用于监控和分析词库使用情况。

    ---
    **请求示例**:
    ```bash
    GET /stats
    ```

    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "total_libraries": 6,
            "total_items": 1500,
            "total_hits": 15000,
            "type_stats": {
                "blacklist": {
                    "count": 3,
                    "total_items": 800,
                    "total_hits": 12000
                },
                "whitelist": {
                    "count": 1,
                    "total_items": 200,
                    "total_hits": 1000
                },
                "reply": {
                    "count": 2,
                    "total_items": 500,
                    "total_hits": 2000
                }
            },
            "library_types": ["blacklist", "whitelist", "reply"]
        }
    }
    ```
    """
    try:
        stats = GuardLibraryService.get_library_stats(db, user.id)
        return get_json_result(data=stats)

    except Exception as e:
        return server_error_response(e)


@router.post("/init", summary="初始化默认词库")
def init_default_libraries(db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    ### POST `/init` 初始化默认词库

    **功能描述**:
    此接口用于为新用户或租户初始化默认的AI安全护栏词库。
    会自动创建包括政治敏感词库、色情内容词库、暴力词库、通用白名单、代答库等基础词库。
    适用于首次使用时的快速设置。

    ---
    **请求示例**:
    ```bash
    POST /init
    ```

    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "created_count": 6,
            "library_ids": [
                "uuid-blacklist-political-id",
                "uuid-blacklist-pornographic-id",
                "uuid-blacklist-violence-id",
                "uuid-whitelist-general-id",
                "uuid-reply-compliance-id",
                "uuid-reply-sensitive-id"
            ]
        }
    }
    ```

    #### 失败响应 (500)
    ```json
    {
        "retcode": -1,
        "retmsg": "服务器内部错误",
        "data": null
    }
    ```
    """
    try:
        library_ids = GuardLibraryService.init_default_libraries(db, user.id, user.id)

        return get_json_result(data={"created_count": len(library_ids), "library_ids": library_ids})

    except Exception as e:
        return server_error_response(e)

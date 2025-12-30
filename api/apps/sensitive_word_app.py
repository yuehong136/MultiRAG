# coding=utf-8
"""
@project: multirag
@Author：龙
@file： sensitive_word_app.py
@date：2025/01/07 09:00
@desc: 敏感词管理接口
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Any

from api.apps import manager
from api.db.db_models import get_db
# from api.db.services.sensitive_word_service import (
#     SensitiveWordService,
#     SensitiveWordCategoryService,
#     SensitiveWordLevelService,
#     SensitiveWordWhitelistService,
#     SensitiveFilterLogService,
#     SensitiveFilterStatsService
# )
from api.utils.api_utils import get_json_result, server_error_response, get_data_error_result
from common.misc_utils import get_uuid
from common import settings

router = APIRouter()


# Pydantic 模型
class CreateCategoryRequest(BaseModel):
    """创建敏感词分类请求模型

    Example:
        {
            "name": "政治敏感",
            "description": "政治相关敏感词分类"
        }
    """
    name: str = Field(..., description="分类名称，不能为空")
    description: str | None = Field(None, description="分类描述，可选")


class UpdateCategoryRequest(BaseModel):
    """更新敏感词分类请求模型

    Example:
        {
            "category_id": "uuid-string",
            "name": "更新后的分类名",
            "description": "更新后的描述",
            "status": "1"
        }
    """
    category_id: str = Field(..., description="分类ID，必填")
    name: str | None = Field(None, description="分类名称，可选")
    description: str | None = Field(None, description="分类描述，可选")
    status: str | None = Field(None, description="状态：1-启用，0-禁用")


class CreateLevelRequest(BaseModel):
    """创建敏感词等级请求模型

    Example:
        {
            "name": "高危",
            "level": 5,
            "description": "高危敏感词等级",
            "action": "block",
            "replacement": "***"
        }
    """
    name: str = Field(..., description="等级名称，不能为空")
    level: int = Field(..., ge=1, le=5, description="等级数值，范围1-5，数值越高越严重")
    description: str | None = Field(None, description="等级描述，可选")
    action: str = Field("block", description="处理动作：block-阻止，replace-替换，warn-警告")
    replacement: str | None = Field(None, description="替换文本，当action为replace时使用")


class UpdateLevelRequest(BaseModel):
    """更新敏感词等级请求模型

    Example:
        {
            "level_id": "uuid-string",
            "name": "中危",
            "level": 3,
            "action": "replace",
            "replacement": "***",
            "status": "1"
        }
    """
    level_id: str = Field(..., description="等级ID，必填")
    name: str | None = Field(None, description="等级名称，可选")
    level: int | None = Field(None, ge=1, le=5, description="等级数值，范围1-5")
    description: str | None = Field(None, description="等级描述，可选")
    action: str | None = Field(None, description="处理动作：block/replace/warn")
    replacement: str | None = Field(None, description="替换文本，可选")
    status: str | None = Field(None, description="状态：1-启用，0-禁用")


class CreateSensitiveWordRequest(BaseModel):
    """创建敏感词请求模型

    Example:
        {
            "word": "敏感词内容",
            "category_id": "uuid-string",
            "level_id": "uuid-string",
            "match_type": "exact",
            "description": "词汇说明",
            "source": "manual"
        }
    """
    word: str = Field(..., description="敏感词内容，不能为空")
    category_id: str = Field(..., description="分类ID，必填")
    level_id: str = Field(..., description="等级ID，必填")
    match_type: str = Field("exact", description="匹配类型：exact-精确匹配，partial-部分匹配，regex-正则匹配")
    description: str | None = Field(None, description="词汇描述，可选")
    source: str | None = Field(None, description="来源标识，可选")


class BatchCreateSensitiveWordRequest(BaseModel):
    """批量创建敏感词请求模型

    Example:
        {
            "words": ["敏感词1", "敏感词2", "敏感词3"],
            "category_id": "uuid-string",
            "level_id": "uuid-string",
            "match_type": "exact",
            "source": "batch_import"
        }
    """
    words: list[str] = Field(..., description="敏感词列表，不能为空")
    category_id: str = Field(..., description="分类ID，必填")
    level_id: str = Field(..., description="等级ID，必填")
    match_type: str = Field("exact", description="匹配类型：exact/partial/regex")
    source: str | None = Field(None, description="来源标识，可选")


class UpdateSensitiveWordRequest(BaseModel):
    """更新敏感词请求模型

    Example:
        {
            "word_id": "uuid-string",
            "word": "更新后的敏感词",
            "category_id": "new-category-id",
            "level_id": "new-level-id",
            "match_type": "partial",
            "status": "1"
        }
    """
    word_id: str = Field(..., description="敏感词ID，必填")
    word: str | None = Field(None, description="敏感词内容，可选")
    category_id: str | None = Field(None, description="分类ID，可选")
    level_id: str | None = Field(None, description="等级ID，可选")
    match_type: str | None = Field(None, description="匹配类型，可选")
    description: str | None = Field(None, description="词汇描述，可选")
    source: str | None = Field(None, description="来源标识，可选")
    status: str | None = Field(None, description="状态：1-启用，0-禁用")


class CreateWhitelistRequest(BaseModel):
    """创建白名单请求模型

    Example:
        {
            "word": "正常词汇",
            "reason": "误判为敏感词，加入白名单"
        }
    """
    word: str = Field(..., description="白名单词汇，不能为空")
    reason: str | None = Field(None, description="加入白名单的原因，可选")


class ContentFilterRequest(BaseModel):
    """内容过滤检测请求模型

    Example:
        {
            "content": "这是一段需要检测的文本内容",
            "strict_mode": false
        }
    """
    content: str = Field(..., description="待检测的文本内容，不能为空")
    strict_mode: bool = Field(False, description="是否启用严格模式，默认false")


class BatchDeleteRequest(BaseModel):
    """批量删除请求模型

    Example:
        {
            "ids": ["uuid1", "uuid2", "uuid3"]
        }
    """
    ids: list[str] = Field(..., description="待删除的ID列表，不能为空")


# 分类管理接口
@router.post('/categories/create', summary="创建敏感词分类")
def create_category(
    request: CreateCategoryRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    创建敏感词分类

    Args:
        request: 创建分类请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 包含创建结果的响应数据
        - 成功: {"code": 200, "data": {"category_id": "uuid"}}
        - 失败: {"code": 400, "retmsg": "错误信息"}

    Raises:
        Exception: 数据库操作异常或其他系统异常
    """
    try:
        category_data = {
            "id": get_uuid(),
            "name": request.name,
            "description": request.description,
            "tenant_id": user.id,
            "created_by": user.id
        }

        if not SensitiveWordCategoryService.save(db, **category_data):
            return get_data_error_result(retmsg="创建分类失败")

        return get_json_result(data={"category_id": category_data["id"]})
    except Exception as e:
        return server_error_response(e)


@router.get('/categories/list', summary="获取敏感词分类列表")
def list_categories(
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取当前租户的所有敏感词分类列表

    Args:
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 包含分类列表的响应数据
        - 成功: {"code": 200, "data": [{"id": "uuid", "name": "分类名", ...}]}
        - 失败: {"code": 500, "retmsg": "错误信息"}
    """
    try:
        categories = SensitiveWordCategoryService.query(db, tenant_id=user.id, status="1")
        return get_json_result(data=[cat.to_dict() for cat in categories])
    except Exception as e:
        return server_error_response(e)


@router.post('/categories/update', summary="更新敏感词分类")
def update_category(
    request: UpdateCategoryRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    更新敏感词分类信息

    Args:
        request: 更新分类请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 更新结果响应
        - 成功: {"code": 200, "data": True}
        - 失败: {"code": 400, "retmsg": "更新分类失败"}
    """
    try:
        update_data = {k: v for k, v in request.model_dump().items() if v is not None and k != "category_id"}

        if not SensitiveWordCategoryService.update_by_id(db, request.category_id, update_data):
            return get_data_error_result(retmsg="更新分类失败")

        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.delete('/categories/{category_id}', summary="删除敏感词分类")
def delete_category(
    category_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    删除指定的敏感词分类

    Args:
        category_id: 分类ID
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 删除结果响应
        - 成功: {"code": 200, "data": True}
        - 失败: {"code": 400, "retmsg": "删除分类失败"}

    Note:
        删除分类前请确保该分类下没有关联的敏感词
    """
    try:
        if not SensitiveWordCategoryService.delete_by_id(db, category_id):
            return get_data_error_result(retmsg="删除分类失败")

        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


# 等级管理接口
@router.post('/levels/create', summary="创建敏感词等级")
def create_level(
    request: CreateLevelRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    创建敏感词等级

    Args:
        request: 创建等级请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 包含创建结果的响应数据
        - 成功: {"code": 200, "data": {"level_id": "uuid"}}
        - 失败: {"code": 400, "retmsg": "创建等级失败"}

    Note:
        等级数值范围1-5，数值越高表示敏感程度越高
        action支持: block(阻止), replace(替换), warn(警告)
    """
    try:
        level_data = {
            "id": get_uuid(),
            "name": request.name,
            "level": request.level,
            "description": request.description,
            "action": request.action,
            "replacement": request.replacement,
            "tenant_id": user.id
        }

        if not SensitiveWordLevelService.save(db, **level_data):
            return get_data_error_result(retmsg="创建等级失败")

        return get_json_result(data={"level_id": level_data["id"]})
    except Exception as e:
        return server_error_response(e)


@router.get('/levels/list', summary="获取敏感词等级列表")
def list_levels(
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取当前租户的所有敏感词等级列表

    Args:
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 包含等级列表的响应数据
        - 成功: {"code": 200, "data": [{"id": "uuid", "name": "等级名", "level": 1, ...}]}
        - 失败: {"code": 500, "retmsg": "错误信息"}
    """
    try:
        levels = SensitiveWordLevelService.query(db, tenant_id=user.id, status="1")
        return get_json_result(data=[level.to_dict() for level in levels])
    except Exception as e:
        return server_error_response(e)


@router.post('/levels/update', summary="更新敏感词等级")
def update_level(
    request: UpdateLevelRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    更新敏感词等级信息

    Args:
        request: 更新等级请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 更新结果响应
        - 成功: {"code": 200, "data": True}
        - 失败: {"code": 400, "retmsg": "更新等级失败"}
    """
    try:
        update_data = {k: v for k, v in request.model_dump().items() if v is not None and k != "level_id"}

        if not SensitiveWordLevelService.update_by_id(db, request.level_id, update_data):
            return get_data_error_result(retmsg="更新等级失败")

        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.delete('/levels/{level_id}', summary="删除敏感词等级")
def delete_level(
    level_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    删除指定的敏感词等级

    Args:
        level_id: 等级ID
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 删除结果响应
        - 成功: {"code": 200, "data": True}
        - 失败: {"code": 400, "retmsg": "删除等级失败"}

    Note:
        删除等级前请确保该等级下没有关联的敏感词
    """
    try:
        if not SensitiveWordLevelService.delete_by_id(db, level_id):
            return get_data_error_result(retmsg="删除等级失败")

        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


# 敏感词管理接口
@router.post('/words/create', summary="创建敏感词")
def create_sensitive_word(
    request: CreateSensitiveWordRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    创建单个敏感词

    Args:
        request: 创建敏感词请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 包含创建结果的响应数据
        - 成功: {"code": 200, "data": {"word_id": "uuid"}}
        - 失败: {"code": 400, "retmsg": "创建敏感词失败"}

    Note:
        创建前会检查敏感词是否已存在，避免重复创建
        match_type支持: exact(精确匹配), partial(部分匹配), regex(正则匹配)
    """
    try:
        result = SensitiveWordService.create_sensitive_word(
            db=db,
            word=request.word,
            category_id=request.category_id,
            level_id=request.level_id,
            match_type=request.match_type,
            description=request.description,
            source=request.source,
            tenant_id=user.id,
            created_by=user.id
        )

        if result:
            return get_json_result(data={"word_id": result})
        else:
            return get_data_error_result(retmsg="创建敏感词失败")
    except Exception as e:
        return server_error_response(e)


@router.post('/words/batch-create', summary="批量创建敏感词")
def batch_create_sensitive_words(
    request: BatchCreateSensitiveWordRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    批量创建敏感词

    Args:
        request: 批量创建敏感词请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 包含批量创建结果的响应数据
        - 成功: {"code": 200, "data": {"success_count": 10, "failed_count": 2, "failed_words": ["词1", "词2"]}}
        - 失败: {"code": 500, "retmsg": "错误信息"}

    Note:
        批量创建会跳过已存在的敏感词，返回成功和失败的统计信息
        所有敏感词使用相同的分类、等级和匹配类型
    """
    try:
        results = SensitiveWordService.batch_create_sensitive_words(
            db=db,
            words=request.words,
            category_id=request.category_id,
            level_id=request.level_id,
            match_type=request.match_type,
            source=request.source,
            tenant_id=user.id,
            created_by=user.id
        )

        return get_json_result(data={
            "success_count": results["success_count"],
            "failed_count": results["failed_count"],
            "failed_words": results["failed_words"]
        })
    except Exception as e:
        return server_error_response(e)


@router.get('/words/list', summary="获取敏感词列表")
def list_sensitive_words(
    page: int = 1,
    page_size: int = 50,
    category_id: str | None = None,
    level_id: str | None = None,
    keyword: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    分页获取敏感词列表，支持多种过滤条件

    Args:
        page: 页码，从1开始，默认1
        page_size: 每页数量，默认50，最大100
        category_id: 分类ID过滤，可选
        level_id: 等级ID过滤，可选
        keyword: 关键词搜索，支持模糊匹配敏感词内容，可选
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 包含分页敏感词列表的响应数据
        - 成功: {"code": 200, "data": {"items": [...], "total": 100, "page": 1, "page_size": 50}}
        - 失败: {"code": 500, "retmsg": "错误信息"}

    Example:
        GET /words/list?page=1&page_size=20&category_id=uuid&keyword=测试
    """
    try:
        result = SensitiveWordService.get_paginated_words(
            db=db,
            tenant_id=user.id,
            page=page,
            page_size=page_size,
            category_id=category_id,
            level_id=level_id,
            keyword=keyword
        )

        return get_json_result(data=result)
    except Exception as e:
        return server_error_response(e)


@router.post('/words/update', summary="更新敏感词")
def update_sensitive_word(
    request: UpdateSensitiveWordRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    更新敏感词信息

    Args:
        request: 更新敏感词请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 更新结果响应
        - 成功: {"code": 200, "data": True}
        - 失败: {"code": 400, "retmsg": "更新敏感词失败"}

    Note:
        只更新提供的非空字段，其他字段保持不变
    """
    try:
        result = SensitiveWordService.update_sensitive_word(
            db=db,
            word_id=request.word_id,
            **{k: v for k, v in request.model_dump().items() if v is not None and k != "word_id"}
        )

        if result:
            return get_json_result(data=True)
        else:
            return get_data_error_result(retmsg="更新敏感词失败")
    except Exception as e:
        return server_error_response(e)


@router.delete('/words/{word_id}', summary="删除敏感词")
def delete_sensitive_word(
    word_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    删除指定的敏感词

    Args:
        word_id: 敏感词ID
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 删除结果响应
        - 成功: {"code": 200, "data": True}
        - 失败: {"code": 400, "retmsg": "删除敏感词失败"}
    """
    try:
        if not SensitiveWordService.delete_by_id(db, word_id):
            return get_data_error_result(retmsg="删除敏感词失败")

        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post('/words/batch-delete', summary="批量删除敏感词")
def batch_delete_sensitive_words(
    request: BatchDeleteRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    批量删除敏感词

    Args:
        request: 批量删除请求参数，包含ID列表
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 批量删除结果响应
        - 成功: {"code": 200, "data": {"success_count": 8, "failed_count": 2}}
        - 失败: {"code": 500, "retmsg": "错误信息"}

    Note:
        会尝试删除所有提供的ID，返回成功和失败的统计信息
    """
    try:
        result = SensitiveWordService.batch_delete_words(db, request.ids)
        return get_json_result(data={
            "success_count": result["success_count"],
            "failed_count": result["failed_count"]
        })
    except Exception as e:
        return server_error_response(e)


# 白名单管理接口
@router.post('/whitelist/create', summary="创建白名单")
def create_whitelist(
    request: CreateWhitelistRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    创建敏感词白名单

    Args:
        request: 创建白名单请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 包含创建结果的响应数据
        - 成功: {"code": 200, "data": {"whitelist_id": "uuid"}}
        - 失败: {"code": 400, "retmsg": "创建白名单失败"}

    Note:
        白名单中的词汇在敏感词检测时会被忽略，不会被标记为敏感词
        适用于误判的正常词汇或特殊业务需求的词汇
    """
    try:
        result = SensitiveWordWhitelistService.create_whitelist_word(
            db=db,
            word=request.word,
            reason=request.reason,
            tenant_id=user.id,
            created_by=user.id
        )

        if result:
            return get_json_result(data={"whitelist_id": result})
        else:
            return get_data_error_result(retmsg="创建白名单失败")
    except Exception as e:
        return server_error_response(e)


@router.get('/whitelist/list', summary="获取白名单列表")
def list_whitelist(
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取当前租户的所有白名单词汇列表

    Args:
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 包含白名单列表的响应数据
        - 成功: {"code": 200, "data": [{"id": "uuid", "word": "词汇", "reason": "原因", ...}]}
        - 失败: {"code": 500, "retmsg": "错误信息"}
    """
    try:
        whitelist = SensitiveWordWhitelistService.query(db, tenant_id=user.id, status="1")
        return get_json_result(data=[item.to_dict() for item in whitelist])
    except Exception as e:
        return server_error_response(e)


@router.delete('/whitelist/{whitelist_id}', summary="删除白名单")
def delete_whitelist(
    whitelist_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    删除指定的白名单词汇

    Args:
        whitelist_id: 白名单ID
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 删除结果响应
        - 成功: {"code": 200, "data": True}
        - 失败: {"code": 400, "retmsg": "删除白名单失败"}

    Note:
        删除后该词汇将重新参与敏感词检测
    """
    try:
        if not SensitiveWordWhitelistService.delete_by_id(db, whitelist_id):
            return get_data_error_result(retmsg="删除白名单失败")

        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


# 内容过滤接口
@router.post('/filter/check', summary="检测内容敏感词")
def check_content(
    request: ContentFilterRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    检测文本内容中的敏感词

    Args:
        request: 内容过滤请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 包含检测结果的响应数据
        - 成功: {
            "code": 200,
            "data": {
                "is_sensitive": true,
                "filtered_content": "过滤后的内容",
                "sensitive_words": [{"word": "敏感词", "level": 5, "action": "block", ...}],
                "statistics": {"total_count": 3, "block_count": 1, "replace_count": 2}
            }
        }
        - 失败: {"code": 500, "retmsg": "错误信息"}

    Note:
        strict_mode=true时会使用更严格的检测规则
        检测结果会记录到过滤日志中用于统计分析
    """
    try:
        result = SensitiveWordService.filter_content(
            db=db,
            content=request.content,
            tenant_id=user.id,
            strict_mode=request.strict_mode,
            user_id=user.id,
            source_type="api_check",
            source_id="manual_check"
        )

        return get_json_result(data=result)
    except Exception as e:
        return server_error_response(e)


# 统计分析接口
@router.get('/stats/overview', summary="获取敏感词统计概览")
def get_stats_overview(
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取当前租户的敏感词统计概览信息

    Args:
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 包含统计概览的响应数据
        - 成功: {
            "code": 200,
            "data": {
                "total_words": 1000,
                "total_categories": 10,
                "total_levels": 5,
                "filter_count_today": 50,
                "filter_count_week": 300,
                "top_categories": [...],
                "recent_activities": [...]
            }
        }
        - 失败: {"code": 500, "retmsg": "错误信息"}
    """
    try:
        stats = SensitiveFilterStatsService.get_tenant_overview(db, user.id)
        return get_json_result(data=stats)
    except Exception as e:
        return server_error_response(e)


@router.get('/logs/list', summary="获取过滤日志")
def list_filter_logs(
    page: int = 1,
    page_size: int = 50,
    start_date: str | None = None,
    end_date: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    分页获取敏感词过滤日志，支持时间范围过滤

    Args:
        page: 页码，从1开始，默认1
        page_size: 每页数量，默认50
        start_date: 开始日期，格式YYYY-MM-DD，可选
        end_date: 结束日期，格式YYYY-MM-DD，可选
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 包含分页日志列表的响应数据
        - 成功: {
            "code": 200,
            "data": {
                "items": [{"id": "uuid", "content": "原始内容", "filtered_content": "过滤后内容", ...}],
                "total": 100,
                "page": 1,
                "page_size": 50
            }
        }
        - 失败: {"code": 500, "retmsg": "错误信息"}

    Example:
        GET /logs/list?page=1&page_size=20&start_date=2025-01-01&end_date=2025-01-07
    """
    try:
        result = SensitiveFilterLogService.get_paginated_logs(
            db=db,
            tenant_id=user.id,
            page=page,
            page_size=page_size,
            start_date=start_date,
            end_date=end_date
        )

        return get_json_result(data=result)
    except Exception as e:
        return server_error_response(e)


# 缓存管理接口
@router.post('/cache/refresh', summary="刷新敏感词缓存")
def refresh_cache(
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    刷新当前租户的敏感词缓存

    Args:
        db: 数据库会话
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 缓存刷新结果响应
        - 成功: {"code": 200, "data": True, "retmsg": "缓存刷新成功"}
        - 失败: {"code": 400, "retmsg": "缓存刷新失败"}

    Note:
        当敏感词配置发生变更时建议手动刷新缓存以确保检测准确性
        系统也会定期自动刷新缓存
    """
    try:
        result = SensitiveWordService.refresh_tenant_cache(db, user.id)
        if result:
            return get_json_result(data=True, retmsg="缓存刷新成功")
        else:
            return get_data_error_result(retmsg="缓存刷新失败")
    except Exception as e:
        return server_error_response(e)


@router.get('/cache/status', summary="获取缓存状态")
def get_cache_status(user=Depends(manager)) -> dict[str, Any]:
    """
    获取当前租户的敏感词缓存状态信息

    Args:
        user: 当前用户信息

    Returns:
        Dict[str, Any]: 包含缓存状态的响应数据
        - 成功: {
            "code": 200,
            "data": {
                "cache_size": 1000,
                "last_refresh_time": "2025-01-07 10:30:00",
                "hit_rate": 0.95,
                "memory_usage": "2.5MB"
            }
        }
        - 失败: {"code": 500, "retmsg": "错误信息"}

    Note:
        缓存状态包括缓存大小、最后刷新时间、命中率等信息
    """
    try:
        stats = SensitiveWordService.get_cache_stats(user.id)
        return get_json_result(data=stats)
    except Exception as e:
        return server_error_response(e)
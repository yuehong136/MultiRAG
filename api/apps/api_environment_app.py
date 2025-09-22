# coding=utf-8
"""
@project: multirag
@Author：龙
@file： environment_app.py
@date：2025/1/15 10:00
@desc: 环境管理API接口
"""
import logging
from typing import Any
from fastapi import APIRouter, HTTPException, Depends, Query, Body
from fastapi.responses import JSONResponse

from api.db.services.api_environment_models import (
    EnvironmentCreate, EnvironmentUpdate, EnvironmentListResponse, EnvironmentDetailResponse,
    EnvironmentVariableCreate, EnvironmentVariableUpdate, EnvironmentVariableResponse,
    EnvironmentDuplicateRequest, BatchVariablesRequest, VariableResolveRequest, VariableResolveResponse,
    GlobalEnvironmentResponse, EnvironmentQueryParams, PaginatedEnvironmentResponse
)
from api.db.services.api_environment_service import environment_service
from api.utils.api_utils import get_json_result, get_data_error_result, server_error_response
from api.apps import manager, app as global_app
from api.db.db_models import get_db, UserTenant

router = APIRouter()
logger = logging.getLogger(__name__)


def get_user_tenant_id(db, user_id: str) -> str:
    """获取用户的租户ID（当前设计：user_id == tenant_id）"""
    # 根据当前设计，user_id 就等于 tenant_id
    # 但为了兼容性，还是查询一下 UserTenant 表
    user_tenant = db.query(UserTenant).filter(UserTenant.user_id == user_id).first()
    if user_tenant:
        return user_tenant.tenant_id
    else:
        # 如果没有找到关系记录，假设 user_id == tenant_id
        logger.warning(f"用户 {user_id} 没有在 UserTenant 表中找到记录，使用 user_id 作为 tenant_id")
        return user_id


@router.get("/environments", 
            summary="获取环境列表", 
            response_description="返回用户的环境列表",
            response_model=PaginatedEnvironmentResponse)
async def get_environments(
    page: int = Query(1, description="页码", ge=1),
    page_size: int = Query(20, description="每页数量", ge=1, le=100),
    search: str = Query(None, description="搜索关键字"),
    is_default: bool = Query(None, description="筛选默认环境"),
    user=Depends(manager),
    db=Depends(get_db)
):
    """
    获取当前用户的所有环境列表
    
    支持分页、搜索和筛选功能：
    - 支持按环境名称和描述搜索
    - 支持筛选默认环境
    - 按默认环境优先、创建时间倒序排列
    """
    try:
        params = EnvironmentQueryParams(
            page=page,
            page_size=page_size,
            search=search,
            is_default=is_default
        )
        
        tenant_id = get_user_tenant_id(db, user.id)
        result = environment_service.get_environments(db, tenant_id, params)
        return get_json_result(data=result, retmsg="获取环境列表成功")
        
    except Exception as e:
        logger.error(f"获取环境列表失败: {str(e)}")
        return get_data_error_result(retmsg=str(e))


@router.get("/environments/{environment_id}", 
            summary="获取环境详情", 
            response_description="返回环境的详细信息",
            response_model=EnvironmentDetailResponse)
async def get_environment_detail(
    environment_id: str,
    user=Depends(manager),
    db=Depends(get_db)
):
    """
    获取指定环境的详细信息，包括所有环境变量
    
    返回环境的基本信息和所有关联的变量列表
    """
    try:
        tenant_id = get_user_tenant_id(db, user.id)
        result = environment_service.get_environment_detail(db, tenant_id, environment_id)
        return get_json_result(data=result, retmsg="获取环境详情成功")
        
    except Exception as e:
        logger.error(f"获取环境详情失败: {str(e)}")
        return get_data_error_result(retmsg=str(e))


@router.post("/environments", 
             summary="创建环境", 
             response_description="返回创建的环境详情",
             response_model=EnvironmentDetailResponse)
async def create_environment(
    env_data: EnvironmentCreate = Body(..., description="环境数据"),
    user=Depends(manager),
    db=Depends(get_db)
):
    """
    创建新的环境
    
    可以同时创建环境和关联的变量：
    - 环境名称在同一用户下必须唯一
    - base_url: 可选的前置URL，如 https://api.example.com
    - 如果设为默认环境，会自动取消其他环境的默认状态
    - 变量名在同一环境下必须唯一
    """
    try:
        tenant_id = get_user_tenant_id(db, user.id)
        result = environment_service.create_environment(db, tenant_id, env_data)
        return get_json_result(data=result, retmsg="创建环境成功")
        
    except Exception as e:
        logger.error(f"创建环境失败: {str(e)}")
        return get_data_error_result(retmsg=str(e))


@router.put("/environments/{environment_id}", 
            summary="更新环境", 
            response_description="返回更新后的环境详情",
            response_model=EnvironmentDetailResponse)
async def update_environment(
    environment_id: str,
    env_data: EnvironmentUpdate = Body(..., description="更新数据"),
    user=Depends(manager),
    db=Depends(get_db)
):
    """
    更新环境基本信息
    
    支持更新环境名称、描述和默认状态：
    - 环境名称在同一用户下必须唯一
    - 如果设为默认环境，会自动取消其他环境的默认状态
    """
    try:
        result = environment_service.update_environment(db, user.id, environment_id, env_data)
        return get_json_result(data=result, retmsg="更新环境成功")
        
    except Exception as e:
        logger.error(f"更新环境失败: {str(e)}")
        return get_data_error_result(retmsg=str(e))


@router.delete("/environments/{environment_id}", 
               summary="删除环境", 
               response_description="返回删除结果")
async def delete_environment(
    environment_id: str,
    user=Depends(manager),
    db=Depends(get_db)
):
    """
    删除指定环境
    
    会同时删除环境下的所有变量
    """
    try:
        environment_service.delete_environment(db, user.id, environment_id)
        return get_json_result(data=True, retmsg="删除环境成功")
        
    except Exception as e:
        logger.error(f"删除环境失败: {str(e)}")
        return get_data_error_result(retmsg=str(e))


@router.post("/environments/{environment_id}/duplicate", 
             summary="复制环境", 
             response_description="返回复制的环境详情",
             response_model=EnvironmentDetailResponse)
async def duplicate_environment(
    environment_id: str,
    duplicate_data: EnvironmentDuplicateRequest = Body(..., description="复制请求数据"),
    user=Depends(manager),
    db=Depends(get_db)
):
    """
    复制现有环境
    
    会复制环境的所有信息和变量：
    - 新环境名称必须唯一
    - 复制的环境不会设为默认环境
    """
    try:
        result = environment_service.duplicate_environment(db, user.id, environment_id, duplicate_data)
        return get_json_result(data=result, retmsg="复制环境成功")
        
    except Exception as e:
        logger.error(f"复制环境失败: {str(e)}")
        return get_data_error_result(retmsg=str(e))


@router.post("/environments/{environment_id}/set-default", 
             summary="设置默认环境", 
             response_description="返回设置后的环境详情",
             response_model=EnvironmentDetailResponse)
async def set_default_environment(
    environment_id: str,
    user=Depends(manager),
    db=Depends(get_db)
):
    """
    将指定环境设为默认环境
    
    会自动取消其他环境的默认状态
    """
    try:
        result = environment_service.set_default_environment(db, user.id, environment_id)
        return get_json_result(data=result, retmsg="设置默认环境成功")
        
    except Exception as e:
        logger.error(f"设置默认环境失败: {str(e)}")
        return get_data_error_result(retmsg=str(e))


@router.post("/environments/{environment_id}/variables", 
             summary="创建环境变量", 
             response_description="返回创建的变量",
             response_model=EnvironmentVariableResponse)
async def create_variable(
    environment_id: str,
    var_data: EnvironmentVariableCreate = Body(..., description="变量数据"),
    user=Depends(manager),
    db=Depends(get_db)
):
    """
    在指定环境中添加变量
    
    变量名在同一环境下必须唯一
    """
    try:
        result = environment_service.create_variable(db, user.id, environment_id, var_data)
        return get_json_result(data=result, retmsg="创建变量成功")
        
    except Exception as e:
        logger.error(f"创建变量失败: {str(e)}")
        return get_data_error_result(retmsg=str(e))


@router.put("/environments/{environment_id}/variables/{variable_id}", 
            summary="更新环境变量", 
            response_description="返回更新后的变量",
            response_model=EnvironmentVariableResponse)
async def update_variable(
    environment_id: str,
    variable_id: str,
    var_data: EnvironmentVariableUpdate = Body(..., description="更新数据"),
    user=Depends(manager),
    db=Depends(get_db)
):
    """
    更新指定变量
    
    变量名在同一环境下必须唯一
    """
    try:
        result = environment_service.update_variable(db, user.id, environment_id, variable_id, var_data)
        return get_json_result(data=result, retmsg="更新变量成功")
        
    except Exception as e:
        logger.error(f"更新变量失败: {str(e)}")
        return get_data_error_result(retmsg=str(e))


@router.delete("/environments/{environment_id}/variables/{variable_id}", 
               summary="删除环境变量", 
               response_description="返回删除结果")
async def delete_variable(
    environment_id: str,
    variable_id: str,
    user=Depends(manager),
    db=Depends(get_db)
):
    """
    删除指定变量
    """
    try:
        environment_service.delete_variable(db, user.id, environment_id, variable_id)
        return get_json_result(data=True, retmsg="删除变量成功")
        
    except Exception as e:
        logger.error(f"删除变量失败: {str(e)}")
        return get_data_error_result(retmsg=str(e))


@router.put("/environments/{environment_id}/variables/batch", 
            summary="批量更新变量", 
            response_description="返回更新后的变量列表",
            response_model=list[EnvironmentVariableResponse])
async def batch_update_variables(
    environment_id: str,
    batch_data: BatchVariablesRequest = Body(..., description="批量数据"),
    user=Depends(manager),
    db=Depends(get_db)
):
    """
    批量更新环境的所有变量
    
    会删除现有的所有变量，然后创建新的变量列表
    """
    try:
        result = environment_service.batch_update_variables(db, user.id, environment_id, batch_data)
        return get_json_result(data=result, retmsg="批量更新变量成功")
        
    except Exception as e:
        logger.error(f"批量更新变量失败: {str(e)}")
        return get_data_error_result(retmsg=str(e))


@router.post("/environments/{environment_id}/resolve", 
             summary="变量解析预览", 
             response_description="返回变量解析结果",
             response_model=VariableResolveResponse)
async def resolve_variables(
    environment_id: str,
    resolve_data: VariableResolveRequest = Body(..., description="解析请求数据"),
    user=Depends(manager),
    db=Depends(get_db)
):
    """
    预览变量解析结果
    
    将文本中的 {{variableName}} 格式的变量替换为实际值：
    - 返回解析后的文本
    - 列出使用的变量
    - 列出缺失的变量
    """
    try:
        tenant_id = get_user_tenant_id(db, user.id)
        result = environment_service.resolve_variables(db, tenant_id, environment_id, resolve_data.text)
        return get_json_result(data=result, retmsg="变量解析成功")
        
    except Exception as e:
        logger.error(f"变量解析失败: {str(e)}")
        return get_data_error_result(retmsg=str(e))


@router.get("/environments/global", 
            summary="获取全局预设环境", 
            response_description="返回系统预设的环境模板",
            response_model=list[GlobalEnvironmentResponse])
async def get_global_environments(
    user=Depends(manager),
    db=Depends(get_db)
):
    """
    获取系统预设的环境模板
    
    用于环境创建时的快速选择
    """
    try:
        result = environment_service.get_global_environments(db)
        return get_json_result(data=result, retmsg="获取全局环境成功")
        
    except Exception as e:
        logger.error(f"获取全局环境失败: {str(e)}")
        return get_data_error_result(retmsg=str(e))

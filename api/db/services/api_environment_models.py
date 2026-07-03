"""
@project: multirag
@Author：龙
@file： environment_models.py
@date：2025/1/15 10:00
@desc: 环境管理相关模型定义
"""
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VariableType(str, Enum):
    """变量类型"""
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class EnvironmentVariableBase(BaseModel):
    """环境变量基础模型"""
    key_name: str = Field(..., description="变量名", min_length=1, max_length=100)
    key_value: str = Field(..., description="变量值")
    description: str | None = Field(None, description="变量描述")
    is_secret: bool = Field(False, description="是否敏感信息")
    variable_type: VariableType = Field(VariableType.STRING, description="变量类型")

    @field_validator('key_name')
    @classmethod
    def validate_key_name(cls, v: str) -> str:
        """验证变量名格式"""
        if not v.replace('_', '').replace('-', '').isalnum():
            raise ValueError("变量名只能包含字母、数字、下划线和连字符")
        return v


class EnvironmentVariableCreate(EnvironmentVariableBase):
    """创建环境变量请求模型"""
    pass


class EnvironmentVariableUpdate(BaseModel):
    """更新环境变量请求模型"""
    key_name: str | None = Field(None, description="变量名", min_length=1, max_length=100)
    key_value: str | None = Field(None, description="变量值")
    description: str | None = Field(None, description="变量描述")
    is_secret: bool | None = Field(None, description="是否敏感信息")
    variable_type: VariableType | None = Field(None, description="变量类型")


class EnvironmentVariableResponse(EnvironmentVariableBase):
    """环境变量响应模型"""
    id: str = Field(..., description="变量ID")
    environment_id: str = Field(..., description="环境ID")
    status: str = Field(..., description="状态")
    create_time: int = Field(..., description="创建时间戳")
    update_time: int = Field(..., description="更新时间戳")
    create_date: datetime = Field(..., description="创建日期")
    update_date: datetime = Field(..., description="更新日期")

    model_config = ConfigDict(from_attributes=True)


class EnvironmentBase(BaseModel):
    """环境基础模型"""
    name: str = Field(..., description="环境名称", min_length=1, max_length=100)
    description: str | None = Field(None, description="环境描述")
    base_url: str | None = Field(None, description="前置URL/基础URL", max_length=500)
    is_default: bool = Field(False, description="是否默认环境")
    is_global: bool = Field(False, description="是否全局环境")

    @field_validator('base_url')
    @classmethod
    def validate_base_url(cls, v: str | None) -> str | None:
        """验证基础URL格式"""
        if v is not None and v.strip():
            v = v.strip()
            # 简单的URL格式验证
            if not (v.startswith('http://') or v.startswith('https://') or v.startswith('{{') or '://' in v):
                raise ValueError("base_url必须是有效的URL格式，如: https://api.example.com 或包含变量 {{baseUrl}}")
            # 移除末尾的斜杠以保持一致性
            if v.endswith('/') and not v.endswith('://'):
                v = v.rstrip('/')
        return v


class EnvironmentCreate(EnvironmentBase):
    """创建环境请求模型"""
    variables: list[EnvironmentVariableCreate] = Field(default_factory=list, description="环境变量列表")


class EnvironmentUpdate(BaseModel):
    """更新环境请求模型"""
    name: str | None = Field(None, description="环境名称", min_length=1, max_length=100)
    description: str | None = Field(None, description="环境描述")
    base_url: str | None = Field(None, description="前置URL/基础URL", max_length=500)
    is_default: bool | None = Field(None, description="是否默认环境")


class EnvironmentListResponse(EnvironmentBase):
    """环境列表响应模型"""
    id: str = Field(..., description="环境ID")
    tenant_id: str = Field(..., description="租户ID")
    status: str = Field(..., description="状态")
    variables_count: int = Field(..., description="变量数量")
    create_time: int = Field(..., description="创建时间戳")
    update_time: int = Field(..., description="更新时间戳")
    create_date: datetime = Field(..., description="创建日期")
    update_date: datetime = Field(..., description="更新日期")

    model_config = ConfigDict(from_attributes=True)


class EnvironmentDetailResponse(EnvironmentBase):
    """环境详情响应模型"""
    id: str = Field(..., description="环境ID")
    tenant_id: str = Field(..., description="租户ID")
    status: str = Field(..., description="状态")
    variables: list[EnvironmentVariableResponse] = Field(default_factory=list, description="环境变量列表")
    create_time: int = Field(..., description="创建时间戳")
    update_time: int = Field(..., description="更新时间戳")
    create_date: datetime = Field(..., description="创建日期")
    update_date: datetime = Field(..., description="更新日期")

    model_config = ConfigDict(from_attributes=True)


class EnvironmentDuplicateRequest(BaseModel):
    """复制环境请求模型"""
    new_name: str = Field(..., description="新环境名称", min_length=1, max_length=100)


class BatchVariablesRequest(BaseModel):
    """批量更新变量请求模型"""
    variables: list[EnvironmentVariableCreate] = Field(..., description="变量列表")


class VariableResolveRequest(BaseModel):
    """变量解析请求模型"""
    text: str = Field(..., description="要解析的文本")


class VariableResolveResponse(BaseModel):
    """变量解析响应模型"""
    resolved_text: str = Field(..., description="解析后的文本")
    variables_used: list[str] = Field(..., description="使用的变量列表")
    missing_variables: list[str] = Field(..., description="缺失的变量列表")


class GlobalEnvironmentBase(BaseModel):
    """全局环境基础模型"""
    name: str = Field(..., description="环境名称", min_length=1, max_length=100)
    description: str | None = Field(None, description="环境描述")
    server_url: str | None = Field(None, description="服务器URL")
    variables: dict[str, Any] = Field(default_factory=dict, description="预设变量")
    is_active: bool = Field(True, description="是否启用")


class GlobalEnvironmentResponse(GlobalEnvironmentBase):
    """全局环境响应模型"""
    id: str = Field(..., description="环境ID")
    status: str = Field(..., description="状态")
    create_time: int = Field(..., description="创建时间戳")
    update_time: int = Field(..., description="更新时间戳")
    create_date: datetime = Field(..., description="创建日期")
    update_date: datetime = Field(..., description="更新日期")

    model_config = ConfigDict(from_attributes=True)


class EnvironmentQueryParams(BaseModel):
    """环境查询参数模型"""
    page: int = Field(1, description="页码", ge=1)
    page_size: int = Field(20, description="每页数量", ge=1, le=100)
    search: str | None = Field(None, description="搜索关键字")
    is_default: bool | None = Field(None, description="筛选默认环境")


class PaginatedEnvironmentResponse(BaseModel):
    """分页环境响应模型"""
    items: list[EnvironmentListResponse] = Field(..., description="环境列表")
    total: int = Field(..., description="总数量")
    page: int = Field(..., description="当前页码")
    page_size: int = Field(..., description="每页数量")
    total_pages: int = Field(..., description="总页数")

"""
@project: multirag
@Author：龙
@file： openapi_filter_models.py
@date：2025/9/04 10:00
@desc: OpenAPI 过滤服务相关模型定义
"""

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class MatchMode(str, Enum):
    """路径匹配模式"""

    EXACT = "exact"  # 精确匹配
    PREFIX = "prefix"  # 前缀匹配
    GLOB = "glob"  # 通配符匹配
    REGEX = "regex"  # 正则匹配


class OASVersionTarget(str, Enum):
    """OpenAPI 规范版本目标"""

    KEEP = "keep"  # 保持原版本
    V30 = "3.0"  # 转换为 3.0
    V31 = "3.1"  # 转换为 3.1


class FilterRule(BaseModel):
    """过滤规则配置"""

    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    paths: list[str] = Field(..., description="要保留的路径列表", min_length=1)
    source: HttpUrl | None = Field(None, description="OpenAPI 文档源URL，为空时使用本服务的 OpenAPI")
    match: MatchMode = Field(MatchMode.EXACT, description="路径匹配模式")
    include_tags: list[str] = Field(default_factory=list, description="要包含的标签列表")
    exclude_paths: list[str] = Field(default_factory=list, description="要排除的路径列表")
    exclude_tags: list[str] = Field(default_factory=list, description="要排除的标签列表")
    strict: bool = Field(True, description="严格模式：对无效$ref直接报错")
    prune_examples: bool = Field(False, description="是否删除示例以减少敏感数据泄露")
    oas_version_target: OASVersionTarget = Field(OASVersionTarget.KEEP, description="目标OAS版本")
    max_depth: int = Field(10, description="$ref 递归的最大深度，防止无限递归", ge=1, le=50)

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("paths 不能为空")
        # 验证路径格式
        for path in v:
            if not path.strip():
                raise ValueError("路径不能为空字符串")
        return v

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: HttpUrl | None) -> HttpUrl | None:
        if v is not None:
            # SSRF 防护：检查是否为内网地址
            host = v.host
            if host:
                # 禁止内网地址
                internal_patterns = [r"^10\.", r"^172\.(1[6-9]|2[0-9]|3[01])\.", r"^192\.168\.", r"^127\.", r"^localhost$", r"^0\.0\.0\.0$"]
                for pattern in internal_patterns:
                    if re.match(pattern, host):
                        raise ValueError(f"不允许访问内网地址: {host}")

            # 只允许 http/https 协议
            if v.scheme not in ["http", "https"]:
                raise ValueError(f"不支持的协议: {v.scheme}，仅支持 http/https")

        return v


class FilterWarning(BaseModel):
    """过滤警告信息"""

    model_config = ConfigDict(frozen=True)

    type: str = Field(..., description="警告类型")
    message: str = Field(..., description="警告信息")
    path: str | None = Field(None, description="相关的 JSON 路径")


class FilterMeta(BaseModel):
    """过滤元信息"""

    model_config = ConfigDict(frozen=True)

    rules: dict[str, Any] = Field(..., description="应用的过滤规则摘要")
    source_etag: str | None = Field(None, description="源文档的ETag")
    generated_at: str = Field(..., description="生成时间")
    processing_time_ms: float = Field(..., description="处理耗时（毫秒）")
    components_before: int = Field(0, description="过滤前的组件数量")
    components_after: int = Field(0, description="过滤后的组件数量")
    paths_before: int = Field(0, description="过滤前的路径数量")
    paths_after: int = Field(0, description="过滤后的路径数量")


class FilterResponse(BaseModel):
    """过滤结果响应"""

    model_config = ConfigDict(populate_by_name=True)

    openapi: str = Field(..., description="OpenAPI 版本")
    info: dict[str, Any] = Field(..., description="API 信息")
    servers: list[dict[str, Any]] | None = Field(None, description="服务器列表")
    tags: list[dict[str, Any]] | None = Field(None, description="标签列表")
    security: list[dict[str, Any]] | None = Field(None, description="安全定义")
    paths: dict[str, Any] = Field(..., description="过滤后的路径")
    components: dict[str, Any] | None = Field(None, description="过滤后的组件")
    external_docs: dict[str, Any] | None = Field(None, alias="externalDocs", description="外部文档")

    # 扩展字段
    x_filter_warnings: list[FilterWarning] = Field(default_factory=list, alias="x-filter-warnings", description="过滤警告")
    x_filter_meta: FilterMeta = Field(..., alias="x-filter-meta", description="过滤元信息")


class RefInfo(BaseModel):
    """$ref 引用信息"""

    model_config = ConfigDict(frozen=True)

    ref: str = Field(..., description="$ref 值")
    source_path: str = Field(..., description="引用来源的 JSON 路径")
    depth: int = Field(0, description="引用深度")


class ComponentStats(BaseModel):
    """组件统计信息"""

    schemas: int = 0
    responses: int = 0
    parameters: int = 0
    examples: int = 0
    request_bodies: int = 0
    headers: int = 0
    security_schemes: int = 0
    links: int = 0
    callbacks: int = 0
    path_items: int = 0

    @property
    def total(self) -> int:
        return sum([self.schemas, self.responses, self.parameters, self.examples, self.request_bodies, self.headers, self.security_schemes, self.links, self.callbacks, self.path_items])


class FilterResult(BaseModel):
    """内部过滤结果（用于缓存和处理）"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    filtered_doc: dict[str, Any] = Field(..., description="过滤后的文档")
    warnings: list[FilterWarning] = Field(default_factory=list, description="警告列表")
    meta: FilterMeta = Field(..., description="元信息")
    cache_key: str = Field(..., description="缓存键")
    etag: str = Field(..., description="结果ETag")

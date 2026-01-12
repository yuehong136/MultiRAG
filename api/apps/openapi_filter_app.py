# coding=utf-8
"""
@project: multirag
@Author：龙
@file： openapi_filter_app.py
@date：2024/12/10 10:00
@desc: OpenAPI 过滤服务接口
"""
import logging
from typing import Any, Dict
from fastapi import APIRouter, HTTPException, Depends, Request, Query, Body
from fastapi.responses import JSONResponse
from pydantic import ValidationError
import json

from api.db.services.openapi_filter_models import (
    FilterRule, FilterResponse, FilterWarning, FilterMeta
)
from api.db.services.openapi_filter_service import openapi_filter_service
from api.utils.api_utils import get_json_result, get_data_error_result, server_error_response
from api.apps import manager, app as global_app

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/openapi-filtered",
             summary="过滤OpenAPI文档",
             response_description="返回过滤后的OpenAPI文档",
             response_model=Dict[str, Any])
async def filter_openapi_post(
        rule: FilterRule = Body(..., description="过滤规则"),
        request: Request = None,
        user=Depends(manager)
):
    """
    根据指定规则过滤 OpenAPI 文档，并仅保留匹配路径及其依赖组件（最小闭包）。

    文档用途：此说明为 Apifox 文档模式准备，覆盖参数定义、示例与响应结构，便于前端与测试同学使用。

    === 参数（Request Body JSON）===
    - paths: list[str]（必填）
      - 说明：要保留的路径集合。与 match 联合使用。
      - 示例：
        - 精确：["/v1/system/status"]
        - 前缀：["/api/v1/"]
        - 通配：["/api/v1/*", "/v1/datasets/*"]
        - 正则：["^/api/v1/(files|datasets)/.*$"]

    - match: "exact" | "prefix" | "glob" | "regex"（默认："exact"）
      - exact：路径等值匹配
      - prefix：路径前缀匹配
      - glob：支持 * ? [...] 通配（同 fnmatch）
      - regex：Python 正则匹配

    - include_tags: list[str]（可选，默认：[]）
      - 说明：进一步按 Operation.tags 过滤，保留至少命中任一 tag 的路径
      - 注意：在 paths 初筛之后执行

    - exclude_paths: list[str]（可选，默认：[]）
      - 说明：结合 match 从结果中排除命中的路径

    - exclude_tags: list[str]（可选，默认：[]）
      - 说明：命中任一 tag 的 Operation 将被排除

    - strict: bool（默认：true）
      - true：闭包阶段遇到缺失/无效 $ref 直接报错（422/400）
      - false：记录到 x-filter-warnings 并继续

    - prune_examples: bool（默认：false）
      - true：递归删除 example/examples 与 components.examples，降低泄敏风险
      - false：保留示例数据

    - oas_version_target: "keep" | "3.0" | "3.1"（默认："keep"）
      - keep：保持源文档版本
      - 3.0/3.1：仅写出目标版本号（当前不做语义转换，推荐先 keep）

    - source: str（可选，外部 openapi.json URL）
      - 说明：不提供时使用本服务自身 OpenAPI；提供时将从外部拉取再过滤
      - 安全（SSRF）限制：
        - 仅允许 http/https；拒绝 file://、ftp://、gopher://、data:
        - 拒绝内网域/CIDR：10/8, 172.16-31/12, 192.168/16, 127/8, localhost, 0.0.0.0
        - 文档大小上限 10MB；Content-Type 需为 json/yaml；connect=3s, read=10s, write=5s；最多 3 次重定向

    - max_depth: int（默认：10，范围：1-50）
      - 说明：递归扫描 $ref 与闭包 BFS 的最大深度，防止极端循环

    === 常用示例 ===
    1) 仅保留 SDK（/api/v1/*）：
    {
      "paths": ["/api/v1/*"],
      "match": "glob",
      "strict": true,
      "prune_examples": true,
      "oas_version_target": "keep"
    }

    2) 精确筛选多个模块（注意复数路径名）：
    {
      "paths": [
        "/api/v1/chats/*",
        "/api/v1/chats_openai/*",
        "/api/v1/sessions/*",
        "/api/v1/datasets/*",
        "/api/v1/files/*",
        "/api/v1/agents/*",
        "/api/v1/agents_openai/*",
        "/api/v1/agentbots/*"
      ],
      "match": "glob",
      "strict": true
    }

    3) 搭配 Tag 过滤、排除内部接口：
    {
      "paths": ["/api/v1/*"],
      "match": "glob",
      "include_tags": ["api", "file"],
      "exclude_tags": ["internal", "admin"],
      "strict": false
    }

    === 响应结构 ===
    - 标准 OpenAPI 顶层：openapi, info, servers, tags, security, paths, components
      - components 已裁剪为最小闭包集合（schemas/responses/parameters/examples/requestBodies/headers/links/callbacks/pathItems）
    - x-filter-warnings: list
      - 当 strict=false 时记录未解析 $ref、非法正则等警告
    - x-filter-meta: object
      - rules：本次规则摘要
      - sourceETag：上游 OpenAPI 的 ETag（如有）
      - generatedAt / processingTimeMs：生成时间与耗时
      - pathsBefore/After, componentsBefore/After：规模变化

    === 响应头 ===
    - ETag：结果文档弱 ETag（便于客户端缓存）
    - Cache-Control：private, max-age=300
    - X-Filter-Cache-Key：服务端缓存键
    - X-Processing-Time-Ms：处理耗时
    - X-Source-ETag：上游文档 ETag（如有）

    === 错误代码 ===
    - 422：规则校验失败（Pydantic）
    - 400：过滤参数/逻辑错误（非法正则、SSRF 拒绝等）
    - 502：外部文档拉取失败（网络/格式错误）
    - 500：服务内部错误

    === 行为说明 ===
    - 路径匹配先于 tag 过滤；最终仅保留同时满足 paths 且未命中 exclude 的 Operation。
    - 闭包：从保留的 PathItem/Operation 递归收集 $ref，自动补种顶层 security 的 securitySchemes；BFS 扩展直至稳定。
    - 稳定排序：对 paths 与各 components.* 子键进行字典序排序，便于 diff 与缓存命中。
    """
    try:
        # 执行过滤
        result = await openapi_filter_service.filter_openapi(rule, global_app)

        # 构建响应
        response_data = FilterResponse(
            openapi=result.filtered_doc.get("openapi", "3.0.3"),
            info=result.filtered_doc.get("info", {}),
            servers=result.filtered_doc.get("servers"),
            tags=result.filtered_doc.get("tags"),
            security=result.filtered_doc.get("security"),
            paths=result.filtered_doc.get("paths", {}),
            components=result.filtered_doc.get("components"),
            external_docs=result.filtered_doc.get("externalDocs"),
            x_filter_warnings=result.warnings,
            x_filter_meta=result.meta
        )

        # 设置响应头
        headers = {
            "ETag": result.etag,
            "Cache-Control": "private, max-age=300",
            "X-Filter-Cache-Key": result.cache_key,
            "X-Processing-Time-Ms": str(result.meta.processing_time_ms)
        }

        if result.meta.source_etag:
            headers["X-Source-ETag"] = result.meta.source_etag

        return JSONResponse(
            content=response_data.model_dump(by_alias=True, exclude_none=True),
            headers=headers
        )

    except ValidationError as e:
        logger.warning(f"过滤规则验证失败: {e}")
        return get_data_error_result(
            retcode=422,
            retmsg="过滤规则验证失败"
        )

    except ValueError as e:
        logger.warning(f"过滤处理失败: {e}")
        return get_data_error_result(
            retcode=400,
            retmsg=str(e)
        )

    except Exception as e:
        logger.error(f"过滤服务异常: {e}", exc_info=True)
        return server_error_response(e)


@router.get("/openapi-filtered",
            summary="过滤OpenAPI文档 (GET)",
            response_description="返回过滤后的OpenAPI文档",
            response_model=Dict[str, Any])
async def filter_openapi_get(
        paths: list[str] = Query(..., description="要保留的路径列表"),
        match: str = Query("exact", description="匹配模式", pattern="^(exact|prefix|glob|regex)$"),
        source: str | None = Query(None, description="外部OpenAPI文档URL"),
        include_tags: list[str] = Query(default=[], description="包含标签"),
        exclude_paths: list[str] = Query(default=[], description="排除路径"),
        exclude_tags: list[str] = Query(default=[], description="排除标签"),
        strict: bool = Query(True, description="严格模式"),
        prune_examples: bool = Query(False, description="删除示例"),
        request: Request = None,
        user=Depends(manager)
):
    """
    GET方式过滤OpenAPI文档（便于调试）

    **注意**: 生产环境建议使用POST接口，避免URL长度限制

    ## 查询参数

    - **paths**: 要保留的路径，可重复指定多个
    - **match**: 匹配模式 (exact/prefix/glob/regex)
    - **source**: 可选的外部OpenAPI文档URL
    - **include_tags**: 包含标签，可重复指定
    - **exclude_paths**: 排除路径，可重复指定
    - **exclude_tags**: 排除标签，可重复指定
    - **strict**: 是否启用严格模式
    - **prune_examples**: 是否删除示例

    ## 使用示例

    ```
    GET /openapi-filtered?paths=/users&paths=/items/{id}&match=exact&strict=true
    ```
    """
    try:
        # 构建过滤规则
        rule_data = {
            "paths": paths,
            "match": match,
            "include_tags": include_tags,
            "exclude_paths": exclude_paths,
            "exclude_tags": exclude_tags,
            "strict": strict,
            "prune_examples": prune_examples
        }

        if source:
            rule_data["source"] = source

        rule = FilterRule(**rule_data)

        # 调用POST接口的逻辑
        result = await openapi_filter_service.filter_openapi(rule, global_app)

        # 构建响应
        response_data = FilterResponse(
            openapi=result.filtered_doc.get("openapi", "3.0.3"),
            info=result.filtered_doc.get("info", {}),
            servers=result.filtered_doc.get("servers"),
            tags=result.filtered_doc.get("tags"),
            security=result.filtered_doc.get("security"),
            paths=result.filtered_doc.get("paths", {}),
            components=result.filtered_doc.get("components"),
            external_docs=result.filtered_doc.get("externalDocs"),
            x_filter_warnings=result.warnings,
            x_filter_meta=result.meta
        )

        # 设置响应头
        headers = {
            "ETag": result.etag,
            "Cache-Control": "private, max-age=300",
            "X-Filter-Cache-Key": result.cache_key,
            "X-Processing-Time-Ms": str(result.meta.processing_time_ms)
        }

        if result.meta.source_etag:
            headers["X-Source-ETag"] = result.meta.source_etag

        return JSONResponse(
            content=response_data.model_dump(by_alias=True, exclude_none=True),
            headers=headers
        )

    except ValidationError as e:
        logger.warning(f"过滤规则验证失败: {e}")
        return get_data_error_result(
            retcode=422,
            retmsg="过滤规则验证失败",
        )

    except ValueError as e:
        logger.warning(f"过滤处理失败: {e}")
        return get_data_error_result(
            retcode=400,
            retmsg=str(e)
        )

    except Exception as e:
        logger.error(f"过滤服务异常: {e}", exc_info=True)
        return server_error_response(e)


@router.get("/openapi-filter-health",
            summary="OpenAPI过滤服务健康检查",
            response_description="返回服务健康状态")
def filter_service_health():
    """
    OpenAPI过滤服务健康检查

    检查服务的各个组件是否正常工作：
    - Redis连接状态
    - HTTP客户端状态
    - 基础功能验证

    返回详细的健康状态信息
    """
    try:
        health_status = {
            "status": "healthy",
            "timestamp": FilterMeta(
                rules={},
                generated_at="",
                processing_time_ms=0.0,
                components_before=0,
                components_after=0,
                paths_before=0,
                paths_after=0
            ).generated_at,
            "components": {}
        }

        # 检查Redis连接
        try:
            from core.utils.redis_conn import REDIS_CONN
            if REDIS_CONN.health():  # 使用正确的health方法
                health_status["components"]["redis"] = {"status": "healthy"}
            else:
                health_status["components"]["redis"] = {
                    "status": "unhealthy",
                    "error": "health check failed"
                }
                health_status["status"] = "unhealthy"
        except Exception as e:
            health_status["components"]["redis"] = {
                "status": "unhealthy",
                "error": str(e)
            }
            health_status["status"] = "unhealthy"

        # 检查HTTP客户端
        try:
            client = openapi_filter_service.get_http_client()
            health_status["components"]["http_client"] = {
                "status": "healthy",
                "is_closed": client.is_closed
            }
        except Exception as e:
            health_status["components"]["http_client"] = {
                "status": "unhealthy",
                "error": str(e)
            }
            health_status["status"] = "unhealthy"

        # 基础功能测试
        try:
            from api.utils.openapi_filter_utils import generate_cache_key
            test_key = generate_cache_key({"test": True})
            if len(test_key) == 32:
                health_status["components"]["utils"] = {"status": "healthy"}
            else:
                raise ValueError("缓存键生成异常")
        except Exception as e:
            health_status["components"]["utils"] = {
                "status": "unhealthy",
                "error": str(e)
            }
            health_status["status"] = "unhealthy"

        return get_json_result(data=health_status)

    except Exception as e:
        logger.error(f"健康检查失败: {e}", exc_info=True)
        return get_data_error_result(
            retcode=500,
            retmsg=f"健康检查失败: {str(e)}"
        )


@router.post("/openapi-filter-validate",
             summary="验证OpenAPI过滤规则",
             response_description="返回规则验证结果")
def validate_filter_rule(
        rule: FilterRule = Body(..., description="要验证的过滤规则"),
        user=Depends(manager)
):
    """
    验证OpenAPI过滤规则的有效性

    在实际执行过滤之前，可以使用此接口验证规则配置是否正确：
    - 路径格式检查
    - URL安全性验证（如果指定了外部源）
    - 正则表达式语法验证
    - 参数范围检查

    返回详细的验证结果和建议
    """
    try:
        validation_result = {
            "valid": True,
            "warnings": [],
            "suggestions": []
        }

        # 验证路径格式
        for path in rule.paths:
            if not path.startswith("/"):
                validation_result["warnings"].append({
                    "type": "path_format",
                    "message": f"路径建议以'/'开头: {path}",
                    "path": path
                })

        # 验证正则表达式（如果使用regex模式）
        if rule.match.value == "regex":
            import re
            for pattern in rule.paths + rule.exclude_paths:
                try:
                    re.compile(pattern)
                except re.error as e:
                    validation_result["valid"] = False
                    validation_result["warnings"].append({
                        "type": "invalid_regex",
                        "message": f"无效的正则表达式 '{pattern}': {e}",
                        "path": pattern
                    })

        # 检查路径重叠
        if rule.exclude_paths:
            overlaps = set(rule.paths) & set(rule.exclude_paths)
            if overlaps:
                validation_result["warnings"].append({
                    "type": "path_overlap",
                    "message": f"包含和排除路径有重叠: {list(overlaps)}",
                    "paths": list(overlaps)
                })

        # 检查标签重叠
        if rule.exclude_tags:
            overlaps = set(rule.include_tags) & set(rule.exclude_tags)
            if overlaps:
                validation_result["warnings"].append({
                    "type": "tag_overlap",
                    "message": f"包含和排除标签有重叠: {list(overlaps)}",
                    "tags": list(overlaps)
                })

        # 性能建议
        if len(rule.paths) > 100:
            validation_result["suggestions"].append({
                "type": "performance",
                "message": "路径数量较多，建议考虑使用通配符模式或分批处理"
            })

        if rule.max_depth > 20:
            validation_result["suggestions"].append({
                "type": "performance",
                "message": "递归深度设置较高，可能影响性能"
            })

        # 安全建议
        if rule.source and not rule.strict:
            validation_result["suggestions"].append({
                "type": "security",
                "message": "使用外部源时建议启用严格模式以确保引用完整性"
            })

        return get_json_result(data=validation_result)

    except ValidationError as e:
        return get_data_error_result(
            retcode=422,
            retmsg="规则验证失败"
        )

    except Exception as e:
        logger.error(f"规则验证异常: {e}", exc_info=True)
        return server_error_response(e)


# 应用生命周期管理
async def cleanup_openapi_filter_service():
    """清理OpenAPI过滤服务资源"""
    try:
        await openapi_filter_service.close()
        logger.info("OpenAPI过滤服务已清理")
    except Exception as e:
        logger.error(f"清理OpenAPI过滤服务失败: {e}")

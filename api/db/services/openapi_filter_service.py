"""
@project: multirag
@Author：龙
@file： openapi_filter_service.py
@date：2024/12/10 10:00
@desc: OpenAPI 过滤服务核心实现
"""
import copy
import json
import logging
from datetime import datetime
from typing import Any

import httpx
from fastapi.openapi.utils import get_openapi

from api.utils.openapi_filter_utils import (
    PerformanceTimer,
    build_components_closure,
    collect_refs,
    compute_components_stats,
    extract_global_security_refs,
    filter_by_tags,
    generate_cache_key,
    generate_etag,
    is_safe_url,
    match_paths,
    prune_examples,
    validate_openapi_structure,
)
from core.utils.redis_conn import REDIS_CONN

from .openapi_filter_models import FilterMeta, FilterResult, FilterRule, FilterWarning, OASVersionTarget

logger = logging.getLogger(__name__)


class OpenApiFilterService:
    """OpenAPI 过滤服务核心类"""

    def __init__(self, cache_ttl: int = 3600):
        """
        初始化过滤服务

        Args:
            cache_ttl: 缓存TTL（秒）
        """
        self.cache_ttl = cache_ttl
        self._client = None

    def get_http_client(self) -> httpx.AsyncClient:
        """获取HTTP客户端（懒加载）"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=3.0,
                    read=10.0,
                    write=5.0,
                    pool=10.0
                ),
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=20
                ),
                follow_redirects=True,
                max_redirects=3
            )
        return self._client

    async def close(self):
        """关闭HTTP客户端"""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def filter_openapi(self, rule: FilterRule, app=None) -> FilterResult:
        """
        根据规则过滤 OpenAPI 文档

        Args:
            rule: 过滤规则
            app: FastAPI 应用实例（用于获取本地 OpenAPI）

        Returns:
            FilterResult: 过滤结果
        """
        timer = PerformanceTimer()
        timer.start()

        try:
            # 生成缓存键
            rule_dict = rule.model_dump()
            cache_key = generate_cache_key(rule_dict)

            # 尝试从缓存获取
            cached_result = await self._get_from_cache(cache_key)
            if cached_result:
                timer.stop()  # 停止计时器
                logger.info(f"从缓存获取过滤结果: {cache_key}，耗时: {timer.elapsed_ms:.1f}ms")
                return cached_result

            # 获取源文档
            source_doc, source_etag = await self._fetch_source_document(rule, app)

            # 重新计算缓存键（包含源ETag）
            cache_key = generate_cache_key(rule_dict, source_etag)
            cached_result = await self._get_from_cache(cache_key)
            if cached_result:
                timer.stop()  # 停止计时器
                logger.info(f"从缓存获取过滤结果（含ETag）: {cache_key}，耗时: {timer.elapsed_ms:.1f}ms")
                return cached_result

            # 执行过滤
            filtered_doc, warnings = await self._apply_filter(source_doc, rule)

            timer.stop()

            # 计算统计信息
            source_stats = compute_components_stats(source_doc.get("components"))
            filtered_stats = compute_components_stats(filtered_doc.get("components"))

            # 构建元信息
            meta = FilterMeta(
                rules={
                    "paths": rule.paths,
                    "match": rule.match.value,
                    "include_tags": rule.include_tags,
                    "exclude_paths": rule.exclude_paths,
                    "exclude_tags": rule.exclude_tags,
                    "strict": rule.strict,
                    "prune_examples": rule.prune_examples,
                    "oas_version_target": rule.oas_version_target.value
                },
                source_etag=source_etag,
                generated_at=datetime.now().isoformat(),
                processing_time_ms=timer.elapsed_ms,
                components_before=source_stats.total,
                components_after=filtered_stats.total,
                paths_before=len(source_doc.get("paths", {})),
                paths_after=len(filtered_doc.get("paths", {}))
            )

            # 生成结果ETag
            result_etag = generate_etag(filtered_doc)

            result = FilterResult(
                filtered_doc=filtered_doc,
                warnings=warnings,
                meta=meta,
                cache_key=cache_key,
                etag=result_etag
            )

            # 缓存结果
            await self._save_to_cache(cache_key, result)

            logger.info(f"完成过滤处理，耗时: {timer.elapsed_ms:.1f}ms，"
                       f"路径: {meta.paths_before} -> {meta.paths_after}，"
                       f"组件: {meta.components_before} -> {meta.components_after}")

            return result

        except Exception as e:
            timer.stop()
            logger.error(f"过滤处理失败，耗时: {timer.elapsed_ms:.1f}ms，错误: {e}")
            raise

    async def _fetch_source_document(self, rule: FilterRule, app=None) -> tuple[dict[str, Any], str | None]:
        """
        获取源 OpenAPI 文档

        Args:
            rule: 过滤规则
            app: FastAPI 应用实例

        Returns:
            Tuple: (文档内容, ETag)
        """
        if rule.source:
            # 从外部URL获取
            return await self._fetch_external_document(str(rule.source))
        else:
            # 从本地应用获取
            return await self._fetch_local_document(app)

    async def _fetch_external_document(self, url: str) -> tuple[dict[str, Any], str | None]:
        """
        从外部URL获取OpenAPI文档

        Args:
            url: 外部URL

        Returns:
            Tuple: (文档内容, ETag)
        """
        if not is_safe_url(url):
            raise ValueError(f"不安全的URL: {url}")

        logger.info(f"从外部URL获取OpenAPI文档: {url}")

        client = self.get_http_client()

        try:
            response = await client.get(
                url,
                headers={
                    "Accept": "application/json, application/x-yaml, text/yaml",
                    "User-Agent": "MultiRAG-OpenAPI-Filter/1.0"
                }
            )
            response.raise_for_status()

            # 检查内容类型和大小
            content_type = response.headers.get("content-type", "").lower()
            content_length = len(response.content)

            if content_length > 10 * 1024 * 1024:  # 10MB 限制
                raise ValueError(f"文档过大: {content_length} 字节")

            if not any(ct in content_type for ct in ["json", "yaml", "yml"]):
                logger.warning(f"可能的非标准内容类型: {content_type}")

            # 尝试解析JSON
            try:
                doc = response.json()
            except json.JSONDecodeError:
                # 尝试解析YAML
                try:
                    import yaml
                    doc = yaml.safe_load(response.text)
                except ImportError:
                    raise ValueError("无法解析YAML格式，请安装PyYAML")
                except yaml.YAMLError as e:
                    raise ValueError(f"无法解析文档格式: {e}")

            if not isinstance(doc, dict):
                raise ValueError("文档格式无效，应为JSON对象")

            # 获取ETag
            etag = response.headers.get("etag")

            logger.info(f"成功获取外部文档，大小: {content_length} 字节")
            return doc, etag

        except httpx.RequestError as e:
            raise ValueError(f"请求外部文档失败: {e}")
        except httpx.HTTPStatusError as e:
            raise ValueError(f"获取外部文档失败 (HTTP {e.response.status_code}): {e}")

    async def _fetch_local_document(self, app=None) -> tuple[dict[str, Any], str | None]:
        """
        从本地应用获取OpenAPI文档

        Args:
            app: FastAPI 应用实例

        Returns:
            Tuple: (文档内容, ETag)
        """
        logger.info("从本地应用获取OpenAPI文档")

        if app is None:
            # 尝试从全局导入获取应用
            try:
                from api.apps import app as global_app
                app = global_app
            except ImportError:
                raise ValueError("无法获取FastAPI应用实例")

        if not hasattr(app, 'openapi_schema'):
            raise ValueError("应用没有openapi_schema属性")

        # 获取OpenAPI schema
        if app.openapi_schema:
            doc = app.openapi_schema
        else:
            doc = get_openapi(
                title=app.title,
                version=app.version,
                description=app.description,
                routes=app.routes,
                tags=app.openapi_tags
            )
            app.openapi_schema = doc

        if not isinstance(doc, dict):
            raise ValueError("本地OpenAPI文档格式无效")

        logger.info("成功获取本地OpenAPI文档")
        return doc, None  # 本地文档没有ETag

    async def _apply_filter(self, source_doc: dict[str, Any], rule: FilterRule) -> tuple[dict[str, Any], list[FilterWarning]]:
        """
        应用过滤规则

        Args:
            source_doc: 源文档
            rule: 过滤规则

        Returns:
            Tuple: (过滤后的文档, 警告列表)
        """
        warnings = []

        # 验证源文档结构
        structure_warnings = validate_openapi_structure(source_doc)
        warnings.extend(structure_warnings)

        # 深拷贝源文档
        filtered_doc = copy.deepcopy(source_doc)

        # Phase A: 筛选路径
        original_paths = filtered_doc.get("paths", {})

        # 1. 根据路径模式过滤
        matched_paths = match_paths(
            original_paths,
            rule.paths,
            rule.exclude_paths,
            rule.match
        )

        # 2. 根据标签过滤
        if rule.include_tags or rule.exclude_tags:
            matched_paths = filter_by_tags(
                matched_paths,
                rule.include_tags,
                rule.exclude_tags
            )

        filtered_doc["paths"] = matched_paths

        if not matched_paths:
            warnings.append(FilterWarning(
                type="no_paths_matched",
                message="没有路径匹配过滤条件",
                path="paths"
            ))

        # Phase B: 收集初始引用种子
        seed_refs = set()

        # 从保留的路径收集引用
        for path_key, path_item in matched_paths.items():
            refs = collect_refs(path_item, f"paths.{path_key}", max_depth=rule.max_depth)
            for ref_info in refs:
                if ref_info.ref.startswith("#/"):  # 只处理内部引用
                    seed_refs.add(ref_info.ref)

        # Phase D: 补充全局有效引用
        global_security_refs = extract_global_security_refs(filtered_doc)
        seed_refs.update(global_security_refs)

        logger.info(f"收集到 {len(seed_refs)} 个种子引用")

        # Phase C: 构建闭包
        components = source_doc.get("components", {})
        if seed_refs and components:
            try:
                closure_refs, closure_warnings = build_components_closure(
                    components, seed_refs, rule.strict, rule.max_depth
                )
                warnings.extend(closure_warnings)

                logger.info(f"闭包扩展后共 {len(closure_refs)} 个引用")

                # Phase E: 裁剪components
                filtered_components = self._build_filtered_components(
                    components, closure_refs, rule.prune_examples
                )
                filtered_doc["components"] = filtered_components

            except ValueError as e:
                if rule.strict:
                    raise
                else:
                    warnings.append(FilterWarning(
                        type="closure_error",
                        message=str(e),
                        path="components"
                    ))
                    # 严格模式失败时，保留原始components
                    logger.warning(f"闭包构建失败，保留原始components: {e}")

        # Phase F: 规范化与排序
        filtered_doc = self._normalize_document(filtered_doc, rule.oas_version_target)

        logger.info(f"过滤完成，生成 {len(warnings)} 个警告")
        return filtered_doc, warnings

    def _build_filtered_components(self, components: dict[str, Any],
                                 closure_refs: set[str], should_prune_examples: bool) -> dict[str, Any]:
        """
        构建过滤后的components

        Args:
            components: 原始components
            closure_refs: 闭包引用集合
            should_prune_examples: 是否删除示例

        Returns:
            Dict[str, Any]: 过滤后的components
        """
        filtered_components = {}

        # 按引用类型分组
        ref_groups = {}
        for ref in closure_refs:
            if ref.startswith("#/components/"):
                path_parts = ref.split("/")
                if len(path_parts) >= 4:
                    component_type = path_parts[2]
                    component_name = path_parts[3]

                    if component_type not in ref_groups:
                        ref_groups[component_type] = set()
                    ref_groups[component_type].add(component_name)

        # 构建过滤后的各个组件类型
        for component_type, component_names in ref_groups.items():
            if component_type in components:
                source_section = components[component_type]
                filtered_section = {}

                for component_name in component_names:
                    if component_name in source_section:
                        component_obj = copy.deepcopy(source_section[component_name])

                        # 删除示例（如果启用）
                        if should_prune_examples:
                            component_obj = prune_examples(component_obj)

                        filtered_section[component_name] = component_obj

                if filtered_section:
                    filtered_components[component_type] = dict(sorted(filtered_section.items()))

        return filtered_components

    def _normalize_document(self, doc: dict[str, Any], version_target: OASVersionTarget) -> dict[str, Any]:
        """
        规范化文档

        Args:
            doc: 文档
            version_target: 目标版本

        Returns:
            Dict[str, Any]: 规范化后的文档
        """
        # 排序顶级键
        ordered_keys = [
            "openapi", "info", "servers", "tags", "security",
            "paths", "components", "externalDocs"
        ]

        normalized = {}

        # 按顺序添加存在的键
        for key in ordered_keys:
            if key in doc:
                value = doc[key]

                # 对特定结构进行排序
                if key == "paths" and isinstance(value, dict):
                    normalized[key] = dict(sorted(value.items()))
                elif key == "components" and isinstance(value, dict):
                    sorted_components = {}
                    for comp_type in sorted(value.keys()):
                        if isinstance(value[comp_type], dict):
                            sorted_components[comp_type] = dict(sorted(value[comp_type].items()))
                        else:
                            sorted_components[comp_type] = value[comp_type]
                    normalized[key] = sorted_components
                else:
                    normalized[key] = value

        # 添加剩余的键
        for key in doc:
            if key not in normalized:
                normalized[key] = doc[key]

        # 版本转换（如果需要）
        if version_target != OASVersionTarget.KEEP:
            normalized = self._convert_version(normalized, version_target)

        return normalized

    def _convert_version(self, doc: dict[str, Any], target_version: OASVersionTarget) -> dict[str, Any]:
        """
        转换OpenAPI版本（占位符实现）

        Args:
            doc: 文档
            target_version: 目标版本

        Returns:
            Dict[str, Any]: 转换后的文档
        """
        # TODO: 实现版本转换逻辑
        # 这是一个复杂的功能，需要深入理解OAS 3.0和3.1的差异
        logger.warning(f"版本转换功能尚未完全实现: {target_version}")
        return doc

    async def _get_from_cache(self, cache_key: str) -> FilterResult | None:
        """从缓存获取结果"""
        try:
            cached_data = REDIS_CONN.get(f"openapi_filter:{cache_key}")
            if cached_data:
                data = json.loads(cached_data)
                return FilterResult(**data)
        except Exception as e:
            logger.warning(f"缓存读取失败: {e}")
        return None

    async def _save_to_cache(self, cache_key: str, result: FilterResult):
        """保存结果到缓存"""
        try:
            data = result.model_dump(mode='json')
            REDIS_CONN.set(
                f"openapi_filter:{cache_key}",
                json.dumps(data, ensure_ascii=False),
                self.cache_ttl
            )
            logger.info(f"结果已缓存: {cache_key}")
        except Exception as e:
            logger.warning(f"缓存保存失败: {e}")


# 全局服务实例
openapi_filter_service = OpenApiFilterService()

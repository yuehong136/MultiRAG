# coding=utf-8
"""
@project: multirag
@Author：龙
@file： openapi_filter_utils.py
@date：2024/12/10 10:00
@desc: OpenAPI 过滤服务工具函数
"""
import re
import fnmatch
import hashlib
import json
import logging
from typing import Any, Dict, Set, Tuple, Deque
from collections import deque
import time
from urllib.parse import urlparse

from api.db.services.openapi_filter_models import MatchMode, RefInfo, ComponentStats, FilterWarning

logger = logging.getLogger(__name__)


def collect_refs(node: Any, path: str = "", depth: int = 0, max_depth: int = 10) -> Set[RefInfo]:
    """
    递归收集JSON对象中的所有 $ref 引用
    
    Args:
        node: 要扫描的JSON节点
        path: 当前JSON路径
        depth: 当前递归深度
        max_depth: 最大递归深度
    
    Returns:
        Set[RefInfo]: 收集到的 $ref 引用集合
    """
    if depth > max_depth:
        logger.warning(f"达到最大递归深度 {max_depth}，停止扫描路径: {path}")
        return set()
    
    refs = set()
    
    if isinstance(node, dict):
        # 检查是否有 $ref 字段
        if "$ref" in node:
            ref_value = node["$ref"]
            if isinstance(ref_value, str):
                refs.add(RefInfo(ref=ref_value, source_path=path, depth=depth))
        
        # 递归扫描所有字段
        for key, value in node.items():
            if key != "$ref":  # 避免重复处理 $ref 值
                child_path = f"{path}.{key}" if path else key
                refs.update(collect_refs(value, child_path, depth + 1, max_depth))
    
    elif isinstance(node, list):
        # 递归扫描数组元素
        for i, item in enumerate(node):
            child_path = f"{path}[{i}]" if path else f"[{i}]"
            refs.update(collect_refs(item, child_path, depth + 1, max_depth))
    
    return refs


def parse_ref(ref: str) -> Tuple[str | None, str | None, str | None]:
    """
    解析 $ref 引用
    
    Args:
        ref: $ref 值，如 "#/components/schemas/User" 或 "external.json#/schemas/User"
    
    Returns:
        Tuple: (文档名, 组件类型, 组件名)
        例如: (None, "schemas", "User") 或 ("external.json", "schemas", "User")
    """
    if not ref or not isinstance(ref, str):
        return None, None, None
    
    # 分离外部文档引用和内部路径
    if "#" in ref:
        external_doc, internal_path = ref.split("#", 1)
        external_doc = external_doc if external_doc else None
    else:
        external_doc = ref
        internal_path = ""
    
    # 解析内部路径
    if internal_path.startswith("/components/"):
        path_parts = internal_path.strip("/").split("/")
        if len(path_parts) >= 3:
            # /components/schemas/User -> ["components", "schemas", "User"]
            component_type = path_parts[1]
            component_name = path_parts[2]
            return external_doc, component_type, component_name
    
    return external_doc, None, None


def is_internal_ref(ref: str) -> bool:
    """检查是否为内部引用（以 # 开头）"""
    return ref.startswith("#/")


def match_paths(paths: Dict[str, Any], include_patterns: list[str], 
               exclude_patterns: list[str], match_mode: MatchMode) -> Dict[str, Any]:
    """
    根据模式匹配路径
    
    Args:
        paths: 原始路径字典
        include_patterns: 包含模式列表
        exclude_patterns: 排除模式列表  
        match_mode: 匹配模式
    
    Returns:
        Dict[str, Any]: 匹配的路径字典
    """
    if not paths:
        return {}
    
    matched_paths = {}
    
    for path_key, path_item in paths.items():
        # 检查是否匹配包含模式
        include_matched = False
        for pattern in include_patterns:
            if _path_matches(path_key, pattern, match_mode):
                include_matched = True
                break
        
        if not include_matched:
            continue
        
        # 检查是否匹配排除模式
        exclude_matched = False
        for pattern in exclude_patterns:
            if _path_matches(path_key, pattern, match_mode):
                exclude_matched = True
                break
        
        if not exclude_matched:
            matched_paths[path_key] = path_item
    
    return matched_paths


def _path_matches(path: str, pattern: str, match_mode: MatchMode) -> bool:
    """检查路径是否匹配模式"""
    match match_mode:
        case MatchMode.EXACT:
            return path == pattern
        case MatchMode.PREFIX:
            return path.startswith(pattern)
        case MatchMode.GLOB:
            return fnmatch.fnmatch(path, pattern)
        case MatchMode.REGEX:
            try:
                return bool(re.match(pattern, path))
            except re.error as e:
                logger.warning(f"无效的正则表达式模式 '{pattern}': {e}")
                return False
        case _:
            return False


def filter_by_tags(paths: Dict[str, Any], include_tags: list[str], exclude_tags: list[str]) -> Dict[str, Any]:
    """
    根据标签过滤路径
    
    Args:
        paths: 路径字典
        include_tags: 要包含的标签
        exclude_tags: 要排除的标签
    
    Returns:
        Dict[str, Any]: 过滤后的路径字典
    """
    if not include_tags and not exclude_tags:
        return paths
    
    filtered_paths = {}
    
    for path_key, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        
        path_tags = set()
        
        # 收集路径中所有操作的标签
        for method in ["get", "post", "put", "delete", "options", "head", "patch", "trace"]:
            if method in path_item:
                operation = path_item[method]
                if isinstance(operation, dict) and "tags" in operation:
                    if isinstance(operation["tags"], list):
                        path_tags.update(operation["tags"])
        
        # 检查包含标签
        if include_tags:
            if not any(tag in path_tags for tag in include_tags):
                continue
        
        # 检查排除标签
        if exclude_tags:
            if any(tag in path_tags for tag in exclude_tags):
                continue
        
        filtered_paths[path_key] = path_item
    
    return filtered_paths


def build_components_closure(components: Dict[str, Any], seed_refs: Set[str], 
                           strict: bool = True, max_depth: int = 10) -> Tuple[Set[str], list[FilterWarning]]:
    """
    构建组件引用闭包
    
    Args:
        components: 组件字典
        seed_refs: 种子引用集合
        strict: 严格模式
        max_depth: 最大递归深度
    
    Returns:
        Tuple: (闭包引用集合, 警告列表)
    """
    warnings = []
    closure_refs = set(seed_refs)
    
    # 使用 BFS 构建闭包
    queue: Deque[str] = deque(seed_refs)
    visited = set()
    depth = 0
    
    while queue and depth < max_depth:
        current_level_size = len(queue)
        
        for _ in range(current_level_size):
            ref = queue.popleft()
            
            if ref in visited:
                continue
            visited.add(ref)
            
            # 解析引用
            _, component_type, component_name = parse_ref(ref)
            
            if not component_type or not component_name:
                if strict:
                    raise ValueError(f"无效的 $ref 格式: {ref}")
                else:
                    warnings.append(FilterWarning(
                        type="invalid_ref",
                        message=f"无效的 $ref 格式: {ref}",
                        path=ref
                    ))
                    continue
            
            # 查找组件
            if component_type not in components:
                if strict:
                    raise ValueError(f"组件类型不存在: {component_type}")
                else:
                    warnings.append(FilterWarning(
                        type="missing_component_type",
                        message=f"组件类型不存在: {component_type}",
                        path=ref
                    ))
                    continue
            
            component_section = components[component_type]
            if component_name not in component_section:
                if strict:
                    raise ValueError(f"组件不存在: {ref}")
                else:
                    warnings.append(FilterWarning(
                        type="missing_component",
                        message=f"组件不存在: {ref}",
                        path=ref
                    ))
                    continue
            
            # 收集组件中的新引用
            component_obj = component_section[component_name]
            new_refs = collect_refs(component_obj, f"components.{component_type}.{component_name}", 0, max_depth)
            
            for ref_info in new_refs:
                new_ref = ref_info.ref
                if is_internal_ref(new_ref) and new_ref not in closure_refs:
                    closure_refs.add(new_ref)
                    queue.append(new_ref)
        
        depth += 1
    
    if depth >= max_depth:
        logger.warning(f"达到最大闭包深度 {max_depth}，可能存在循环引用")
    
    return closure_refs, warnings


def extract_global_security_refs(openapi_doc: Dict[str, Any]) -> Set[str]:
    """
    从全局 security 定义中提取 SecurityScheme 引用
    
    Args:
        openapi_doc: OpenAPI 文档
    
    Returns:
        Set[str]: SecurityScheme 引用集合
    """
    security_refs = set()
    
    # 检查顶层 security
    if "security" in openapi_doc and isinstance(openapi_doc["security"], list):
        for security_requirement in openapi_doc["security"]:
            if isinstance(security_requirement, dict):
                for scheme_name in security_requirement.keys():
                    # 构造对 securitySchemes 的引用
                    ref = f"#/components/securitySchemes/{scheme_name}"
                    security_refs.add(ref)
    
    return security_refs


def compute_components_stats(components: Dict[str, Any] | None) -> ComponentStats:
    """计算组件统计信息"""
    stats = ComponentStats()
    
    if not components:
        return stats
    
    # 计算各类型组件数量
    stats.schemas = len(components.get("schemas", {}))
    stats.responses = len(components.get("responses", {}))
    stats.parameters = len(components.get("parameters", {}))
    stats.examples = len(components.get("examples", {}))
    stats.request_bodies = len(components.get("requestBodies", {}))
    stats.headers = len(components.get("headers", {}))
    stats.security_schemes = len(components.get("securitySchemes", {}))
    stats.links = len(components.get("links", {}))
    stats.callbacks = len(components.get("callbacks", {}))
    stats.path_items = len(components.get("pathItems", {}))
    
    return stats


def generate_cache_key(rule_dict: Dict[str, Any], source_etag: str | None = None) -> str:
    """
    生成缓存键
    
    Args:
        rule_dict: 规则字典
        source_etag: 源文档 ETag
    
    Returns:
        str: 缓存键
    """
    # 标准化规则字典（排序确保一致性）
    normalized_rule = json.dumps(rule_dict, sort_keys=True, separators=(',', ':'))
    
    # 组合规则和源ETag
    cache_input = f"{normalized_rule}:{source_etag or 'local'}"
    
    # 计算SHA256哈希
    return hashlib.sha256(cache_input.encode('utf-8')).hexdigest()[:32]


def generate_etag(content: Dict[str, Any]) -> str:
    """
    为内容生成 ETag
    
    Args:
        content: 内容字典
    
    Returns:
        str: ETag 值
    """
    content_json = json.dumps(content, sort_keys=True, separators=(',', ':'))
    hash_value = hashlib.sha256(content_json.encode('utf-8')).hexdigest()
    return f'W/"{hash_value[:16]}"'


def prune_examples(component_obj: Any) -> Any:
    """
    递归删除示例数据
    
    Args:
        component_obj: 组件对象
    
    Returns:
        Any: 删除示例后的对象
    """
    if isinstance(component_obj, dict):
        pruned = {}
        for key, value in component_obj.items():
            if key in ["example", "examples"]:
                # 删除示例
                continue
            else:
                pruned[key] = prune_examples(value)
        return pruned
    elif isinstance(component_obj, list):
        return [prune_examples(item) for item in component_obj]
    else:
        return component_obj


def validate_openapi_structure(doc: Dict[str, Any]) -> list[FilterWarning]:
    """
    验证 OpenAPI 文档结构
    
    Args:
        doc: OpenAPI 文档
    
    Returns:
        List[FilterWarning]: 验证警告列表
    """
    warnings = []
    
    # 检查必需字段
    required_fields = ["openapi", "info", "paths"]
    for field in required_fields:
        if field not in doc:
            warnings.append(FilterWarning(
                type="missing_required_field",
                message=f"缺少必需字段: {field}",
                path=field
            ))
    
    # 检查版本格式
    if "openapi" in doc:
        version = doc["openapi"]
        if not isinstance(version, str) or not re.match(r"^3\.[01]\.\d+$", version):
            warnings.append(FilterWarning(
                type="invalid_version",
                message=f"无效的 OpenAPI 版本: {version}",
                path="openapi"
            ))
    
    # 检查路径格式
    if "paths" in doc and isinstance(doc["paths"], dict):
        for path_key in doc["paths"].keys():
            if not path_key.startswith("/"):
                warnings.append(FilterWarning(
                    type="invalid_path_format",
                    message=f"路径应以 '/' 开头: {path_key}",
                    path=f"paths.{path_key}"
                ))
    
    return warnings


def is_safe_url(url: str) -> bool:
    """
    检查URL是否安全（SSRF防护）
    
    Args:
        url: 要检查的URL
    
    Returns:
        bool: 是否安全
    """
    try:
        parsed = urlparse(url)
        
        # 只允许 http/https
        if parsed.scheme not in ['http', 'https']:
            return False
        
        host = parsed.hostname
        if not host:
            return False
        
        # 禁止内网地址
        internal_patterns = [
            r'^10\.',
            r'^172\.(1[6-9]|2[0-9]|3[01])\.',
            r'^192\.168\.',
            r'^127\.',
            r'^localhost$',
            r'^0\.0\.0\.0$'
        ]
        
        for pattern in internal_patterns:
            if re.match(pattern, host):
                return False
        
        return True
        
    except Exception:
        return False


class PerformanceTimer:
    """性能计时器"""
    
    def __init__(self):
        self.start_time = None
        self.end_time = None
    
    def start(self):
        self.start_time = time.perf_counter()
        return self
    
    def stop(self):
        if self.start_time is None:
            raise ValueError("Timer not started")
        self.end_time = time.perf_counter()
        return self
    
    @property
    def elapsed_ms(self) -> float:
        if self.start_time is None:
            raise ValueError("Timer not started")
        # 如果还没停止，使用当前时间计算
        end_time = self.end_time if self.end_time is not None else time.perf_counter()
        return (end_time - self.start_time) * 1000.0
    
    def __enter__(self):
        return self.start()
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

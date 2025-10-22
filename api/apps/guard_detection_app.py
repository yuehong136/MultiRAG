# coding=utf-8
"""
@project: multirag
@Author：龙
@file： guard_detection_app.py
@date：2025/01/31
@desc: 渐进式AI安全护栏检测接口 - 优先词库检测，预留完整流程兼容
"""
from __future__ import annotations
from typing import Any, List, Dict
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from api.apps import manager
from api.db.db_models import get_db
from api.db.services.ai_guard_engine_service import AiGuardEngineService
from api.db.services.guard_log_service import GuardLogService
from api.db.services.guard_service_service import GuardServiceService
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.utils.api_utils import get_json_result, server_error_response, get_data_error_result
from core.utils.storage_factory import STORAGE_IMPL
from core.app import naive
import xxhash

router = APIRouter()


class ProgressiveDetectionRequest(BaseModel):
    """渐进式检测请求模型"""
    service_id: str = Field(..., description="服务ID")
    content: str = Field(..., description="待检测的内容")
    request_id: str | None = Field(None, description="请求ID")
    chat_id: str | None = Field(None, description="会话ID")
    source_type: str | None = Field("api", description="来源类型")
    source_id: str | None = Field(None, description="来源ID")


class BatchContentItem(BaseModel):
    """批量检测内容项模型"""
    content: str = Field(..., description="待检测的内容")
    item_id: str | None = Field(None, description="内容项ID，用于标识和关联结果")


class BatchDetectionRequest(BaseModel):
    """批量检测请求模型"""
    service_id: str = Field(..., description="服务ID")
    items: List[BatchContentItem] = Field(..., description="待检测的内容列表", min_items=1, max_items=100)
    request_id: str | None = Field(None, description="请求ID")
    source_type: str | None = Field("batch_api", description="来源类型")
    source_id: str | None = Field(None, description="来源ID")


class DocumentItem(BaseModel):
    """批量文档检测文档项模型"""
    doc_id: str = Field(..., description="待检测的文档ID")
    max_chunks: int = Field(default=100, description="最大检测切片数量")
    doc_alias: str | None = Field(None, description="文档别名，用于标识和关联结果")


class BatchDocumentDetectionRequest(BaseModel):
    """批量文档安全检测请求模型"""
    service_id: str = Field(..., description="服务ID")
    documents: List[DocumentItem] = Field(..., description="待检测的文档列表", min_items=1, max_items=50)
    request_id: str | None = Field(None, description="请求ID")
    source_type: str | None = Field("batch_document_api", description="来源类型")
    source_id: str | None = Field(None, description="来源ID")


class DocumentGuardDetectionRequest(BaseModel):
    """文档安全检测请求模型"""
    service_id: str = Field(..., description="服务ID")
    doc_id: str = Field(..., description="待检测的文档ID")
    max_chunks: int = Field(default=100, description="最大检测切片数量")
    request_id: str | None = Field(None, description="请求ID")
    source_type: str | None = Field("document_api", description="来源类型")
    source_id: str | None = Field(None, description="来源ID")


class BatchDocumentResult(BaseModel):
    """批量文档检测单文档结果"""
    doc_id: str = Field(..., description="文档ID")
    doc_alias: str | None = Field(..., description="文档别名")
    doc_index: int = Field(..., description="文档索引")
    doc_name: str = Field(..., description="文档名称")
    total_chunks: int = Field(..., description="总切片数")
    checked_chunks: int = Field(..., description="已检测切片数")
    blocked_chunks: int = Field(..., description="被拦截切片数")
    overall_risk_score: float = Field(..., description="文档整体风险分数")
    overall_action: str = Field(..., description="文档整体处理动作")
    process_time_ms: int = Field(..., description="文档处理耗时(毫秒)")
    chunk_results: List[Dict[str, Any]] = Field(default_factory=list, description="切片检测结果")
    detection_summary: Dict[str, Any] = Field(default_factory=dict, description="检测汇总信息")
    error_message: str | None = Field(None, description="错误信息（如果检测失败）")


class BatchItemResult(BaseModel):
    """批量检测单项结果"""
    item_id: str | None = Field(..., description="内容项ID")
    item_index: int = Field(..., description="项目索引")
    content_preview: str = Field(..., description="内容预览（截断显示）")
    is_blocked: bool = Field(..., description="是否被拦截")
    risk_score: float = Field(..., description="风险分数")
    action: str = Field(..., description="处理动作")
    matched_items: List[Dict[str, Any]] = Field(default_factory=list, description="匹配项")
    risk_words: List[str] = Field(default_factory=list, description="风险词")
    process_time_ms: int = Field(..., description="单项处理耗时(毫秒)")
    detection_result: Dict[str, Any] = Field(default_factory=dict, description="完整检测结果")


class CreateServiceRequest(BaseModel):
    """创建检测服务请求模型"""
    code: str = Field(..., description="服务代码，唯一标识", min_length=1, max_length=100)
    name: str = Field(..., description="服务名称", min_length=1, max_length=200)
    description: str | None = Field(None, description="服务描述")
    service_type: str = Field("api", description="服务类型")
    enabled_dimensions: list[str] = Field(default_factory=list, description="启用的维度列表")
    enabled_labels: list[str] = Field(default_factory=list, description="启用的标签列表")
    policy_config: dict[str, Any] = Field(default_factory=dict, description="策略配置")
    cache_enabled: bool = Field(True, description="是否启用缓存")
    timeout_ms: int = Field(1000, description="超时时间（毫秒）", ge=100, le=30000)


class UpdateServiceRequest(BaseModel):
    """更新检测服务请求模型"""
    name: str | None = Field(None, description="服务名称", min_length=1, max_length=200)
    description: str | None = Field(None, description="服务描述")
    service_type: str | None = Field(None, description="服务类型")
    enabled_dimensions: list[str] | None = Field(None, description="启用的维度列表")
    enabled_labels: list[str] | None = Field(None, description="启用的标签列表")
    policy_config: dict[str, Any] | None = Field(None, description="策略配置")
    cache_enabled: bool | None = Field(None, description="是否启用缓存")
    timeout_ms: int | None = Field(None, description="超时时间（毫秒）", ge=100, le=30000)


class ChunkDetectionResult(BaseModel):
    """单个切片检测结果"""
    chunk_id: str = Field(..., description="切片唯一ID（基于内容哈希）")
    chunk_index: int = Field(..., description="切片索引")
    content: str = Field(..., description="切片内容（截断显示）")
    is_blocked: bool = Field(..., description="是否被拦截")
    risk_score: float = Field(..., description="风险分数")
    action: str = Field(..., description="处理动作")
    matched_items: List[Dict[str, Any]] = Field(default_factory=list, description="匹配项")
    risk_words: List[str] = Field(default_factory=list, description="风险词")


@router.post('/detect', summary="内容安全检测（渐进式实现）")
def detect_content_progressive(
    request: ProgressiveDetectionRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    ### POST `/detect` 内容安全检测
    
    **功能描述**:
    此接口基于服务ID对文本内容进行安全检测，采用渐进式实现策略。
    当前版本优先使用服务绑定的黑白名单词库进行检测，后续版本将无缝升级到完整的维度-标签-规则检测流程。
    支持链路追踪、会话关联等高级特性，检测结果自动记录日志。
    
    ---
    ### 请求体 (Request Body)
    | 字段          | 类型     | 必填 | 默认值 | 描述                           |
    |---------------|----------|------|--------|--------------------------------|
    | `service_id`  | `string` | 是   | -      | 服务ID，用于获取检测配置       |
    | `content`     | `string` | 是   | -      | 待检测的文本内容               |
    | `request_id`  | `string` | 否   | null   | 请求ID，用于链路追踪           |
    | `chat_id`     | `string` | 否   | null   | 会话ID，用于关联对话           |
    | `source_type` | `string` | 否   | "api"  | 来源类型                       |
    | `source_id`   | `string` | 否   | null   | 来源标识                       |
    
    **请求示例**:
    ```json
    {
        "service_id": "service_uuid_123",
        "content": "这是需要检测的文本内容",
        "request_id": "req_123456789",
        "chat_id": "chat_abc123",
        "source_type": "api",
        "source_id": "frontend_v1"
    }
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "code": 200,
        "data": {
            "is_blocked": false,
            "overall_risk_score": 15.0,
            "action": "pass",
            "process_time_ms": 120,
            "detection_mode": "library_only",
            "library_results": {
                "whitelist_matched": [
                    {
                        "library_id": "lib_001",
                        "library_name": "通用白名单",
                        "matched_words": ["正常词汇"],
                        "action": "ignored"
                    }
                ],
                "blacklist_matched": [
                    {
                        "library_id": "lib_002",
                        "library_name": "违禁词库",
                        "matched_words": ["敏感词"],
                        "custom_label": "custom_violation",
                        "risk_score": 85.0
                    }
                ]
            },
            "matched_items": [
                {
                    "type": "keyword",
                    "content": "敏感词",
                    "source": "blacklist_library",
                    "library_name": "违禁词库"
                }
            ],
            "risk_words": ["敏感词"],
            "log_id": "log_123456789"
        }
    }
    ```
    
    #### 失败响应 (400)
    ```json
    {
        "code": 400,
        "retmsg": "服务ID不能为空"
    }
    ```
    
    #### 失败响应 (500)
    ```json
    {
        "code": 500,
        "retmsg": "服务器内部错误: 具体错误信息"
    }
    ```
    """
    try:
        # 参数验证
        if not request.service_id or not request.service_id.strip():
            return get_data_error_result(retmsg="服务ID不能为空")
        
        if not request.content or not request.content.strip():
            return get_data_error_result(retmsg="检测内容不能为空")
        
        # 验证服务存在性和权限
        service = GuardServiceService.get_by_id(db, request.service_id)
        if not service:
            return get_data_error_result(retmsg="服务不存在", retcode=404)
        
        if service.tenant_id != user.id:
            return get_data_error_result(retmsg="无权限访问该服务", retcode=403)
        
        # 执行渐进式检测
        detection_result = AiGuardEngineService.detect_content(
            db=db,
            content=request.content,
            service_id=request.service_id,
            tenant_id=user.id,
            user_id=user.id,
            request_id=request.request_id,
            chat_id=request.chat_id,
            source_type=request.source_type,
            source_id=request.source_id
        )
        
        return get_json_result(data=detection_result)
        
    except Exception as e:
        return server_error_response(e)


@router.post('/batch-detect', summary="批量内容安全检测")
def detect_batch_content(
    request: BatchDetectionRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    ### POST `/batch-detect` 批量内容安全检测
    
    **功能描述**:
    此接口基于现有的单文件检测功能，支持批量处理多个内容项（1-100项）。
    每个内容项独立检测，使用相同的服务配置，返回统一格式的批量检测结果。
    支持错误容错机制，单项检测失败不影响其他项目，适用于大量文本内容的批量安全审核场景。
    
    ---
    ### 请求体 (Request Body)
    | 字段          | 类型     | 必填 | 默认值      | 描述                               |
    |---------------|----------|------|-------------|------------------------------------|
    | `service_id`  | `string` | 是   | -           | 服务ID，用于获取检测配置           |
    | `items`       | `array`  | 是   | -           | 待检测的内容列表（1-100项）        |
    | `request_id`  | `string` | 否   | null        | 请求ID，用于链路追踪               |
    | `source_type` | `string` | 否   | "batch_api" | 来源类型                           |
    | `source_id`   | `string` | 否   | null        | 来源标识                           |
    
    **items 数组元素结构**:
    | 字段      | 类型     | 必填 | 默认值 | 描述                             |
    |-----------|----------|------|--------|----------------------------------|
    | `content` | `string` | 是   | -      | 待检测的文本内容                 |
    | `item_id` | `string` | 否   | null   | 内容项ID，用于标识和关联结果     |
    
    **请求示例**:
    ```json
    {
        "service_id": "service_uuid_123",
        "items": [
            {
                "content": "第一条需要检测的文本内容",
                "item_id": "item_001"
            },
            {
                "content": "第二条需要检测的文本内容",
                "item_id": "item_002"
            }
        ],
        "request_id": "batch_req_123456789",
        "source_type": "batch_api",
        "source_id": "frontend_batch_v1"
    }
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "code": 200,
        "data": {
            "batch_summary": {
                "total_items": 2,
                "blocked_items": 1,
                "passed_items": 1,
                "overall_risk_score": 85.0,
                "overall_action": "block",
                "total_process_time_ms": 240
            },
            "item_results": [
                {
                    "item_id": "item_001",
                    "item_index": 0,
                    "content_preview": "第一条需要检测的文本内容",
                    "is_blocked": true,
                    "risk_score": 85.0,
                    "action": "block",
                    "matched_items": [...],
                    "risk_words": ["敏感词"],
                    "process_time_ms": 120,
                    "detection_result": {...}
                }
            ],
            "batch_stats": {
                "risk_distribution": {
                    "high_risk": 1,
                    "medium_risk": 0,
                    "low_risk": 0,
                    "safe": 1
                },
                "action_distribution": {
                    "block": 1,
                    "warn": 0,
                    "pass": 1
                },
                "top_risk_words": ["敏感词"],
                "detection_mode": "library_only"
            }
        }
    }
    ```
    
    #### 失败响应 (400)
    ```json
    {
        "code": 400,
        "retmsg": "检测内容列表不能为空"
    }
    ```
    
    #### 失败响应 (500)
    ```json
    {
        "code": 500,
        "retmsg": "服务器内部错误: 具体错误信息"
    }
    ```
    """
    try:
        import time
        start_time = time.time()
        
        # 参数验证
        if not request.service_id or not request.service_id.strip():
            return get_data_error_result(retmsg="服务ID不能为空")
        
        if not request.items or len(request.items) == 0:
            return get_data_error_result(retmsg="检测内容列表不能为空")
        
        if len(request.items) > 100:
            return get_data_error_result(retmsg="批量检测项数量不能超过100个")
        
        # 验证每个内容项
        for i, item in enumerate(request.items):
            if not item.content or not item.content.strip():
                return get_data_error_result(retmsg=f"第{i+1}项的检测内容不能为空")
        
        # 验证服务存在性和权限
        service = GuardServiceService.get_by_id(db, request.service_id)
        if not service:
            return get_data_error_result(retmsg="服务不存在", retcode=404)
        
        if service.tenant_id != user.id:
            return get_data_error_result(retmsg="无权限访问该服务", retcode=403)
        
        # 执行批量检测
        item_results = []
        blocked_count = 0
        max_risk_score = 0.0
        all_risk_words = set()
        risk_distribution = {"high_risk": 0, "medium_risk": 0, "low_risk": 0, "safe": 0}
        action_distribution = {"block": 0, "warn": 0, "pass": 0}
        
        for i, item in enumerate(request.items):
            item_start_time = time.time()
            
            # 生成item_id（如果未提供）
            item_id = item.item_id or f"item_{i}_{int(time.time()*1000)}"
            
            try:
                # 执行单项检测
                detection_result = AiGuardEngineService.detect_content(
                    db=db,
                    content=item.content,
                    service_id=request.service_id,
                    tenant_id=user.id,
                    user_id=user.id,
                    request_id=f"{request.request_id}_item_{item_id}" if request.request_id else f"batch_item_{item_id}",
                    chat_id=None,
                    source_type=request.source_type,
                    source_id=request.source_id
                )
                
                item_process_time = int((time.time() - item_start_time) * 1000)
                
                # 构建单项结果
                item_result = BatchItemResult(
                    item_id=item_id,
                    item_index=i,
                    content_preview=item.content[:100] + "..." if len(item.content) > 100 else item.content,
                    is_blocked=detection_result["is_blocked"],
                    risk_score=detection_result["overall_risk_score"],
                    action=detection_result["action"],
                    matched_items=detection_result.get("matched_items", []),
                    risk_words=detection_result.get("risk_words", []),
                    process_time_ms=item_process_time,
                    detection_result=detection_result
                )
                
                item_results.append(item_result)
                
                # 统计汇总信息
                if detection_result["is_blocked"]:
                    blocked_count += 1
                
                max_risk_score = max(max_risk_score, detection_result["overall_risk_score"])
                all_risk_words.update(detection_result.get("risk_words", []))
                
                # 风险分数分布统计
                risk_score = detection_result["overall_risk_score"]
                if risk_score >= 80:
                    risk_distribution["high_risk"] += 1
                elif risk_score >= 50:
                    risk_distribution["medium_risk"] += 1
                elif risk_score > 0:
                    risk_distribution["low_risk"] += 1
                else:
                    risk_distribution["safe"] += 1
                
                # 动作分布统计
                action = detection_result["action"]
                action_distribution[action] = action_distribution.get(action, 0) + 1
                
            except Exception as item_error:
                # 单项检测失败，记录错误但不影响整体处理
                item_process_time = int((time.time() - item_start_time) * 1000)
                
                error_result = BatchItemResult(
                    item_id=item_id,
                    item_index=i,
                    content_preview=item.content[:100] + "..." if len(item.content) > 100 else item.content,
                    is_blocked=False,
                    risk_score=0.0,
                    action="error",
                    matched_items=[],
                    risk_words=[],
                    process_time_ms=item_process_time,
                    detection_result={"error": f"检测失败: {str(item_error)}"}
                )
                
                item_results.append(error_result)
                action_distribution["error"] = action_distribution.get("error", 0) + 1
        
        # 确定整体动作
        if blocked_count > 0:
            overall_action = "block"
        elif max_risk_score >= 50:
            overall_action = "warn"
        else:
            overall_action = "pass"
        
        total_process_time = int((time.time() - start_time) * 1000)
        
        # 构建响应数据
        response_data = {
            "batch_summary": {
                "total_items": len(request.items),
                "blocked_items": blocked_count,
                "passed_items": len(request.items) - blocked_count,
                "overall_risk_score": max_risk_score,
                "overall_action": overall_action,
                "total_process_time_ms": total_process_time
            },
            "item_results": [item.model_dump() for item in item_results],
            "batch_stats": {
                "risk_distribution": risk_distribution,
                "action_distribution": action_distribution,
                "top_risk_words": list(all_risk_words)[:10],  # 只显示前10个风险词
                "detection_mode": "library_only"
            }
        }
        
        return get_json_result(data=response_data)
        
    except Exception as e:
        return server_error_response(e)


@router.post('/batch-detect-documents', summary="批量文档安全检测")
def detect_batch_documents(
    request: BatchDocumentDetectionRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    ### POST `/batch-detect-documents` 批量文档安全检测
    
    **功能描述**:
    此接口基于现有的单文档检测功能，支持批量处理多个文档（1-50个）。
    每个文档独立进行切片解析和安全检测，返回详细的批量文档检测结果。
    支持多种文档格式（PDF、Word、PPT、Excel等），提供完整的错误容错机制。
    
    ---
    ### 请求体 (Request Body)
    | 字段          | 类型     | 必填 | 默认值               | 描述                           |
    |---------------|----------|------|----------------------|--------------------------------|
    | `service_id`  | `string` | 是   | -                    | 服务ID，用于获取检测配置       |
    | `documents`   | `array`  | 是   | -                    | 待检测的文档列表（1-50个）     |
    | `request_id`  | `string` | 否   | null                 | 请求ID，用于链路追踪           |
    | `source_type` | `string` | 否   | "batch_document_api" | 来源类型                       |
    | `source_id`   | `string` | 否   | null                 | 来源标识                       |
    
    **documents 数组元素结构**:
    | 字段         | 类型      | 必填 | 默认值 | 描述                           |
    |--------------|----------|------|--------|--------------------------------|
    | `doc_id`     | `string` | 是   | -      | 文档ID                         |
    | `max_chunks` | `integer`| 否   | 100    | 最大检测切片数量               |
    | `doc_alias`  | `string` | 否   | null   | 文档别名，用于标识和关联结果   |
    
    **请求示例**:
    ```json
    {
        "service_id": "service_uuid_123",
        "documents": [
            {
                "doc_id": "doc_123456",
                "max_chunks": 100,
                "doc_alias": "技术报告"
            },
            {
                "doc_id": "doc_789012",
                "max_chunks": 50,
                "doc_alias": "商务合同"
            }
        ],
        "request_id": "batch_doc_req_123456789",
        "source_type": "batch_document_api",
        "source_id": "frontend_batch_doc_v1"
    }
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "code": 200,
        "data": {
            "batch_summary": {
                "total_documents": 2,
                "processed_documents": 2,
                "failed_documents": 0,
                "total_chunks": 75,
                "blocked_chunks": 8,
                "overall_risk_score": 88.0,
                "overall_action": "block",
                "total_process_time_ms": 5240
            },
            "document_results": [
                {
                    "doc_id": "doc_123456",
                    "doc_alias": "技术报告",
                    "doc_index": 0,
                    "doc_name": "技术报告.pdf",
                    "total_chunks": 40,
                    "checked_chunks": 40,
                    "blocked_chunks": 5,
                    "overall_risk_score": 88.0,
                    "overall_action": "block",
                    "process_time_ms": 2800,
                    "chunk_results": [...],
                    "detection_summary": {...},
                    "error_message": null
                }
            ],
            "batch_stats": {
                "risk_distribution": {
                    "high_risk": 8,
                    "medium_risk": 6,
                    "low_risk": 4,
                    "safe": 57
                },
                "action_distribution": {
                    "block": 8,
                    "warn": 6,
                    "pass": 61
                },
                "top_risk_words": ["敏感词1", "敏感词2"],
                "detection_mode": "library_only",
                "document_stats": {
                    "blocked_documents": 1,
                    "warned_documents": 1,
                    "passed_documents": 0
                }
            }
        }
    }
    ```
    
    #### 失败响应 (400)
    ```json
    {
        "code": 400,
        "retmsg": "文档列表不能为空"
    }
    ```
    
    #### 失败响应 (500)
    ```json
    {
        "code": 500,
        "retmsg": "服务器内部错误: 具体错误信息"
    }
    ```
    """
    try:
        import time
        start_time = time.time()
        
        # 参数验证
        if not request.service_id or not request.service_id.strip():
            return get_data_error_result(retmsg="服务ID不能为空")
        
        if not request.documents or len(request.documents) == 0:
            return get_data_error_result(retmsg="文档列表不能为空")
        
        if len(request.documents) > 50:
            return get_data_error_result(retmsg="批量文档检测数量不能超过50个")
        
        # 验证每个文档参数
        for i, doc in enumerate(request.documents):
            if not doc.doc_id or not doc.doc_id.strip():
                return get_data_error_result(retmsg=f"第{i+1}个文档的doc_id不能为空")
            if doc.max_chunks <= 0:
                return get_data_error_result(retmsg=f"第{i+1}个文档的max_chunks必须大于0")
        
        # 验证服务存在性和权限
        service = GuardServiceService.get_by_id(db, request.service_id)
        if not service:
            return get_data_error_result(retmsg="服务不存在", retcode=404)
        
        if service.tenant_id != user.id:
            return get_data_error_result(retmsg="无权限访问该服务", retcode=403)
        
        # 执行批量文档检测
        document_results = []
        processed_count = 0
        failed_count = 0
        total_chunks = 0
        total_blocked_chunks = 0
        max_risk_score = 0.0
        all_risk_words = set()
        risk_distribution = {"high_risk": 0, "medium_risk": 0, "low_risk": 0, "safe": 0}
        action_distribution = {"block": 0, "warn": 0, "pass": 0}
        document_stats = {"blocked_documents": 0, "warned_documents": 0, "passed_documents": 0}
        
        for i, doc_item in enumerate(request.documents):
            doc_start_time = time.time()
            doc_alias = doc_item.doc_alias or doc_item.doc_id
            
            try:
                # 验证文档存在性和权限
                if not DocumentService.accessible(db, doc_item.doc_id, user.id):
                    error_result = BatchDocumentResult(
                        doc_id=doc_item.doc_id,
                        doc_alias=doc_alias,
                        doc_index=i,
                        doc_name="未知文档",
                        total_chunks=0,
                        checked_chunks=0,
                        blocked_chunks=0,
                        overall_risk_score=0.0,
                        overall_action="error",
                        process_time_ms=int((time.time() - doc_start_time) * 1000),
                        chunk_results=[],
                        detection_summary={},
                        error_message="无权限访问该文档"
                    )
                    document_results.append(error_result)
                    failed_count += 1
                    continue
                
                doc = DocumentService.get_by_id(db, doc_item.doc_id)
                if not doc:
                    error_result = BatchDocumentResult(
                        doc_id=doc_item.doc_id,
                        doc_alias=doc_alias,
                        doc_index=i,
                        doc_name="未知文档",
                        total_chunks=0,
                        checked_chunks=0,
                        blocked_chunks=0,
                        overall_risk_score=0.0,
                        overall_action="error",
                        process_time_ms=int((time.time() - doc_start_time) * 1000),
                        chunk_results=[],
                        detection_summary={},
                        error_message="文档不存在"
                    )
                    document_results.append(error_result)
                    failed_count += 1
                    continue
                
                # 获取文件存储地址
                try:
                    bucket, name = File2DocumentService.get_storage_address(db, doc_id=doc_item.doc_id)
                    if not bucket or not name:
                        raise Exception("无法获取文档存储地址")
                except Exception as e:
                    error_result = BatchDocumentResult(
                        doc_id=doc_item.doc_id,
                        doc_alias=doc_alias,
                        doc_index=i,
                        doc_name=doc.name,
                        total_chunks=0,
                        checked_chunks=0,
                        blocked_chunks=0,
                        overall_risk_score=0.0,
                        overall_action="error",
                        process_time_ms=int((time.time() - doc_start_time) * 1000),
                        chunk_results=[],
                        detection_summary={},
                        error_message=f"获取文档存储地址失败: {str(e)}"
                    )
                    document_results.append(error_result)
                    failed_count += 1
                    continue
                
                # 从MinIO获取文件内容
                try:
                    binary = STORAGE_IMPL.get(bucket, name)
                    if not binary:
                        raise Exception("无法从存储获取文档内容")
                except Exception as e:
                    error_result = BatchDocumentResult(
                        doc_id=doc_item.doc_id,
                        doc_alias=doc_alias,
                        doc_index=i,
                        doc_name=doc.name,
                        total_chunks=0,
                        checked_chunks=0,
                        blocked_chunks=0,
                        overall_risk_score=0.0,
                        overall_action="error",
                        process_time_ms=int((time.time() - doc_start_time) * 1000),
                        chunk_results=[],
                        detection_summary={},
                        error_message=f"获取文档内容失败: {str(e)}"
                    )
                    document_results.append(error_result)
                    failed_count += 1
                    continue
                
                # 使用naive解析器进行切片处理
                try:
                    parser_config = {
                        "chunk_token_num": 128, 
                        "delimiter": "\n!?。；！？", 
                        "layout_recognize": "DeepDOC"
                    }
                    
                    def progress_callback(progress=None, msg=""):
                        if msg:
                            import logging
                            logging.info(f"Document parsing progress: {msg}")
                    
                    chunks = naive.chunk(
                        filename=doc.name,
                        binary=binary,
                        from_page=0,
                        to_page=100000,
                        lang="Chinese",
                        callback=progress_callback,
                        parser_config=parser_config,
                        kb_id=doc.kb_id,
                        tenant_id=service.tenant_id
                    )
                    
                    if not chunks:
                        # 文档无内容但处理成功
                        success_result = BatchDocumentResult(
                            doc_id=doc_item.doc_id,
                            doc_alias=doc_alias,
                            doc_index=i,
                            doc_name=doc.name,
                            total_chunks=0,
                            checked_chunks=0,
                            blocked_chunks=0,
                            overall_risk_score=0.0,
                            overall_action="pass",
                            process_time_ms=int((time.time() - doc_start_time) * 1000),
                            chunk_results=[],
                            detection_summary={
                                "risk_distribution": {"high_risk": 0, "medium_risk": 0, "low_risk": 0, "safe": 0},
                                "action_distribution": {"block": 0, "warn": 0, "pass": 0},
                                "top_risk_words": [],
                                "detection_mode": "library_only"
                            },
                            error_message=None
                        )
                        document_results.append(success_result)
                        processed_count += 1
                        document_stats["passed_documents"] += 1
                        continue
                        
                except Exception as e:
                    error_result = BatchDocumentResult(
                        doc_id=doc_item.doc_id,
                        doc_alias=doc_alias,
                        doc_index=i,
                        doc_name=doc.name,
                        total_chunks=0,
                        checked_chunks=0,
                        blocked_chunks=0,
                        overall_risk_score=0.0,
                        overall_action="error",
                        process_time_ms=int((time.time() - doc_start_time) * 1000),
                        chunk_results=[],
                        detection_summary={},
                        error_message=f"文档切片处理失败: {str(e)}"
                    )
                    document_results.append(error_result)
                    failed_count += 1
                    continue
                
                # 限制检测的切片数量
                doc_total_chunks = len(chunks)
                chunks_to_check = chunks[:doc_item.max_chunks] if doc_item.max_chunks > 0 else chunks
                
                # 对每个切片执行安全检测
                chunk_detection_results = []
                doc_blocked_count = 0
                doc_max_risk_score = 0.0
                doc_risk_words = set()
                doc_risk_distribution = {"high_risk": 0, "medium_risk": 0, "low_risk": 0, "safe": 0}
                doc_action_distribution = {"block": 0, "warn": 0, "pass": 0}
                
                for j, chunk in enumerate(chunks_to_check):
                    chunk_content = chunk.get("content_with_weight", "")
                    if not chunk_content:
                        continue
                    
                    # 生成唯一的chunk_id
                    chunk_id = xxhash.xxh64(
                        (chunk_content + str(doc_item.doc_id)).encode("utf-8")
                    ).hexdigest()
                    
                    # 执行渐进式检测
                    detection_result = AiGuardEngineService.detect_content(
                        db=db,
                        content=chunk_content,
                        service_id=request.service_id,
                        tenant_id=user.id,
                        user_id=user.id,
                        request_id=f"{request.request_id}_doc_{doc_item.doc_id}_chunk_{chunk_id}" if request.request_id else f"batch_doc_{doc_item.doc_id}_chunk_{chunk_id}",
                        source_type=request.source_type,
                        source_id=request.source_id,
                        chunk_id=chunk_id,
                        doc_id=doc_item.doc_id
                    )
                    
                    # 构建切片检测结果
                    chunk_result = ChunkDetectionResult(
                        chunk_id=chunk_id,
                        chunk_index=j,
                        content=chunk_content[:200] + "..." if len(chunk_content) > 200 else chunk_content,
                        is_blocked=detection_result["is_blocked"],
                        risk_score=detection_result["overall_risk_score"],
                        action=detection_result["action"],
                        matched_items=detection_result.get("matched_items", []),
                        risk_words=detection_result.get("risk_words", [])
                    )
                    
                    chunk_detection_results.append(chunk_result)
                    
                    # 统计信息
                    if detection_result["is_blocked"]:
                        doc_blocked_count += 1
                    
                    doc_max_risk_score = max(doc_max_risk_score, detection_result["overall_risk_score"])
                    doc_risk_words.update(detection_result.get("risk_words", []))
                    
                    # 风险分数分布统计
                    risk_score = detection_result["overall_risk_score"]
                    if risk_score >= 80:
                        doc_risk_distribution["high_risk"] += 1
                    elif risk_score >= 50:
                        doc_risk_distribution["medium_risk"] += 1
                    elif risk_score > 0:
                        doc_risk_distribution["low_risk"] += 1
                    else:
                        doc_risk_distribution["safe"] += 1
                    
                    # 动作分布统计
                    action = detection_result["action"]
                    doc_action_distribution[action] = doc_action_distribution.get(action, 0) + 1
                
                # 按风险分数降序排列
                chunk_detection_results.sort(key=lambda x: x.risk_score, reverse=True)
                
                # 确定文档整体动作
                if doc_blocked_count > 0:
                    doc_overall_action = "block"
                    document_stats["blocked_documents"] += 1
                elif doc_max_risk_score >= 50:
                    doc_overall_action = "warn"
                    document_stats["warned_documents"] += 1
                else:
                    doc_overall_action = "pass"
                    document_stats["passed_documents"] += 1
                
                # 构建文档检测结果
                doc_result = BatchDocumentResult(
                    doc_id=doc_item.doc_id,
                    doc_alias=doc_alias,
                    doc_index=i,
                    doc_name=doc.name,
                    total_chunks=doc_total_chunks,
                    checked_chunks=len(chunk_detection_results),
                    blocked_chunks=doc_blocked_count,
                    overall_risk_score=doc_max_risk_score,
                    overall_action=doc_overall_action,
                    process_time_ms=int((time.time() - doc_start_time) * 1000),
                    chunk_results=[chunk.model_dump() for chunk in chunk_detection_results],
                    detection_summary={
                        "risk_distribution": doc_risk_distribution,
                        "action_distribution": doc_action_distribution,
                        "top_risk_words": list(doc_risk_words)[:10],
                        "detection_mode": "library_only"
                    },
                    error_message=None
                )
                
                document_results.append(doc_result)
                processed_count += 1
                
                # 累加到批量统计
                total_chunks += doc_total_chunks
                total_blocked_chunks += doc_blocked_count
                max_risk_score = max(max_risk_score, doc_max_risk_score)
                all_risk_words.update(doc_risk_words)
                
                for key in risk_distribution:
                    risk_distribution[key] += doc_risk_distribution[key]
                for key in action_distribution:
                    action_distribution[key] += doc_action_distribution.get(key, 0)
                
            except Exception as doc_error:
                # 单个文档检测失败
                error_result = BatchDocumentResult(
                    doc_id=doc_item.doc_id,
                    doc_alias=doc_alias,
                    doc_index=i,
                    doc_name="未知文档",
                    total_chunks=0,
                    checked_chunks=0,
                    blocked_chunks=0,
                    overall_risk_score=0.0,
                    overall_action="error",
                    process_time_ms=int((time.time() - doc_start_time) * 1000),
                    chunk_results=[],
                    detection_summary={},
                    error_message=f"文档检测失败: {str(doc_error)}"
                )
                document_results.append(error_result)
                failed_count += 1
        
        # 确定整体动作
        if total_blocked_chunks > 0:
            overall_action = "block"
        elif max_risk_score >= 50:
            overall_action = "warn"
        else:
            overall_action = "pass"
        
        total_process_time = int((time.time() - start_time) * 1000)
        
        # 构建响应数据
        response_data = {
            "batch_summary": {
                "total_documents": len(request.documents),
                "processed_documents": processed_count,
                "failed_documents": failed_count,
                "total_chunks": total_chunks,
                "blocked_chunks": total_blocked_chunks,
                "overall_risk_score": max_risk_score,
                "overall_action": overall_action,
                "total_process_time_ms": total_process_time
            },
            "document_results": [doc.model_dump() for doc in document_results],
            "batch_stats": {
                "risk_distribution": risk_distribution,
                "action_distribution": action_distribution,
                "top_risk_words": list(all_risk_words)[:10],
                "detection_mode": "library_only",
                "document_stats": document_stats
            }
        }
        
        return get_json_result(data=response_data)
        
    except Exception as e:
        return server_error_response(e)


@router.get('/services/{service_id}/detection-config', summary="获取服务检测配置")
def get_service_detection_config(
    service_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    ### GET `/services/{service_id}/detection-config` 获取服务检测配置
    
    **功能描述**:
    此接口用于获取指定服务的检测配置信息，包括检测模式、绑定的词库、
    启用的维度和标签等详细配置。适用于前端显示服务配置详情和调试检测规则。
    
    ---
    ### 路径参数 (Path Parameters)
    | 参数         | 类型     | 必填 | 描述       |
    |--------------|----------|------|------------|
    | `service_id` | `string` | 是   | 服务ID     |
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "code": 200,
        "data": {
            "service_id": "service_uuid_123",
            "service_name": "用户查询安全检测",
            "service_code": "query_security_check",
            "detection_mode": "library_only",
            "enabled_dimensions": [],
            "enabled_labels": [],
            "bound_libraries": [
                {
                    "library_id": "lib_001",
                    "library_name": "通用白名单",
                    "library_type": "whitelist",
                    "priority": 100,
                    "enabled": true
                }
            ],
            "policy_config": {
                "risk_threshold": 70.0,
                "default_action": "warn"
            }
        }
    }
    ```
    
    #### 失败响应 (404)
    ```json
    {
        "code": 404,
        "retmsg": "服务不存在"
    }
    ```
    """
    try:
        # 验证服务存在性和权限
        service = GuardServiceService.get_by_id(db, service_id)
        if not service:
            return get_data_error_result(retmsg="服务不存在", retcode=404)
        
        if service.tenant_id != user.id:
            return get_data_error_result(retmsg="无权限访问该服务", retcode=403)
        
        # 获取绑定的词库
        from api.db.services.guard_service_library_service import GuardServiceLibraryService
        bound_libraries = GuardServiceLibraryService.get_libraries_by_service(
            db, service_id, enabled_only=False
        )
        
        # 判断检测模式
        detection_mode = "comprehensive" if (service.enabled_dimensions and service.enabled_labels) else "library_only"
        
        config_data = {
            "service_id": service.id,
            "service_name": service.name,
            "service_code": service.code,
            "detection_mode": detection_mode,
            "enabled_dimensions": service.enabled_dimensions or [],
            "enabled_labels": service.enabled_labels or [],
            "bound_libraries": [
                {
                    "library_id": lib["id"],
                    "library_name": lib["name"],
                    "library_type": lib["library_type"],
                    "priority": lib["binding"]["priority"],
                    "enabled": lib["binding"]["enabled"]
                }
                for lib in bound_libraries
            ],
            "policy_config": service.policy_config or {}
        }
        
        return get_json_result(data=config_data)
        
    except Exception as e:
        return server_error_response(e)


@router.post('/test-libraries', summary="测试词库匹配")
def test_library_matching(
    request: dict[str, Any],
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    测试词库匹配功能（调试用）
    
    Args:
        request: {
            "content": "测试内容",
            "library_ids": ["lib_001", "lib_002"]
        }
        
    Returns:
        词库匹配测试结果
    """
    try:
        content = request.get("content", "")
        library_ids = request.get("library_ids", [])
        
        if not content:
            return get_data_error_result(retmsg="测试内容不能为空")
        
        if not library_ids:
            return get_data_error_result(retmsg="词库ID列表不能为空")
        
        # 获取词库并测试匹配
        from api.db.services.guard_library_service import GuardLibraryService
        from api.db.services.guard_library_item_service import GuardLibraryItemService
        
        test_results = []
        
        for library_id in library_ids:
            library = GuardLibraryService.get_by_id(db, library_id)
            if not library or library.tenant_id != user.id:
                continue
            
            # 获取词库项，只测试启用的项
            result = GuardLibraryItemService.get_items_by_library(
                db, library_id, page=1, page_size=10000
            )
            # 过滤出启用的词库项
            library_items = [item for item in result["items"] if item.status == "1"]
            
            matched_words = []
            for item in library_items:
                if item.content.lower() in content.lower():
                    matched_words.append(item.content)
            
            test_results.append({
                "library_id": library_id,
                "library_name": library.name,
                "library_type": library.library_type,
                "matched_words": matched_words,
                "match_count": len(matched_words)
            })
        
        return get_json_result(data={
            "test_content": content,
            "library_results": test_results
        })
        
    except Exception as e:
        return server_error_response(e)


@router.post('/detect-document', summary="文档安全检测")
def detect_document_chunks(
    request: DocumentGuardDetectionRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    ### POST `/detect-document` 文档安全检测
    
    **功能描述**:
    此接口根据文档ID进行文档安全检测，会自动获取文档内容，使用智能解析器进行切片处理，
    然后对每个切片执行安全检测。支持多种文档格式（PDF、Word、PPT、Excel等），
    提供详细的切片级检测结果和统计信息。
    
    ---
    ### 请求体 (Request Body)                  
    | 字段          | 类型      | 必填 | 默认值        | 描述                             |
    |---------------|----------|------|---------------|----------------------------------|
    | `service_id`  | `string` | 是   | -             | 服务ID，用于获取检测配置         |
    | `doc_id`      | `string` | 是   | -             | 文档ID                           |
    | `max_chunks`  | `integer`| 否   | 100           | 最大检测切片数量                 |
    | `request_id`  | `string` | 否   | null          | 请求ID，用于链路追踪             |
    | `source_type` | `string` | 否   | "document_api"| 来源类型                         |
    | `source_id`   | `string` | 否   | null          | 来源标识                         |
    
    **请求示例**:
    ```json
    {
        "service_id": "service_uuid_123",
        "doc_id": "doc_123456",
        "max_chunks": 100,
        "request_id": "doc_req_123456789",
        "source_type": "document_api",
        "source_id": "frontend_v1"
    }
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "code": 200,
        "data": {
            "doc_id": "doc_123456",
            "doc_name": "示例文档.pdf",
            "total_chunks": 25,
            "checked_chunks": 25,
            "blocked_chunks": 3,
            "overall_risk_score": 85.0,
            "overall_action": "block",
            "process_time_ms": 1250,
            "chunk_results": [
                {
                    "chunk_id": "a1b2c3d4e5f6g7h8",
                    "chunk_index": 0,
                    "content": "这是文档内容的一部分...",
                    "is_blocked": true,
                    "risk_score": 88.0,
                    "action": "block",
                    "matched_items": [...],
                    "risk_words": ["敏感词"]
                }
            ],
            "detection_summary": {
                "risk_distribution": {
                    "high_risk": 3,
                    "medium_risk": 2,
                    "low_risk": 1,
                    "safe": 19
                },
                "action_distribution": {
                    "block": 3,
                    "warn": 2,
                    "pass": 20
                },
                "top_risk_words": ["敏感词1", "敏感词2"],
                "detection_mode": "library_only"
            }
        }
    }
    ```
    
    #### 失败响应 (400)
    ```json
    {
        "code": 400,
        "retmsg": "文档ID不能为空"
    }
    ```
    
    #### 失败响应 (500)
    ```json
    {
        "code": 500,
        "retmsg": "服务器内部错误: 具体错误信息"
    }
    ```
    """
    try:
        import time
        start_time = time.time()
        
        # 参数验证
        if not request.service_id or not request.service_id.strip():
            return get_data_error_result(retmsg="服务ID不能为空")
        
        if not request.doc_id or not request.doc_id.strip():
            return get_data_error_result(retmsg="文档ID不能为空")
        
        # 验证服务存在性和权限
        service = GuardServiceService.get_by_id(db, request.service_id)
        if not service:
            return get_data_error_result(retmsg="服务不存在", retcode=404)
        
        if service.tenant_id != user.id:
            return get_data_error_result(retmsg="无权限访问该服务", retcode=403)
        
        # 验证文档存在性和权限
        if not DocumentService.accessible(db, request.doc_id, user.id):
            return get_data_error_result(retmsg="无权限访问该文档", retcode=403)
        
        doc = DocumentService.get_by_id(db, request.doc_id)
        if not doc:
            return get_data_error_result(retmsg="文档不存在", retcode=404)
        
        # 获取文件存储地址
        try:
            bucket, name = File2DocumentService.get_storage_address(db, doc_id=request.doc_id)
            if not bucket or not name:
                return get_data_error_result(retmsg="无法获取文档存储地址")
        except Exception as e:
            return get_data_error_result(retmsg=f"获取文档存储地址失败: {str(e)}")
        
        # 从MinIO获取文件内容
        try:
            binary = STORAGE_IMPL.get(bucket, name)
            if not binary:
                return get_data_error_result(retmsg="无法从存储获取文档内容")
        except Exception as e:
            return get_data_error_result(retmsg=f"获取文档内容失败: {str(e)}")
        
        # 使用naive解析器进行切片处理
        try:
            # 设置解析器配置
            parser_config = {
                "chunk_token_num": 128, 
                "delimiter": "\n!?。；！？", 
                "layout_recognize": "DeepDOC"
            }
            
            # 定义回调函数用于处理进度报告
            def progress_callback(progress=None, msg=""):
                """处理naive解析器的进度回调"""
                if msg:
                    import logging
                    logging.info(f"Document parsing progress: {msg}")
            
            # 调用naive解析器进行切片
            chunks = naive.chunk(
                filename=doc.name,
                binary=binary,
                from_page=0,
                to_page=100000,
                lang="Chinese",
                callback=progress_callback,
                parser_config=parser_config,
                kb_id=doc.kb_id,
                tenant_id=service.tenant_id
            )
            
            if not chunks:
                return get_json_result(data={
                    "doc_id": request.doc_id,
                    "doc_name": doc.name,
                    "total_chunks": 0,
                    "checked_chunks": 0,
                    "blocked_chunks": 0,
                    "overall_risk_score": 0.0,
                    "overall_action": "pass",
                    "chunk_results": [],
                    "detection_summary": {
                        "risk_distribution": {"high_risk": 0, "medium_risk": 0, "low_risk": 0, "safe": 0},
                        "action_distribution": {"block": 0, "warn": 0, "pass": 0},
                        "top_risk_words": [],
                        "detection_mode": "library_only"
                    },
                    "process_time_ms": int((time.time() - start_time) * 1000)
                })
                
        except Exception as e:
            return get_data_error_result(retmsg=f"文档切片处理失败: {str(e)}")
        
        # 限制检测的切片数量
        total_chunks = len(chunks)
        chunks_to_check = chunks[:request.max_chunks] if request.max_chunks > 0 else chunks
        
        # 对每个切片执行安全检测
        chunk_detection_results = []
        blocked_count = 0
        max_risk_score = 0.0
        all_risk_words = set()
        risk_distribution = {"high_risk": 0, "medium_risk": 0, "low_risk": 0, "safe": 0}
        action_distribution = {"block": 0, "warn": 0, "pass": 0}
        
        for i, chunk in enumerate(chunks_to_check):
            chunk_content = chunk.get("content_with_weight", "")
            if not chunk_content:
                continue
            
            # 生成唯一的chunk_id，使用与task_executor.py相同的方案
            chunk_id = xxhash.xxh64(
                (chunk_content + str(request.doc_id)).encode("utf-8")
            ).hexdigest()
            
            # 执行渐进式检测
            detection_result = AiGuardEngineService.detect_content(
                db=db,
                content=chunk_content,
                service_id=request.service_id,
                tenant_id=user.id,
                user_id=user.id,
                request_id=f"{request.request_id}_chunk_{chunk_id}" if request.request_id else f"doc_{request.doc_id}_chunk_{chunk_id}",
                source_type=request.source_type,
                source_id=request.source_id,
                chunk_id=chunk_id,
                doc_id=request.doc_id
            )
            
            # 构建切片检测结果
            chunk_result = ChunkDetectionResult(
                chunk_id=chunk_id,
                chunk_index=i,
                content=chunk_content[:200] + "..." if len(chunk_content) > 200 else chunk_content,
                is_blocked=detection_result["is_blocked"],
                risk_score=detection_result["overall_risk_score"],
                action=detection_result["action"],
                matched_items=detection_result.get("matched_items", []),
                risk_words=detection_result.get("risk_words", [])
            )
            
            chunk_detection_results.append(chunk_result)
            
            # 统计信息
            if detection_result["is_blocked"]:
                blocked_count += 1
            
            max_risk_score = max(max_risk_score, detection_result["overall_risk_score"])
            all_risk_words.update(detection_result.get("risk_words", []))
            
            # 风险分数分布统计
            risk_score = detection_result["overall_risk_score"]
            if risk_score >= 80:
                risk_distribution["high_risk"] += 1
            elif risk_score >= 50:
                risk_distribution["medium_risk"] += 1
            elif risk_score > 0:
                risk_distribution["low_risk"] += 1
            else:
                risk_distribution["safe"] += 1
            
            # 动作分布统计
            action = detection_result["action"]
            action_distribution[action] = action_distribution.get(action, 0) + 1
        
        # 按风险分数降序排列
        chunk_detection_results.sort(key=lambda x: x.risk_score, reverse=True)
        
        # 确定整体动作
        if blocked_count > 0:
            overall_action = "block"
        elif max_risk_score >= 50:
            overall_action = "warn"
        else:
            overall_action = "pass"
        
        # 构建响应数据
        response_data = {
            "doc_id": request.doc_id,
            "doc_name": doc.name,
            "total_chunks": total_chunks,
            "checked_chunks": len(chunk_detection_results),
            "blocked_chunks": blocked_count,
            "overall_risk_score": max_risk_score,
            "overall_action": overall_action,
            "chunk_results": [chunk.model_dump() for chunk in chunk_detection_results],
            "detection_summary": {
                "risk_distribution": risk_distribution,
                "action_distribution": action_distribution,
                "top_risk_words": list(all_risk_words)[:10],  # 只显示前10个风险词
                "detection_mode": "library_only"
            },
            "process_time_ms": int((time.time() - start_time) * 1000)
        }
        
        return get_json_result(data=response_data)
        
    except Exception as e:
        return server_error_response(e)


@router.get('/services', summary="获取可用检测服务")
def get_available_services(
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取当前租户可用的检测服务列表
    
    Args:
        db: 数据库会话
        user: 当前用户信息
        
    Returns:
        dict[str, Any]: 服务列表
        - 成功: {"code": 200, "data": [{"code": "xxx", "name": "xxx", ...}]}
        - 失败: {"code": 500, "retmsg": "错误信息"}
    """
    try:
        services = GuardServiceService.get_services_by_tenant(db, user.id)
        
        service_list = []
        for service in services:
            service_list.append({
                "id": service.id,
                "code": service.code,
                "name": service.name,
                "description": service.description,
                "service_type": service.service_type,
                "enabled_dimensions": service.enabled_dimensions,
                "enabled_labels": service.enabled_labels,
                "total_requests": service.total_requests,
                "blocked_requests": service.blocked_requests,
                "block_rate": (service.blocked_requests / service.total_requests * 100) 
                             if service.total_requests > 0 else 0.0
            })
        
        return get_json_result(data=service_list)
        
    except Exception as e:
        return server_error_response(e)


@router.post('/services', summary="创建检测服务")
def create_detection_service(
    request: CreateServiceRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    ### POST `/services` 创建检测服务
    
    **功能描述**:
    创建一个新的检测服务配置，用于后续的内容安全检测。
    
    ---
    ### 请求体 (Request Body)
    | 字段                  | 类型        | 必填 | 默认值 | 描述                           |
    |----------------------|-------------|------|--------|--------------------------------|
    | `code`               | `string`    | 是   | -      | 服务代码，唯一标识             |
    | `name`               | `string`    | 是   | -      | 服务名称                       |
    | `description`        | `string`    | 否   | null   | 服务描述                       |
    | `service_type`       | `string`    | 否   | "api"  | 服务类型                       |
    | `enabled_dimensions` | `list[str]` | 否   | []     | 启用的维度列表                 |
    | `enabled_labels`     | `list[str]` | 否   | []     | 启用的标签列表                 |
    | `policy_config`      | `dict`      | 否   | {}     | 策略配置                       |
    | `cache_enabled`      | `bool`      | 否   | true   | 是否启用缓存                   |
    | `timeout_ms`         | `int`       | 否   | 1000   | 超时时间（毫秒），范围100-30000|
    
    **请求示例**:
    ```json
    {
        "code": "custom_security_check",
        "name": "自定义安全检测",
        "description": "用于特定场景的安全检测服务",
        "service_type": "api",
        "enabled_dimensions": ["CONTENT_COMPLIANCE"],
        "enabled_labels": ["political_entity"],
        "policy_config": {
            "risk_threshold": 70,
            "default_action": "block"
        },
        "cache_enabled": true,
        "timeout_ms": 1500
    }
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "code": 200,
        "data": {
            "service_id": "newly_created_service_id",
            "message": "服务创建成功"
        }
    }
    ```
    
    #### 失败响应 (400)
    ```json
    {
        "code": 400,
        "retmsg": "服务代码已存在"
    }
    ```
    """
    try:
        # 创建服务
        service_id = GuardServiceService.create_service(
            db=db,
            code=request.code,
            name=request.name,
            description=request.description,
            tenant_id=user.id,
            created_by=user.id,
            service_type=request.service_type,
            enabled_dimensions=request.enabled_dimensions,
            enabled_labels=request.enabled_labels,
            policy_config=request.policy_config,
            cache_enabled=request.cache_enabled,
            timeout_ms=request.timeout_ms
        )
        
        if not service_id:
            return get_data_error_result(retmsg=f"服务代码 {request.code} 已存在")
        
        return get_json_result(data={
            "service_id": service_id,
            "message": "服务创建成功"
        })
        
    except Exception as e:
        return server_error_response(e)


@router.put('/services/{service_id}', summary="修改检测服务")
def update_detection_service(
    service_id: str,
    request: UpdateServiceRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    ### PUT `/services/{service_id}` 修改检测服务
    
    **功能描述**:
    修改指定检测服务的配置信息。只需要提供需要修改的字段，未提供的字段保持不变。
    
    **注意**: 
    - `code` 字段不可修改（业务主键，与历史日志关联）
    - 如需修改 code，建议删除后重建服务
    
    ---
    ### 路径参数 (Path Parameters)
    | 参数         | 类型     | 必填 | 描述       |
    |--------------|----------|------|------------|
    | `service_id` | `string` | 是   | 服务ID     |
    
    ---
    ### 请求体 (Request Body)
    | 字段                  | 类型        | 必填 | 描述                           |
    |----------------------|-------------|------|--------------------------------|
    | `name`               | `string`    | 否   | 服务名称                       |
    | `description`        | `string`    | 否   | 服务描述                       |
    | `service_type`       | `string`    | 否   | 服务类型                       |
    | `enabled_dimensions` | `list[str]` | 否   | 启用的维度列表                 |
    | `enabled_labels`     | `list[str]` | 否   | 启用的标签列表                 |
    | `policy_config`      | `dict`      | 否   | 策略配置                       |
    | `cache_enabled`      | `bool`      | 否   | 是否启用缓存                   |
    | `timeout_ms`         | `int`       | 否   | 超时时间（毫秒），范围100-30000|
    
    **请求示例**:
    ```json
    {
        "name": "更新后的服务名称",
        "description": "更新后的服务描述",
        "policy_config": {
            "risk_threshold": 80,
            "default_action": "warn"
        }
    }
    ```
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "code": 200,
        "data": {
            "message": "服务更新成功"
        }
    }
    ```
    
    #### 失败响应 (404)
    ```json
    {
        "code": 404,
        "retmsg": "服务不存在"
    }
    ```
    
    #### 失败响应 (403)
    ```json
    {
        "code": 403,
        "retmsg": "无权限访问该服务"
    }
    ```
    """
    try:
        # 验证服务存在性和权限
        service = GuardServiceService.get_by_id(db, service_id)
        if not service:
            return get_data_error_result(retmsg="服务不存在", retcode=404)
        
        if service.tenant_id != user.id:
            return get_data_error_result(retmsg="无权限访问该服务", retcode=403)
        
        # 构建更新数据（只包含提供的字段）
        update_data = {}
        if request.name is not None:
            update_data["name"] = request.name
        if request.description is not None:
            update_data["description"] = request.description
        if request.service_type is not None:
            update_data["service_type"] = request.service_type
        if request.enabled_dimensions is not None:
            update_data["enabled_dimensions"] = request.enabled_dimensions
        if request.enabled_labels is not None:
            update_data["enabled_labels"] = request.enabled_labels
        if request.policy_config is not None:
            update_data["policy_config"] = request.policy_config
        if request.cache_enabled is not None:
            update_data["cache_enabled"] = request.cache_enabled
        if request.timeout_ms is not None:
            update_data["timeout_ms"] = request.timeout_ms
        
        # 检查是否有数据需要更新
        if not update_data:
            return get_data_error_result(retmsg="没有提供需要更新的数据")
        
        # 执行更新
        success = GuardServiceService.update_service(db, service_id, update_data)
        
        if not success:
            return get_data_error_result(retmsg="服务更新失败")
        
        return get_json_result(data={"message": "服务更新成功"})
        
    except Exception as e:
        return server_error_response(e)


@router.delete('/services/{service_id}', summary="删除检测服务")
def delete_detection_service(
    service_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    ### DELETE `/services/{service_id}` 删除检测服务
    
    **功能描述**:
    删除指定的检测服务。这是逻辑删除，实际上是将服务状态标记为已删除。
    
    ---
    ### 路径参数 (Path Parameters)
    | 参数         | 类型     | 必填 | 描述       |
    |--------------|----------|------|------------|
    | `service_id` | `string` | 是   | 服务ID     |
    
    ---
    ### 响应 (Response)
    #### 成功响应 (200)
    ```json
    {
        "code": 200,
        "data": {
            "message": "服务删除成功"
        }
    }
    ```
    
    #### 失败响应 (404)
    ```json
    {
        "code": 404,
        "retmsg": "服务不存在"
    }
    ```
    
    #### 失败响应 (403)
    ```json
    {
        "code": 403,
        "retmsg": "无权限访问该服务"
    }
    ```
    """
    try:
        # 验证服务存在性和权限
        service = GuardServiceService.get_by_id(db, service_id)
        if not service:
            return get_data_error_result(retmsg="服务不存在", retcode=404)
        
        if service.tenant_id != user.id:
            return get_data_error_result(retmsg="无权限访问该服务", retcode=403)
        
        # 逻辑删除（将状态设置为 "0"）
        success = GuardServiceService.update_service(db, service_id, {"status": "0"})
        
        if not success:
            return get_data_error_result(retmsg="服务删除失败")
        
        return get_json_result(data={"message": "服务删除成功"})
        
    except Exception as e:
        return server_error_response(e)


@router.get('/logs', summary="获取检测日志")
def get_detection_logs(
    page: int = 1,
    page_size: int = 50,
    start_date: str | None = None,
    end_date: str | None = None,
    service_code: str | None = None,
    is_blocked: bool | None = None,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取检测日志列表（分页查询）
    
    Args:
        page (int): 页码，从1开始，默认为1
        page_size (int): 每页记录数量，默认50，最大100
        start_date (str, optional): 开始日期，ISO格式字符串
            - 格式: "YYYY-MM-DD" 或 "YYYY-MM-DDTHH:MM:SS"
            - 示例: "2025-07-01" 或 "2025-07-01T00:00:00"
            - 如果不提供，则不限制开始时间
        end_date (str, optional): 结束日期，ISO格式字符串
            - 格式: "YYYY-MM-DD" 或 "YYYY-MM-DDTHH:MM:SS"  
            - 示例: "2025-07-14" 或 "2025-07-14T23:59:59"
            - 如果不提供，则不限制结束时间
        service_code (str, optional): 服务代码过滤（如 "query_security_check"）
            - 示例: "query_security_check"
            - 如果不提供，则返回所有服务的日志
            - 注意：使用 service_code 而非 service_id 以提高查询性能
        is_blocked (bool, optional): 是否被拦截过滤
            - True: 只返回被拦截的记录
            - False: 只返回未被拦截的记录
            - None: 返回所有记录（默认）
        db (Session): 数据库会话（自动注入）
        user: 当前用户信息（自动注入）
        
    Returns:
        dict[str, Any]: 检测日志列表和分页信息
        成功响应 (200):
        {
            "code": 200,
            "data": {
                "logs": [
                    {
                        "id": "log_id",
                        "service_id": "service_uuid_123",
                        "service_code": "query_security_check",
                        "content_preview": "检测内容预览...",
                        "is_blocked": true,
                        "risk_score": 85.0,
                        "action_taken": "block",
                        "create_time": 1705123456789,  // 毫秒时间戳
                        "create_date": "2025-07-14T10:30:56",
                        ...
                    }
                ],
                "total": 150,          // 总记录数
                "page": 1,             // 当前页码
                "page_size": 50,       // 每页数量
                "total_pages": 3       // 总页数
            }
        }
        失败响应 (500):
        {
            "code": 500,
            "retmsg": "获取日志列表失败: 具体错误信息"
        }
        
    HTTP Status Codes:
        200: 请求成功
        400: 参数错误
        401: 未授权
        500: 服务器内部错误
    """
    try:
        result = GuardLogService.get_logs_by_tenant(
            db=db,
            tenant_id=user.id,
            page=page,
            page_size=page_size,
            start_date=start_date,
            end_date=end_date,
            service_code=service_code,
            is_blocked=is_blocked
        )
        
        # 转换为字典格式
        logs_data = []
        for log in result["logs"]:
            logs_data.append(log.to_dict())
        
        result["logs"] = logs_data
        return get_json_result(data=result)
        
    except Exception as e:
        return server_error_response(e)


@router.get('/stats', summary="获取检测统计")
def get_detection_stats(
    days: int = 30,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取指定时间范围内的检测统计信息
    
    Args:
        days (int): 统计天数，从今天开始向前计算
            - 默认: 30天
            - 范围: 1-365天
            - 示例: days=7 表示最近7天的统计
            - 示例: days=1 表示今天的统计
        db (Session): 数据库会话（自动注入）
        user: 当前用户信息（自动注入）
        
    Returns:
        dict[str, Any]: 检测统计信息
        成功响应 (200):
        {
            "code": 200,
            "data": {
                "total_requests": 1500,        // 总请求数
                "blocked_requests": 120,       // 被拦截请求数
                "pass_requests": 1380,         // 通过请求数
                "block_rate": 8.0,             // 拦截率(%)
                "service_stats": {             // 各服务统计
                    "service_uuid_123": {
                        "total": 800,
                        "blocked": 60
                    },
                    "service_uuid_456": {
                        "total": 700,
                        "blocked": 60
                    }
                },
                "risk_level_stats": {          // 风险等级统计
                    "high": 45,
                    "medium": 75,
                    "low": 80,
                    "none": 1300
                },
                "period_days": 30              // 统计期间天数
            }
        }
        失败响应 (500):
        {
            "code": 500,
            "retmsg": "获取检测统计失败: 具体错误信息"
        }
        
    HTTP Status Codes:
        200: 请求成功
        400: 参数错误（如days超出范围）
        401: 未授权
        500: 服务器内部错误
        
    注意事项:
        - 统计时间基于记录的create_time字段（毫秒时间戳）
        - 统计数据实时计算，可能有轻微延迟
        - 大时间范围查询可能响应较慢
    """
    try:
        stats = GuardLogService.get_log_stats(db, user.id, days)
        return get_json_result(data=stats)
        
    except Exception as e:
        return server_error_response(e)


@router.get('/stats/trend', summary="获取检测趋势")
def get_detection_trend(
    days: int = 7,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取指定时间范围内的检测趋势数据（按天分组）
    
    Args:
        days (int): 统计天数，从今天开始向前计算
            - 默认: 7天
            - 范围: 1-90天
            - 示例: days=7 表示最近7天的每日趋势
            - 示例: days=30 表示最近30天的每日趋势
        db (Session): 数据库会话（自动注入）
        user: 当前用户信息（自动注入）
        
    Returns:
        dict[str, Any]: 按日期分组的趋势数据
        成功响应 (200):
        {
            "code": 200,
            "data": [
                {
                    "date": "2025-07-14",          // 日期 (YYYY-MM-DD格式)
                    "total_requests": 150,         // 当日总请求数
                    "blocked_requests": 12,        // 当日被拦截数
                    "pass_requests": 138           // 当日通过数
                },
                {
                    "date": "2025-07-13",
                    "total_requests": 180,
                    "blocked_requests": 15,
                    "pass_requests": 165
                },
                // ... 按日期倒序排列
            ]
        }
        失败响应 (500):
        {
            "code": 500,
            "retmsg": "获取趋势数据失败: 具体错误信息"
        }
        
    HTTP Status Codes:
        200: 请求成功
        400: 参数错误（如days超出范围）
        401: 未授权
        500: 服务器内部错误
        
    注意事项:
        - 返回数据按日期正序排列（最早日期在前）
        - 如果某天没有数据，该日期不会出现在结果中
        - 时间基于记录的create_time字段（毫秒时间戳）转换为日期
        - 适用于生成时间序列图表和趋势分析
    """
    try:
        trend_data = GuardLogService.get_trend_data(db, user.id, days)
        return get_json_result(data=trend_data)
        
    except Exception as e:
        return server_error_response(e)


@router.get('/stats/risk-words', summary="获取高频风险词")
def get_top_risk_words(
    days: int = 30,
    limit: int = 10,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取指定时间范围内的高频风险词统计（按出现次数降序排列）
    
    Args:
        days (int): 统计天数，从今天开始向前计算
            - 默认: 30天
            - 范围: 1-365天
            - 示例: days=7 表示最近7天出现的风险词
            - 示例: days=1 表示今天出现的风险词
        limit (int): 返回的风险词数量限制
            - 默认: 10个
            - 范围: 1-100个
            - 按出现频次从高到低排序
        db (Session): 数据库会话（自动注入）
        user: 当前用户信息（自动注入）
        
    Returns:
        dict[str, Any]: 高频风险词统计列表
        成功响应 (200):
        {
            "code": 200,
            "data": [
                {
                    "word": "敏感词1",           // 风险词内容
                    "count": 25                // 出现次数
                },
                {
                    "word": "敏感词2",
                    "count": 18
                },
                {
                    "word": "敏感词3", 
                    "count": 12
                },
                // ... 按count降序排列，最多limit个
            ]
        }
        失败响应 (500):
        {
            "code": 500,
            "retmsg": "获取高频风险词失败: 具体错误信息"
        }
        
    HTTP Status Codes:
        200: 请求成功
        400: 参数错误（如days或limit超出范围）
        401: 未授权
        500: 服务器内部错误
        
    注意事项:
        - 只统计包含risk_words字段的检测记录
        - 风险词来源于各类检测结果（敏感词、违禁词等）
        - 相同词汇在不同记录中出现会累加计数
        - 如果记录的risk_words是对象数组，会提取其中的'word'字段
        - 时间基于记录的create_time字段（毫秒时间戳）
        - 适用于了解热点风险词汇和优化检测规则
    """
    try:
        risk_words = GuardLogService.get_top_risk_words(db, user.id, days, limit)
        return get_json_result(data=risk_words)
        
    except Exception as e:
        return server_error_response(e)
# coding=utf-8
"""
@project: multirag
@Author：龙
@file： ai_guard_engine_service.py
@date：2025/01/31 
@desc: 渐进式AI安全护栏检测引擎服务 - 优先词库检测，预留完整流程兼容
"""
import logging
import hashlib
import time
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session

from api.db.services.guard_service_service import GuardServiceService
from api.db.services.guard_service_library_service import GuardServiceLibraryService
from api.db.services.guard_library_item_service import GuardLibraryItemService
from api.db.services.guard_log_service import GuardLogService


class AiGuardEngineService:
    """渐进式AI安全护栏检测引擎服务"""

    @classmethod
    def detect_content(cls, db: Session, content: str, service_id: str,
                       tenant_id: str, user_id: str = None, **kwargs) -> Dict[str, Any]:
        """
        统一内容检测接口（渐进式实现）
        
        Args:
            db: 数据库会话
            content: 待检测内容
            service_id: 服务ID
            tenant_id: 租户ID
            user_id: 用户ID
            **kwargs: 其他参数
            
        Returns:
            统一格式的检测结果
        """
        start_time = time.time()
        
        try:
            # 1. 获取服务配置
            service = GuardServiceService.get_by_id(db, service_id)
            if not service or service.tenant_id != tenant_id:
                return cls._create_error_result(content, "服务不存在或无权限")
            
            # 2. 检测模式判断
            detection_mode = cls._determine_detection_mode(service)
            
            # 3. 执行检测
            if detection_mode == "library_only":
                # 当前实现：仅基于词库检测
                detection_results = cls._detect_library_only(db, content, service)
            elif detection_mode == "comprehensive":
                # 后续实现：完整检测流程
                detection_results = cls._detect_comprehensive(db, content, service)
            else:
                return cls._create_pass_result(content, "未配置检测模式")
            
            # 4. 记录处理时间
            process_time_ms = int((time.time() - start_time) * 1000)
            detection_results["process_time_ms"] = process_time_ms
            detection_results["detection_mode"] = detection_mode
            
            # 5. 记录日志
            log_id = cls._log_detection_result(
                db, service, content, detection_results, 
                tenant_id, user_id, process_time_ms, **kwargs
            )
            detection_results["log_id"] = log_id
            
            # 6. 更新服务统计
            GuardServiceService.increment_request_count(
                db, service_id, detection_results["is_blocked"]
            )
            
            return detection_results
            
        except Exception as e:
            logging.error(f"内容检测失败: {e}")
            return cls._create_error_result(content, str(e))

    @classmethod
    def _determine_detection_mode(cls, service: Any) -> str:
        """
        判断检测模式
        
        Args:
            service: 服务对象
            
        Returns:
            检测模式: library_only/comprehensive
        """
        # 判断是否配置了维度和标签
        has_dimensions = bool(service.enabled_dimensions)
        has_labels = bool(service.enabled_labels)
        
        if has_dimensions and has_labels:
            return "comprehensive"  # 后续实现
        else:
            return "library_only"   # 当前实现

    @classmethod
    def _detect_library_only(cls, db: Session, content: str, service: Any) -> Dict[str, Any]:
        """
        仅基于词库的检测（当前实现）
        
        Args:
            db: 数据库会话
            content: 待检测内容
            service: 服务对象
            
        Returns:
            检测结果
        """
        # 获取服务绑定的词库
        logging.info(f"开始词库检测，服务ID: {service.id}, 内容: {content}")
        libraries = GuardServiceLibraryService.get_libraries_by_service(
            db, service.id, enabled_only=False  # 临时改为False，包含禁用的词库
        )
        logging.info(f"获取到 {len(libraries)} 个绑定词库: {[lib.get('name', 'unknown') for lib in libraries]}")
        
        detection_results = {
            "is_blocked": False,
            "overall_risk_score": 0.0,
            "action": "pass",
            "library_results": {
                "whitelist_matched": [],
                "blacklist_matched": []
            },
            "matched_items": [],
            "risk_words": [],
            # 预留完整版字段
            "dimension_results": {},
            "label_results": []
        }
        
        # 第一步：白名单预处理
        processed_content = content
        whitelist_results = []
        
        for library_info in libraries:
            effective_type = library_info.get("effective_library_type")
            logging.info(f"词库 {library_info.get('name')} 的有效类型: {effective_type}")
            if effective_type == "whitelist":
                whitelist_result = cls._process_whitelist(
                    content, processed_content, library_info, db
                )
                if whitelist_result["matched"]:
                    whitelist_results.append(whitelist_result["result"])
                    processed_content = whitelist_result["processed_content"]
        
        detection_results["library_results"]["whitelist_matched"] = whitelist_results
        
        # 第二步：黑名单检测
        blacklist_results = []
        logging.info("开始黑名单检测...")
        
        for library_info in libraries:
            effective_type = library_info.get("effective_library_type")
            logging.info(f"检查词库: {library_info.get('name')}, 有效类型: {effective_type}")
            if effective_type == "blacklist":
                logging.info(f"处理黑名单词库: {library_info.get('name')}")
                blacklist_result = cls._process_blacklist(
                    content, library_info, db  # 使用原始内容
                )
                logging.info(f"黑名单检测结果: {blacklist_result}")
                if blacklist_result["matched"]:
                    blacklist_results.append(blacklist_result["result"])
                    
                    # 更新总体风险分数
                    risk_score = blacklist_result["result"]["risk_score"]
                    detection_results["overall_risk_score"] = max(
                        detection_results["overall_risk_score"], risk_score
                    )
                    
                    # 添加到通用匹配项
                    for word in blacklist_result["result"]["matched_words"]:
                        detection_results["matched_items"].append({
                            "type": "keyword",
                            "content": word,
                            "source": "blacklist_library",
                            "library_name": blacklist_result["result"]["library_name"]
                        })
                        detection_results["risk_words"].append(word)
        
        detection_results["library_results"]["blacklist_matched"] = blacklist_results
        
        # 第三步：决定最终动作
        final_action = cls._determine_action_library_mode(
            detection_results, service.policy_config
        )
        detection_results["action"] = final_action
        detection_results["is_blocked"] = final_action == "block"
        
        return detection_results

    @classmethod
    def _process_whitelist(cls, original_content: str, processed_content: str,
                          library_info: Dict[str, Any], db: Session) -> Dict[str, Any]:
        """
        处理白名单词库
        
        Args:
            original_content: 原始内容
            processed_content: 已处理内容
            library_info: 词库信息
            db: 数据库会话
            
        Returns:
            处理结果
        """
        # 直接查询词库项，不过滤status（因为改为硬删除）
        from api.db.db_models import GuardLibraryItem
        library_items = db.query(GuardLibraryItem).filter(
            GuardLibraryItem.library_id == library_info["id"]
        ).all()
        
        matched_words = []
        current_content = processed_content
        
        for item in library_items:
            if item.content.lower() in original_content.lower():
                matched_words.append(item.content)
                # 从待检测内容中移除白名单词汇
                current_content = current_content.replace(item.content, "")
                # 更新命中统计
                GuardLibraryItemService.increment_hit_count(db, item.id)
        
        return {
            "matched": len(matched_words) > 0,
            "processed_content": current_content,
            "result": {
                "library_id": library_info["id"],
                "library_name": library_info["name"],
                "matched_words": matched_words,
                "action": "ignored"
            }
        }

    @classmethod
    def _process_blacklist(cls, content: str, library_info: Dict[str, Any],
                          db: Session) -> Dict[str, Any]:
        """
        处理黑名单词库
        
        Args:
            content: 待检测内容
            library_info: 词库信息
            db: 数据库会话
            
        Returns:
            处理结果
        """
        # 直接查询词库项，不过滤status（因为改为硬删除）
        from api.db.db_models import GuardLibraryItem
        library_items = db.query(GuardLibraryItem).filter(
            GuardLibraryItem.library_id == library_info["id"]
        ).all()
        logging.info(f"黑名单词库 {library_info.get('name')} 包含 {len(library_items)} 个词汇")
        
        matched_words = []
        
        for item in library_items:
            logging.info(f"检查词汇: '{item.content}' 是否在内容 '{content}' 中")
            if item.content.lower() in content.lower():
                logging.info(f"匹配到黑名单词汇: {item.content}")
                matched_words.append(item.content)
                # 更新命中统计
                GuardLibraryItemService.increment_hit_count(db, item.id)
        
        if matched_words:
            # 计算风险分数
            base_score = 80.0  # 黑名单基础分数
            risk_score = min(100.0, base_score * (1 + len(matched_words) * 0.1))
            
            # 获取自定义标签配置
            binding_config = library_info.get("binding", {}).get("config", {})
            custom_label = binding_config.get("custom_label", f"blacklist_{library_info['id']}")
            
            return {
                "matched": True,
                "result": {
                    "library_id": library_info["id"],
                    "library_name": library_info["name"],
                    "matched_words": matched_words,
                    "custom_label": custom_label,
                    "risk_score": risk_score
                }
            }
        
        return {"matched": False, "result": {}}

    @classmethod
    def _detect_comprehensive(cls, db: Session, content: str, service: Any) -> Dict[str, Any]:
        """
        完整检测流程（当前先实现词库检测部分）
        
        Args:
            db: 数据库会话
            content: 待检测内容
            service: 服务对象
            
        Returns:
            检测结果
        """
        # 当前实现：先执行词库检测，为完整流程打基础
        logging.info(f"执行完整检测流程，服务ID: {service.id}")
        
        # 直接调用词库检测逻辑
        detection_results = cls._detect_library_only(db, content, service)
        
        # TODO: 后续在这里添加维度检测逻辑
        # TODO: 后续在这里添加标签检测逻辑  
        # TODO: 后续在这里添加代答库处理逻辑
        
        logging.info(f"完整检测结果: {detection_results}")
        return detection_results

    @classmethod
    def _determine_action_library_mode(cls, detection_results: Dict[str, Any],
                                      policy_config: Dict[str, Any]) -> str:
        """
        词库模式下的动作决策
        
        Args:
            detection_results: 检测结果
            policy_config: 策略配置
            
        Returns:
            动作: pass/warn/block
        """
        try:
            risk_score = detection_results["overall_risk_score"]
            risk_threshold = policy_config.get("risk_threshold", 70.0)
            default_action = policy_config.get("default_action", "warn")
            
            if risk_score >= 90.0:
                return "block"
            elif risk_score >= risk_threshold:
                return default_action
            else:
                return "pass"
                
        except Exception as e:
            logging.error(f"决定动作失败: {e}")
            return "pass"

    @classmethod
    def _log_detection_result(cls, db: Session, service: Any, content: str,
                             detection_results: Dict[str, Any], tenant_id: str,
                             user_id: str = None, process_time_ms: int = 0,
                             **kwargs) -> Optional[str]:
        """
        记录检测结果日志
        """
        try:
            log_data = {
                "service_id": service.id,
                "service_code": service.code,
                "content": content,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "is_blocked": detection_results["is_blocked"],
                "risk_score": detection_results["overall_risk_score"],
                "action_taken": detection_results["action"],
                "risk_words": detection_results["risk_words"],
                "process_time_ms": process_time_ms,
                "cloud_service_used": False,
                
                # 当前基于词库的检测结果
                "content_results": detection_results.get("library_results", {}),
                "sensitive_results": [],
                "attack_results": [],
                "customized_hits": detection_results.get("matched_items", []),
                
                # 额外信息
                "request_id": kwargs.get("request_id"),
                "chat_id": kwargs.get("chat_id"),
                "source_type": kwargs.get("source_type", "api"),
                "source_id": kwargs.get("source_id")
            }
            
            return GuardLogService.create_log(db, **log_data)
            
        except Exception as e:
            logging.error(f"记录检测日志失败: {e}")
            return None

    @classmethod
    def _create_pass_result(cls, content: str, reason: str = "") -> Dict[str, Any]:
        """创建通过结果"""
        return {
            "is_blocked": False,
            "overall_risk_score": 0.0,
            "action": "pass",
            "library_results": {"whitelist_matched": [], "blacklist_matched": []},
            "dimension_results": {},
            "label_results": [],
            "matched_items": [],
            "risk_words": [],
            "reason": reason,
            "process_time_ms": 0
        }

    @classmethod
    def _create_error_result(cls, content: str, error: str) -> Dict[str, Any]:
        """创建错误结果"""
        return {
            "is_blocked": False,
            "overall_risk_score": 0.0,
            "action": "pass",
            "library_results": {"whitelist_matched": [], "blacklist_matched": []},
            "dimension_results": {},
            "label_results": [],
            "matched_items": [],
            "risk_words": [],
            "error": error,
            "process_time_ms": 0
        }
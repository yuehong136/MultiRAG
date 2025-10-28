# coding=utf-8
"""
@project: multirag
@Author：龙
@file： guard_log_service.py
@date：2025/01/11 16:45
@desc: AI安全护栏日志管理服务
"""
import logging
import hashlib
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import and_, desc, func, text
from datetime import datetime, timedelta

from api.db.db_models import GuardLog
from api.db.services.common_service import CommonService
from api.utils import get_uuid


class GuardLogService(CommonService):
    """AI安全护栏日志管理服务"""
    model = GuardLog

    @classmethod
    def create_log(cls, db: Session, service_id: str, service_code: str,
                  content: str, tenant_id: str, **kwargs) -> Optional[str]:
        """
        创建护栏日志
        
        Args:
            db: 数据库会话
            service_id: 服务ID
            service_code: 服务代码
            content: 检测内容
            tenant_id: 租户ID
            **kwargs: 其他参数
            
        Returns:
            创建成功返回日志ID，失败返回None
        """
        try:
            content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
            content_preview = content[:500] if len(content) > 500 else content
            
            log_data = {
                "id": get_uuid(),
                "service_id": service_id,
                "service_code": service_code,
                "tenant_id": tenant_id,
                "content_hash": content_hash,
                "content_length": len(content),
                "content_preview": content_preview,
                "request_id": kwargs.get("request_id"),
                "chat_id": kwargs.get("chat_id"),
                "user_id": kwargs.get("user_id"),
                "is_blocked": kwargs.get("is_blocked", False),
                "risk_score": kwargs.get("risk_score", 0.0),
                "content_risk_level": kwargs.get("content_risk_level"),
                "content_results": kwargs.get("content_results", []),
                "sensitive_level": kwargs.get("sensitive_level"),
                "sensitive_results": kwargs.get("sensitive_results", []),
                "attack_level": kwargs.get("attack_level"),
                "attack_results": kwargs.get("attack_results", []),
                "customized_hits": kwargs.get("customized_hits", []),
                "risk_words": kwargs.get("risk_words", []),
                "sensitive_data": kwargs.get("sensitive_data", []),
                "action_taken": kwargs.get("action_taken"),
                "action_detail": kwargs.get("action_detail", {}),
                "source_type": kwargs.get("source_type"),
                "source_id": kwargs.get("source_id"),
                "client_ip": kwargs.get("client_ip"),
                "user_agent": kwargs.get("user_agent"),
                "process_time_ms": kwargs.get("process_time_ms"),
                "cloud_service_used": kwargs.get("cloud_service_used", False)
            }
            
            log = cls.save(db, **log_data)
            return log.id
            
        except Exception as e:
            logging.error(f"创建护栏日志失败: {e}")
            return None

    @classmethod
    def get_logs_by_tenant(cls, db: Session, tenant_id: str,
                          page: int = 1, page_size: int = 50,
                          start_date: str = None, end_date: str = None,
                          service_code: str = None, is_blocked: bool = None) -> Dict[str, Any]:
        """
        获取租户的日志列表（分页）
        
        Args:
            db: 数据库会话
            tenant_id: 租户ID
            page: 页码
            page_size: 每页数量
            start_date: 开始日期
            end_date: 结束日期
            service_code: 服务代码
            is_blocked: 是否被拦截
            
        Returns:
            包含日志列表和分页信息的字典
        """
        try:
            query = db.query(cls.model).filter(
                cls.model.tenant_id == tenant_id
            )
            
            # 时间范围过滤
            if start_date:
                start_dt = datetime.fromisoformat(start_date)
                start_timestamp_ms = int(start_dt.timestamp() * 1000)
                query = query.filter(cls.model.create_time >= start_timestamp_ms)
            
            if end_date:
                end_dt = datetime.fromisoformat(end_date)
                end_timestamp_ms = int(end_dt.timestamp() * 1000)
                query = query.filter(cls.model.create_time <= end_timestamp_ms)
            
            # 服务代码过滤
            if service_code:
                query = query.filter(cls.model.service_code == service_code)
            
            # 拦截状态过滤
            if is_blocked is not None:
                query = query.filter(cls.model.is_blocked == is_blocked)
            
            total = query.count()
            logs = query.order_by(desc(cls.model.create_time))\
                       .offset((page - 1) * page_size)\
                       .limit(page_size).all()
            
            return {
                "logs": logs,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size
            }
        except Exception as e:
            logging.error(f"获取日志列表失败: {e}")
            return {"logs": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0}

    @classmethod
    def get_log_stats(cls, db: Session, tenant_id: str, 
                     days: int = 30) -> Dict[str, Any]:
        """
        获取日志统计信息
        
        Args:
            db: 数据库会话
            tenant_id: 租户ID
            days: 统计天数
            
        Returns:
            统计信息字典
        """
        try:
            start_date = datetime.now() - timedelta(days=days)
            start_timestamp_ms = int(start_date.timestamp() * 1000)
            
            query = db.query(cls.model).filter(
                and_(
                    cls.model.tenant_id == tenant_id,
                    cls.model.create_time >= start_timestamp_ms
                )
            )
            
            total_requests = query.count()
            blocked_requests = query.filter(cls.model.is_blocked == True).count()
            
            # 服务统计
            service_stats = db.query(
                cls.model.service_code,
                func.count(cls.model.id).label('count'),
                func.sum(func.cast(cls.model.is_blocked, int)).label('blocked_count')
            ).filter(
                and_(
                    cls.model.tenant_id == tenant_id,
                    cls.model.create_time >= start_timestamp_ms
                )
            ).group_by(cls.model.service_code).all()
            
            # 风险等级统计
            risk_level_stats = db.query(
                cls.model.content_risk_level,
                func.count(cls.model.id).label('count')
            ).filter(
                and_(
                    cls.model.tenant_id == tenant_id,
                    cls.model.create_time >= start_timestamp_ms,
                    cls.model.content_risk_level.isnot(None)
                )
            ).group_by(cls.model.content_risk_level).all()
            
            return {
                "total_requests": total_requests,
                "blocked_requests": blocked_requests,
                "pass_requests": total_requests - blocked_requests,
                "block_rate": (blocked_requests / total_requests * 100) if total_requests > 0 else 0,
                "service_stats": {stat.service_code: {"total": stat.count, "blocked": stat.blocked_count or 0} for stat in service_stats},
                "risk_level_stats": {stat.content_risk_level: stat.count for stat in risk_level_stats},
                "period_days": days
            }
        except Exception as e:
            logging.error(f"获取日志统计失败: {e}")
            return {}

    @classmethod
    def get_trend_data(cls, db: Session, tenant_id: str, 
                      days: int = 7) -> List[Dict[str, Any]]:
        """
        获取趋势数据
        
        Args:
            db: 数据库会话
            tenant_id: 租户ID
            days: 统计天数
            
        Returns:
            趋势数据列表
        """
        try:
            start_date = datetime.now() - timedelta(days=days)
            start_timestamp_ms = int(start_date.timestamp() * 1000)
            
            # 获取所有符合条件的记录
            logs = db.query(cls.model).filter(
                and_(
                    cls.model.tenant_id == tenant_id,
                    cls.model.create_time >= start_timestamp_ms
                )
            ).all()
            
            # 在Python中按天分组统计
            daily_stats = {}
            for log in logs:
                # 将毫秒时间戳转换为日期
                log_date = datetime.fromtimestamp(log.create_time / 1000).date()
                date_str = log_date.isoformat()
                
                if date_str not in daily_stats:
                    daily_stats[date_str] = {
                        "total": 0,
                        "blocked": 0
                    }
                
                daily_stats[date_str]["total"] += 1
                if log.is_blocked:
                    daily_stats[date_str]["blocked"] += 1
            
            # 转换为列表格式
            trend_data = []
            for date_str, stats in daily_stats.items():
                trend_data.append({
                    "date": date_str,
                    "total_requests": stats["total"],
                    "blocked_requests": stats["blocked"],
                    "pass_requests": stats["total"] - stats["blocked"]
                })
            
            return sorted(trend_data, key=lambda x: x["date"])
        except Exception as e:
            logging.error(f"获取趋势数据失败: {e}")
            return []

    @classmethod
    def get_top_risk_words(cls, db: Session, tenant_id: str, 
                          days: int = 30, limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取高频风险词
        
        Args:
            db: 数据库会话
            tenant_id: 租户ID
            days: 统计天数
            limit: 返回数量
            
        Returns:
            高频风险词列表
        """
        try:
            start_date = datetime.now() - timedelta(days=days)
            start_timestamp_ms = int(start_date.timestamp() * 1000)
            
            logs = db.query(cls.model).filter(
                and_(
                    cls.model.tenant_id == tenant_id,
                    cls.model.create_time >= start_timestamp_ms,
                    cls.model.risk_words.isnot(None)
                )
            ).all()
            
            word_counts = {}
            for log in logs:
                for word in log.risk_words:
                    if isinstance(word, str):
                        word_counts[word] = word_counts.get(word, 0) + 1
                    elif isinstance(word, dict) and 'word' in word:
                        w = word['word']
                        word_counts[w] = word_counts.get(w, 0) + 1
            
            # 排序并返回前N个
            top_words = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
            
            return [{"word": word, "count": count} for word, count in top_words]
        except Exception as e:
            logging.error(f"获取高频风险词失败: {e}")
            return []

    @classmethod
    def cleanup_old_logs(cls, db: Session, tenant_id: str, 
                        days: int = 90) -> int:
        """
        清理旧日志
        
        Args:
            db: 数据库会话
            tenant_id: 租户ID
            days: 保留天数
            
        Returns:
            删除的记录数
        """
        try:
            cutoff_date = datetime.now() - timedelta(days=days)
            cutoff_timestamp_ms = int(cutoff_date.timestamp() * 1000)
            
            deleted_count = db.query(cls.model).filter(
                and_(
                    cls.model.tenant_id == tenant_id,
                    cls.model.create_time < cutoff_timestamp_ms
                )
            ).delete()
            
            db.commit()
            return deleted_count
        except Exception as e:
            logging.error(f"清理旧日志失败: {e}")
            db.rollback()
            return 0
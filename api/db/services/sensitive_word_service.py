# coding=utf-8
"""
@project: multirag
@Author：龙
@file： sensitive_word_service.py
@date：2025/01/07 09:30
@desc: 敏感词管理服务
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional
import pickle
import base64

from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, desc, func
from sqlalchemy.exc import IntegrityError

# from api.db.db_models import (
#     SensitiveWordCategory, SensitiveWordLevel, SensitiveWord,
#     SensitiveWordWhitelist, SensitiveFilterLog, SensitiveFilterStats
# )
from api.db.services.common_service import CommonService
from common.misc_utils import get_uuid
from core.utils.redis_conn import RedisDB


class ACAutomaton:
    """AC自动机算法实现"""
    
    def __init__(self):
        self.goto = {}
        self.fail = {}
        self.output = {}
        self.words = set()
    
    def add_word(self, word: str, word_info: dict = None):
        """添加敏感词"""
        if not word or word in self.words:
            return
            
        self.words.add(word)
        current = 0
        
        for char in word:
            if (current, char) not in self.goto:
                self.goto[(current, char)] = len(self.goto) + 1
            current = self.goto[(current, char)]
        
        if current not in self.output:
            self.output[current] = []
        self.output[current].append({
            'word': word,
            'info': word_info or {}
        })
    
    def build_failure_function(self):
        """构建失败函数"""
        queue = []
        
        # 初始化第一层的失败函数
        for key, state in self.goto.items():
            if key[0] == 0:
                self.fail[state] = 0
                queue.append(state)
        
        # BFS构建失败函数
        while queue:
            current_state = queue.pop(0)
            
            for key, next_state in self.goto.items():
                if key[0] == current_state:
                    char = key[1]
                    queue.append(next_state)
                    
                    temp_state = self.fail[current_state]
                    while temp_state != 0 and (temp_state, char) not in self.goto:
                        temp_state = self.fail[temp_state]
                    
                    if (temp_state, char) in self.goto:
                        self.fail[next_state] = self.goto[(temp_state, char)]
                    else:
                        self.fail[next_state] = 0
                    
                    # 继承失败状态的输出
                    if self.fail[next_state] in self.output:
                        if next_state not in self.output:
                            self.output[next_state] = []
                        self.output[next_state].extend(self.output[self.fail[next_state]])
    
    def search(self, text: str) -> list[dict]:
        """搜索敏感词"""
        if not self.goto:
            return []
        
        results = []
        current_state = 0
        
        for i, char in enumerate(text):
            # 状态转移
            while current_state != 0 and (current_state, char) not in self.goto:
                current_state = self.fail[current_state]
            
            if (current_state, char) in self.goto:
                current_state = self.goto[(current_state, char)]
            
            # 检查输出
            if current_state in self.output:
                for match in self.output[current_state]:
                    word = match['word']
                    start_pos = i - len(word) + 1
                    results.append({
                        'word': word,
                        'start': start_pos,
                        'end': i + 1,
                        'info': match['info']
                    })
        
        return results


class RegexMatcher:
    """正则表达式匹配器"""
    
    def __init__(self):
        self.patterns = []  # [(compiled_regex, word_info), ...]
        
    def add_pattern(self, pattern: str, word_info: dict):
        """添加正则表达式模式"""
        try:
            # 编译正则表达式，提高匹配性能
            flags = 0
            if not word_info.get('case_sensitive', False):
                flags |= re.IGNORECASE
                
            compiled_pattern = re.compile(pattern, flags)
            self.patterns.append((compiled_pattern, word_info))
        except re.error as e:
            logging.warning(f"无效的正则表达式 '{pattern}': {e}")
    
    def search(self, text: str) -> list[dict]:
        """在文本中搜索所有匹配的正则表达式"""
        results = []
        
        for pattern, word_info in self.patterns:
            try:
                for match in pattern.finditer(text):
                    results.append({
                        'word': match.group(),
                        'start': match.start(),
                        'end': match.end(),
                        'info': word_info,
                        'match_type': 'regex',
                        'pattern': pattern.pattern
                    })
            except Exception as e:
                logging.warning(f"正则匹配失败 '{pattern.pattern}': {e}")
                
        return results


class PartialMatcher:
    """部分匹配器（支持通配符）"""
    
    def __init__(self):
        self.patterns = []  # [(pattern, word_info), ...]
        
    def add_pattern(self, pattern: str, word_info: dict):
        """添加部分匹配模式（支持*和?通配符）"""
        # 将通配符转换为正则表达式
        # * -> .*  (匹配任意字符)
        # ? -> .   (匹配单个字符)
        regex_pattern = re.escape(pattern)
        regex_pattern = regex_pattern.replace(r'\*', '.*').replace(r'\?', '.')
        regex_pattern = f'({regex_pattern})'  # 添加捕获组
        
        try:
            flags = 0
            if not word_info.get('case_sensitive', False):
                flags |= re.IGNORECASE
                
            compiled_pattern = re.compile(regex_pattern, flags)
            self.patterns.append((compiled_pattern, word_info, pattern))
        except re.error as e:
            logging.warning(f"无效的部分匹配模式 '{pattern}': {e}")
    
    def search(self, text: str) -> list[dict]:
        """搜索部分匹配"""
        results = []
        
        for pattern, word_info, original_pattern in self.patterns:
            try:
                for match in pattern.finditer(text):
                    results.append({
                        'word': match.group(),
                        'start': match.start(),
                        'end': match.end(),
                        'info': word_info,
                        'match_type': 'partial',
                        'pattern': original_pattern
                    })
            except Exception as e:
                logging.warning(f"部分匹配失败 '{original_pattern}': {e}")
                
        return results


class PIIDetector:
    """个人敏感信息检测器"""
    
    def __init__(self):
        self.patterns = {
            'phone': [
                r'1[3-9]\d{9}',  # 中国手机号
                r'\+86\s*1[3-9]\d{9}',  # 带国际区号的中国手机号
            ],
            'email': [
                r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
            ],
            'id_card': [
                r'\b\d{15}\b',  # 15位身份证号
                r'\b\d{17}[\dXx]\b',  # 18位身份证号
            ],
            'bank_card': [
                r'\b\d{16,19}\b'  # 银行卡号
            ],
            'ip_address': [
                r'\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b'
            ]
        }
        
        # PII类型的替换文本
        self.pii_types = {
            'phone': {'name': '手机号', 'replacement': '[手机号]'},
            'email': {'name': '邮箱', 'replacement': '[邮箱]'},
            'id_card': {'name': '身份证', 'replacement': '[身份证]'},
            'bank_card': {'name': '银行卡', 'replacement': '[银行卡]'},
            'ip_address': {'name': 'IP地址', 'replacement': '[IP地址]'}
        }
    
    def detect(self, text: str) -> list[dict]:
        """检测PII信息"""
        results = []
        
        for pii_type, patterns in self.patterns.items():
            for pattern in patterns:
                matches = re.finditer(pattern, text)
                for match in matches:
                    results.append({
                        'type': pii_type,
                        'value': match.group(),
                        'start': match.start(),
                        'end': match.end()
                    })
        
        return results

#
# class SensitiveWordCacheManager:
#     """敏感词缓存管理器"""
#
#     def __init__(self):
#         self.redis = RedisDB()
#         self.cache_prefix = "sensitive_words"
#         self.expire_time = 3600 * 24  # 24小时过期
#
#     def get_cache_key(self, tenant_id: str, key_type: str = "words"):
#         """获取缓存键"""
#         return f"{self.cache_prefix}:{tenant_id}:{key_type}"
#
#     def get_tenant_matchers(self, tenant_id: str) -> tuple[ACAutomaton, RegexMatcher, PartialMatcher] | None:
#         """获取租户的所有匹配器（从缓存）"""
#         cache_key = self.get_cache_key(tenant_id, "matchers")
#         try:
#             cached_data = self.redis.get(cache_key)
#             if cached_data:
#                 # 反序列化匹配器
#                 matchers_data = pickle.loads(base64.b64decode(cached_data))
#                 return (
#                     matchers_data.get('ac_automaton'),
#                     matchers_data.get('regex_matcher'),
#                     matchers_data.get('partial_matcher')
#                 )
#         except Exception as e:
#             logging.warning(f"获取匹配器缓存失败: {e}")
#         return None
#
#     def set_tenant_matchers(self, tenant_id: str, ac_automaton: ACAutomaton,
#                           regex_matcher: RegexMatcher, partial_matcher: PartialMatcher):
#         """缓存租户的所有匹配器"""
#         cache_key = self.get_cache_key(tenant_id, "matchers")
#         try:
#             # 序列化匹配器
#             matchers_data = {
#                 'ac_automaton': ac_automaton,
#                 'regex_matcher': regex_matcher,
#                 'partial_matcher': partial_matcher,
#                 'timestamp': datetime.utcnow().isoformat()
#             }
#
#             serialized_data = pickle.dumps(matchers_data)
#             self.redis.set(cache_key, base64.b64encode(serialized_data).decode('utf-8'), self.expire_time)
#
#             # 记录缓存大小
#             cache_size_kb = len(serialized_data) / 1024
#             logging.info(f"缓存匹配器成功，租户: {tenant_id}, 大小: {cache_size_kb:.2f}KB")
#
#         except Exception as e:
#             logging.warning(f"缓存匹配器失败: {e}")
#
#     def get_tenant_words(self, tenant_id: str) -> list[dict] | None:
#         """获取租户敏感词缓存"""
#         cache_key = self.get_cache_key(tenant_id, "words")
#         try:
#             cached_data = self.redis.get(cache_key)
#             if cached_data:
#                 return json.loads(cached_data)
#         except Exception as e:
#             logging.warning(f"获取敏感词缓存失败: {e}")
#         return None
#
#     def set_tenant_words(self, tenant_id: str, words: list[dict]):
#         """设置租户敏感词缓存"""
#         cache_key = self.get_cache_key(tenant_id, "words")
#         try:
#             # 使用现有RedisDB的set方法，exp参数是位置参数
#             self.redis.set(cache_key, json.dumps(words, default=str), self.expire_time)
#         except Exception as e:
#             logging.warning(f"设置敏感词缓存失败: {e}")
#
#     def get_tenant_whitelist(self, tenant_id: str) -> list[str] | None:
#         """获取租户白名单缓存"""
#         cache_key = self.get_cache_key(tenant_id, "whitelist")
#         try:
#             cached_data = self.redis.get(cache_key)
#             if cached_data:
#                 return json.loads(cached_data)
#         except Exception as e:
#             logging.warning(f"获取白名单缓存失败: {e}")
#         return None
#
#     def set_tenant_whitelist(self, tenant_id: str, whitelist: list[str]):
#         """设置租户白名单缓存"""
#         cache_key = self.get_cache_key(tenant_id, "whitelist")
#         try:
#             # 使用现有RedisDB的set方法，exp参数是位置参数
#             self.redis.set(cache_key, json.dumps(whitelist), self.expire_time)
#         except Exception as e:
#             logging.warning(f"设置白名单缓存失败: {e}")
#
#     def invalidate_tenant_cache(self, tenant_id: str):
#         """清除租户缓存"""
#         try:
#             # 由于现有RedisDB没有keys方法，我们需要手动删除已知的缓存键
#             keys_to_delete = [
#                 self.get_cache_key(tenant_id, "words"),
#                 self.get_cache_key(tenant_id, "whitelist"),
#                 self.get_cache_key(tenant_id, "matchers"),
#                 self.get_cache_key(tenant_id, "version")
#             ]
#
#             # 检查并删除现有的缓存键
#             if hasattr(self.redis, 'REDIS') and self.redis.REDIS:
#                 for key in keys_to_delete:
#                     try:
#                         if self.redis.exist(key):
#                             self.redis.REDIS.delete(key)
#                     except Exception:
#                         pass
#         except Exception as e:
#             logging.warning(f"清除租户缓存失败: {e}")
#
#     def get_cache_stats(self, tenant_id: str) -> dict:
#         """获取缓存统计信息"""
#         try:
#             words_key = self.get_cache_key(tenant_id, "words")
#             whitelist_key = self.get_cache_key(tenant_id, "whitelist")
#             matchers_key = self.get_cache_key(tenant_id, "matchers")
#
#             stats = {
#                 "words_cached": bool(self.redis.exist(words_key)),
#                 "whitelist_cached": bool(self.redis.exist(whitelist_key)),
#                 "matchers_cached": bool(self.redis.exist(matchers_key)),
#                 "cache_hit_rate": 0.0,  # 需要实现计数器来统计
#                 "last_refresh_time": None,
#                 "memory_usage": "N/A"
#             }
#
#             # 获取最后刷新时间
#             if stats["matchers_cached"]:
#                 try:
#                     matchers_data = self.redis.get(matchers_key)
#                     if matchers_data:
#                         unpacked = pickle.loads(base64.b64decode(matchers_data))
#                         stats["last_refresh_time"] = unpacked.get('timestamp')
#                         stats["memory_usage"] = f"{len(base64.b64decode(matchers_data)) / 1024:.2f}KB"
#                 except Exception:
#                     pass
#
#             return stats
#         except Exception as e:
#             logging.warning(f"获取缓存统计失败: {e}")
#             return {}
#
#
# class SensitiveWordCategoryService(CommonService):
#     """敏感词分类服务"""
#     model = SensitiveWordCategory
#
#
# class SensitiveWordLevelService(CommonService):
#     """敏感词等级服务"""
#     model = SensitiveWordLevel
#
#
# class SensitiveWordWhitelistService(CommonService):
#     """敏感词白名单服务"""
#     model = SensitiveWordWhitelist
#
#     @classmethod
#     def create_whitelist_word(cls, db: Session, word: str, reason: str = None,
#                             tenant_id: str = None, created_by: str = None) -> str | None:
#         """创建白名单词汇"""
#         try:
#             word_hash = hashlib.md5(word.encode('utf-8')).hexdigest()
#
#             whitelist_data = {
#                 "id": get_uuid(),
#                 "word": word,
#                 "word_hash": word_hash,
#                 "reason": reason,
#                 "tenant_id": tenant_id,
#                 "created_by": created_by
#             }
#
#             if cls.save(db, **whitelist_data):
#                 # 清除相关缓存
#                 cache_manager = SensitiveWordCacheManager()
#                 cache_manager.invalidate_tenant_cache(tenant_id)
#                 return whitelist_data["id"]
#             return None
#         except Exception as e:
#             logging.error(f"创建白名单失败: {e}")
#             return None
#
#
# class SensitiveFilterLogService(CommonService):
#     """敏感词过滤日志服务"""
#     model = SensitiveFilterLog
#
#     @classmethod
#     def log_filter_action(cls, db: Session, user_id: str | None = None, tenant_id: str | None = None,
#                          content: str | None = None, matched_words: list[dict] | None = None,
#                          filter_action: str | None = None, source_type: str | None = None,
#                          source_id: str | None = None, ip_address: str | None = None,
#                          user_agent: str | None = None):
#         """记录过滤日志"""
#         try:
#             # content_hash = hashlib.md5(content.encode('utf-8')).hexdigest() if content else ""
#
#             log_data = {
#                 "id": get_uuid(),
#                 "user_id": user_id,
#                 "tenant_id": tenant_id,
#                 # "content_hash": content_hash,
#                 "content_hash": content[:50] + "..." if len(content) > 50 else content,
#                 "matched_words": matched_words or [],
#                 "filter_action": filter_action,
#                 "source_type": source_type[:64] if source_type else None,  # 限制长度
#                 "source_id": source_id[:255] if source_id else None,  # 限制长度
#                 "ip_address": ip_address[:64] if ip_address else None,  # 限制长度
#                 "user_agent": user_agent
#             }
#
#             cls.save(db, **log_data)
#         except Exception as e:
#             logging.error(f"记录过滤日志失败: {e}")
#
#     @classmethod
#     def get_paginated_logs(cls, db: Session, tenant_id: str, page: int = 1,
#                           page_size: int = 50, start_date: str = None,
#                           end_date: str = None) -> dict:
#         """获取分页日志"""
#         try:
#             query = db.query(cls.model).filter(cls.model.tenant_id == tenant_id)
#
#             if start_date:
#                 start_dt = datetime.fromisoformat(start_date)
#                 query = query.filter(cls.model.create_date >= start_dt)
#
#             if end_date:
#                 end_dt = datetime.fromisoformat(end_date)
#                 query = query.filter(cls.model.create_date <= end_dt)
#
#             total = query.count()
#
#             logs = query.order_by(desc(cls.model.create_date))\
#                        .offset((page - 1) * page_size)\
#                        .limit(page_size)\
#                        .all()
#
#             return {
#                 "logs": [log.to_dict() for log in logs],
#                 "total": total,
#                 "page": page,
#                 "page_size": page_size,
#                 "total_pages": (total + page_size - 1) // page_size
#             }
#         except Exception as e:
#             logging.error(f"获取过滤日志失败: {e}")
#             return {"logs": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0}
#
#
# class SensitiveFilterStatsService(CommonService):
#     """敏感词过滤统计服务"""
#     model = SensitiveFilterStats
#
#     @classmethod
#     def get_tenant_overview(cls, db: Session, tenant_id: str) -> dict:
#         """获取租户统计概览"""
#         try:
#             # 获取最近30天的统计
#             thirty_days_ago = datetime.now() - timedelta(days=30)
#
#             stats = db.query(cls.model)\
#                      .filter(cls.model.tenant_id == tenant_id)\
#                      .filter(cls.model.stat_date >= thirty_days_ago)\
#                      .all()
#
#             total_requests = sum(stat.total_requests for stat in stats)
#             filtered_requests = sum(stat.filtered_requests for stat in stats)
#
#             # 获取敏感词数量
#             words_count = db.query(SensitiveWord)\
#                            .filter(SensitiveWord.tenant_id == tenant_id)\
#                            .filter(SensitiveWord.status == "1")\
#                            .count()
#
#             # 获取分类数量
#             categories_count = db.query(SensitiveWordCategory)\
#                               .filter(SensitiveWordCategory.tenant_id == tenant_id)\
#                               .filter(SensitiveWordCategory.status == "1")\
#                               .count()
#
#             # 计算过滤率
#             filter_rate = (filtered_requests / total_requests * 100) if total_requests > 0 else 0
#
#             return {
#                 "total_requests": total_requests,
#                 "filtered_requests": filtered_requests,
#                 "filter_rate": round(filter_rate, 2),
#                 "words_count": words_count,
#                 "categories_count": categories_count,
#                 "stats_period": "last_30_days"
#             }
#         except Exception as e:
#             logging.error(f"获取统计概览失败: {e}")
#             return {}
#
#
# class SensitiveWordService(CommonService):
#     """敏感词核心服务"""
#     model = SensitiveWord
#
#     @classmethod
#     def create_sensitive_word(cls, db: Session, word: str, category_id: str,
#                             level_id: str, match_type: str = "exact",
#                             description: str = None, source: str = None,
#                             tenant_id: str = None, created_by: str = None) -> str | None:
#         """创建敏感词"""
#         try:
#             word_hash = hashlib.md5(word.encode('utf-8')).hexdigest()
#
#             # 检查是否已存在
#             existing = db.query(cls.model)\
#                         .filter(cls.model.word_hash == word_hash)\
#                         .filter(cls.model.tenant_id == tenant_id)\
#                         .filter(cls.model.status == "1")\
#                         .first()
#
#             if existing:
#                 return None  # 已存在
#
#             word_data = {
#                 "id": get_uuid(),
#                 "word": word,
#                 "word_hash": word_hash,
#                 "category_id": category_id,
#                 "level_id": level_id,
#                 "match_type": match_type,
#                 "description": description,
#                 "source": source,
#                 "tenant_id": tenant_id,
#                 "created_by": created_by
#             }
#
#             if cls.save(db, **word_data):
#                 # 清除相关缓存
#                 cache_manager = SensitiveWordCacheManager()
#                 cache_manager.invalidate_tenant_cache(tenant_id)
#                 return word_data["id"]
#             return None
#         except Exception as e:
#             logging.error(f"创建敏感词失败: {e}")
#             return None
#
#     @classmethod
#     def batch_create_sensitive_words(cls, db: Session, words: list[str],
#                                    category_id: str, level_id: str,
#                                    match_type: str = "exact", source: str = None,
#                                    tenant_id: str = None, created_by: str = None) -> dict:
#         """批量创建敏感词"""
#         success_count = 0
#         failed_count = 0
#         failed_words = []
#
#         for word in words:
#             word = word.strip()
#             if not word:
#                 continue
#
#             result = cls.create_sensitive_word(
#                 db=db, word=word, category_id=category_id, level_id=level_id,
#                 match_type=match_type, source=source, tenant_id=tenant_id,
#                 created_by=created_by
#             )
#
#             if result:
#                 success_count += 1
#             else:
#                 failed_count += 1
#                 failed_words.append(word)
#
#         return {
#             "success_count": success_count,
#             "failed_count": failed_count,
#             "failed_words": failed_words
#         }
#
#     @classmethod
#     def get_paginated_words(cls, db: Session, tenant_id: str, page: int = 1,
#                           page_size: int = 50, category_id: str = None,
#                           level_id: str = None, keyword: str = None) -> dict:
#         """获取分页敏感词列表"""
#         try:
#             query = db.query(cls.model)\
#                       .filter(cls.model.tenant_id == tenant_id)\
#                       .filter(cls.model.status == "1")
#
#             if category_id:
#                 query = query.filter(cls.model.category_id == category_id)
#
#             if level_id:
#                 query = query.filter(cls.model.level_id == level_id)
#
#             if keyword:
#                 query = query.filter(cls.model.word.contains(keyword))
#
#             total = query.count()
#
#             words = query.order_by(desc(cls.model.create_date))\
#                         .offset((page - 1) * page_size)\
#                         .limit(page_size)\
#                         .all()
#
#             return {
#                 "words": [word.to_dict() for word in words],
#                 "total": total,
#                 "page": page,
#                 "page_size": page_size,
#                 "total_pages": (total + page_size - 1) // page_size
#             }
#         except Exception as e:
#             logging.error(f"获取敏感词列表失败: {e}")
#             return {"words": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0}
#
#     @classmethod
#     def update_sensitive_word(cls, db: Session, word_id: str, **kwargs) -> bool:
#         """更新敏感词"""
#         try:
#             # 如果更新了词汇内容，需要重新计算hash
#             if 'word' in kwargs:
#                 kwargs['word_hash'] = hashlib.md5(kwargs['word'].encode('utf-8')).hexdigest()
#
#             result = cls.update_by_id(db, word_id, kwargs)
#
#             if result:
#                 # 获取更新的记录来清除缓存
#                 word_obj = cls.get_by_id(db, word_id)
#                 if word_obj:
#                     cache_manager = SensitiveWordCacheManager()
#                     cache_manager.invalidate_tenant_cache(word_obj.tenant_id)
#
#             return result
#         except Exception as e:
#             logging.error(f"更新敏感词失败: {e}")
#             return False
#
#     @classmethod
#     def batch_delete_words(cls, db: Session, word_ids: list[str]) -> dict:
#         """批量删除敏感词"""
#         success_count = 0
#         failed_count = 0
#
#         for word_id in word_ids:
#             try:
#                 if cls.delete_by_id(db, word_id):
#                     success_count += 1
#                 else:
#                     failed_count += 1
#             except Exception:
#                 failed_count += 1
#
#         return {
#             "success_count": success_count,
#             "failed_count": failed_count
#         }
#
#     @classmethod
#     def get_tenant_words_for_filter(cls, db: Session, tenant_id: str) -> tuple[list[dict], list[str]]:
#         """获取租户用于过滤的敏感词和白名单"""
#         cache_manager = SensitiveWordCacheManager()
#
#         # 尝试从缓存获取
#         cached_words = cache_manager.get_tenant_words(tenant_id)
#         cached_whitelist = cache_manager.get_tenant_whitelist(tenant_id)
#
#         if cached_words is not None and cached_whitelist is not None:
#             return cached_words, cached_whitelist
#
#         # 从数据库获取
#         try:
#             # 获取敏感词
#             words_query = db.query(cls.model, SensitiveWordLevel)\
#                            .join(SensitiveWordLevel, cls.model.level_id == SensitiveWordLevel.id)\
#                            .filter(cls.model.tenant_id == tenant_id)\
#                            .filter(cls.model.status == "1")\
#                            .filter(SensitiveWordLevel.status == "1")
#
#             words_data = []
#             for word, level in words_query.all():
#                 words_data.append({
#                     "word": word.word,
#                     "match_type": word.match_type,
#                     "level": level.level,
#                     "action": level.action,
#                     "replacement": level.replacement
#                 })
#
#             # 获取白名单
#             whitelist_query = db.query(SensitiveWordWhitelist)\
#                                .filter(SensitiveWordWhitelist.tenant_id == tenant_id)\
#                                .filter(SensitiveWordWhitelist.status == "1")
#
#             whitelist_data = [item.word for item in whitelist_query.all()]
#
#             # 设置缓存
#             cache_manager.set_tenant_words(tenant_id, words_data)
#             cache_manager.set_tenant_whitelist(tenant_id, whitelist_data)
#
#             return words_data, whitelist_data
#         except Exception as e:
#             logging.error(f"获取租户敏感词数据失败: {e}")
#             return [], []
#
#     @classmethod
#     def get_or_build_matchers(cls, db: Session, tenant_id: str, words_data: list[dict]) -> tuple[ACAutomaton, RegexMatcher, PartialMatcher]:
#         """获取或构建租户的匹配器"""
#         cache_manager = SensitiveWordCacheManager()
#
#         # 尝试从缓存获取
#         cached_matchers = cache_manager.get_tenant_matchers(tenant_id)
#         if cached_matchers and all(cached_matchers):
#             logging.debug(f"使用缓存的匹配器，租户: {tenant_id}")
#             return cached_matchers
#
#         # 构建新的匹配器
#         logging.info(f"构建新的匹配器，租户: {tenant_id}, 词汇数: {len(words_data)}")
#
#         ac_automaton = ACAutomaton()
#         regex_matcher = RegexMatcher()
#         partial_matcher = PartialMatcher()
#
#         # 按匹配类型分配敏感词
#         for word_info in words_data:
#             match_type = word_info.get('match_type', 'exact')
#
#             if match_type == 'regex':
#                 regex_matcher.add_pattern(word_info['word'], word_info)
#             elif match_type == 'partial':
#                 partial_matcher.add_pattern(word_info['word'], word_info)
#             else:  # exact or default
#                 ac_automaton.add_word(word_info['word'], word_info)
#
#         # 构建AC自动机
#         ac_automaton.build_failure_function()
#
#         # 缓存匹配器
#         cache_manager.set_tenant_matchers(tenant_id, ac_automaton, regex_matcher, partial_matcher)
#
#         return ac_automaton, regex_matcher, partial_matcher
#
#     @classmethod
#     def filter_content(cls, db: Session, content: str, tenant_id: str,
#                       strict_mode: bool = False, user_id: Optional[str] = None,
#                       source_type: Optional[str] = None, source_id: Optional[str] = None,
#                       ip_address: Optional[str] = None, user_agent: Optional[str] = None) -> dict:
#         """过滤内容中的敏感词"""
#         try:
#             # 获取租户敏感词和白名单
#             words_data, whitelist = cls.get_tenant_words_for_filter(db, tenant_id)
#
#             # logging.info(f"获取到敏感词数量: {len(words_data)}, 白名单数量: {len(whitelist)}")
#             # if words_data:
#             #     logging.info(f"敏感词示例: {[w['word'] for w in words_data[:5]]}")
#
#             if not words_data:
#                 return {
#                     "is_sensitive": False,
#                     "filtered_content": content,
#                     "matched_words": [],
#                     "action": "pass"
#                 }
#
#             # 白名单检查
#             for white_word in whitelist:
#                 if white_word in content:
#                     return {
#                         "is_sensitive": False,
#                         "filtered_content": content,
#                         "matched_words": [],
#                         "action": "whitelist_pass"
#                     }
#
#             # 获取或构建匹配器（使用缓存）
#             ac_automaton, regex_matcher, partial_matcher = cls.get_or_build_matchers(db, tenant_id, words_data)
#
#             # 执行不同类型的匹配
#             all_matches = []
#
#             # 1. AC自动机精确匹配
#             ac_matches = ac_automaton.search(content)
#             all_matches.extend(ac_matches)
#
#             # 2. 正则表达式匹配
#             regex_matches = regex_matcher.search(content)
#             all_matches.extend(regex_matches)
#
#             # 3. 部分匹配
#             partial_matches = partial_matcher.search(content)
#             all_matches.extend(partial_matches)
#
#             # 4. PII检测
#             pii_detector = PIIDetector()
#             pii_matches = pii_detector.detect(content)
#
#             # 将PII匹配转换为统一格式
#             for pii in pii_matches:
#                 all_matches.append({
#                     'word': pii['value'],
#                     'start': pii['start'],
#                     'end': pii['end'],
#                     'info': {
#                         'level': 5,
#                         'action': 'block',
#                         'type': 'pii',
#                         'pii_type': pii['type'],
#                         'replacement': pii_detector.pii_types[pii['type']]['replacement']
#                     }
#                 })
#
#             if not all_matches:
#                 return {
#                     "is_sensitive": False,
#                     "filtered_content": content,
#                     "matched_words": [],
#                     "action": "pass"
#                 }
#
#             # 处理匹配结果
#             filtered_content = content
#             highest_level = 0
#             matched_words_info = []
#
#             # 按位置倒序排列，避免替换时位置偏移
#             all_matches.sort(key=lambda x: x['start'], reverse=True)
#
#             # 去重（处理重叠的匹配）
#             processed_ranges = []
#             unique_matches = []
#
#             for match in all_matches:
#                 start, end = match['start'], match['end']
#                 # 检查是否与已处理的范围重叠
#                 overlapped = False
#                 for processed_start, processed_end in processed_ranges:
#                     if not (end <= processed_start or start >= processed_end):
#                         overlapped = True
#                         break
#
#                 if not overlapped:
#                     unique_matches.append(match)
#                     processed_ranges.append((start, end))
#
#             # 处理唯一匹配
#             for match in unique_matches:
#                 word_info = match['info']
#                 level = word_info.get('level', 1)
#                 action = word_info.get('action', 'block')
#
#                 highest_level = max(highest_level, level)
#
#                 matched_words_info.append({
#                     'word': match['word'],
#                     'level': level,
#                     'action': action,
#                     'position': [match['start'], match['end']],
#                     'match_type': match.get('match_type', 'exact'),
#                     'pattern': match.get('pattern', match['word'])
#                 })
#
#                 # 执行过滤动作
#                 if action == 'replace':
#                     replacement = word_info.get('replacement', '*' * len(match['word']))
#                     filtered_content = filtered_content[:match['start']] + replacement + filtered_content[match['end']:]
#                 elif action == 'block':
#                     if strict_mode:
#                         # 严格模式直接拒绝
#                         break
#                     else:
#                         # 非严格模式替换为星号
#                         replacement = '*' * len(match['word'])
#                         filtered_content = filtered_content[:match['start']] + replacement + filtered_content[match['end']:]
#
#             # 确定最终动作
#             final_action = "block" if highest_level >= 4 and strict_mode else "filter"
#
#             # 记录日志
#             SensitiveFilterLogService.log_filter_action(
#                 db=db,
#                 user_id=user_id,
#                 tenant_id=tenant_id,
#                 content=content,
#                 matched_words=matched_words_info,
#                 filter_action=final_action,
#                 source_type=source_type,
#                 source_id=source_id,
#                 ip_address=ip_address,
#                 user_agent=user_agent
#             )
#
#             return {
#                 "is_sensitive": True,
#                 "filtered_content": filtered_content if final_action != "block" else "",
#                 "matched_words": matched_words_info,
#                 "action": final_action,
#                 "highest_level": highest_level
#             }
#
#         except Exception as e:
#             logging.error(f"内容过滤失败: {e}")
#             return {
#                 "is_sensitive": False,
#                 "filtered_content": content,
#                 "matched_words": [],
#                 "action": "error"
#             }
#
#     @classmethod
#     def refresh_tenant_cache(cls, db: Session, tenant_id: str) -> bool:
#         """刷新租户缓存"""
#         try:
#             cache_manager = SensitiveWordCacheManager()
#             cache_manager.invalidate_tenant_cache(tenant_id)
#
#             # 重新加载数据到缓存
#             cls.get_tenant_words_for_filter(db, tenant_id)
#             return True
#         except Exception as e:
#             logging.error(f"刷新租户缓存失败: {e}")
#             return False
#
#     @classmethod
#     def get_cache_stats(cls, tenant_id: str) -> dict:
#         """获取缓存统计信息"""
#         cache_manager = SensitiveWordCacheManager()
#         return cache_manager.get_cache_stats(tenant_id)
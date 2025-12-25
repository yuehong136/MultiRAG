# coding=utf-8
"""
@project: multirag
@Author：龙
@file： sensitive_word_middleware.py
@date：2025/01/07 10:00
@desc: 敏感词过滤中间件
"""

import json
import logging
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from api.db.db_models import SessionLocal
from api.utils.api_utils import get_json_result
from common import settings
from common.constants import RetCode


class SensitiveWordFilterMiddleware(BaseHTTPMiddleware):
    """敏感词过滤中间件"""
    
    def __init__(self, app, excluded_paths: list = None, strict_paths: list = None):
        super().__init__(app)
        # 默认排除的路径（不进行敏感词检查）
        self.excluded_paths = excluded_paths or [
            "/docs",
            "/redoc", 
            "/openapi.json",
            "/auth/",
            "/v1/sensitive_word/",  # 敏感词管理接口本身不检查
            "/health",
            "/metrics"
        ]
        
        # 严格模式路径（检测到敏感词直接拒绝）
        self.strict_paths = strict_paths or [
            # 注意：SSE接口不应该在严格模式路径中
            # 它们应该使用过滤模式，让应用层处理过滤后的内容
        ]
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """中间件处理逻辑"""
        
        # 添加调试日志
        logging.info(f"[敏感词中间件] 处理请求: {request.url.path}, Method: {request.method}")
        
        # 检查是否需要过滤
        if not self._should_filter(request):
            logging.info(f"[敏感词中间件] 跳过过滤: {request.url.path}")
            return await call_next(request)
        
        logging.info(f"[敏感词中间件] 需要过滤: {request.url.path}")
        
        # 判断是否为SSE接口
        is_sse_endpoint = (request.url.path.endswith("_sse") or 
                          "stream" in str(request.url) or 
                          "completion" in request.url.path or
                          request.headers.get("accept") == "text/event-stream")
        
        # 获取用户信息
        user_info = await self._get_user_info(request)
        if not user_info:
            # 如果无法获取用户信息，跳过敏感词检查
            logging.warning(f"[敏感词中间件] 无法获取用户信息，跳过检查")
            return await call_next(request)
        
        logging.info(f"[敏感词中间件] 用户: {user_info.get('email')}")
        
        # 读取请求体
        body = await self._get_request_body(request)
        if not body:
            logging.info(f"[敏感词中间件] 请求体为空，跳过检查")
            return await call_next(request)
        
        # 提取需要检查的文本内容
        text_content = self._extract_text_content(body)
        if not text_content:
            logging.info(f"[敏感词中间件] 提取的文本为空，跳过检查")
            return await call_next(request)
        
        logging.info(f"[敏感词中间件] 提取的文本: {text_content[:100]}...")
        
        # 进行敏感词过滤
        filter_result = await self._filter_content(
            content=text_content,
            tenant_id=user_info.get("id"),
            user_id=user_info.get("id"),
            strict_mode=self._is_strict_path(request.url.path),
            source_type="api_request",
            source_id=str(request.url),
            ip_address=self._get_client_ip(request),
            user_agent=request.headers.get("user-agent")
        )
        
        logging.info(f"[敏感词中间件] 过滤结果: is_sensitive={filter_result.get('is_sensitive')}, action={filter_result.get('action')}")
        if filter_result.get('matched_words'):
            logging.info(f"[敏感词中间件] 匹配的敏感词: {[w.get('word') for w in filter_result.get('matched_words', [])]}")
        
        # 处理过滤结果
        if filter_result.get("action") == "block":
            # 记录过滤日志
            await self._log_filter_action(user_info, filter_result, request)
            
            # 检查是否是SSE接口
            if is_sse_endpoint:
                # 对于SSE接口，返回特殊的错误流
                from fastapi.responses import StreamingResponse
                import asyncio
                
                async def error_generator():
                    error_msg = {
                        "error": "内容包含敏感信息，请修改后重试",
                        "code": RetCode.OPERATING_ERROR,
                        "sensitive_words": [w.get('word', '') for w in filter_result.get('matched_words', [])]
                    }
                    yield f"data: {json.dumps(error_msg, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                
                return StreamingResponse(
                    error_generator(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "Access-Control-Allow-Origin": "*"
                    }
                )
            else:
                # 普通接口返回JSON响应
                return get_json_result(
                    data=False,
                    retmsg="内容包含敏感信息，请修改后重试",
                    retcode=RetCode.OPERATING_ERROR
                )
        
        # 如果内容被过滤且不是SSE接口，修改请求体
        # 注意：对于SSE接口，不修改请求体，让应用层自行处理过滤后的内容
        if filter_result.get("is_sensitive") and filter_result.get("action") == "filter" and not is_sse_endpoint:
            # 获取过滤后的完整内容
            filtered_full_content = filter_result.get("filtered_content", text_content)
            
            # 修改请求体
            modified_body = self._modify_request_body(body, text_content, filtered_full_content)
            
            # 创建符合ASGI规范的receive函数
            message_sent = False
            
            async def new_receive():
                nonlocal message_sent
                if not message_sent:
                    message_sent = True
                    return {
                        "type": "http.request",
                        "body": modified_body.encode("utf-8") if isinstance(modified_body, str) else modified_body,
                        "more_body": False
                    }
                # 后续调用返回空body
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False
                }
            
            # 替换原始receive函数
            request._receive = new_receive
        elif filter_result.get("is_sensitive") and filter_result.get("action") == "filter" and is_sse_endpoint:
            # 对于SSE接口的filter动作，将过滤信息添加到request.state中
            # 让应用层自行决定如何处理
            request.state.sensitive_filter_result = filter_result
            logging.info(f"[敏感词中间件] SSE接口filter模式，将过滤信息传递给应用层处理")
        
        # 继续处理请求
        response = await call_next(request)
        
        # 记录过滤日志（如果有敏感词）
        if filter_result.get("is_sensitive"):
            await self._log_filter_action(user_info, filter_result, request)
        
        return response
    
    def _should_filter(self, request: Request) -> bool:
        """判断是否需要进行敏感词过滤"""
        path = request.url.path
        method = request.method
        
        # 只过滤POST/PUT/PATCH请求
        if method not in ["POST", "PUT", "PATCH"]:
            return False
        
        # 检查排除路径
        for excluded_path in self.excluded_paths:
            if path.startswith(excluded_path):
                return False
        
        return True
    
    def _is_strict_path(self, path: str) -> bool:
        """判断是否为严格模式路径"""
        for strict_path in self.strict_paths:
            if path.startswith(strict_path):
                return True
        return False
    
    async def _get_user_info(self, request: Request) -> dict | None:
        """获取用户信息"""
        try:
            # 从request.state中获取用户信息（假设认证中间件已经设置）
            if hasattr(request.state, 'user'):
                user = request.state.user
                return {
                    "id": getattr(user, 'id', None),
                    "email": getattr(user, 'email', None)
                }
            
            # 尝试从JWT token中解析用户信息
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                try:
                    import jwt
                    from api.db.db_models import SessionLocal
                    from api.db.services.user_service import UserService
                    
                    token = auth_header.split(" ")[1]
                    # 直接解析JWT token
                    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
                    email = payload.get("sub")
                    
                    if email:
                        # 从数据库获取用户信息
                        db = SessionLocal()
                        try:
                            user = UserService.query_user_onlywith_email(db, email)
                            if user:
                                return {
                                    "id": user.id,
                                    "email": user.email
                                }
                        finally:
                            db.close()
                except Exception as token_error:
                    logging.debug(f"JWT token解析失败: {token_error}")
                
        except Exception as e:
            logging.warning(f"获取用户信息失败: {e}")
        
        return None
    
    async def _get_request_body(self, request: Request) -> str:
        """获取请求体内容"""
        try:
            # 检查是否已经读取过
            if hasattr(request, '_body'):
                return request._body.decode('utf-8')
            
            # 读取原始请求体
            body = await request.body()
            
            # 保存body到request对象，避免重复读取
            request._body = body
            
            # 创建符合ASGI规范的receive函数
            message_sent = False
            
            async def receive():
                nonlocal message_sent
                if not message_sent:
                    message_sent = True
                    return {
                        "type": "http.request",
                        "body": body,
                        "more_body": False
                    }
                # 后续调用返回空body
                return {
                    "type": "http.request", 
                    "body": b"",
                    "more_body": False
                }
            
            # 只在非SSE接口时替换receive函数
            # SSE接口可能需要特殊的receive处理
            # 检查更多SSE相关路径标识
            is_sse = (request.url.path.endswith("_sse") or 
                     "stream" in str(request.url) or 
                     "completion" in request.url.path or
                     request.headers.get("accept") == "text/event-stream")
            
            if not is_sse:
                request._receive = receive
            
            if body:
                return body.decode('utf-8')
        except Exception as e:
            logging.warning(f"读取请求体失败: {e}")
        
        return ""
    
    def _extract_text_content(self, body: str) -> str:
        """从请求体中提取需要检查的文本内容"""
        try:
            if not body:
                return ""
            
            # 尝试解析JSON
            data = json.loads(body)
            
            # 定义需要检查的字段
            check_fields = [
                "content", "message", "text", "description", 
                "title", "summary", "prompt", "question",
                "answer", "comment", "remark", "note"
            ]
            
            extracted_texts = []
            
            # 特殊处理messages字段（LLM聊天接口）
            if isinstance(data.get("messages"), list):
                for msg in data["messages"]:
                    if isinstance(msg, dict) and "content" in msg:
                        extracted_texts.append(msg["content"])
            
            # 处理prompt字段
            if "prompt" in data and isinstance(data["prompt"], str):
                extracted_texts.append(data["prompt"])
            
            def extract_from_dict(obj, prefix=""):
                if isinstance(obj, dict):
                    for key, value in obj.items():
                        full_key = f"{prefix}.{key}" if prefix else key
                        if isinstance(value, str) and key.lower() in check_fields:
                            extracted_texts.append(value)
                        elif isinstance(value, (dict, list)) and key != "messages":  # 避免重复处理messages
                            extract_from_dict(value, full_key)
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, (dict, list)):
                            extract_from_dict(item, prefix)
                        elif isinstance(item, str):
                            extracted_texts.append(item)
            
            extract_from_dict(data)
            
            return " ".join(extracted_texts)
            
        except json.JSONDecodeError:
            # 如果不是JSON，直接返回原文本
            return body
        except Exception as e:
            logging.warning(f"提取文本内容失败: {e}")
            return ""
    
    async def _filter_content(self, content: str, tenant_id: str, **kwargs) -> dict:
        """执行AI安全护栏检测"""
        db = SessionLocal()
        try:
            # 使用新的AI护栏检测引擎
            from api.db.services.ai_guard_engine_service import AIGuardEngineService
            
            # 根据请求路径确定服务代码
            service_code = self._determine_service_code(kwargs.get('source_id', ''))
            
            detection_result = AIGuardEngineService.detect_content(
                db=db,
                content=content,
                service_code=service_code,
                tenant_id=tenant_id,
                user_id=kwargs.get('user_id'),
                request_id=kwargs.get('request_id'),
                source_type=kwargs.get('source_type'),
                source_id=kwargs.get('source_id'),
                client_ip=kwargs.get('ip_address'),
                user_agent=kwargs.get('user_agent')
            )
            
            # 转换为中间件兼容格式
            return {
                "is_sensitive": detection_result.get("is_blocked", False),
                "filtered_content": content,  # 新系统暂时不修改内容
                "matched_words": self._extract_matched_words(detection_result.get("matched_items", [])),
                "action": detection_result.get("action", "pass"),
                "risk_score": detection_result.get("overall_risk_score", 0.0)
            }
        except Exception as e:
            logging.error(f"AI安全护栏检测失败: {e}")
            return {
                "is_sensitive": False,
                "filtered_content": content,
                "matched_words": [],
                "action": "error"
            }
        finally:
            db.close()
    
    def _determine_service_code(self, source_id: str) -> str:
        """根据请求路径确定服务代码"""
        try:
            if not source_id:
                return "query_security_check"
            
            # 根据URL路径选择服务代码
            if "/llm/" in source_id or "/chat" in source_id:
                return "query_security_check"
            elif "/conversation/" in source_id:
                return "response_security_check"
            else:
                return "query_security_check"
        except Exception:
            return "query_security_check"
    
    def _extract_matched_words(self, matched_items: list) -> list:
        """从匹配项中提取词汇信息"""
        try:
            matched_words = []
            for item in matched_items:
                if isinstance(item, dict):
                    matched_words.append({
                        "word": item.get("content", ""),
                        "type": item.get("type", "unknown"),
                        "weight": item.get("weight", 1.0)
                    })
            return matched_words
        except Exception as e:
            logging.warning(f"提取匹配词汇失败: {e}")
            return []
    
    def _modify_request_body(self, original_body: str, original_content: str, filtered_content: str) -> str:
        """修改请求体中的敏感内容"""
        try:
            # 如果是JSON格式，精确替换
            data = json.loads(original_body)
            
            # 特殊处理messages字段
            if isinstance(data.get("messages"), list):
                for msg in data["messages"]:
                    if isinstance(msg, dict) and "content" in msg:
                        # 直接使用过滤后的内容替换
                        if original_content in msg["content"]:
                            # 如果原始内容是消息内容的一部分，进行替换
                            msg["content"] = msg["content"].replace(original_content, filtered_content)
                        elif msg["content"] == original_content:
                            # 如果原始内容就是完整的消息内容，直接替换
                            msg["content"] = filtered_content
                        elif original_content.strip() in msg["content"]:
                            # 尝试去除空格后替换
                            msg["content"] = msg["content"].replace(original_content.strip(), filtered_content)
            
            # 处理prompt字段
            if "prompt" in data and isinstance(data["prompt"], str):
                if original_content in data["prompt"]:
                    data["prompt"] = data["prompt"].replace(original_content, filtered_content)
                elif data["prompt"] == original_content:
                    data["prompt"] = filtered_content
            
            def replace_in_dict(obj):
                if isinstance(obj, dict):
                    for key, value in obj.items():
                        if isinstance(value, str):
                            if value == original_content:
                                obj[key] = filtered_content
                            elif original_content in value:
                                obj[key] = value.replace(original_content, filtered_content)
                        elif isinstance(value, (dict, list)) and key != "messages":  # 避免重复处理
                            replace_in_dict(value)
                elif isinstance(obj, list):
                    for i, item in enumerate(obj):
                        if isinstance(item, str):
                            if item == original_content:
                                obj[i] = filtered_content
                            elif original_content in item:
                                obj[i] = item.replace(original_content, filtered_content)
                        elif isinstance(item, (dict, list)):
                            replace_in_dict(item)
            
            replace_in_dict(data)
            return json.dumps(data, ensure_ascii=False)
            
        except json.JSONDecodeError:
            # 如果不是JSON，直接替换
            return original_body.replace(original_content, filtered_content)
        except Exception as e:
            logging.warning(f"修改请求体失败: {e}")
            return original_body
    
    def _get_client_ip(self, request: Request) -> str:
        """获取客户端IP"""
        # 尝试从各种header中获取真实IP
        headers_to_check = [
            "X-Forwarded-For",
            "X-Real-IP", 
            "X-Client-IP",
            "CF-Connecting-IP"
        ]
        
        for header in headers_to_check:
            ip = request.headers.get(header)
            if ip:
                # X-Forwarded-For可能包含多个IP，取第一个
                return ip.split(',')[0].strip()
        
        # fallback到直连IP
        if hasattr(request.client, 'host'):
            return request.client.host
        
        return "unknown"
    
    async def _log_filter_action(self, user_info: dict, filter_result: dict, request: Request):
        """记录过滤行为日志"""
        try:
            logging.info(f"敏感词过滤: 用户{user_info.get('email')} "
                        f"在{request.url.path}触发敏感词过滤，"
                        f"动作: {filter_result.get('action')}，"
                        f"匹配词数: {len(filter_result.get('matched_words', []))}")
        except Exception as e:
            logging.warning(f"记录过滤日志失败: {e}")


# 中间件配置类
class SensitiveWordMiddlewareConfig:
    """敏感词中间件配置"""
    
    @staticmethod
    def get_default_excluded_paths():
        """获取默认排除路径"""
        return [
            "/docs",
            "/redoc", 
            "/openapi.json",
            "/auth/",
            "/v1/sensitive_word/",
            "/health",
            "/metrics",
            "/static/",
            "/favicon.ico",
            "/v1/conversation/set"
        ]
    
    @staticmethod
    def get_default_strict_paths():
        """获取默认严格模式路径"""
        return [
            # 严格模式路径：检测到敏感词时直接阻止
            # 注意：SSE接口不应该在这里，它们应该使用过滤模式
            # "/v1/llm/chat_service_sse",  # 移除，让其使用过滤模式
            # "/v1/conversation/completion"  # 移除，让其使用过滤模式
        ]
    
    @classmethod
    def create_middleware(cls, app, custom_config: dict = None):
        """创建中间件实例"""
        config = custom_config or {}
        
        excluded_paths = config.get("excluded_paths", cls.get_default_excluded_paths())
        strict_paths = config.get("strict_paths", cls.get_default_strict_paths())
        
        return SensitiveWordFilterMiddleware(
            app=app,
            excluded_paths=excluded_paths,
            strict_paths=strict_paths
        )
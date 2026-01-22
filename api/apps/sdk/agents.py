import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import time
from typing import Any

import jwt
from fastapi import APIRouter, Depends, Query, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse, Response, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from api.db import CanvasCategory
from api.db.db_models import get_db
from api.db.services.canvas_service import UserCanvasService
from api.db.services.file_service import FileService
from api.db.services.user_canvas_version import UserCanvasVersionService
from api.utils.api_utils import get_error_data_result, get_result, token_required
from common.constants import RetCode
from common.misc_utils import get_uuid
from core.utils.redis_conn import REDIS_CONN

router = APIRouter()


class CreateAgentRequest(BaseModel):
    title: str
    dsl: dict[str, Any] | str


class UpdateAgentRequest(BaseModel):
    title: str | None = None
    dsl: dict[str, Any] | str | None = None


class DeleteAgentsRequest(BaseModel):
    ids: list[str] | None = None


@router.get("/agents", summary="获取代理列表")
def list_agents(
    id: str | None = Query(None),
    title: str | None = Query(None),
    page: int = Query(1),
    page_size: int = Query(30),
    order_by: str = Query("update_time"),
    desc: bool = Query(True),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    获取代理列表
    
    Args:
        id: 代理ID过滤
        title: 代理标题过滤
        page: 页码
        page_size: 每页数量
        order_by: 排序字段
        desc: 是否降序
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        代理列表
    """
    if id or title:
        canvas = UserCanvasService.query(db, id=id, title=title, user_id=tenant_id)
        if not canvas:
            return get_error_data_result(retmsg="The agent doesn't exist.")
    
    canvas = UserCanvasService.get_list(db, tenant_id, page, page_size, order_by, desc, id, title)
    return get_result(data=canvas)


@router.post("/agents", summary="创建代理")
def create_agent(
    request: CreateAgentRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    创建新的代理
    
    Args:
        request: 代理创建参数
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        创建结果
    """
    req = request.model_dump()
    req["user_id"] = tenant_id

    if req.get("dsl") is not None:
        if not isinstance(req["dsl"], str):
            req["dsl"] = json.dumps(req["dsl"], ensure_ascii=False)
        req["dsl"] = json.loads(req["dsl"])
    else:
        return get_error_data_result(retmsg="No DSL data in request.")

    if req.get("title") is not None:
        req["title"] = req["title"].strip()
    else:
        return get_error_data_result(retmsg="No title in request.")

    if UserCanvasService.query(db, user_id=tenant_id, title=req["title"]):
        return get_error_data_result(retmsg=f"Agent with title {req['title']} already exists.")

    agent_id = get_uuid()
    req["id"] = agent_id

    if not UserCanvasService.save(db, **req):
        return get_error_data_result(retmsg="Fail to create agent.")

    UserCanvasVersionService.insert(
        db,
        user_canvas_id=agent_id,
        title="{0}_{1}".format(req["title"], time.strftime("%Y_%m_%d_%H_%M_%S")),
        dsl=req["dsl"]
    )

    return get_result(data=True)


@router.put("/agents/{agent_id}", summary="更新代理")
def update_agent(
    agent_id: str,
    request: UpdateAgentRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    更新代理
    
    Args:
        agent_id: 代理ID
        request: 更新请求参数
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        更新结果
    """
    req = request.model_dump(exclude_unset=True)
    req["user_id"] = tenant_id

    if req.get("dsl") is not None:
        if not isinstance(req["dsl"], str):
            req["dsl"] = json.dumps(req["dsl"], ensure_ascii=False)
        req["dsl"] = json.loads(req["dsl"])
    
    if req.get("title") is not None:
        req["title"] = req["title"].strip()

    if not UserCanvasService.query(db, user_id=tenant_id, id=agent_id):
        return get_error_data_result(retmsg="Only owner of canvas authorized for this operation.")

    UserCanvasService.update_by_id(db, agent_id, req)

    if req.get("dsl") is not None:
        UserCanvasVersionService.insert(
            db,
            user_canvas_id=agent_id,
            title="{0}_{1}".format(req["title"], time.strftime("%Y_%m_%d_%H_%M_%S")),
            dsl=req["dsl"]
        )
        UserCanvasVersionService.delete_all_versions(db, agent_id)

    return get_result(data=True)


@router.delete("/agents/{agent_id}", summary="删除代理")
def delete_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    删除代理
    
    Args:
        agent_id: 代理ID
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        删除结果
    """
    if not UserCanvasService.query(db, user_id=tenant_id, id=agent_id):
        return get_error_data_result(retmsg="Only owner of canvas authorized for this operation.")

    UserCanvasService.delete_by_id(db, agent_id)
    return get_result(data=True)


@router.api_route("/webhook/{agent_id}", methods=["POST", "GET", "PUT", "PATCH", "DELETE", "HEAD"], summary="Webhook触发代理")
@router.api_route("/webhook_test/{agent_id}", methods=["POST", "GET", "PUT", "PATCH", "DELETE", "HEAD"], summary="Webhook测试")
async def webhook(
    agent_id: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    通过Webhook触发代理执行（增强版）

    特性：
    - 支持多种HTTP方法（POST/GET/PUT/PATCH/DELETE/HEAD）
    - 安全验证（max_body_size, IP白名单, 速率限制, 认证）
    - 文件上传支持
    - 统一的Content-Type处理
    - Schema-based提取和类型验证
    - 两种执行模式：立即返回/流式返回

    Args:
        agent_id: 代理ID
        request: HTTP请求对象
        db: 数据库会话

    Returns:
        根据执行模式返回响应或SSE流
    """
    is_test = request.url.path.startswith("/api/v1/webhook_test")
    start_ts = time.time()

    # 1. Fetch canvas by agent_id
    cvs = await asyncio.to_thread(UserCanvasService.get_by_id, db, agent_id)
    if not cvs:
        return get_error_data_result(retcode=RetCode.BAD_REQUEST, retmsg="Canvas not found.")

    # 2. Check canvas category
    if cvs.canvas_category == CanvasCategory.DataFlow:
        return get_error_data_result(retcode=RetCode.BAD_REQUEST, retmsg="Dataflow can not be triggered by webhook.")

    # 3. Load DSL from canvas
    dsl = getattr(cvs, "dsl", None)
    if not isinstance(dsl, dict):
        return get_error_data_result(retcode=RetCode.BAD_REQUEST, retmsg="Invalid DSL format.")

    # 4. Check webhook configuration in DSL
    webhook_cfg = None
    components = dsl.get("components", {})
    for k in components:
        cpn_obj = components[k]["obj"]
        if cpn_obj["component_name"].lower() == "begin" and cpn_obj["params"].get("mode") == "Webhook":
            webhook_cfg = cpn_obj["params"]
            break

    if not webhook_cfg:
        return get_error_data_result(retcode=RetCode.BAD_REQUEST, retmsg="Webhook not configured for this agent.")

    # 5. Validate request method against webhook_cfg.methods
    allowed_methods = webhook_cfg.get("methods", [])
    request_method = request.method.upper()
    if allowed_methods and request_method not in allowed_methods:
        return get_error_data_result(
            retcode=RetCode.BAD_REQUEST,
            retmsg=f"HTTP method '{request_method}' not allowed for this webhook."
        )

    # 6. Validate webhook security
    async def validate_webhook_security(security_cfg: dict):
        """Validate webhook security rules based on security configuration."""
        if not security_cfg:
            return  # No security config → allowed by default

        # 1. Validate max body size
        await _validate_max_body_size(security_cfg)

        # 2. Validate IP whitelist
        _validate_ip_whitelist(security_cfg)

        # 3. Validate rate limiting
        _validate_rate_limit(security_cfg)

        # 4. Validate authentication
        auth_type = security_cfg.get("auth_type", "none")

        if auth_type == "none":
            return

        if auth_type == "token":
            _validate_token_auth(security_cfg)
        elif auth_type == "basic":
            _validate_basic_auth(security_cfg)
        elif auth_type == "jwt":
            _validate_jwt_auth(security_cfg)
        else:
            raise Exception(f"Unsupported auth_type: {auth_type}")

    async def _validate_max_body_size(security_cfg):
        """Check request size does not exceed max_body_size."""
        max_size = security_cfg.get("max_body_size")
        if not max_size:
            return

        # Convert "10MB" → bytes
        units = {"kb": 1024, "mb": 1024**2}
        size_str = max_size.lower()

        limit = None
        for suffix, factor in units.items():
            if size_str.endswith(suffix):
                limit = int(size_str.replace(suffix, "")) * factor
                break

        if limit is None:
            raise Exception("Invalid max_body_size format")

        MAX_LIMIT = 10 * 1024 * 1024  # 10MB
        if limit > MAX_LIMIT:
            raise Exception("max_body_size exceeds maximum allowed size (10MB)")

        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > limit:
            raise Exception(f"Request body too large: {content_length} > {limit}")

    def _validate_ip_whitelist(security_cfg):
        """Allow only IPs listed in ip_whitelist."""
        whitelist = security_cfg.get("ip_whitelist", [])
        if not whitelist:
            return

        client_ip = request.client.host if request.client else "unknown"

        for rule in whitelist:
            if "/" in rule:
                # CIDR notation
                if ipaddress.ip_address(client_ip) in ipaddress.ip_network(rule, strict=False):
                    return
            else:
                # Single IP
                if client_ip == rule:
                    return

        raise Exception(f"IP {client_ip} is not allowed by whitelist")

    def _validate_rate_limit(security_cfg):
        """Simple in-memory rate limiting using Redis token bucket."""
        rl = security_cfg.get("rate_limit")
        if not rl:
            return

        limit = int(rl.get("limit", 60))
        if limit <= 0:
            raise Exception("rate_limit.limit must be > 0")
        per = rl.get("per", "minute")

        window = {
            "second": 1,
            "minute": 60,
            "hour": 3600,
            "day": 86400,
        }.get(per)

        if not window:
            raise Exception(f"Invalid rate_limit.per: {per}")

        capacity = limit
        rate = limit / window
        cost = 1

        key = f"rl:tb:{agent_id}"
        now = time.time()

        try:
            res = REDIS_CONN.lua_token_bucket(
                keys=[key],
                args=[capacity, rate, now, cost],
                client=REDIS_CONN.REDIS,
            )

            allowed = int(res[0])
            if allowed != 1:
                raise Exception("Too many requests (rate limit exceeded)")

        except Exception as e:
            raise Exception(f"Rate limit error: {e}")

    def _validate_token_auth(security_cfg):
        """Validate header-based token authentication."""
        token_cfg = security_cfg.get("token", {})
        header = token_cfg.get("token_header")
        token_value = token_cfg.get("token_value")

        provided = request.headers.get(header)
        if provided != token_value:
            raise Exception("Invalid token authentication")

    def _validate_basic_auth(security_cfg):
        """Validate HTTP Basic Auth credentials."""
        auth_cfg = security_cfg.get("basic_auth", {})
        username = auth_cfg.get("username")
        password = auth_cfg.get("password")

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Basic "):
            raise Exception("Missing Basic Auth")

        try:
            encoded = auth_header[6:]
            decoded = base64.b64decode(encoded).decode("utf-8")
            provided_user, provided_pass = decoded.split(":", 1)
            if provided_user != username or provided_pass != password:
                raise Exception("Invalid Basic Auth credentials")
        except Exception:
            raise Exception("Invalid Basic Auth credentials")

    def _validate_jwt_auth(security_cfg):
        """Validate JWT token in Authorization header."""
        jwt_cfg = security_cfg.get("jwt", {})
        secret = jwt_cfg.get("secret")
        if not secret:
            raise Exception("JWT secret not configured")

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            raise Exception("Missing Bearer token")

        token = auth_header[len("Bearer "):].strip()
        if not token:
            raise Exception("Empty Bearer token")

        alg = (jwt_cfg.get("algorithm") or "HS256").upper()

        decode_kwargs = {
            "key": secret,
            "algorithms": [alg],
        }
        options = {}
        if jwt_cfg.get("audience"):
            decode_kwargs["audience"] = jwt_cfg["audience"]
            options["verify_aud"] = True
        else:
            options["verify_aud"] = False

        if jwt_cfg.get("issuer"):
            decode_kwargs["issuer"] = jwt_cfg["issuer"]
            options["verify_iss"] = True
        else:
            options["verify_iss"] = False

        try:
            decoded = jwt.decode(
                token,
                options=options,
                **decode_kwargs,
            )
        except Exception as e:
            raise Exception(f"Invalid JWT: {str(e)}")

        raw_required_claims = jwt_cfg.get("required_claims", [])
        if isinstance(raw_required_claims, str):
            required_claims = [raw_required_claims]
        elif isinstance(raw_required_claims, (list, tuple, set)):
            required_claims = list(raw_required_claims)
        else:
            required_claims = []

        required_claims = [
            c for c in required_claims
            if isinstance(c, str) and c.strip()
        ]

        RESERVED_CLAIMS = {"exp", "sub", "aud", "iss", "nbf", "iat"}
        for claim in required_claims:
            if claim in RESERVED_CLAIMS:
                raise Exception(f"Reserved JWT claim cannot be required: {claim}")

        for claim in required_claims:
            if claim not in decoded:
                raise Exception(f"Missing JWT claim: {claim}")

        return decoded

    try:
        security_config = webhook_cfg.get("security", {})
        await validate_webhook_security(security_config)
    except Exception as e:
        return get_error_data_result(retcode=RetCode.BAD_REQUEST, retmsg=str(e))

    if not isinstance(cvs.dsl, str):
        dsl_str = json.dumps(cvs.dsl, ensure_ascii=False)
    else:
        dsl_str = cvs.dsl

    try:
        canvas = Canvas(dsl_str, cvs.user_id, agent_id, canvas_id=agent_id)
    except Exception as e:
        return get_error_data_result(retcode=RetCode.BAD_REQUEST, retmsg=str(e))

    # 7. Parse request body
    async def parse_webhook_request(content_type):
        """Parse request based on content-type and return structured data."""
        # 1. Query
        query_data = dict(request.query_params)

        # 2. Headers
        header_data = dict(request.headers)

        # 3. Body
        ctype = request.headers.get("content-type", "").split(";")[0].strip()
        if ctype and content_type and ctype != content_type:
            raise ValueError(
                f"Invalid Content-Type: expect '{content_type}', got '{ctype}'"
            )

        body_data: dict = {}

        try:
            if ctype == "application/json":
                body_data = await request.json() or {}

            elif ctype == "multipart/form-data":
                nonlocal canvas
                form_data = await request.form()

                body_data = {}

                # Process regular form fields
                for key, value in form_data.items():
                    if not isinstance(value, UploadFile):
                        body_data[key] = value

                # Process file uploads
                files_list = []
                for key, value in form_data.items():
                    if isinstance(value, UploadFile):
                        if len(files_list) >= 10:
                            raise Exception("Too many uploaded files")

                        desc = await asyncio.to_thread(
                            FileService.upload_info,
                            cvs.user_id,
                            value,
                            None
                        )
                        file_parsed = await canvas.get_files_async([desc])
                        body_data[key] = file_parsed

            elif ctype == "application/x-www-form-urlencoded":
                form_data = await request.form()
                body_data = dict(form_data)

            else:
                # text/plain / octet-stream / empty / unknown
                raw = await request.body()
                if raw:
                    try:
                        body_data = json.loads(raw.decode("utf-8"))
                    except Exception:
                        body_data = {}
                else:
                    body_data = {}

        except Exception:
            body_data = {}

        return {
            "query": query_data,
            "headers": header_data,
            "body": body_data,
            "content_type": ctype,
        }

    def extract_by_schema(data, schema, name="section"):
        """
        Extract only fields defined in schema.
        Required fields must exist.
        Optional fields default to type-based default values.
        Type validation included.
        """
        props = schema.get("properties", {})
        required = schema.get("required", [])

        extracted = {}

        for field, field_schema in props.items():
            field_type = field_schema.get("type")

            # 1. Required field missing
            if field in required and field not in data:
                raise Exception(f"{name} missing required field: {field}")

            # 2. Optional → default value
            if field not in data:
                extracted[field] = default_for_type(field_type)
                continue

            raw_value = data[field]

            # 3. Auto convert value
            try:
                value = auto_cast_value(raw_value, field_type)
            except Exception as e:
                raise Exception(f"{name}.{field} auto-cast failed: {str(e)}")

            # 4. Type validation
            if not validate_type(value, field_type):
                raise Exception(
                    f"{name}.{field} type mismatch: expected {field_type}, got {type(value).__name__}"
                )

            extracted[field] = value

        return extracted

    def default_for_type(t):
        """Return default value for the given schema type."""
        if t == "file":
            return []
        if t == "object":
            return {}
        if t == "boolean":
            return False
        if t == "number":
            return 0
        if t == "string":
            return ""
        if t and t.startswith("array"):
            return []
        if t == "null":
            return None
        return None

    def auto_cast_value(value, expected_type):
        """Convert string values into schema type when possible."""
        # Non-string values already good
        if not isinstance(value, str):
            return value

        v = value.strip()

        # Boolean
        if expected_type == "boolean":
            if v.lower() in ["true", "1"]:
                return True
            if v.lower() in ["false", "0"]:
                return False
            raise Exception(f"Cannot convert '{value}' to boolean")

        # Number
        if expected_type == "number":
            # integer
            if v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
                return int(v)

            # float
            try:
                return float(v)
            except Exception:
                raise Exception(f"Cannot convert '{value}' to number")

        # Object
        if expected_type == "object":
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    return parsed
                else:
                    raise Exception("JSON is not an object")
            except Exception:
                raise Exception(f"Cannot convert '{value}' to object")

        # Array <T>
        if expected_type.startswith("array"):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
                else:
                    raise Exception("JSON is not an array")
            except Exception:
                raise Exception(f"Cannot convert '{value}' to array")

        # String (accept original)
        if expected_type == "string":
            return value

        # File
        if expected_type == "file":
            return value

        # Default: do nothing
        return value

    def validate_type(value, t):
        """Validate value type against schema type t."""
        if t == "file":
            return isinstance(value, list)

        if t == "string":
            return isinstance(value, str)

        if t == "number":
            return isinstance(value, (int, float))

        if t == "boolean":
            return isinstance(value, bool)

        if t == "object":
            return isinstance(value, dict)

        # array<string> / array<number> / array<object>
        if t.startswith("array"):
            if not isinstance(value, list):
                return False

            if "<" in t and ">" in t:
                inner = t[t.find("<") + 1 : t.find(">")]

                # Check each element type
                for item in value:
                    if not validate_type(item, inner):
                        return False

            return True

        return True

    parsed = await parse_webhook_request(webhook_cfg.get("content_types"))
    SCHEMA = webhook_cfg.get("schema", {"query": {}, "headers": {}, "body": {}})

    # Extract strictly by schema
    try:
        query_clean = extract_by_schema(parsed["query"], SCHEMA.get("query", {}), name="query")
        header_clean = extract_by_schema(parsed["headers"], SCHEMA.get("headers", {}), name="headers")
        body_clean = extract_by_schema(parsed["body"], SCHEMA.get("body", {}), name="body")
    except Exception as e:
        return get_error_data_result(retcode=RetCode.BAD_REQUEST, retmsg=str(e))

    clean_request = {
        "query": query_clean,
        "headers": header_clean,
        "body": body_clean,
        "input": parsed
    }

    execution_mode = webhook_cfg.get("execution_mode", "Immediately")
    response_cfg = webhook_cfg.get("response", {})

    def append_webhook_trace(agent_id: str, start_ts: float, event: dict, ttl=600):
        """Append event to webhook trace log in Redis."""
        key = f"webhook-trace-{agent_id}-logs"

        raw = REDIS_CONN.get(key)
        obj = json.loads(raw) if raw else {"webhooks": {}}

        ws = obj["webhooks"].setdefault(
            str(start_ts),
            {"start_ts": start_ts, "events": []}
        )

        ws["events"].append({
            "ts": time.time(),
            **event
        })

        REDIS_CONN.set_obj(key, obj, ttl)

    if execution_mode == "Immediately":
        status = response_cfg.get("status", 200)
        try:
            status = int(status)
        except (TypeError, ValueError):
            return get_error_data_result(
                retcode=RetCode.BAD_REQUEST,
                retmsg=f"Invalid response status code: {status}"
            )

        if not (200 <= status <= 399):
            return get_error_data_result(
                retcode=RetCode.BAD_REQUEST,
                retmsg=f"Invalid response status code: {status}, must be between 200 and 399"
            )

        body_tpl = response_cfg.get("body_template", "")

        def parse_body(body: str):
            if not body:
                return None, "application/json"

            try:
                parsed_body = json.loads(body)
                return parsed_body, "application/json"
            except (json.JSONDecodeError, TypeError):
                return body, "text/plain"

        body, content_type = parse_body(body_tpl)

        async def background_run():
            try:
                async for ans in canvas.run(
                    query="",
                    user_id=cvs.user_id,
                    webhook_payload=clean_request
                ):
                    if is_test:
                        append_webhook_trace(agent_id, start_ts, ans)

                if is_test:
                    append_webhook_trace(
                        agent_id,
                        start_ts,
                        {
                            "event": "finished",
                            "elapsed_time": time.time() - start_ts,
                            "success": True,
                        }
                    )

                cvs.dsl = json.loads(str(canvas))
                await asyncio.to_thread(UserCanvasService.update_by_id, db, agent_id, cvs.to_dict())

            except Exception as e:
                logging.exception("Webhook background run failed")
                if is_test:
                    try:
                        append_webhook_trace(
                            agent_id,
                            start_ts,
                            {
                                "event": "error",
                                "message": str(e),
                                "error_type": type(e).__name__,
                            }
                        )
                        append_webhook_trace(
                            agent_id,
                            start_ts,
                            {
                                "event": "finished",
                                "elapsed_time": time.time() - start_ts,
                                "success": False,
                            }
                        )
                    except Exception:
                        logging.exception("Failed to append webhook trace")

        asyncio.create_task(background_run())

        if content_type == "application/json":
            return JSONResponse(content=body, status_code=status)
        else:
            return Response(content=body, status_code=status, media_type=content_type)

    else:  # Streaming mode
        async def sse():
            nonlocal canvas
            contents: list[str] = []
            status = 200
            try:
                async for ans in canvas.run(
                    query="",
                    user_id=cvs.user_id,
                    webhook_payload=clean_request,
                ):
                    if ans["event"] == "message":
                        content = ans["data"]["content"]
                        if ans["data"].get("start_to_think", False):
                            content = "<think>"
                        elif ans["data"].get("end_to_think", False):
                            content = "</think>"
                        if content:
                            contents.append(content)
                    if ans["event"] == "message_end":
                        status = int(ans["data"].get("status", status))
                    if is_test:
                        append_webhook_trace(agent_id, start_ts, ans)

                if is_test:
                    append_webhook_trace(
                        agent_id,
                        start_ts,
                        {
                            "event": "finished",
                            "elapsed_time": time.time() - start_ts,
                            "success": True,
                        }
                    )

                final_content = "".join(contents)
                return {
                    "message": final_content,
                    "success": True,
                    "code": status,
                }

            except Exception as e:
                if is_test:
                    append_webhook_trace(
                        agent_id,
                        start_ts,
                        {
                            "event": "error",
                            "message": str(e),
                            "error_type": type(e).__name__,
                        }
                    )
                    append_webhook_trace(
                        agent_id,
                        start_ts,
                        {
                            "event": "finished",
                            "elapsed_time": time.time() - start_ts,
                            "success": False,
                        }
                    )
                return {"code": 400, "message": str(e), "success": False}

        result = await sse()
        return Response(
            json.dumps(result),
            status_code=result["code"],
            media_type="application/json",
        )


@router.get("/webhook_trace/{agent_id}", summary="获取Webhook跟踪日志")
async def webhook_trace(agent_id: str, request: Request):
    """
    获取Webhook执行的跟踪日志

    Args:
        agent_id: 代理ID
        request: HTTP请求对象（用于获取查询参数）

    Returns:
        包含webhook执行事件的JSON响应
    """
    def encode_webhook_id(start_ts: str) -> str:
        WEBHOOK_ID_SECRET = "webhook_id_secret"
        sig = hmac.new(
            WEBHOOK_ID_SECRET.encode("utf-8"),
            start_ts.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")

    def decode_webhook_id(enc_id: str, webhooks: dict) -> str | None:
        for ts in webhooks.keys():
            if encode_webhook_id(ts) == enc_id:
                return ts
        return None

    since_ts = request.query_params.get("since_ts")
    if since_ts:
        since_ts = float(since_ts)
    webhook_id = request.query_params.get("webhook_id")

    key = f"webhook-trace-{agent_id}-logs"
    raw = REDIS_CONN.get(key)

    if since_ts is None:
        now = time.time()
        return get_result(
            data={
                "webhook_id": None,
                "events": [],
                "next_since_ts": now,
                "finished": False,
            }
        )

    if not raw:
        return get_result(
            data={
                "webhook_id": None,
                "events": [],
                "next_since_ts": since_ts,
                "finished": False,
            }
        )

    obj = json.loads(raw)
    webhooks = obj.get("webhooks", {})

    if webhook_id is None:
        candidates = [
            float(k) for k in webhooks.keys() if float(k) > since_ts
        ]

        if not candidates:
            return get_result(
                data={
                    "webhook_id": None,
                    "events": [],
                    "next_since_ts": since_ts,
                    "finished": False,
                }
            )

        start_ts_found = min(candidates)
        real_id = str(start_ts_found)
        webhook_id = encode_webhook_id(real_id)

        return get_result(
            data={
                "webhook_id": webhook_id,
                "events": [],
                "next_since_ts": start_ts_found,
                "finished": False,
            }
        )

    real_id = decode_webhook_id(webhook_id, webhooks)

    if not real_id:
        return get_result(
            data={
                "webhook_id": webhook_id,
                "events": [],
                "next_since_ts": since_ts,
                "finished": True,
            }
        )

    ws = webhooks.get(str(real_id))
    events = ws.get("events", [])
    new_events = [e for e in events if e.get("ts", 0) > since_ts]

    next_ts = since_ts
    for e in new_events:
        next_ts = max(next_ts, e["ts"])

    finished = any(e.get("event") == "finished" for e in new_events)

    return get_result(
        data={
            "webhook_id": webhook_id,
            "events": new_events,
            "next_since_ts": next_ts,
            "finished": finished,
        }
    )

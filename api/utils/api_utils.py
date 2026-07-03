import asyncio
import inspect
import logging
import os
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from functools import wraps
from typing import Any

from fastapi import Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.db.db_models import APIToken, get_db
from api.db.services.api_service import APITokenService
from api.db.services.tenant_llm_service import LLMFactoriesService
from common import settings
from common.connection_utils import timeout
from common.constants import RetCode
from common.mcp_tool_call_conn import MCPToolCallSession, close_multiple_mcp_toolcall_sessions
from common.misc_utils import thread_pool_exec


class SDKAuthError(Exception):
    def __init__(
        self,
        retmsg: str,
        retcode: RetCode = RetCode.AUTHENTICATION_ERROR,
        data: bool = False,
    ) -> None:
        super().__init__(retmsg)
        self.retmsg = retmsg
        self.retcode = retcode
        self.data = data


class BusinessError(Exception):
    """业务层异常，可在 Depends 等任意位置抛出，由全局 handler 转为 get_json_result 格式。"""

    def __init__(
        self,
        retmsg: str,
        retcode: RetCode = RetCode.AUTHENTICATION_ERROR,
        data: bool = False,
    ) -> None:
        super().__init__(retmsg)
        self.retmsg = retmsg
        self.retcode = retcode
        self.data = data


async def _coerce_request_data(request: Request) -> dict:
    """
    Fetch JSON body with sane defaults; fallback to form data.

    Note: FastAPI typically uses Pydantic models for request validation,
    making this function rarely needed. However, it's provided for
    edge cases where manual request body parsing is required.

    Args:
        request: FastAPI Request object

    Returns:
        dict: Parsed request data from JSON body or form data

    Raises:
        ValueError: When no JSON body or form data found
        TypeError: When payload type is unsupported
    """
    if hasattr(request.state, '_cached_payload'):
        return request.state._cached_payload
    payload: Any = None

    body_bytes = await request.body()
    has_body = bool(body_bytes)
    content_type = (request.headers.get("content-type") or "").lower()
    is_json = content_type.startswith("application/json")

    if not has_body:
        payload = {}
    elif is_json:
        payload = await request.json()
        if isinstance(payload, dict):
            payload = payload or {}
        elif isinstance(payload, str):
            raise AttributeError("'str' object has no attribute 'get'")
        else:
            raise TypeError("JSON payload must be an object.")
    else:
        form = await request.form()
        payload = dict(form) if form else None
        if payload is None:
            raise TypeError("Request body is not a valid form payload.")

    request.state._cached_payload = payload
    return payload


async def get_request_json(request: Request) -> dict:
    """
    Get request JSON data with fallback to form data.

    This is a convenience wrapper around _coerce_request_data().
    For most FastAPI endpoints, prefer using Pydantic models instead.

    Args:
        request: FastAPI Request object

    Returns:
        dict: Parsed request data
    """
    return await _coerce_request_data(request)


def serialize_for_json(obj):
    """
    Recursively serialize objects to make them JSON serializable.
    Handles ModelMetaclass and other non-serializable objects.
    """
    if hasattr(obj, '__dict__'):
        # For objects with __dict__, try to serialize their attributes
        try:
            return {key: serialize_for_json(value) for key, value in obj.__dict__.items() if not key.startswith('_')}
        except (AttributeError, TypeError):
            return str(obj)
    elif hasattr(obj, '__name__'):
        # For classes and metaclasses, return their name
        return f"<{obj.__module__}.{obj.__name__}>" if hasattr(obj, '__module__') else f"<{obj.__name__}>"
    elif isinstance(obj, (list, tuple)):
        return [serialize_for_json(item) for item in obj]
    elif isinstance(obj, dict):
        return {key: serialize_for_json(value) for key, value in obj.items()}
    elif isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    else:
        # Fallback: convert to string representation
        return str(obj)


def get_data_error_result(retcode=RetCode.DATA_ERROR, retmsg='Sorry! Data missing!'):
    logging.error(retmsg)
    result_dict = {
        "retcode": retcode,
        "retmsg": retmsg
    }
    response = {key: value for key, value in result_dict.items() if value is not None or key == "retcode"}
    return JSONResponse(content=jsonable_encoder(response))


def server_error_response(e):
    logging.error("Unhandled exception during request", exc_info=(type(e), e, e.__traceback__))
    try:
        msg = repr(e).lower()
        if getattr(e, "code", None) == 401 or ("unauthorized" in msg) or ("401" in msg):
            return JSONResponse(
                status_code=RetCode.UNAUTHORIZED,
                content=jsonable_encoder({"retcode": RetCode.UNAUTHORIZED, "retmsg": "Unauthorized", "data": None}),
            )
    except Exception as ex:
        logging.warning(f"error checking authorization: {ex}")

    if repr(e).find("index_not_found_exception") >= 0:
        return get_json_result(retcode=RetCode.EXCEPTION_ERROR, retmsg="No chunk found, please upload file and parse it.")

    return get_json_result(retcode=RetCode.EXCEPTION_ERROR, retmsg=repr(e))


def validate_request(*args, **kwargs):
    """
    参数验证装饰器（已废弃，FastAPI 推荐使用 Pydantic 模型验证）
    保留此函数是为了向后兼容和代码完整性

    注意：此装饰器已不再使用，FastAPI 通过 Pydantic 模型自动处理参数验证
    """
    def process_args(input_arguments):
        """提取验证逻辑，便于复用"""
        no_arguments = []
        error_arguments = []
        for arg in args:
            if arg not in input_arguments:
                no_arguments.append(arg)
        for k, v in kwargs.items():
            config_value = input_arguments.get(k, None)
            if config_value is None:
                no_arguments.append(k)
            elif isinstance(v, (tuple, list)):
                if config_value not in v:
                    error_arguments.append((k, set(v)))
            elif config_value != v:
                error_arguments.append((k, v))
        if no_arguments or error_arguments:
            error_string = ""
            if no_arguments:
                error_string += f"required argument are missing: {', '.join(no_arguments)}; "
            if error_arguments:
                error_string += "required argument values: " + ", ".join(
                    [f"{a[0]}={a[1]}" for a in error_arguments])
            return error_string
        return None

    def wrapper(func):
        @wraps(func)
        async def decorated_function(request: Request, *_args, **_kwargs):
            if args or kwargs:
                try:
                    input_arguments = await _coerce_request_data(request)
                except (AttributeError, TypeError):
                    input_arguments = {}
            else:
                input_arguments = await _coerce_request_data(request)
            errs = process_args(input_arguments)
            if errs:
                return get_json_result(retcode=RetCode.ARGUMENT_ERROR, retmsg=errs)

            # 支持同步和异步函数
            if inspect.iscoroutinefunction(func):
                return await func(request, *_args, **_kwargs)
            return func(request, *_args, **_kwargs)

        return decorated_function

    return wrapper


def get_json_result(retcode: RetCode = RetCode.SUCCESS, retmsg='success', data=None):
    response = {"retcode": retcode, "retmsg": retmsg, "data": data}
    return JSONResponse(content=jsonable_encoder(response))


def apikey_required(func: Callable) -> Callable:
    """
    装饰器形式的 API Key 验证（已废弃，建议使用 apikey_dependency）
    保留此函数是为了向后兼容和代码完整性

    注意：此装饰器已不再使用，FastAPI 推荐使用依赖注入方式
    """
    @wraps(func)
    async def decorated_function(*args, **kwargs):
        request: Request = kwargs.get('request')  # 从 kwargs 中获取 FastAPI Request 对象
        db: Session = kwargs.get('db')  # 从 kwargs 中获取数据库会话对象

        authorization_header = request.headers.get('Authorization')

        token = authorization_header.split()[1]
        objs = APITokenService.query(db, token=token)

        if not objs:
            return build_error_result(
                error_msg='API-KEY is invalid!', retcode=RetCode.FORBIDDEN
            )

        kwargs['tenant_id'] = objs[0].tenant_id

        # 支持同步和异步函数
        if inspect.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        return func(*args, **kwargs)

    return decorated_function


def apikey_dependency(request: Request, db: Session = Depends(get_db)) -> str:
    """
    FastAPI 依赖注入形式的 API Key 验证

    从请求头中提取并验证 API Key，返回 tenant_id

    Args:
        request: FastAPI Request 对象
        db: 数据库会话

    Returns:
        str: 租户ID

    Raises:
        HTTPException: 当 API Key 无效或缺失时

    Example:
        @router.post("/endpoint")
        def endpoint(tenant_id: str = Depends(apikey_dependency)):
            # 使用 tenant_id
            pass
    """
    authorization_header = request.headers.get('Authorization')

    if not authorization_header:
        raise build_error_result(
            error_msg='Authorization header is missing!',
            retcode=RetCode.FORBIDDEN
        )

    authorization_list = authorization_header.split()
    if len(authorization_list) < 2:
        raise build_error_result(
            error_msg='Invalid Authorization format!',
            retcode=RetCode.FORBIDDEN
        )

    token = authorization_list[1]
    objs = APITokenService.query(db, token=token)

    if not objs:
        raise build_error_result(
            error_msg='API-KEY is invalid!',
            retcode=RetCode.FORBIDDEN
        )

    return objs[0].tenant_id


def build_error_result(retcode=RetCode.FORBIDDEN, error_msg='success'):
    response_content = {"error_code": retcode, "error_msg": error_msg}
    return JSONResponse(content=response_content, status_code=retcode)


def construct_json_result(code: RetCode = RetCode.SUCCESS, message='success', data=None):
    if data is None:
        return JSONResponse(content={"code": code, "message": message})
    else:
        return JSONResponse(content={"code": code, "message": message, "data": data})


def construct_error_response(e):
    logging.exception(e)
    try:
        if e.code == 401:
            return construct_json_result(code=RetCode.UNAUTHORIZED, message=repr(e))
    except Exception:
        pass
    if len(e.args) > 1:
        return construct_json_result(code=RetCode.EXCEPTION_ERROR, message=repr(e.args[0]), data=e.args[1])
    if repr(e).find("index_not_found_exception") >= 0:
        return construct_json_result(code=RetCode.EXCEPTION_ERROR,
                                     message="No chunk found, please upload file and parse it.")
    return construct_json_result(code=RetCode.EXCEPTION_ERROR, message=repr(e))


def convert_datetime_to_str(data: dict):
    """
    Convert datetime objects in a dictionary to string format.
    """
    for key, value in data.items():
        if isinstance(value, datetime):
            data[key] = value.strftime('%Y-%m-%d %H:%M:%S')
    return data

def token_required(request: Request, db: Session = Depends(get_db)):
    """
    FastAPI 依赖注入形式的 Token 验证

    从请求头中提取并验证 API Token，返回 tenant_id

    Args:
        request: FastAPI Request 对象
        db: 数据库会话

    Returns:
        str: 租户ID

    Example:
        @router.post("/endpoint")
        def endpoint(tenant_id: str = Depends(token_required)):
            # 使用 tenant_id
            pass
    """
    if os.environ.get("DISABLE_SDK"):
        raise SDKAuthError("SDK API is disabled.")
    authorization_str = request.headers.get("Authorization")
    if not authorization_str:
        raise SDKAuthError("`Authorization` can't be empty")

    authorization_list = authorization_str.split()
    if len(authorization_list) < 2:
        raise SDKAuthError("Please check your authorization format.")

    token = authorization_list[1]
    objs = APIToken.query(db, token=token)
    if not objs:
        raise SDKAuthError("Authentication error: API key is invalid!")
    return objs[0].tenant_id


def beta_token_required(request: Request, db: Session = Depends(get_db)) -> str:
    """
    Beta token validation for chatbots/agentbots/searchbots embedded endpoints.
    Validates Authorization header against APIToken.beta field.
    Returns tenant_id.
    """
    authorization_str = request.headers.get("Authorization", "")
    parts = authorization_str.split()
    if len(parts) != 2:
        raise SDKAuthError("Authorization is not valid!")
    token = parts[1]
    objs = APIToken.query(db, beta=token)
    if not objs:
        raise SDKAuthError("Authentication error: API key is invalid!")
    return objs[0].tenant_id


def current_tenant_id(request: Request, db: Session = Depends(get_db)) -> str:
    """统一鉴权依赖：同时接受 web 会话 JWT 与 SDK API-key，返回 tenant_id。

    对标 ragflow ``api/apps/__init__.py:_load_user`` 的统一加载思路：
    先按 web 会话 token(JWT) 解析（复用 ``LoginManager`` 的解码 + ``user_loader``），
    失败再 fallback 到 SDK API-key（``APIToken`` 表）。这样挂在 ``/api/v1`` 下的
    RESTful 端点既能服务 web 前端（会话登录），又能服务 SDK（API-key），向后兼容。

    Args:
        request: FastAPI Request 对象
        db: 数据库会话

    Returns:
        str: 租户 ID（tenant_id）

    Raises:
        SDKAuthError: 两种凭证均无法通过校验
    """
    authorization_str = request.headers.get("Authorization")
    if not authorization_str:
        raise SDKAuthError("`Authorization` can't be empty")

    parts = authorization_str.split()
    token = parts[1] if len(parts) >= 2 else parts[0]

    # 1) web 会话 JWT：复用 LoginManager 的解码逻辑与 user_loader
    #    （延迟导入避免与 api.apps 的循环依赖）
    from api.apps import load_user, manager

    try:
        payload = manager._get_payload(token)
        email = payload.get("sub")
        if email:
            user = load_user(email, db)
            if user is not None:
                return user.id
    except Exception:
        # 非 web JWT（如 SDK API-key）会在此抛出，转入 fallback
        pass

    # 2) fallback：SDK API-key
    if os.environ.get("DISABLE_SDK"):
        raise SDKAuthError("SDK API is disabled.")
    objs = APIToken.query(db, token=token)
    if not objs:
        raise SDKAuthError("Authentication error: invalid credentials!")
    return objs[0].tenant_id


# def token_required(func):
#     @wraps(func)
#     def decorated_function(*args, **kwargs):
#         request: Request = kwargs.get('request')  # 从 kwargs 中获取 FastAPI Request 对象
#         db: Session = kwargs.get('db')  # 从 kwargs 中获取数据库会话对象
#
#         authorization_header = request.headers.get('Authorization')
#
#         token = authorization_header.split()[1]
#         objs = APIToken.query(db, token=token)
#
#         if not objs:
#             return get_json_result(
#                 data=False, retmsg='Token is not valid!', retcode=RetCode.AUTHENTICATION_ERROR
#             )
#         kwargs['tenant_id'] = objs[0].tenant_id
#         return func(*args, **kwargs)
#
#     return decorated_function


def get_result(retcode=RetCode.SUCCESS, retmsg='error', data=None, total=None):
    """
    Standard API response format:
    {
        "code": 0,
        "data": [...],        # List or object, backward compatible
        "total": 47,          # Optional field for pagination
        "message": "..."      # Error or status message
    }
    """
    response = {"code": retcode}

    if retcode == RetCode.SUCCESS:
        if data is not None:
            response["data"] = data
        if total is not None:
            response["total_datasets"] = total
    else:
        response["message"] = retmsg or "Error"

    return JSONResponse(content=jsonable_encoder(response))


def get_error_data_result(
        retcode=RetCode.DATA_ERROR,
        retmsg='Sorry! Data missing!'
):
    import re
    result_dict = {
        "code": retcode,
        "message": re.sub(
            r"rag",
            "seceum",
            retmsg,
            flags=re.IGNORECASE)}
    response = {}
    for key, value in result_dict.items():
        if value is None and key != "code":
            continue
        else:
            response[key] = value
    return JSONResponse(content=jsonable_encoder(response))


def get_error_argument_result(message="Invalid arguments"):
    return get_result(retcode=RetCode.ARGUMENT_ERROR, retmsg=message)


def get_error_permission_result(message="Permission error"):
    return get_result(retcode=RetCode.PERMISSION_ERROR, retmsg=message)


def get_error_operating_result(message="Operating error"):
    return get_result(retcode=RetCode.OPERATING_ERROR, retmsg=message)


def generate_confirmation_token():
    import secrets
    return "multirag-" + secrets.token_urlsafe(32)


def valid(permission, valid_permission, language, valid_language, chunk_method, valid_chunk_method):
    if valid_parameter(permission, valid_permission):
        return valid_parameter(permission, valid_permission)
    if valid_parameter(language, valid_language):
        return valid_parameter(language, valid_language)
    if valid_parameter(chunk_method, valid_chunk_method):
        return valid_parameter(chunk_method, valid_chunk_method)


def valid_parameter(parameter, valid_values):
    if parameter and parameter not in valid_values:
        return get_error_data_result(f"`{parameter}` is not in {valid_values}")


def flatten_parent_child_config(parser_config: dict[str, Any]) -> dict[str, Any]:
    pc = parser_config.get("parent_child", {})
    if pc.get("use_parent_child"):
        parser_config["children_delimiter"] = pc.get("children_delimiter", "\n")
    elif pc:
        parser_config["children_delimiter"] = ""
    return parser_config


def get_parser_config(chunk_method, parser_config):
    if not chunk_method:
        chunk_method = "naive"

    # Define default configurations for each chunking method
    base_defaults = {
        "table_context_size": 0,
        "image_context_size": 0,
    }
    key_mapping = {
        "naive": {
            "layout_recognize": "DeepDOC",
            "chunk_token_num": 512,
            "delimiter": "\n",
            "auto_keywords": 0,
            "auto_questions": 0,
            "html4excel": False,
            "topn_tags": 3,
            "raptor": {
                "use_raptor": True,
                "prompt": "Please summarize the following paragraphs. Be careful with the numbers, do not make things up. Paragraphs as following:\n      {cluster_content}\nThe above is the content you need to summarize.",
                "max_token": 256,
                "threshold": 0.1,
                "max_cluster": 64,
                "random_seed": 0,
            },
            "graphrag": {
                "use_graphrag": False,
                "entity_types": [
                    "organization",
                    "person",
                    "geo",
                    "event",
                    "category",
                ],
                "method": "light",
            },
            "parent_child": {
                "use_parent_child": False,
                "children_delimiter": "\n",
            },
        },
        "qa": {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}},
        "tag": None,
        "resume": None,
        "manual": {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}},
        "table": None,
        "paper": {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}},
        "book": {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}},
        "laws": {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}},
        "presentation": {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}},
        "one": None,
        "knowledge_graph": {
            "chunk_token_num": 8192,
            "delimiter": r"\n",
            "entity_types": ["organization", "person", "location", "event", "time"],
            "raptor": {"use_raptor": False},
            "graphrag": {"use_graphrag": False},
        },
        "email": None,
        "picture": None,
        "audio": None,
    }

    default_config = key_mapping[chunk_method]

    # If no parser_config provided, return default merged with base defaults
    if not parser_config:
        if default_config is None:
            return flatten_parent_child_config(deep_merge(base_defaults, {}))
        return flatten_parent_child_config(deep_merge(base_defaults, default_config))

    # If parser_config is provided, merge with defaults to ensure required fields exist
    if default_config is None:
        return flatten_parent_child_config(deep_merge(base_defaults, parser_config))

    # Ensure raptor and graph_rag fields have default values if not provided
    merged_config = deep_merge(base_defaults, default_config)
    merged_config = deep_merge(merged_config, parser_config)

    return flatten_parent_child_config(merged_config)


def get_data_openai(
        id=None,
        created=None,
        model=None,
        prompt_tokens=0,
        completion_tokens=0,
        content=None,
        finish_reason=None,
        object="chat.completion",
        param=None,
        stream=False
):
    total_tokens = prompt_tokens + completion_tokens

    if stream:
        return {
            "id": f"{id}",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{
                "delta": {"content": content},
                "finish_reason": finish_reason,
                "index": 0,
            }],
        }

    return {
        "id": f"{id}",
        "object": object,
        "created": int(time.time()) if created else None,
        "model": model,
        "param": param,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "completion_tokens_details": {
                "reasoning_tokens": 0,
                "accepted_prediction_tokens": 0,
                "rejected_prediction_tokens": 0,
            },
        },
        "choices": [{
            "message": {
                "role": "assistant",
                "content": content
            },
            "logprobs": None,
            "finish_reason": finish_reason,
            "index": 0,
        }],
    }


def check_duplicate_ids(ids, id_type="item"):
    """
    Check for duplicate IDs in a list and return unique IDs and error messages.

    Args:
        ids (list): List of IDs to check for duplicates
        id_type (str): Type of ID for error messages (e.g., 'document', 'dataset', 'chunk')

    Returns:
        tuple: (unique_ids, error_messages)
            - unique_ids (list): List of unique IDs
            - error_messages (list): List of error messages for duplicate IDs
    """
    id_count = {}
    duplicate_messages = []

    # Count occurrences of each ID
    for id_value in ids:
        id_count[id_value] = id_count.get(id_value, 0) + 1

    # Check for duplicates
    for id_value, count in id_count.items():
        if count > 1:
            duplicate_messages.append(f"Duplicate {id_type} ids: {id_value}")

    # Return unique IDs and error messages
    return list(set(ids)), duplicate_messages


def verify_embedding_availability(db: Session, embd_id: str, tenant_id: str) -> tuple[bool, str | None]:
    """
    Verifies availability of an embedding model for a specific tenant.

    Performs comprehensive verification through:
    1. Identifier Parsing: Decomposes embd_id into name and factory components
    2. System Verification: Checks model registration in LLMService
    3. Tenant Authorization: Validates tenant-specific model assignments
    4. Built-in Model Check: Confirms inclusion in predefined system models

    Args:
        embd_id (str): Unique identifier for the embedding model in format "model_name@factory"
        tenant_id (str): Tenant identifier for access control

    Returns:
        tuple[bool, str | None]:
        - First element (bool):
            - True: Model is available and authorized
            - False: Validation failed
        - Second element contains:
            - None on success
            - Error message string on failure (service 层友好，不耦合 HTTP 响应)

    Raises:
        ValueError: When model identifier format is invalid
        OperationalError: When database connection fails (auto-handled)

    Examples:
        >>> verify_embedding_availability(db, "text-embedding@openai", "tenant_123")
        (True, None)

        >>> verify_embedding_availability(db, "invalid_model", "tenant_123")
        (False, "Unsupported model: <invalid_model>")
    """
    from api.db.services.llm_service import LLMService
    from api.db.services.tenant_llm_service import TenantLLMService
    try:
        llm_name, llm_factory = TenantLLMService.split_model_name_and_factory(embd_id)
        in_llm_service = bool(LLMService.query(db=db, llm_name=llm_name, fid=llm_factory, model_type="embedding"))

        tenant_llms = TenantLLMService.get_my_llms(db=db, tenant_id=tenant_id)
        is_tenant_model = any(
            llm.llm_name == llm_name and llm.llm_factory == llm_factory and llm.mdl_type == "embedding"
            for llm in tenant_llms
        )

        is_builtin_model = llm_factory=='Builtin'
        if not (is_builtin_model or is_tenant_model or in_llm_service):
            return False, f"Unsupported model: <{embd_id}>"

        if not (is_builtin_model or is_tenant_model):
            return False, f"Unauthorized model: <{embd_id}>"
    except OperationalError as e:
        logging.exception(e)
        return False, "Database operation failed"

    return True, None


def deep_merge(default: dict, custom: dict) -> dict:
    """
    Recursively merges two dictionaries with priority given to `custom` values.

    Creates a deep copy of the `default` dictionary and iteratively merges nested
    dictionaries using a stack-based approach. Non-dict values in `custom` will
    completely override corresponding entries in `default`.

    Args:
        default (dict): Base dictionary containing default values.
        custom (dict): Dictionary containing overriding values.

    Returns:
        dict: New merged dictionary combining values from both inputs.

    Example:
        >>> from copy import deepcopy
        >>> default = {"a": 1, "nested": {"x": 10, "y": 20}}
        >>> custom = {"b": 2, "nested": {"y": 99, "z": 30}}
        >>> deep_merge(default, custom)
        {'a': 1, 'b': 2, 'nested': {'x': 10, 'y': 99, 'z': 30}}

        >>> deep_merge({"config": {"mode": "auto"}}, {"config": "manual"})
        {'config': 'manual'}

    Notes:
        1. Merge priority is always given to `custom` values at all nesting levels
        2. Non-dict values (e.g. list, str) in `custom` will replace entire values
           in `default`, even if the original value was a dictionary
        3. Time complexity: O(N) where N is total key-value pairs in `custom`
        4. Recommended for configuration merging and nested data updates
    """
    merged = deepcopy(default)
    stack = [(merged, custom)]

    while stack:
        base_dict, override_dict = stack.pop()

        for key, val in override_dict.items():
            if key in base_dict and isinstance(val, dict) and isinstance(base_dict[key], dict):
                stack.append((base_dict[key], val))
            else:
                base_dict[key] = val

    return merged


def remap_dictionary_keys(source_data: dict, key_aliases: dict = None) -> dict:
    """
    Transform dictionary keys using a configurable mapping schema.

    Args:
        source_data: Original dictionary to process
        key_aliases: Custom key transformation rules (Optional)
            When provided, overrides default key mapping
            Format: {<original_key>: <new_key>, ...}

    Returns:
        dict: New dictionary with transformed keys preserving original values

    Example:
        >>> input_data = {"old_key": "value", "another_field": 42}
        >>> remap_dictionary_keys(input_data, {"old_key": "new_key"})
        {'new_key': 'value', 'another_field': 42}
    """
    DEFAULT_KEY_MAP = {
        "chunk_num": "chunk_count",
        "doc_num": "document_count",
        "parser_id": "chunk_method",
        "embd_id": "embedding_model",
    }

    transformed_data = {}
    mapping = key_aliases or DEFAULT_KEY_MAP

    for original_key, value in source_data.items():
        mapped_key = mapping.get(original_key, original_key)
        transformed_data[mapped_key] = value

    return transformed_data


def group_by(list_of_dict, key):
    res = {}
    for item in list_of_dict:
        if item[key] in res.keys():
            res[item[key]].append(item)
        else:
            res[item[key]] = [item]
    return res


def get_mcp_tools(mcp_servers: list, timeout: float | int = 10) -> tuple[dict, str]:
    results = {}
    tool_call_sessions = []
    try:
        for mcp_server in mcp_servers:
            server_key = mcp_server.id

            cached_tools = mcp_server.variables.get("tools", {})

            tool_call_session = MCPToolCallSession(mcp_server, mcp_server.variables)
            tool_call_sessions.append(tool_call_session)

            try:
                tools = tool_call_session.get_tools(timeout)
            except Exception:
                tools = []

            results[server_key] = []
            for tool in tools:
                tool_dict = tool.model_dump()
                cached_tool = cached_tools.get(tool_dict["name"], {})

                tool_dict["enabled"] = cached_tool.get("enabled", True)
                results[server_key].append(tool_dict)

        # PERF: blocking call to close sessions — consider moving to background thread or task queue
        close_multiple_mcp_toolcall_sessions(tool_call_sessions)
        return results, ""
    except Exception as e:
        return {}, str(e)




async def is_strong_enough(chat_model, embedding_model):
    count = settings.STRONG_TEST_COUNT
    if not chat_model or not embedding_model:
        return
    if isinstance(count, int) and count <= 0:
        return

    @timeout(60, 2)
    async def _is_strong_enough():
        nonlocal chat_model, embedding_model
        if embedding_model:
            await asyncio.wait_for(
                thread_pool_exec(embedding_model.encode, ["Are you strong enough!?"]),
                timeout=10
            )

        if chat_model:
            res = await asyncio.wait_for(
                chat_model.async_chat("Nothing special.", [{"role": "user", "content": "Are you strong enough!?"}]),
                timeout=30
            )
            if "**ERROR**" in res:
                raise Exception(res)

    # Pressure test for GraphRAG task
    tasks = [
        asyncio.create_task(_is_strong_enough())
        for _ in range(count)
    ]
    try:
        await asyncio.gather(*tasks, return_exceptions=False)
    except Exception as e:
        logging.error(f"Pressure test failed: {e}")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def get_allowed_llm_factories(db) -> list:
    """
    获取允许的LLM工厂列表

    如果在配置中设置了 ALLOWED_LLM_FACTORIES，则只返回配置中指定的工厂；
    否则返回所有工厂。

    Args:
        db: 数据库会话

    Returns:
        list: 允许的LLM工厂对象列表
    """
    factories = list(LLMFactoriesService.get_all(db, reverse=True, order_by="rank"))
    if settings.ALLOWED_LLM_FACTORIES is None:
        return factories

    return [factory for factory in factories if factory.name in settings.ALLOWED_LLM_FACTORIES]

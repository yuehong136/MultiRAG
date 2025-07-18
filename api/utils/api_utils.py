# coding=utf-8
"""
@project: multirag
@Author：龙
@file： api_utils.py
@date：2025/7/17 16:00
@desc:
"""
import asyncio
import logging
import queue
import json
import random
import threading
import time
from datetime import datetime
from functools import wraps
from io import BytesIO
from typing import Any, Callable, Coroutine, Type

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.orm import Session
from hmac import HMAC
from base64 import b64encode
from uuid import uuid1
from urllib.parse import quote, urlencode
import requests

from api.db.db_models import APIToken
from api.db.services.api_service import APITokenService
from api import settings
from api.utils import HTTP_STATUS_CODES, get_uuid
from api.constants import REQUEST_WAIT_SEC, REQUEST_MAX_WAIT_SEC

import trio

from core.utils.mcp_tool_call_conn import MCPToolCallSession, close_multiple_mcp_toolcall_sessions


def request(**kwargs):
    sess = requests.Session()
    stream = kwargs.pop('stream', sess.stream)
    timeout = kwargs.pop('timeout', None)
    kwargs['headers'] = {k.replace('_', '-').upper(): v for k, v in kwargs.get('headers', {}).items()}
    prepped = requests.Request(**kwargs).prepare()

    if settings.CLIENT_AUTHENTICATION and settings.HTTP_APP_KEY and settings.SECRET_KEY:
        timestamp = str(round(time.time() * 1000))
        nonce = str(uuid1())
        signature = b64encode(HMAC(settings.SECRET_KEY.encode('ascii'), b'\n'.join([
            timestamp.encode('ascii'),
            nonce.encode('ascii'),
            settings.HTTP_APP_KEY.encode('ascii'),
            prepped.path_url.encode('ascii'),
            prepped.body if kwargs.get('json') else b'',
            urlencode(
                sorted(
                    kwargs['data'].items()),
                quote_via=quote,
                safe='-._~').encode('ascii')
            if kwargs.get('data') and isinstance(kwargs['data'], dict) else b'',
        ]), 'sha1').digest()).decode('ascii')

        prepped.headers.update({
            'TIMESTAMP': timestamp,
            'NONCE': nonce,
            'APP-KEY': settings.HTTP_APP_KEY,
            'SIGNATURE': signature,
        })

    return sess.send(prepped, stream=stream, timeout=timeout)


def get_exponential_backoff_interval(retries, full_jitter=False):
    countdown = min(REQUEST_MAX_WAIT_SEC, REQUEST_WAIT_SEC * (2 ** retries))
    if full_jitter:
        countdown = random.randrange(countdown + 1)
    return max(0, countdown)


def get_data_error_result(retcode=settings.RetCode.DATA_ERROR, retmsg='Sorry! Data missing!'):
    logging.error(retmsg)
    result_dict = {
        "retcode": retcode,
        "retmsg": retmsg
    }
    response = {key: value for key, value in result_dict.items() if value is not None or key == "retcode"}
    return JSONResponse(content=jsonable_encoder(response))


def server_error_response(e):
    logging.exception(e)
    try:
        if e.code == 401:
            return get_json_result(retcode=401, retmsg=repr(e))
    except Exception:
        pass
    if len(e.args) > 1:
        return get_json_result(retcode=settings.RetCode.EXCEPTION_ERROR, retmsg=repr(e.args[0]), data=e.args[1])
    if repr(e).find("index_not_found_exception") >= 0:
        return get_json_result(retcode=settings.RetCode.EXCEPTION_ERROR,
                               retmsg="No chunk found, please upload file and parse it.")
    return get_json_result(retcode=settings.RetCode.EXCEPTION_ERROR, retmsg=repr(e))


def error_response(response_code, retmsg=None):
    if retmsg is None:
        retmsg = HTTP_STATUS_CODES.get(response_code, 'Unknown Error')
    return JSONResponse(status_code=response_code, content={
        'retmsg': retmsg,
        'retcode': response_code,
    })


def validate_request(*args, **kwargs):
    def wrapper(func):
        @wraps(func)
        async def decorated_function(request: Request, *args, **kwargs):
            input_arguments = await request.json()
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
                return get_json_result(retcode=settings.RetCode.ARGUMENT_ERROR, retmsg=error_string)
            return await func(request, *args, **kwargs)

        return decorated_function

    return wrapper


def is_localhost(ip):
    return ip in {'127.0.0.1', '::1', '[::1]', 'localhost'}


def send_file_in_mem(data, filename):
    if not isinstance(data, (str, bytes)):
        data = json.dumps(data)
    if isinstance(data, str):
        data = data.encode('utf-8')

    f = BytesIO()
    f.write(data)
    f.seek(0)

    return Response(content=f.getvalue(), media_type='application/octet-stream', headers={
        'Content-Disposition': f'attachment; filename={filename}'
    })


# def get_json_result(retcode=RetCode.SUCCESS, retmsg='success', data=None, job_id=None, meta=None):
#     result_dict = {
#         "retcode": retcode,
#         "retmsg": retmsg,
#         "data": data,
#         "jobId": job_id,
#         "meta": meta,
#     }
#     response = {key: value for key, value in result_dict.items() if value is not None or key == "retcode"}
#     return JSONResponse(content=jsonable_encoder(response))


def get_json_result(retcode=settings.RetCode.SUCCESS, retmsg='success', data=None):
    response = {"retcode": retcode, "retmsg": retmsg, "data": data}
    return JSONResponse(content=jsonable_encoder(response))


def apikey_required(func: Callable) -> Callable:
    @wraps(func)
    async def decorated_function(*args, **kwargs):
        request: Request = kwargs.get('request')  # 从 kwargs 中获取 FastAPI Request 对象
        db: Session = kwargs.get('db')  # 从 kwargs 中获取数据库会话对象

        authorization_header = request.headers.get('Authorization')

        token = authorization_header.split()[1]
        objs = APITokenService.query(db, token=token)

        if not objs:
            return build_error_result(
                error_msg='API-KEY is invalid!', retcode=settings.RetCode.FORBIDDEN
            )

        kwargs['tenant_id'] = objs[0].tenant_id
        return await func(*args, **kwargs)

    return decorated_function


def build_error_result(retcode=settings.RetCode.FORBIDDEN, error_msg='success'):
    response_content = {"error_code": retcode, "error_msg": error_msg}
    return JSONResponse(content=response_content, status_code=retcode)


def construct_response(retcode=settings.RetCode.SUCCESS, retmsg='success', data=None, auth=None):
    result_dict = {"retcode": retcode, "retmsg": retmsg, "data": data}
    response_dict = {key: value for key, value in result_dict.items() if value is not None or key == "retcode"}
    response = JSONResponse(content=jsonable_encoder(response_dict))
    if auth:
        response.headers["Authorization"] = auth
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Method"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Expose-Headers"] = "Authorization"
    return response


def construct_result(code=settings.RetCode.DATA_ERROR, message='data is missing'):
    result_dict = {"code": code, "message": message}
    response = {key: value for key, value in result_dict.items() if value is not None or key == "code"}
    return JSONResponse(content=jsonable_encoder(response))


def construct_json_result(code=settings.RetCode.SUCCESS, message='success', data=None):
    if data is None:
        return JSONResponse(content={"code": code, "message": message})
    else:
        return JSONResponse(content={"code": code, "message": message, "data": data})


def construct_error_response(e):
    logging.exception(e)
    try:
        if e.code == 401:
            return construct_json_result(code=settings.RetCode.UNAUTHORIZED, message=repr(e))
    except Exception:
        pass
    if len(e.args) > 1:
        return construct_json_result(code=settings.RetCode.EXCEPTION_ERROR, message=repr(e.args[0]), data=e.args[1])
    if repr(e).find("index_not_found_exception") >= 0:
        return construct_json_result(code=settings.RetCode.EXCEPTION_ERROR,
                                     message="No chunk found, please upload file and parse it.")
    return construct_json_result(code=settings.RetCode.EXCEPTION_ERROR, message=repr(e))


def convert_datetime_to_str(data: dict):
    """
    Convert datetime objects in a dictionary to string format.
    """
    for key, value in data.items():
        if isinstance(value, datetime):
            data[key] = value.strftime('%Y-%m-%d %H:%M:%S')
    return data


def token_required(func):
    @wraps(func)
    def decorated_function(*args, **kwargs):
        request: Request = kwargs.get('request')  # 从 kwargs 中获取 FastAPI Request 对象
        db: Session = kwargs.get('db')  # 从 kwargs 中获取数据库会话对象

        authorization_header = request.headers.get('Authorization')

        token = authorization_header.split()[1]
        objs = APIToken.query(db, token=token)

        if not objs:
            return get_json_result(
                data=False, retmsg='Token is not valid!', retcode=settings.RetCode.AUTHENTICATION_ERROR
            )
        kwargs['tenant_id'] = objs[0].tenant_id
        return func(*args, **kwargs)

    return decorated_function


def get_result(retcode=settings.RetCode.SUCCESS, retmsg='error', data=None):
    if retcode == 0:
        if data is not None:
            response = {"code": retcode, "data": data}
        else:
            response = {"code": retcode}
    else:
        response = {"code": retcode, "message": retmsg}
    return JSONResponse(content=jsonable_encoder(response))


def get_error_data_result(retcode=settings.RetCode.DATA_ERROR,
                          retmsg='Sorry! Data missing!'):
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
    return get_result(retcode=settings.RetCode.ARGUMENT_ERROR, retmsg=message)


def generate_confirmation_token(tenant_id: str) -> str:
    serializer = URLSafeTimedSerializer(tenant_id)
    return "multirag-" + serializer.dumps(get_uuid(), salt=tenant_id)[2:34]


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


def get_parser_config(chunk_method, parser_config):
    if parser_config:
        return parser_config
    if not chunk_method:
        chunk_method = "naive"
    key_mapping = {
        "naive": {"chunk_token_num": 128, "delimiter": r"\n", "html4excel": False, "layout_recognize": "DeepDOC", "raptor": {"use_raptor": False}},
        "qa": {"raptor": {"use_raptor": False}},
        "tag": None,
        "resume": None,
        "manual": {"raptor": {"use_raptor": False}},
        "table": None,
        "paper": {"raptor": {"use_raptor": False}},
        "book": {"raptor": {"use_raptor": False}},
        "laws": {"raptor": {"use_raptor": False}},
        "presentation": {"raptor": {"use_raptor": False}},
        "one": None,
        "knowledge_graph": {"chunk_token_num": 8192, "delimiter": r"\n", "entity_types": ["organization", "person", "location", "event", "time"]},
        "email": None,
        "picture": None,
    }
    parser_config = key_mapping[chunk_method]
    return parser_config


def get_data_openai(id=None,
                    created=None,
                    model=None,
                    prompt_tokens=0,
                    completion_tokens=0,
                    content=None,
                    finish_reason=None,
                    object="chat.completion",
                    param=None,
                    ):
    total_tokens = prompt_tokens + completion_tokens
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
                "rejected_prediction_tokens": 0
            }
        },
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content
                },
                "logprobs": None,
                "finish_reason": finish_reason,
                "index": 0
            }
        ]
    }


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


TimeoutException = Type[BaseException] | BaseException
OnTimeoutCallback = Callable[..., Any] | Coroutine[Any, Any, Any]

def timeout(
    seconds: float | int | None = None,
    attempts: int = 2,
    *,
    exception: TimeoutException | None = None,
    on_timeout: OnTimeoutCallback | None = None
):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            result_queue = queue.Queue(maxsize=1)
            def target():
                try:
                    result = func(*args, **kwargs)
                    result_queue.put(result)
                except Exception as e:
                    result_queue.put(e)

            thread = threading.Thread(target=target)
            thread.daemon = True
            thread.start()

            for a in range(attempts):
                try:
                    result = result_queue.get(timeout=seconds)
                    if isinstance(result, Exception):
                        raise result
                    return result
                except queue.Empty:
                    pass
            raise TimeoutError(f"Function '{func.__name__}' timed out after {seconds} seconds and {attempts} attempts.")

        @wraps(func)
        async def async_wrapper(*args, **kwargs) -> Any:
            if seconds is None:
                return await func(*args, **kwargs)

            for a in range(attempts):
                try:
                    with trio.fail_after(seconds):
                        return await func(*args, **kwargs)
                except trio.TooSlowError:
                    if a < attempts - 1:
                        continue
                    if on_timeout is not None:
                        if callable(on_timeout):
                            result = on_timeout()
                            if isinstance(result, Coroutine):
                                return await result
                            return result
                        return on_timeout

                    if exception is None:
                        raise TimeoutError(f"Operation timed out after {seconds} seconds and {attempts} attempts.")

                    if isinstance(exception, BaseException):
                        raise exception

                    if isinstance(exception, type) and issubclass(exception, BaseException):
                        raise exception(f"Operation timed out after {seconds} seconds and {attempts} attempts.")

                    raise RuntimeError("Invalid exception type provided")

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return wrapper
    return decorator


async def is_strong_enough(chat_model, embedding_model):

    @timeout(30, 2)
    async def _is_strong_enough():
        nonlocal chat_model, embedding_model
        _ = await trio.to_thread.run_sync(lambda: embedding_model.encode(["Are you strong enough!?"]))
        res =  await trio.to_thread.run_sync(lambda: chat_model.chat("Nothing special.", [{"role":"user", "content": "Are you strong enough!?"}], {}))
        if res.find("**ERROR**") >= 0:
            raise Exception(res)

    # Pressure test for GraphRAG task
    async with trio.open_nursery() as nursery:
        for _ in range(12):
            nursery.start_soon(_is_strong_enough)
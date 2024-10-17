# coding=utf-8
"""
@project: multirag
@Author：龙
@file： api_utils.py
@date：2024/7/9 9:00
@desc:
"""
import json
import random
import time
from datetime import datetime
from functools import wraps
from io import BytesIO
from typing import Callable

from fastapi import FastAPI, Request, Response, Depends
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from hmac import HMAC
from base64 import b64encode
from uuid import uuid1
from urllib.parse import quote, urlencode
import requests

from api.db.services.api_service import APITokenService
from api.settings import RetCode, REQUEST_MAX_WAIT_SEC, REQUEST_WAIT_SEC, stat_logger, CLIENT_AUTHENTICATION, \
    HTTP_APP_KEY, SECRET_KEY
from api.utils import HTTP_STATUS_CODES, get_uuid


def request(**kwargs):
    sess = requests.Session()
    stream = kwargs.pop('stream', sess.stream)
    timeout = kwargs.pop('timeout', None)
    kwargs['headers'] = {k.replace('_', '-').upper(): v for k, v in kwargs.get('headers', {}).items()}
    prepped = requests.Request(**kwargs).prepare()

    if CLIENT_AUTHENTICATION and HTTP_APP_KEY and SECRET_KEY:
        timestamp = str(round(time.time() * 1000))
        nonce = str(uuid1())
        signature = b64encode(HMAC(SECRET_KEY.encode('ascii'), b'\n'.join([
            timestamp.encode('ascii'),
            nonce.encode('ascii'),
            HTTP_APP_KEY.encode('ascii'),
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
            'APP-KEY': HTTP_APP_KEY,
            'SIGNATURE': signature,
        })

    return sess.send(prepped, stream=stream, timeout=timeout)


def get_exponential_backoff_interval(retries, full_jitter=False):
    countdown = min(REQUEST_MAX_WAIT_SEC, REQUEST_WAIT_SEC * (2 ** retries))
    if full_jitter:
        countdown = random.randrange(countdown + 1)
    return max(0, countdown)


def get_data_error_result(retcode=RetCode.DATA_ERROR, retmsg='Sorry! Data missing!'):
    result_dict = {
        "retcode": retcode,
        "retmsg": retmsg.replace("rag", "seceum"),
    }
    response = {key: value for key, value in result_dict.items() if value is not None or key == "retcode"}
    return JSONResponse(content=jsonable_encoder(response))


def server_error_response(e):
    stat_logger.exception(e)
    try:
        if e.code == 401:
            return get_json_result(retcode=401, retmsg=repr(e))
    except Exception:
        pass
    if len(e.args) > 1:
        return get_json_result(retcode=RetCode.EXCEPTION_ERROR, retmsg=repr(e.args[0]), data=e.args[1])
    if repr(e).find("index_not_found_exception") >= 0:
        return get_json_result(retcode=RetCode.EXCEPTION_ERROR,
                               retmsg="No chunk found, please upload file and parse it.")
    return get_json_result(retcode=RetCode.EXCEPTION_ERROR, retmsg=repr(e))


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
                return get_json_result(retcode=RetCode.ARGUMENT_ERROR, retmsg=error_string)
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


def get_json_result(retcode=RetCode.SUCCESS, retmsg='success', data=None):
    response = {"retcode": retcode, "retmsg": retmsg, "data": data}
    return JSONResponse(content=jsonable_encoder(response))


def apikey_required(func: Callable) -> Callable:
    @wraps(func)
    async def decorated_function(*args, **kwargs):
        request: Request = kwargs.get('request')  # 从 kwargs 中获取 FastAPI Request 对象
        db: Session = kwargs.get('db')  # 从 kwargs 中获取数据库会话对象

        authorization_header = request.headers.get('Authorization')

        token = authorization_header.split()[1]
        objs = APITokenService.query(db, token=token, db=db)

        if not objs:
            return build_error_result(
                error_msg='API-KEY is invalid!', retcode=RetCode.FORBIDDEN
            )

        kwargs['tenant_id'] = objs[0].tenant_id
        return await func(*args, **kwargs)

    return decorated_function


def build_error_result(retcode=RetCode.FORBIDDEN, error_msg='success'):
    response_content = {"error_code": retcode, "error_msg": error_msg}
    return JSONResponse(content=response_content, status_code=retcode)


def construct_response(retcode=RetCode.SUCCESS, retmsg='success', data=None, auth=None):
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


def construct_result(code=RetCode.DATA_ERROR, message='data is missing'):
    result_dict = {"code": code, "message": message.replace("rag", "seceum")}
    response = {key: value for key, value in result_dict.items() if value is not None or key == "code"}
    return JSONResponse(content=jsonable_encoder(response))


def construct_json_result(code=RetCode.SUCCESS, message='success', data=None):
    if data is None:
        return JSONResponse(content={"code": code, "message": message})
    else:
        return JSONResponse(content={"code": code, "message": message, "data": data})


def construct_error_response(e):
    stat_logger.exception(e)
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


def get_result(retcode=RetCode.SUCCESS, retmsg='error', data=None):
    if retcode == 0:
        if data is not None:
            response = {"code": retcode, "data": data}
        else:
            response = {"code": retcode}
    else:
        response = {"code": retcode, "message": retmsg}
    return JSONResponse(content=jsonable_encoder(response))


def get_error_data_result(retcode=RetCode.DATA_ERROR,
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


def generate_confirmation_token(tenent_id: object) -> object:
    serializer = URLSafeTimedSerializer(tenent_id)
    return "multirag-" + serializer.dumps(get_uuid(), salt=tenent_id)[2:34]

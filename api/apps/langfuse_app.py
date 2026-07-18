from fastapi import APIRouter, Depends
from langfuse import Langfuse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.db.services.langfuse_service import TenantLangfuseService
from api.utils.api_utils import get_error_data_result, get_json_result, server_error_response
from api.utils.web_utils import validate_outbound_url
from common.app_config import get_app_config


class LangfuseKeysRequest(BaseModel):
    secret_key: str
    public_key: str
    host: str


router = APIRouter()


@router.post("/api_key", summary="设置Langfuse API密钥")
@router.put("/api_key", summary="更新Langfuse API密钥")
def set_api_key(request: LangfuseKeysRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    设置或更新Langfuse API密钥

    该接口用于设置或更新当前用户的Langfuse API密钥。

    参数:
    - request: LangfuseKeysRequest对象，包含API密钥信息
        - secret_key: Langfuse密钥
        - public_key: Langfuse公钥
        - host: Langfuse主机地址

    返回:
    - 成功时返回包含API密钥信息的JSON结果
    - 失败时返回错误信息
    """
    try:
        secret_key = request.secret_key
        public_key = request.public_key
        host = request.host

        if not all([secret_key, public_key, host]):
            return get_error_data_result(retmsg="Missing required fields")

        try:
            validate_outbound_url(host, get_app_config().observability.langfuse_allowed_hosts)
        except ValueError as e:
            return get_error_data_result(retmsg=f"Invalid Langfuse host: {e}")

        langfuse_keys = {
            "tenant_id": user.id,
            "secret_key": secret_key,
            "public_key": public_key,
            "host": host,
        }

        # 验证API密钥有效性
        langfuse = Langfuse(public_key=langfuse_keys["public_key"], secret_key=langfuse_keys["secret_key"], host=langfuse_keys["host"])

        if not langfuse.auth_check():
            return get_error_data_result(retmsg="Invalid Langfuse keys")

        langfuse_entry = TenantLangfuseService.filter_by_tenant(db, tenant_id=user.id)

        if not langfuse_entry:
            TenantLangfuseService.save(db, **langfuse_keys)
        else:
            TenantLangfuseService.update_by_tenant(db, tenant_id=user.id, langfuse_keys=langfuse_keys)

        return get_json_result(data=langfuse_keys)
    except Exception as e:
        db.rollback()
        return server_error_response(e)


@router.get("/api_key", summary="获取Langfuse API密钥")
def get_api_key(db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取Langfuse API密钥

    该接口用于获取当前用户的Langfuse API密钥信息。

    返回:
    - 成功时返回包含API密钥信息的JSON结果
    - 失败时返回错误信息
    """
    try:
        langfuse_entry = TenantLangfuseService.filter_by_tenant_with_info(db, tenant_id=user.id)
        if not langfuse_entry:
            return get_error_data_result(retmsg="Have not record any Langfuse keys.")

        # 纵深防御：存量行（本校验上线前写入）在探测前同样过出网边界
        try:
            validate_outbound_url(langfuse_entry["host"], get_app_config().observability.langfuse_allowed_hosts)
        except ValueError as e:
            return get_error_data_result(retmsg=f"Invalid Langfuse host: {e}")

        langfuse = Langfuse(public_key=langfuse_entry["public_key"], secret_key=langfuse_entry["secret_key"], host=langfuse_entry["host"])
        try:
            if not langfuse.auth_check():
                return get_error_data_result(retmsg="Invalid Langfuse keys loaded")
        except langfuse.api.core.api_error.ApiError as api_err:
            return get_json_result(retmsg=f"Error from Langfuse: {api_err}")
        except Exception as e:
            return server_error_response(e)

        # langfuse fern SDK 的模型是 pydantic.v1 风格（无 model_dump），保留 .dict()
        project = langfuse.api.projects.get().dict()["data"][0]
        langfuse_entry["project_id"] = project["id"]
        langfuse_entry["project_name"] = project["name"]

        return get_json_result(data=langfuse_entry)
    except Exception as e:
        return server_error_response(e)


@router.delete("/api_key", summary="删除Langfuse API密钥")
def delete_api_key(db: Session = Depends(get_db), user=Depends(manager)):
    """
    删除Langfuse API密钥

    该接口用于删除当前用户的Langfuse API密钥。

    返回:
    - 成功时返回操作结果
    - 失败时返回错误信息
    """
    try:
        langfuse_entry = TenantLangfuseService.filter_by_tenant(db, tenant_id=user.id)
        if not langfuse_entry:
            return get_error_data_result(retmsg="Have not record any Langfuse keys.")

        TenantLangfuseService.delete_model(db, langfuse_entry)
        return get_json_result(data=True)
    except Exception as e:
        db.rollback()
        return server_error_response(e)

# coding=utf-8
"""
@project: multirag
@Author：龙
@file： llm_app.py
@date：2024/7/11 14:30
@desc:
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_login.exceptions import InvalidCredentialsException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from api.apps import manager
from api.db.database import get_db
from api.db.services.llm_service import LLMFactoriesService, TenantLLMService, LLMService, LLMBundle
from api.db.services.user_service import TenantService
from api.settings import RetCode, LIGHTEN
from api.utils.api_utils import get_json_result, server_error_response, validate_request, get_data_error_result
from api.db import StatusEnum, LLMType
from api.db.db_models import TenantLLM
from core.llm import EmbeddingModel, ChatModel, CvModel, RerankModel, TTSModel
from pydantic import BaseModel
from typing import Optional, Any
import requests


class SetAPIKeyRequest(BaseModel):
    llm_factory: str
    api_key: str
    base_url: Optional[str] = None


class AddLLMRequest(BaseModel):
    llm_factory: str
    llm_name: str
    mdl_type: str
    api_key: str = None
    api_base: Optional[str] = None
    ark_api_key: Optional[str] = None
    endpoint_id: Optional[str] = None
    bedrock_ak: Optional[str] = None
    bedrock_sk: Optional[str] = None
    bedrock_region: Optional[str] = None


class DeleteLLMRequest(BaseModel):
    llm_factory: str
    llm_name: str


class ListLLMRequest(BaseModel):
    mdl_type: Optional[str] = None


class DeleteFactoryRequest(BaseModel):
    llm_factory: Optional[str] = None


class LLMServiceRequest(BaseModel):
    prompt: Optional[str] = None
    messages: list[dict] = []
    llm_name: str
    gen_conf: dict[str, Any] = {}

router = APIRouter()


@router.get('/factories', summary="获取模型供应商信息", response_description="成功获取到所有模型供应商信息")
async def factories(db: Session = Depends(get_db), user=Depends(manager)):
    """
    此异步函数用于获取所有模型供应商的信息，排除特定供应商，并将结果以JSON格式返回。
    摘要: 获取模型供应商信息
    响应描述: 成功获取到所有模型供应商信息

    参数:
    - db (Session): 依赖注入的数据库会话，用于执行数据库查询。
    - user: 依赖注入的当前用户信息，由manager提供，但在此函数中未使用。

    返回:
    - dict: 包含模型供应商信息的JSON结果，数据部分是一个字典列表，每个字典代表一个供应商的信息。

    功能:
    1. 查询数据库中所有的模型供应商信息。
    2. 排除名为"Youdao"、"FastEmbed"和"BAAI"的供应商信息。
    3. 将筛选后的供应商信息转换为字典列表。
    4. 将结果封装为JSON格式的字典并返回。

    流程:
    1. 使用LLMFactoriesService从数据库中获取所有供应商信息。
    2. 遍历获取的供应商信息，排除特定名称的供应商。
    3. 将筛选后的供应商信息转换为字典列表。
    4. 返回封装后的JSON结果。

    异常处理:
    - 如果在执行数据库操作或数据处理过程中发生异常，将捕获异常并调用server_error_response函数返回服务器错误响应。

    注意:
    - 被排除的供应商名称"Youdao"、"FastEmbed"和"BAAI"可能是系统默认供应商或特殊供应商，具体原因需根据实际业务逻辑确定。
    """

    try:
        fac = LLMFactoriesService.get_all(db)
        fac = [f.to_dict() for f in fac if f.name not in ["Youdao", "FastEmbed", "BAAI"]]
        llms = LLMService.get_all(db)
        mdl_types = {}
        for m in llms:
            if m.status != StatusEnum.VALID.value:
                continue
            if m.fid not in mdl_types:
                mdl_types[m.fid] = set([])
            mdl_types[m.fid].add(m.mdl_type)
        for f in fac:
            f["model_types"] = list(mdl_types.get(f["name"], [LLMType.CHAT, LLMType.EMBEDDING, LLMType.RERANK,
                                                              LLMType.IMAGE2TEXT, LLMType.SPEECH2TEXT, LLMType.TTS]))
        return get_json_result(data=fac)
    except Exception as e:
        return server_error_response(e)


@router.post('/set_api_key', summary="新增模型厂商api key", response_description="成功保存该模型服务厂商的api key")
async def set_api_key(request: SetAPIKeyRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    此异步函数用于设置模型制造商的API密钥，并验证其是否能正确访问特定类型的模型。
    摘要: 新增模型厂商api key
    响应描述: 成功保存该模型服务厂商的api key

    参数:
    - request (SetAPIKeyRequest): 一个依赖注入的请求对象，包含模型工厂ID、API密钥等信息。
    - db (Session): 依赖注入的数据库会话，用于执行数据库操作。
    - user: 依赖注入的当前用户信息，由manager提供。

    返回:
    - dict: 成功时返回一个表示操作成功的JSON结果；失败时返回一个包含错误信息的JSON结果。

    功能:
    1. 验证API密钥是否能够成功访问聊天模型（Chat），并尝试访问嵌入（Embedding）和重排序（Rerank）模型（注：后两者当前未实现）。
    2. 如果API密钥无法访问任何模型，函数将返回一个错误结果。
    3. 更新或创建租户的模型配置，包括API密钥、基础URL等信息。
    4. 如果在更新或创建过程中遇到完整性错误（例如，API密钥已存在），则抛出HTTP异常。

    流程:
    1. 解析请求体并初始化变量。
    2. 遍历所有属于指定模型工厂的模型，尝试使用API密钥访问它们。
    3. 如果访问失败，收集错误信息。
    4. 如果有错误信息，返回错误结果。
    5. 否则，更新或创建租户的模型配置。
    6. 返回操作成功的JSON结果。

    注意:
    - 目前仅实现了对聊天模型的访问测试。
    - 未来可能扩展到嵌入和重排序模型的测试。
    - 在更新或创建租户模型配置时，会检查API密钥是否已存在，以避免重复。
    """
    req = request.model_dump()
    chat_passed, embd_passed, rerank_passed = False, False, False
    factory = req["llm_factory"]
    msg = ""
    for llm in LLMService.query(db, fid=factory):
        # todo 适配其他模型的测试，目前只有chat进行了测试
        if not embd_passed and llm.mdl_type == LLMType.EMBEDDING.value:
            mdl = EmbeddingModel[factory](req["api_key"], llm.llm_name, base_url=req.get("base_url"))
            try:
                arr, tc = mdl.encode(["Test if the api key is available"])
                if len(arr[0]) == 0:
                    raise Exception("Fail")
                embd_passed = True
            except Exception as e:
                msg += f"\nFail to access embedding model({llm.llm_name}) using this api key." + str(e)
        elif not chat_passed and llm.mdl_type == LLMType.CHAT.value:
            mdl = ChatModel[factory](req["api_key"], llm.llm_name, base_url=req.get("base_url"))
            try:
                m, tc = mdl.chat(None, [{"role": "user", "content": "Hello! How are you doing!"}],
                                 {"temperature": 0.9, 'max_tokens': 50})
                print(m)
                if m.find("**ERROR**") >= 0:
                    raise Exception(m)
            except Exception as e:
                msg += f"\nFail to access model({llm.llm_name}) using this api key." + str(e)
            # todo 是否有必要每一个模型都测试呢？目前都是我内置的型号，都是可靠的，如果除本项目维护人员，进行自由添加可能出现问题
            chat_passed = True
        elif not rerank_passed and llm.model_type == LLMType.RERANK:
            mdl = RerankModel[factory](req["api_key"], llm.llm_name, base_url=req.get("base_url"))
            try:
                arr, tc = mdl.similarity("What's the weather?", ["Is it sunny today?"])
                if len(arr) == 0 or tc == 0:
                    raise Exception("Fail")
                rerank_passed = True
                print(f'passed model rerank{llm.llm_name}', flush=True)
            except Exception as e:
                msg += f"\nFail to access model({llm.llm_name}) using this api key." + str(e)

    if msg:
        return get_data_error_result(retmsg=msg)

    llm_config = {
        "api_key": req["api_key"],
        "api_base": req.get("base_url", "")
    }
    for n in ["mdl_type", "llm_name"]:
        if n in req:
            llm_config[n] = req[n]

    try:
        for llm in LLMService.query(db, fid=factory):
            if not TenantLLMService.filter_update(
                    db,
                    [TenantLLM.tenant_id == user.id,
                     TenantLLM.llm_factory == factory,
                     TenantLLM.llm_name == llm.llm_name],
                    llm_config
            ):
                TenantLLMService.save(
                    db,
                    tenant_id=user.id,
                    llm_factory=factory,
                    llm_name=llm.llm_name,
                    mdl_type=llm.mdl_type,
                    api_key=llm_config["api_key"],
                    api_base=llm_config["api_base"]
                )
    except IntegrityError as e:
        raise HTTPException(status_code=400, detail="API key already exists for this LLM factory and name.")

    return get_json_result(data=True)


@router.post('/add_llm', summary="新增模型", response_description="成功新增该模型")
async def add_llm(request: AddLLMRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    此异步函数用于添加新的语言模型（LLM），支持不同的模型供应商，并验证模型的可用性。
    摘要: 新增模型
    响应描述: 成功新增该模型

    参数:
    - request (AddLLMRequest): 一个依赖注入的请求对象，包含模型供应商、模型类型、模型名称、API密钥等信息。
    - db (Session): 依赖注入的数据库会话，用于执行数据库操作。
    - user: 依赖注入的当前用户信息，由manager提供。

    返回:
    - dict: 成功时返回一个表示操作成功的JSON结果。

    功能:
    1. 根据模型供应商（如VolcEngine、Bedrock或其他）构造特定格式的API密钥。
    2. 验证模型的可用性，包括嵌入（Embedding）和聊天（Chat）模型。
    3. 如果模型不可用，收集错误信息并抛出HTTP异常。
    4. 更新或创建租户的模型配置。
    5. 如果在更新或创建过程中遇到完整性错误（例如，模型已存在），则抛出HTTP异常。

    流程:
    1. 解析请求体，根据模型供应商构造API密钥。
    2. 根据模型类型实例化相应的模型类，并尝试访问模型。
    3. 如果访问失败，收集错误信息。
    4. 如果有错误信息，抛出HTTP异常。
    5. 否则，更新或创建租户的模型配置。
    6. 返回操作成功的JSON结果。

    异常处理:
    - 如果在验证模型可用性或执行数据库操作过程中发生异常，将捕获异常并抛出HTTP异常。

    注意:
    - 支持的模型供应商包括VolcEngine、Bedrock和其他供应商，每种供应商的API密钥格式不同。
    - 模型验证过程包括尝试访问嵌入和聊天模型，确保API密钥有效。
    - 在更新或创建租户模型配置时，会检查模型是否已存在，以避免重复。
    """
    req = request.model_dump()
    factory = req["llm_factory"]

    def apikey_json(keys):
        nonlocal req
        return json.dumps({k: req.get(k, "") for k in keys})

    if factory == "VolcEngine":
        # For VolcEngine, due to its special authentication method
        # Assemble ark_api_key endpoint_id into api_key
        llm_name = req["llm_name"]
        api_key = apikey_json(["ark_api_key", "endpoint_id"])

    elif factory == "Tencent Hunyuan":
        req["api_key"] = apikey_json(["hunyuan_sid", "hunyuan_sk"])
        return set_api_key()

    elif factory == "Tencent Cloud":
        req["api_key"] = apikey_json(["tencent_cloud_sid", "tencent_cloud_sk"])

    elif factory == "Bedrock":
        llm_name = req["llm_name"]
        api_key = apikey_json(["bedrock_ak", "bedrock_sk", "bedrock_region"])

    elif factory == "LocalAI":
        llm_name = req["llm_name"] + "___LocalAI"
        api_key = "xxxxxxxxxxxxxxx"

    elif factory == "HuggingFace":
        llm_name = req["llm_name"] + "___HuggingFace"
        api_key = "xxxxxxxxxxxxxxx"

    elif factory == "OpenAI-API-Compatible":
        llm_name = req["llm_name"] + "___OpenAI-API"
        api_key = req.get("api_key", "xxxxxxxxxxxxxxx")

    elif factory == "XunFei Spark":
        llm_name = req["llm_name"]
        if req["mdl_type"] == "chat":
            api_key = req.get("spark_api_password", "xxxxxxxxxxxxxxx")
        elif req["mdl_type"] == "tts":
            api_key = apikey_json(["spark_app_id", "spark_api_secret", "spark_api_key"])

    elif factory == "BaiduYiyan":
        llm_name = req["llm_name"]
        api_key = apikey_json(["yiyan_ak", "yiyan_sk"])

    elif factory == "Fish Audio":
        llm_name = req["llm_name"]
        api_key = apikey_json(["fish_audio_ak", "fish_audio_refid"])

    elif factory == "Google Cloud":
        llm_name = req["llm_name"]
        api_key = apikey_json(["google_project_id", "google_region", "google_service_account_key"])

    elif factory == "Azure-OpenAI":
        llm_name = req["llm_name"]
        api_key = apikey_json(["api_key", "api_version"])

    else:
        llm_name = req["llm_name"]
        api_key = req.get("api_key", "xxxxxxxxxxxxxxx")

    llm = {
        "tenant_id": user.id,
        "llm_factory": factory,
        "mdl_type": req["mdl_type"],
        "llm_name": llm_name,
        "api_base": req.get("api_base", ""),
        "api_key": api_key
    }

    msg = ""
    if llm["mdl_type"] == LLMType.EMBEDDING.value:
        mdl = EmbeddingModel[factory](
            key=llm['api_key'],
            model_name=llm["llm_name"],
            base_url=llm["api_base"])
        try:
            arr, tc = mdl.encode(["Test if the api key is available"])
            if len(arr[0]) == 0:
                raise Exception("Fail")
        except Exception as e:
            msg += f"\nFail to access embedding model({llm['llm_name']})." + str(e)
    elif llm["mdl_type"] == LLMType.CHAT.value:
        mdl = ChatModel[factory](
            key=llm['api_key'],
            model_name=llm["llm_name"],
            base_url=llm["api_base"]
        )
        try:
            m, tc = mdl.chat(None, [{"role": "user", "content": "Hello! How are you doing!"}],
                             {"temperature": 0.9, 'max_tokens': 50})
            if m.find("**ERROR**") >= 0:
                raise Exception(m)
        except Exception as e:
            msg += f"\nFail to access model({llm['llm_name']})." + str(e)
    elif llm["mdl_type"] == LLMType.RERANK:
        mdl = RerankModel[factory](
            key=llm["api_key"],
            model_name=llm["llm_name"],
            base_url=llm["api_base"]
        )
        try:
            arr, tc = mdl.similarity("Hello~ Ragflower!", ["Hi, there!"])
            if len(arr) == 0 or tc == 0:
                raise Exception("Not known.")
        except Exception as e:
            msg += f"\nFail to access model({llm['llm_name']})." + str(
                e)
    elif llm["mdl_type"] == LLMType.IMAGE2TEXT.value:
        mdl = CvModel[factory](
            key=llm["api_key"],
            model_name=llm["llm_name"],
            base_url=llm["api_base"]
        )
        try:
            img_url = (
                "https://upload.wikimedia.org/wikipedia/comm"
                "ons/thumb/d/dd/Gfp-wisconsin-madison-the-nature-boardwalk.jpg/256"
                "0px-Gfp-wisconsin-madison-the-nature-boardwalk.jpg"
            )
            res = requests.get(img_url)
            if res.status_code == 200:
                m, tc = mdl.describe(res.content)
                if not tc:
                    raise Exception(m)
            else:
                pass
        except Exception as e:
            msg += f"\nFail to access model({llm['llm_name']})." + str(e)
    elif llm["mdl_type"] == LLMType.TTS:
        mdl = TTSModel[factory](
            key=llm["api_key"], model_name=llm["llm_name"], base_url=llm["api_base"]
        )
        try:
            for resp in mdl.tts("Hello~ Multirager!"):
                pass
        except RuntimeError as e:
            msg += f"\nFail to access model({llm['llm_name']})." + str(e)
    else:
        # TODO: check other type of models
        pass

    if msg:
        raise HTTPException(status_code=400, detail=msg)

    try:
        if not TenantLLMService.filter_update(db, [TenantLLM.tenant_id == user.id, TenantLLM.llm_factory == factory,
                                                   TenantLLM.llm_name == llm["llm_name"]], llm):
            TenantLLMService.save(db, **llm)
    except IntegrityError as e:
        raise HTTPException(status_code=400, detail="LLM already exists for this tenant, factory, and name.")

    return get_json_result(data=True)


@router.post('/delete_llm', summary="删除模型", response_description="成功删除该模型")
async def delete_llm(request: DeleteLLMRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    此异步函数用于删除指定的语言模型（LLM）。
    摘要: 删除模型
    响应描述: 成功删除该模型

    参数:
    - request (DeleteLLMRequest): 一个依赖注入的请求对象，包含模型供应商和模型名称的信息。
    - db (Session): 依赖注入的数据库会话，用于执行数据库操作。
    - user: 依赖注入的当前用户信息，由manager提供。

    返回:
    - dict: 成功时返回一个表示操作成功的JSON结果。

    功能:
    1. 删除指定供应商和名称的模型。

    流程:
    1. 解析请求体，获取模型供应商和模型名称。
    2. 检查指定的模型是否存在。
    3. 从数据库中删除指定的模型。
    4. 返回操作成功的JSON结果。

    异常处理:
    - 如果在删除模型的过程中发生异常，将捕获异常并返回服务器错误响应。
    """
    try:
        req = request.model_dump()
        llm = TenantLLMService.query(db, tenant_id=user.id, llm_factory=req["llm_factory"], llm_name=req["llm_name"])
        if not llm:
            raise HTTPException(status_code=404, detail="LLM not found")

        TenantLLMService.filter_delete(db, [TenantLLM.tenant_id == user.id, TenantLLM.llm_factory == req["llm_factory"],
                                            TenantLLM.llm_name == req["llm_name"]])
        return get_json_result(data=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post('/delete_factory', summary="删除模型厂商", response_description="成功删除模型厂商")
def delete_factory(request: DeleteFactoryRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req = request.model_dump()
    TenantLLMService.filter_delete(
        db, [TenantLLM.tenant_id == user.id, TenantLLM.llm_factory == req["llm_factory"]])
    return get_json_result(data=True)


@router.get('/my_llms', summary="获取用户的所有模型", response_description="成功获取到用户的所有模型")
async def my_llms(db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取当前用户的所有模型信息。
    摘要: 获取用户的所有模型
    响应描述: 成功获取到用户的所有模型

    参数:
    - db (Session): 依赖注入的数据库会话，用于执行数据库查询。
    - user: 依赖注入的当前用户信息，由manager提供。

    返回:
    - dict: 包含用户所有模型信息的JSON结果，数据部分是一个字典，其中每个键代表一个模型工厂，值是该工厂下的模型信息列表。

    功能:
    1. 查询当前用户的所有模型信息。
    2. 按模型工厂分类整理模型信息。
    3. 将整理后的模型信息封装为JSON格式的字典并返回。

    流程:
    1. 使用TenantLLMService从数据库中获取当前用户的所有模型信息。
    2. 遍历获取的模型信息，按模型工厂分类整理模型信息。
    3. 将整理后的模型信息封装为JSON格式的字典并返回。

    异常处理:
    - 如果在执行数据库操作或数据处理过程中发生异常，将捕获异常并抛出HTTP异常，返回服务器错误响应。

    注意:
    - 用户的模型信息按模型工厂分类，每个工厂下包含多个模型信息。
    """
    try:
        res = {}
        for o in TenantLLMService.get_my_llms(db, user.id):
            if o.llm_factory not in res:
                res[o.llm_factory] = {
                    "tags": o.tags,
                    "llm": []
                }
            res[o.llm_factory]["llm"].append({
                "type": o.mdl_type,
                "name": o.llm_name,
                "used_token": o.used_tokens
            })
        return get_json_result(data=res)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get('/list', summary="列出所有模型", response_description="成功列出所有模型")
async def list_app(mdl_type: Optional[str] = None, db: Session = Depends(get_db), user=Depends(manager)):
    """
    列出所有模型，支持按模型类型筛选。
    摘要: 列出所有模型
    响应描述: 成功列出所有模型

    参数:
    - mdl_type (Optional[str]): 可选的模型类型，用于筛选模型。
    - db (Session): 依赖注入的数据库会话，用于执行数据库查询。
    - user: 依赖注入的当前用户信息，由manager提供。

    返回:
    - dict: 包含所有模型信息的JSON结果，数据部分是一个字典，其中每个键代表一个模型工厂，值是该工厂下的模型信息列表。

    功能:
    1. 查询所有有效的模型信息。
    2. 根据模型类型筛选模型信息。
    3. 按模型工厂分类整理模型信息。
    4. 将整理后的模型信息封装为JSON格式的字典并返回。

    流程:
    1. 使用TenantLLMService从数据库中获取当前用户的所有模型信息。
    2. 使用LLMService获取所有有效的模型信息。
    3. 遍历获取的模型信息，按模型工厂分类整理模型信息。
    4. 将整理后的模型信息封装为JSON格式的字典并返回。

    异常处理:
    - 如果在执行数据库操作或数据处理过程中发生异常，将捕获异常并抛出HTTP异常，返回服务器错误响应。

    注意:
    - 模型信息按模型工厂分类，每个工厂下包含多个模型信息。
    - 可选的模型类型参数用于筛选模型信息，只返回指定类型的模型。
    """
    self_deploied = ["Youdao", "FastEmbed", "BAAI", "Ollama", "Xinference", "LocalAI", "LM-Studio"]
    weighted = ["Youdao","FastEmbed", "BAAI"] if LIGHTEN != 0 else []
    try:
        objs = TenantLLMService.query(db, tenant_id=user.id)
        facts = set(o.llm_factory for o in objs if o.api_key)
        llms = LLMService.get_all(db)
        llms = [m.to_dict() for m in llms if m.status == StatusEnum.VALID.value and m.fid not in weighted]

        for m in llms:
            m["available"] = m["fid"] in facts or m["llm_name"].lower() == "flag-embedding" or m["fid"] in self_deploied

        llm_set = set([m["llm_name"]+"@"+m["fid"] for m in llms])
        for o in objs:
            if not o.api_key:
                continue
            if o.llm_name+"@"+o.llm_factory in llm_set:
                continue
            llms.append({"llm_name": o.llm_name, "mdl_type": o.mdl_type, "fid": o.llm_factory, "available": True})

        res = {}
        for m in llms:
            if mdl_type and m["mdl_type"].find(mdl_type) < 0:
                continue
            if m["fid"] not in res:
                res[m["fid"]] = []
            res[m["fid"]].append(m)

        return get_json_result(data=res)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post('/chat_service', summary="模型对话服务", response_description="成功调用对话模型")
async def chat_service(request: LLMServiceRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    模型对话服务的接口说明文档。

    概要：调用对话模型来生成响应。
    响应描述：成功调用对话模型并获得回复数据。

    参数：
    - request (LLMServiceRequest): 依赖注入的请求对象，包含模型参数、模型名称等信息。
    - db (Session): 依赖注入的数据库会话，用于执行数据库操作。
    - user：依赖注入的当前用户信息，由manager提供。

    返回：
    - dict: 返回包含模型对话结果的JSON结果。

    功能：
    1. 查询当前用户的租户信息，验证用户的租户身份。
    2. 获取对应的调用模型，包括返回话答。
    3. 使用chat_mdl对求问、消息和生成配置参数进行调用，并返回调用结果。

    流程：
    1. 用TenantService从数据库中获取当前用户的租户信息。
    2. 如果租户不存在，抛出HTTP异常。
    3. 获取LLMBundle来调用模型对话服务。
    4. 返回对话模型的响应数据。

    异常处理：
    - 如果在查询用户或调用模型的过程中发生异常，将抛出HTTP异常，并返回相应的错误信息。

    注意：
    - 当前用户的租户信息必须存在，才能调用对话模型。
    """
    req = request.model_dump()
    tenants = TenantService.get_by_user_id(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")
    chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, req["llm_name"])

    data =  chat_mdl.chat(req["prompt"], req["messages"],req["gen_conf"])

    return get_json_result(data=data)
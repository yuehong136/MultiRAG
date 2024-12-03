# coding=utf-8
"""
@project: multirag
@Author：龙
@file： llm_app.py
@date：2024/7/11 14:30
@desc:
"""
import logging
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from api.apps import manager
from api.db.database import get_db
from api.db.services.llm_service import LLMFactoriesService, TenantLLMService, LLMService, LLMBundle
from api.db.services.user_service import TenantService
from api import settings
from api.utils.api_utils import get_json_result, server_error_response, get_data_error_result
from api.db import StatusEnum, LLMType
from api.db.db_models import TenantLLM
from core.llm import EmbeddingModel, ChatModel, CvModel, RerankModel, TTSModel
from pydantic import BaseModel, Field
from typing import Any
import requests


class SetAPIKeyRequest(BaseModel):
    llm_factory: str
    api_key: str
    base_url: str | None = None


class AddLLMRequest(BaseModel):
    llm_factory: str
    llm_name: str
    mdl_type: str
    api_key: str = None
    api_base: str | None = None
    ark_api_key: str | None = None
    endpoint_id: str | None = None
    bedrock_ak: str | None = None
    bedrock_sk: str | None = None
    bedrock_region: str | None = None


class DeleteLLMRequest(BaseModel):
    llm_factory: str
    llm_name: str


class ListLLMRequest(BaseModel):
    mdl_type: str | None = None


class DeleteFactoryRequest(BaseModel):
    llm_factory: str | None = None


class LLMServiceRequest(BaseModel):
    prompt: str | None = None
    messages: list[dict]
    llm_name: str
    stream: bool = False
    gen_conf: dict[str, Any]
    image: str = ""


class FinePromptRequest(BaseModel):
    prompt: str
    llm_name: str
    gen_conf: dict[str, Any]


class SuggestionRequest(BaseModel):
    llm_name: str  # 模型名称
    last_response: str  # 模型的最后一轮回复
    messages: list[dict]  # 当前对话上下文
    gen_conf: dict[str, Any] = Field({}) # 大模型的配置信息
    num: int = Field(3)  # 返回建议的条数


router = APIRouter()


@router.get('/factories', summary="获取模型供应商信息", response_description="成功获取到所有模型供应商信息")
async def factories(db: Session = Depends(get_db), user=Depends(manager)):
    """
    此异步函数用于获取所有模型供应商的信息，排除特定供应商，并将结果以JSON格式返回。
    摘要: 获取模型供应商信息
    响应描述: 成功获取到所有模型供应商信息

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
            chat_passed = True
        elif not rerank_passed and llm.mdl_type == LLMType.RERANK:
            mdl = RerankModel[factory](req["api_key"], llm.llm_name, base_url=req.get("base_url"))
            try:
                arr, tc = mdl.similarity("What's the weather?", ["Is it sunny today?"])
                if len(arr) == 0 or tc == 0:
                    raise Exception("Fail")
                rerank_passed = True
                logging.debug(f'passed model rerank {llm.llm_name}')
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
            arr, tc = mdl.similarity("Hello~ Ragflower!", ["Hi, there!", "Ohh, my friend!"])
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
async def list_app(mdl_type: str | None = None, db: Session = Depends(get_db), user=Depends(manager)):
    """
    列出所有模型，支持按模型类型筛选。
    摘要: 列出所有模型
    响应描述: 成功列出所有模型

    参数:
    - mdl_type (Optional[str]): 可选的模型类型，用于筛选模型。

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
    weighted = ["Youdao", "FastEmbed", "BAAI"] if settings.LIGHTEN != 0 else []
    try:
        objs = TenantLLMService.query(db, tenant_id=user.id)
        facts = set(o.llm_factory for o in objs if o.api_key)
        llms = LLMService.get_all(db)
        llms = [m.to_dict() for m in llms if m.status == StatusEnum.VALID.value and m.fid not in weighted]

        for m in llms:
            m["available"] = m["fid"] in facts or m["llm_name"].lower() == "flag-embedding" or m["fid"] in self_deploied

        llm_set = set([m["llm_name"] + "@" + m["fid"] for m in llms])
        for o in objs:
            if not o.api_key:
                continue
            if o.llm_name + "@" + o.llm_factory in llm_set:
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
def chat_service(request: LLMServiceRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/v1/llm/chat_service` 模型对话服务

**功能描述**:
此接口用于调用对话模型，基于用户提供的输入生成对应的响应内容。支持文本生成、图像到文本转换、消息处理等多种模型类型，接口根据请求体中的配置，选择适当的模型及生成方式，提供流式和非流式响应模式。

---

### 请求体 (Request Body)

| 字段         | 类型                | 必填 | 描述                                                                                  |
|--------------|---------------------|------|---------------------------------------------------------------------------------------|
| `prompt`     | `string`           | 否   | 用户提供的提示内容，用于引导对话模型生成响应。                                        |
| `messages`   | `list[dict]`       | 是   | 对话消息列表，包含用户与模型之间的对话历史，格式为 `{ "role": "user/assistant", "content": "..." }`。|
| `llm_name`   | `string`           | 是   | 模型名称，用于指定所调用的语言模型。                                                 |
| `stream`     | `boolean`          | 是   | 指定是否使用流式响应，`true` 表示流式响应，`false` 表示非流式响应。                   |
| `gen_conf`   | `object`           | 否   | 生成配置，控制对话生成行为，例如温度值、生成长度等（具体配置视模型能力而定）。         |
| `image`      | `string (Base64)`  | 否   | Base64 编码的图像数据，适用于图像到文本的转换模型。                                   |

---

### 响应 (Response)

#### 成功响应 (200)

- **流式响应**:
    - **`Content-Type: text/event-stream`**
    - 数据按块流式返回，每条消息以 `data:` 开头，并以两个换行符 `\\n\\n` 结束。

    **示例**:

    ```plaintext
    data: {"retcode": 0, "retmsg": "", "data": "你好"}

    data: {"retcode": 0, "retmsg": "", "data": "你好👋！"}

    data: {"retcode": 0, "retmsg": "", "data": "你好👋！我是人工智能助手"}

    data: {"retcode": 0, "retmsg": "", "data": true}
    ```

- **非流式响应**:
    - **`Content-Type: application/json`**
    - **示例**:
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": "你好👋！我是人工智能助手，很高兴见到你！欢迎问我任何问题。"
    }
    ```

---

### 错误响应

#### **404: Tenant not found**
- **描述**: 当根据用户ID查找租户信息失败时，返回此错误。
- **示例**:
    ```json
    {
        "detail": "Tenant not found!"
    }
    ```

#### **404: Model not found**
- **描述**: 当指定的模型名称在用户租户可用模型列表中未找到时，返回此错误。
- **示例**:
    ```json
    {
        "detail": "Model glm-4-airx not found in the list."
    }
    ```

#### **500: 内部错误**
- **描述**: 当发生意外错误时，返回此错误。
- **示例**:
    - **流式响应错误**:
        ```plaintext
        data: {"retcode": 500, "retmsg": "Internal server error", "data": {"answer": "**ERROR**: Internal error"}}
        ```
    - **非流式响应错误**:
        ```json
        {
            "retcode": 500,
            "retmsg": "Internal server error",
            "data": {"answer": "**ERROR**: Internal error"}
        }
        ```

---

### 主要流程

1. 从请求中提取用户输入的内容、模型名称和配置信息。
2. 通过用户信息获取租户信息，确保用户的租户身份；如果未找到租户信息，返回404错误。
3. 获取用户租户关联的模型列表，确定模型类型 (`llm_type`)。
4. 根据 `llm_type` 判断是否需要传入 `image` 参数，构建生成请求。
5. 根据 `stream` 参数选择流式或非流式的生成方法，调用模型获取对话响应内容。
6. 返回生成的对话结果。

---

### 注意事项

- **模型选择**:
    - 仅当 `llm_type` 为 `image2text` 时传递 `image` 参数，以确保在需要图像到文本转换时能处理Base64编码的图像数据。
    - 支持多种模型类型 (如文本生成、图像到文本、消息对话)，请根据需求选择适当的 `llm_name` 和 `llm_type`。
- **流式调用**:
    - 若 `stream` 参数为 `True`，将返回流式响应，用于实时数据生成；若为 `False`，返回完整的响应数据。
- **数据格式**:
    - 返回数据格式可能因模型及请求内容不同而有所变化；默认返回JSON格式的结构化数据或文本响应。
- **流式响应结束标记**:
    - 流式响应的最后一条消息为:
      ```plaintext
      data: {"retcode": 0, "retmsg": "", "data": true}
      ```

    """
    req = request.model_dump()
    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")

    my_llms = TenantLLMService.get_my_llms(db, tenants[0]["tenant_id"])

    def get_llm_type(model_name, my_llms):
        for row in my_llms:
            if row[4] == model_name:  # 这里的第5个元素是 model_name
                return row[-3]  # 倒数第三个元素是 llm_type
        return None  # 如果找不到，返回 None

    llm_type = get_llm_type(req["llm_name"], my_llms)
    if llm_type:
        logging.debug(f"The llm_type for model {req['llm_name']} is: {llm_type}")
    else:
        raise HTTPException(status_code=404, detail=f"Model {req['llm_name']} not found in the list.")

    chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], llm_type, req["llm_name"])
    # 构建调用参数
    call_params = {
        "system": req["prompt"],
        "history": req["messages"],
        "gen_conf": req["gen_conf"]
    }

    # 如果llm_type为image2text，添加image参数
    if llm_type == 'image2text':
        call_params["image"] = req["image"]

    async def stream_response():
        """
        Stream SSE response to the client.
        """
        try:
            last_ans = ""  # 初始化累加变量
            for ans in chat_mdl.chat_streamly(**call_params):
                delta_ans = ans[len(last_ans):]  # 计算增量
                if not delta_ans:  # 如果没有新内容，跳过
                    continue
                last_ans = ans  # 更新累加内容
                sse_data = json.dumps({"retcode": 0, "retmsg": "", "data": last_ans}, ensure_ascii=False)
                yield f"data: {sse_data}\n\n"  # SSE 格式：data: 数据\n\n
        except Exception as e:
            error_message = json.dumps({"retcode": 500, "retmsg": str(e), "data": {"answer": f"**ERROR**: {str(e)}"}},
                                       ensure_ascii=False)
            yield f"data: {error_message}\n\n"
        finally:
            # 流结束标记
            end_message = json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False)
            yield f"data: {end_message}\n\n"

    # 根据是否流式调用选择合适的方法
    if req["stream"]:
        # data = chat_mdl.chat_streamly(**call_params)
        return StreamingResponse(stream_response(), media_type="text/event-stream")
    else:
        data = chat_mdl.chat(**call_params)

        return get_json_result(data=data)


@router.post('/fine_prompt', summary="优化提示词", response_description="返回优化后的提示词")
def fine_prompt(request: FinePromptRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    **功能描述**:
    此接口用于根据用户输入的任务描述或现有提示词，优化生成详细的系统提示词，以便更好地引导语言模型完成任务。该接口结合预定义的优化提示模板 (META_PROMPT)，确保模型输出的提示词更加清晰、具体，并且能合理规划任务的完成步骤。

    ### 请求体 (Request Body):
    - **model_dump (dict)**: 包含以下字段：
        - `prompt` (str): 用户提供的任务描述或现有提示词，将根据此内容进行优化。
        - `llm_name` (str): 大语言模型名称，用于选择特定的模型来生成优化的提示词。
        - `gen_conf` (dict, 可选): 生成配置，控制提示词生成的行为。

    ### 响应 (Response):
    - **成功响应 (200)**:
        - `data` (dict): 返回包含优化后的提示词。格式可能包括简单的字符串或结构化的JSON，具体取决于模型输出的要求。

    ### 错误响应:
    - **404: Tenant not found**:
        - 当根据用户ID查找租户信息失败时，返回此错误。

    ### 主要流程:
    1. 从请求中提取用户输入的提示词及相关配置信息。
    2. 通过用户信息检索相关租户信息。如果租户信息未找到，抛出404错误。
    3. 根据预定义的META_PROMPT，结合用户输入的任务描述，使用指定的LLM模型生成优化后的系统提示词。
    4. 将生成的提示词返回给用户，格式可能为JSON或简单文本。

    ### 注意事项:
    - **优化策略**:
        - 系统优先保留用户提供的内容，对于简单提示词进行微调，对于复杂提示词则在不改变原始结构的前提下增强清晰度。
        - 系统会根据内容需要添加示例和步骤，确保输出提示词具有高可读性和清晰性。
    - **常量与格式**:
        - 提示词中应包括不易受到提示注入影响的常量，例如规则、评分标准等。
        - 对于任务输出明确的结构化数据(如JSON)会更倾向于返回JSON格式，但不会使用代码块包装（除非明确要求）。
    """
    req = request.model_dump()
    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")
    META_PROMPT = """
    Given a task description or existing prompt, produce a detailed system prompt to guide a language model in completing the task effectively.

    # Guidelines

    - Understand the Task: Grasp the main objective, goals, requirements, constraints, and expected output.
    - Minimal Changes: If an existing prompt is provided, improve it only if it's simple. For complex prompts, enhance clarity and add missing elements without altering the original structure.
    - Reasoning Before Conclusions**: Encourage reasoning steps before any conclusions are reached. ATTENTION! If the user provides examples where the reasoning happens afterward, REVERSE the order! NEVER START EXAMPLES WITH CONCLUSIONS!
        - Reasoning Order: Call out reasoning portions of the prompt and conclusion parts (specific fields by name). For each, determine the ORDER in which this is done, and whether it needs to be reversed.
        - Conclusion, classifications, or results should ALWAYS appear last.
    - Examples: Include high-quality examples if helpful, using placeholders [in brackets] for complex elements.
       - What kinds of examples may need to be included, how many, and whether they are complex enough to benefit from placeholders.
    - Clarity and Conciseness: Use clear, specific language. Avoid unnecessary instructions or bland statements.
    - Formatting: Use markdown features for readability. DO NOT USE ``` CODE BLOCKS UNLESS SPECIFICALLY REQUESTED.
    - Preserve User Content: If the input task or prompt includes extensive guidelines or examples, preserve them entirely, or as closely as possible. If they are vague, consider breaking down into sub-steps. Keep any details, guidelines, examples, variables, or placeholders provided by the user.
    - Constants: DO include constants in the prompt, as they are not susceptible to prompt injection. Such as guides, rubrics, and examples.
    - Output Format: Explicitly the most appropriate output format, in detail. This should include length and syntax (e.g. short sentence, paragraph, JSON, etc.)
        - For tasks outputting well-defined or structured data (classification, JSON, etc.) bias toward outputting a JSON.
        - JSON should never be wrapped in code blocks (```) unless explicitly requested.

    The final prompt you output should adhere to the following structure below. Do not include any additional commentary, only output the completed system prompt. SPECIFICALLY, do not include any additional messages at the start or end of the prompt. (e.g. no "---")

    [Concise instruction describing the task - this should be the first line in the prompt, no section header]

    [Additional details as needed.]

    [Optional sections with headings or bullet points for detailed steps.]

    # Steps [optional]

    [optional: a detailed breakdown of the steps necessary to accomplish the task]

    # Output Format

    [Specifically call out how the output should be formatted, be it response length, structure e.g. JSON, markdown, etc]

    # Examples [optional]

    [Optional: 1-3 well-defined examples with placeholders if necessary. Clearly mark where examples start and end, and what the input and output are. User placeholders as necessary.]
    [If the examples are shorter than what a realistic example is expected to be, make a reference with () explaining how real examples should be longer / shorter / different. AND USE PLACEHOLDERS! ]

    # Notes [optional]

    [optional: edge cases, details, and an area to call or repeat out specific important considerations]
    """.strip()
    chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, req["llm_name"])

    data = chat_mdl.chat(META_PROMPT, [{"role": "user", "content": "Task, Goal, or Current Prompt:\n" + req["prompt"]}],
                         req["gen_conf"])

    return get_json_result(data=data)


@router.post('/generate_suggestions', summary="生成用户输入建议", response_description="成功调用大模型生成建议")
def generate_suggestions(request: SuggestionRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    ### POST `/v1/suggestions/generate_suggestions` 生成用户输入建议

**功能描述**
此接口用于调用大模型根据当前对话上下文及智能体的配置信息，生成用户下一轮输入建议。生成的建议应紧密相关、多样性高、避免重复，并与用户的角色匹配。

---

## 请求体 (Request Body)

| 字段            | 类型            | 必填 | 描述                                                                                     |
|-----------------|-----------------|------|------------------------------------------------------------------------------------------|
| `llm_name`      | `string`       | 是   | 模型名称，指定用于生成建议的大模型。                                                    |
| `last_response` | `string`       | 是   | 模型在对话中的最后一轮回复内容。                                                        |
| `messages`      | `list[dict]`   | 是   | 对话消息列表，包括用户与模型之间的上下文历史，格式为 `{ "role": "user/assistant", "content": "..." }`。 |
| `gen_conf`      | `object`       | 否   | 大模型的生成配置，用于控制生成行为，例如温度值、生成长度等。                              |
| `num`           | `integer`      | 否   | 返回的建议条数，默认为3。                                                               |

---

## 响应 (Response)

### 成功响应 (200)

- **`Content-Type: application/json`**
- **示例**:
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": [
            "建议 1",
            "建议 2",
            "建议 3"
        ]
    }
    ```

---

## 错误响应

### 404: Tenant not found
- **描述**
  当根据用户ID查找租户信息失败时，返回此错误。
- **示例**
    ```json
    {
        "detail": "Tenant not found!"
    }
    ```

### 500: JSON 解析错误
- **描述**
  模型返回数据无法解析为有效的JSON格式。
- **示例**
    ```json
    {
        "detail": "模型返回数据无法解析为 JSON: Expecting value: line 1 column 1 (char 0)"
    }
    ```

### 500: 无效的建议列表
- **描述**
  模型返回的建议列表为空或格式不正确。
- **示例**
    ```json
    {
        "detail": "模型未返回有效的建议列表"
    }
    ```

### 500: 解析模型返回数据时发生未知错误
- **描述**
  发生未知错误导致无法解析模型返回的数据。
- **示例**
    ```json
    {
        "detail": "解析模型返回数据时发生未知错误: list index out of range"
    }
    ```

---

## 主要流程

1. 从请求中提取 `llm_name`、`last_response`、`messages`、`gen_conf` 和 `num` 等字段。
2. 获取用户对应的租户信息；如果未找到，返回404错误。
3. 构造 `system_prompt`，用于明确生成建议的任务及格式。
4. 调用大模型生成建议，并解析模型返回的 JSON 数据。
5. 校验建议列表的有效性，确保返回符合要求的结果。
6. 返回生成的建议列表。

---

## 注意事项

- **紧密相关性**
  建议内容必须与最后一轮模型回复和对话上下文相关，避免偏离主题。

- **多样性**
  确保生成的建议从不同方向或角度切入，不重复也不雷同。

- **角色适配性**
  根据用户的身份和对话场景定制建议内容，确保更具针对性。

- **JSON 格式输出**
  大模型返回数据必须是纯 JSON 格式，不应包含多余标记或格式化代码块。

- **异常处理**
  当模型返回数据无法解析或格式不符合预期时，记录日志并返回错误响应。

    """
    req = request.model_dump()

    system_prompt = f"""
    你是一个智能对话助手，当前任务是根据用户对话上下文和智能体的配置信息，为用户生成 {req["num"]} 条下一轮输入建议。生成的建议需满足以下要求：

    1. **紧密相关**：建议内容应与最后一轮的模型回复紧密相关。
    2. **避免重复**：建议的输入内容不能与上下文中用户已提问或模型已回答的内容重复。
    3. **角色匹配**：建议内容应与用户当前的角色及对话类型匹配。例如，如果用户是技术开发人员，建议可以是具体的技术问题；如果是普通用户，则建议应更加简洁和实用。
    4. **多样性**：生成的 {req["num"]} 条建议应具有一定的多样性，涵盖不同的方向或角度。

    ### 输入格式

    以下是输入数据的格式：
    - 最后一轮模型回复（`last_response`）：{json.dumps(req["last_response"], ensure_ascii=False)}
    - 对话上下文（`messages`）：{json.dumps(req["messages"], ensure_ascii=False)}

    ### 输出格式

    请仅输出以下格式的纯 JSON 数据，不要添加任何其他标记：
    ```json
    {{
        "suggestions": [
            "建议 1",
            "建议 2",
            "建议 3"
        ]
    }}
    """
    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")

    chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], LLMType.CHAT, req["llm_name"])

    # 调用大模型
    response = chat_mdl.chat(
        system=system_prompt,
        history=[{"role": "user", "content": "请按照要求输出"}],
        gen_conf=req["gen_conf"].get("gen_conf", {})
    )

    try:
        # 检查模型返回数据
        logging.info("模型返回原始数据: %s", response)

        if response.startswith("```json"):
            logging.warning("检测到带格式的代码块标记，正在移除")
            response = response.replace("```json", "").replace("```", "").strip()
        elif response.startswith("```"):
            logging.warning("检测到代码块标记，正在移除")
            response = response.strip("```").strip()

        # 解析 JSON
        response_data = json.loads(response)
        suggestions = response_data.get("suggestions", [])
    except json.JSONDecodeError as e:
        logging.error("JSON 解析错误: %s", str(e))
        logging.debug("模型返回数据: %s", response)
        raise HTTPException(
            status_code=500,
            detail=f"模型返回数据无法解析为 JSON: {str(e)}"
        )
    except Exception as e:
        logging.error("解析模型返回数据时发生未知错误: %s", str(e))
        raise HTTPException(
            status_code=500,
            detail=f"解析模型返回数据时发生未知错误: {str(e)}"
        )

    # 确保 suggestions 是一个非空列表
    if not suggestions or not isinstance(suggestions, list):
        logging.error("模型返回无效建议列表: %s", response_data)
        raise HTTPException(status_code=500, detail="模型未返回有效的建议列表")

    logging.info("生成的用户输入建议: %s", suggestions)

    return get_json_result(data=suggestions)
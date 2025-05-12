# coding=utf-8
"""
@project: multirag
@Author：龙
@file： llm_app.py
@date：2024/7/11 14:30
@desc:
"""
import asyncio
import logging
import json
import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from api.apps import manager, executor
# from api.db.database import get_db
from api.db.services.llm_service import LLMFactoriesService, TenantLLMService, LLMService, LLMBundle
from api.db.services.user_service import TenantService
from api import settings
from api.utils.api_utils import get_json_result, server_error_response, get_data_error_result
from api.db import StatusEnum, LLMType
from api.db.db_models import TenantLLM, get_db
from api.utils.file_utils import get_project_base_directory
from core.llm import EmbeddingModel, ChatModel, CvModel, RerankModel, TTSModel
from pydantic import BaseModel, Field
from typing import Any

from core.prompts import kb_prompt
from core.utils.tavily_conn import Tavily


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
    fish_audio_ak: str | None = None
    fish_audio_refid: str | None = None
    hunyuan_sid: str | None = None
    hunyuan_sk: str | None = None
    tencent_cloud_sid: str | None = None
    tencent_cloud_sk: str | None = None
    spark_api_password: str | None = None
    spark_app_id: str | None = None
    spark_api_secret: str | None = None
    spark_api_key: str | None = None
    google_project_id: str | None = None
    google_region: str | None = None
    google_service_account_key: str | None = None


class DeleteLLMRequest(BaseModel):
    llm_factory: str
    llm_name: str


class ListLLMRequest(BaseModel):
    mdl_type: str | None = None


class DeleteFactoryRequest(BaseModel):
    llm_factory: str | None = None


class LLMServiceRequest(BaseModel):
    prompt: str = ""
    messages: list[dict]
    llm_name: str
    stream: bool = False
    gen_conf: dict[str, Any]
    image: str = ""
    tavily_api_key: str = ""


class FinePromptRequest(BaseModel):
    prompt: str
    llm_name: str
    gen_conf: dict[str, Any]


class SuggestionRequest(BaseModel):
    llm_name: str  # 模型名称
    last_response: str  # 模型的最后一轮回复
    messages: list[dict]  # 当前对话上下文
    gen_conf: dict[str, Any] = None # 大模型的配置信息
    num: int = Field(3)  # 返回建议的条数


router = APIRouter()


@router.get('/factories', summary="获取模型供应商信息", response_description="成功获取到所有模型供应商信息")
def factories(db: Session = Depends(get_db), user=Depends(manager)):
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
def set_api_key(request: SetAPIKeyRequest, db: Session = Depends(get_db), user=Depends(manager)):
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
        if not embd_passed and llm.mdl_type == LLMType.EMBEDDING.value:
            assert factory in EmbeddingModel, f"Embedding model from {factory} is not supported yet."
            mdl = EmbeddingModel[factory](req["api_key"], llm.llm_name, base_url=req.get("base_url"))
            try:
                arr, tc = mdl.encode(["Test if the api key is available"])
                if len(arr[0]) == 0:
                    raise Exception("Fail")
                embd_passed = True
            except Exception as e:
                msg += f"\nFail to access embedding model({llm.llm_name}) using this api key." + str(e)
        elif not chat_passed and llm.mdl_type == LLMType.CHAT.value:
            assert factory in ChatModel, f"Chat model from {factory} is not supported yet."
            mdl = ChatModel[factory](req["api_key"], llm.llm_name, base_url=req.get("base_url"))
            try:
                m, tc = mdl.chat("", [{"role": "user", "content": "Hello! How are you doing!"}],
                                 {"temperature": 0.9, 'max_tokens': 50})
                print(m)
                if m.find("**ERROR**") >= 0:
                    raise Exception(m)
            except Exception as e:
                msg += f"\nFail to access model({llm.llm_name}) using this api key." + str(e)
            chat_passed = True
        elif not rerank_passed and llm.mdl_type == LLMType.RERANK:
            assert factory in RerankModel, f"Re-rank model from {factory} is not supported yet."
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

    for llm in LLMService.query(db, fid=factory):
        llm_config["max_tokens"] = llm.max_tokens
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
                api_base=llm_config["api_base"],
                max_tokens=llm_config["max_tokens"]
            )

    return get_json_result(data=True)


@router.post('/add_llm', summary="新增模型", response_description="成功新增该模型")
def add_llm(request: AddLLMRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
# POST /add_llm

## 接口描述
用于向系统中添加一个新的大语言模型(LLM)，并验证相关连接是否有效。

## 请求方式
POST

## 接口权限
需要用户认证

## 请求参数
| 参数名称 | 类型 | 必填 | 描述 |
|----------|------|------|------|
| llm_factory | string | 是 | 模型提供商/工厂，例如: "OpenAI", "Azure-OpenAI", "Bedrock", "VolcEngine", "Tencent Hunyuan", "Tencent Cloud", "LocalAI", "HuggingFace", "OpenAI-API-Compatible", "XunFei Spark", "Fish Audio", "Google Cloud" 等 |
| llm_name | string | 是 | 模型名称 |
| mdl_type | string | 是 | 模型类型，可以是 "chat" (聊天), "embedding" (嵌入), "rerank" (重排序), "image2text" (图像转文本), "tts" (文本转语音) |
| api_key | string | 否 | API密钥，根据不同的工厂可能需要或者不需要 |
| api_base | string | 否 | API基础URL地址 |
| ark_api_key | string | 否 | VolcEngine专用: ARK API密钥 |
| endpoint_id | string | 否 | VolcEngine专用: 终端节点ID |
| bedrock_ak | string | 否 | AWS Bedrock专用: Access Key |
| bedrock_sk | string | 否 | AWS Bedrock专用: Secret Key |
| bedrock_region | string | 否 | AWS Bedrock专用: 区域名称 |
| fish_audio_ak | string | 否 | Fish Audio专用: Access Key |
| fish_audio_refid | string | 否 | Fish Audio专用: 参考ID |
| hunyuan_sid | string | 否 | 腾讯混元专用: SID |
| hunyuan_sk | string | 否 | 腾讯混元专用: Secret Key |
| tencent_cloud_sid | string | 否 | 腾讯云专用: SID |
| tencent_cloud_sk | string | 否 | 腾讯云专用: Secret Key |
| spark_api_password | string | 否 | 讯飞星火专用: API密码 (用于chat模型) |
| spark_app_id | string | 否 | 讯飞星火专用: 应用ID (用于tts模型) |
| spark_api_secret | string | 否 | 讯飞星火专用: API Secret (用于tts模型) |
| spark_api_key | string | 否 | 讯飞星火专用: API Key (用于tts模型) |
| google_project_id | string | 否 | Google Cloud专用: 项目ID |
| google_region | string | 否 | Google Cloud专用: 区域 |
| google_service_account_key | string | 否 | Google Cloud专用: 服务账号密钥 |

## 响应参数
| 参数名称 | 类型 | 描述 |
|----------|------|------|
| success | boolean | 操作是否成功 |
| data | boolean | 返回true表示添加成功 |
| message | string | 当操作失败时，返回错误信息 |

## 特性
1. 根据不同的模型提供商，自动组装API密钥格式
2. 添加模型前，会进行连接测试，确保API密钥和URL有效
3. 对于不同类型的模型，执行不同的有效性验证:
   - 对于embedding模型: 验证能否成功编码测试文本
   - 对于chat模型: 验证能否成功生成回复
   - 对于rerank模型: 验证能否成功计算相似度
   - 对于image2text模型: 验证能否成功描述图像
   - 对于tts模型: 验证能否成功生成语音

## 错误码
| 错误码 | 描述 |
|--------|------|
| 400 | 请求参数错误或模型已存在或模型验证失败 |
| 401 | 用户未认证 |

## 示例

### 请求示例 (添加OpenAI模型)
```json
{
  "llm_factory": "OpenAI",
  "llm_name": "gpt-4",
  "mdl_type": "chat",
  "api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "api_base": "https://api.openai.com/v1"
}
```

### 响应示例 (成功)
```json
{
  "success": true,
  "data": true,
  "message": ""
}
```

### 响应示例 (失败)
```json
{
  "success": false,
  "data": null,
  "message": "Fail to access model(gpt-4). Invalid API key."
}
```

## 注意事项
1. 不同的模型提供商需要不同的API密钥格式，请确保提供正确的参数组合
2. 添加前，系统会验证模型连接，如果验证失败将返回400错误
3. 对于同一个租户，相同工厂和名称的模型不能重复添加
4. 对于某些特殊的模型工厂，系统会自动在模型名称后添加后缀，例如LocalAI会添加"___LocalAI"
    """
    req = request.model_dump()
    factory = req["llm_factory"]
    api_key = req.get("api_key", "x")
    llm_name = req["llm_name"]
    def apikey_json(keys):
        nonlocal req
        return json.dumps({k: req.get(k, "") for k in keys})

    if factory == "VolcEngine":
        # For VolcEngine, due to its special authentication method
        # Assemble ark_api_key endpoint_id into api_key
        api_key = apikey_json(["ark_api_key", "endpoint_id"])

    elif factory == "Tencent Hunyuan":
        req["api_key"] = apikey_json(["hunyuan_sid", "hunyuan_sk"])
        return set_api_key(SetAPIKeyRequest(**req), db, user)

    elif factory == "Tencent Cloud":
        req["api_key"] = apikey_json(["tencent_cloud_sid", "tencent_cloud_sk"])
        return set_api_key(SetAPIKeyRequest(**req), db, user)

    elif factory == "Bedrock":
        api_key = apikey_json(["bedrock_ak", "bedrock_sk", "bedrock_region"])

    elif factory == "LocalAI":
        llm_name += "___LocalAI"

    elif factory == "HuggingFace":
        llm_name += "___HuggingFace"

    elif factory == "OpenAI-API-Compatible":
        llm_name += "___OpenAI-API"

    elif factory == "VLLM":
        llm_name += "___VLLM"

    elif factory == "XunFei Spark":
        if req["mdl_type"] == "chat":
            api_key = req.get("spark_api_password", "")
        elif req["mdl_type"] == "tts":
            api_key = apikey_json(["spark_app_id", "spark_api_secret", "spark_api_key"])

    elif factory == "Fish Audio":
        api_key = apikey_json(["fish_audio_ak", "fish_audio_refid"])

    elif factory == "Google Cloud":
        api_key = apikey_json(["google_project_id", "google_region", "google_service_account_key"])

    elif factory == "Azure-OpenAI":
        api_key = apikey_json(["api_key", "api_version"])

    llm = {
        "tenant_id": user.id,
        "llm_factory": factory,
        "mdl_type": req["mdl_type"],
        "llm_name": llm_name,
        "api_base": req.get("api_base", ""),
        "api_key": api_key
    }

    msg = ""
    mdl_nm = llm["llm_name"].split("___")[0]
    if llm["mdl_type"] == LLMType.EMBEDDING.value:
        assert factory in EmbeddingModel, f"Embedding model from {factory} is not supported yet."
        mdl = EmbeddingModel[factory](
            key=llm['api_key'],
            model_name=mdl_nm,
            base_url=llm["api_base"])
        try:
            arr, tc = mdl.encode(["Test if the api key is available"])
            if len(arr[0]) == 0:
                raise Exception("Fail")
        except Exception as e:
            msg += f"\nFail to access embedding model({mdl_nm})." + str(e)
    elif llm["mdl_type"] == LLMType.CHAT.value:
        assert factory in ChatModel, f"Chat model from {factory} is not supported yet."
        mdl = ChatModel[factory](
            key=llm['api_key'],
            model_name=mdl_nm,
            base_url=llm["api_base"]
        )
        try:
            m, tc = mdl.chat("", [{"role": "user", "content": "Hello! How are you doing!"}],
                             {"temperature": 0.9, 'max_tokens': 500})
            if not tc and m.find("**ERROR**:") >= 0:
                raise Exception(m)
        except Exception as e:
            msg += f"\nFail to access model({mdl_nm})." + str(e)
    elif llm["mdl_type"] == LLMType.RERANK:
        assert factory in RerankModel, f"RE-rank model from {factory} is not supported yet."
        try:
            mdl = RerankModel[factory](
                key=llm["api_key"],
                model_name=mdl_nm,
                base_url=llm["api_base"]
            )
            arr, tc = mdl.similarity("Hello~ Multirager!", ["Hi, there!", "Ohh, my friend!"])
            if len(arr) == 0:
                raise Exception("Not known.")
        except KeyError:
            msg += f"{factory} dose not support this model({mdl_nm})"
        except Exception as e:
            msg += f"\nFail to access model({mdl_nm})." + str(
                e)
    elif llm["mdl_type"] == LLMType.IMAGE2TEXT.value:
        assert factory in CvModel, f"Image to text model from {factory} is not supported yet."
        mdl = CvModel[factory](
            key=llm["api_key"],
            model_name=mdl_nm,
            base_url=llm["api_base"]
        )
        try:
            with open(os.path.join(get_project_base_directory(), "assets/imgs/logo.png"), "rb") as f:
                m, tc = mdl.describe(f.read())
                if not m and not tc:
                    raise Exception(m)
        except Exception as e:
            msg += f"\nFail to access model({llm['llm_name']})." + str(e)
    elif llm["mdl_type"] == LLMType.TTS:
        assert factory in TTSModel, f"TTS model from {factory} is not supported yet."
        mdl = TTSModel[factory](
            key=llm["api_key"], model_name=mdl_nm, base_url=llm["api_base"]
        )
        try:
            for resp in mdl.tts("Hello~ Multirager!"):
                pass
        except RuntimeError as e:
            msg += f"\nFail to access model({mdl_nm})." + str(e)
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
def delete_llm(request: DeleteLLMRequest, db: Session = Depends(get_db), user=Depends(manager)):
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
def my_llms(db: Session = Depends(get_db), user=Depends(manager)):
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
def list_app(mdl_type: str | None = None, db: Session = Depends(get_db), user=Depends(manager)):
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
    self_deployed = ["Youdao", "FastEmbed", "BAAI", "Ollama", "Xinference", "LocalAI", "LM-Studio", "GPUStack"]
    weighted = ["Youdao", "FastEmbed", "BAAI"] if settings.LIGHTEN != 0 else []
    try:
        objs = TenantLLMService.query(db, tenant_id=user.id)
        facts = set(o.llm_factory for o in objs if o.api_key)
        llms = LLMService.get_all(db)
        llms = [m.to_dict() for m in llms if m.status == StatusEnum.VALID.value and m.fid not in weighted]

        for m in llms:
            m["available"] = m["fid"] in facts or m["llm_name"].lower() == "flag-embedding" or m["fid"] in self_deployed

        llm_set = set([m["llm_name"] + "@" + m["fid"] for m in llms])
        for o in objs:
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
    **功能描述**:
    此接口用于调用对话模型，基于用户提供的输入生成对应的响应内容。支持文本生成、图像到文本转换、消息处理等多种模型类型。接口根据请求体中的配置，选择适当的模型及生成方式，提供流式和非流式响应模式。

    ### 请求体 (Request Body):
    - **model_dump (dict)**: 包含以下字段：
        - `prompt` (str, 可选): 用户提供的提示内容，用于引导对话模型生成响应。
        - `messages` (list[dict]): 对话消息列表，包含用户与模型之间的对话历史。
        - `llm_name` (str): 模型名称，用于指定所调用的语言模型。
        - `stream` (bool): 指定是否使用流式响应。
        - `gen_conf` (dict, 可选): 生成配置，控制对话生成行为。
        - `image` (str, 可选): Base64编码的图像数据，适用于图像到文本的转换模型。

    ### 响应 (Response):
    - **成功响应 (200)**:
        - `data` (dict): 返回包含模型生成的响应内容，格式可能包括文本、结构化数据或基于图像的文本输出，具体取决于模型类型和请求内容。

    ### 错误响应:
    - **404: Tenant not found**:
        - 当根据用户ID查找租户信息失败时，返回此错误，表示该用户无对应的租户记录。
    - **404: Model not found**:
        - 当指定的模型名称在用户租户可用模型列表中未找到时，返回此错误。

    ### 主要流程:
    1. 从请求中提取用户输入的内容、模型名称和配置信息。
    2. 通过用户信息获取租户信息，确保用户的租户身份；如果未找到租户信息，返回404错误。
    3. 获取用户租户关联的模型列表，确定模型类型 (`llm_type`)。
    4. 根据 `llm_type` 判断是否需要传入 `image` 参数，构建生成请求。
    5. 根据 `stream` 参数选择流式或非流式的生成方法，调用模型获取对话响应内容。
    6. 返回生成的对话结果。

    ### 注意事项:
    - **模型选择**:
        - 仅当 `llm_type` 为 `image2text` 时传递 `image` 参数，以确保在需要图像到文本转换时能处理Base64编码的图像数据。
        - 支持多种模型类型 (如文本生成、图像到文本、消息对话)，请根据需求选择适当的 `llm_name` 和 `llm_type`。
    - **流式调用**:
        - 若 `stream` 参数为 `True`，将返回流式响应，用于实时数据生成；若为 `False`，返回完整的响应数据。
    - **数据格式**:
        - 返回数据格式可能因模型及请求内容不同而有所变化；默认返回JSON格式的结构化数据或文本响应。
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

    # 根据是否流式调用选择合适的方法
    if req["stream"]:
        data = chat_mdl.chat_streamly(**call_params)
    else:
        data = chat_mdl.chat(**call_params)

    return get_json_result(data=data)

@router.post('/chat_service_sse', summary="模型对话服务", response_description="成功调用对话模型")
async def chat_service_sse(request: LLMServiceRequest, db: Session = Depends(get_db), user=Depends(manager)):
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

    # 将同步操作封装在函数中
    def process_non_streaming_request():
        # 获取租户信息
        tenants = TenantService.get_info_by(db, user.id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        my_llms = TenantLLMService.get_my_llms(db, tenants[0]["tenant_id"])

        def get_llm_type(model_name, my_llms):
            for row in my_llms:
                if row[4] == model_name:
                    return row[-3]
            return None

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

        # 非流式调用，直接返回完整响应
        data = chat_mdl.chat(**call_params)
        return data

    # 获取初始设置的同步函数
    def get_initial_setup():
        tenants = TenantService.get_info_by(db, user.id)
        if not tenants:
            raise HTTPException(status_code=404, detail="Tenant not found!")

        my_llms = TenantLLMService.get_my_llms(db, tenants[0]["tenant_id"])

        def get_llm_type(model_name, my_llms):
            for row in my_llms:
                if row[4] == model_name:
                    return row[-3]
            return None

        llm_type = get_llm_type(req["llm_name"], my_llms)
        if not llm_type:
            raise HTTPException(status_code=404, detail=f"Model {req['llm_name']} not found in the list.")

        chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], llm_type, req["llm_name"])

        # 构建调用参数
        call_params = {
            "system": req["prompt"],
            "history": req["messages"],
            "gen_conf": req["gen_conf"]
        }

        # # 如果llm_type为image2text，添加image参数
        # if llm_type == 'image2text':
        #     call_params["image"] = req["image"]

        if llm_type == "image2text":
            llm_model_config = TenantLLMService.get_model_config(db, tenants[0]["tenant_id"], LLMType.IMAGE2TEXT,
                                                                 req["llm_name"])
            call_params["image"] = req["image"]
        else:
            llm_model_config = TenantLLMService.get_model_config(db, tenants[0]["tenant_id"], LLMType.CHAT,
                                                                 req["llm_name"])

        max_tokens = llm_model_config.get("max_tokens", 8192)
        kbinfos = {"total": 0, "chunks": [], "doc_aggs": []}
        questions = [m["content"] for m in req["messages"] if m["role"] == "user"]
        if req["tavily_api_key"]:
            tav = Tavily(req["tavily_api_key"])
            tav_res = tav.retrieve_chunks(" ".join(questions))
            kbinfos["chunks"].extend(tav_res["chunks"])
            kbinfos["doc_aggs"].extend(tav_res["doc_aggs"])
            kbinfos["total"] = len(kbinfos["chunks"])
        knowledges = kb_prompt(kbinfos, max_tokens)
        knowledges = "\n------\n" + "\n\n------\n\n".join(knowledges)
        call_params["system"] = "\n------\n" + call_params["system"] + knowledges
        return chat_mdl, call_params

    # 处理流式响应
    async def stream_response():
        try:
            # 在线程池中获取初始设置
            loop = asyncio.get_running_loop()
            chat_mdl, call_params = await loop.run_in_executor(executor, get_initial_setup)

            # 在线程池中执行流式生成并迭代结果
            def generate_stream():
                try:
                    # 调用模型的流式生成方法
                    result_generator = chat_mdl.chat_streamly(**call_params)
                    # 返回生成器对象
                    return result_generator
                except Exception as e:
                    # 捕获异常并返回
                    raise e

            # 获取生成器
            generator = await loop.run_in_executor(executor, generate_stream)

            # 保持与原代码相同的累加逻辑
            last_ans = ""  # 初始化累加变量

            # 使用线程池处理每次迭代，但保持原始累加逻辑
            def get_next_chunk(generator):
                try:
                    return next(generator), False  # 返回下一个结果和未完成标志
                except StopIteration:
                    return None, True  # 返回None和完成标志
                except Exception as e:
                    raise e

            is_complete = False
            while not is_complete:
                # 在线程池中获取下一个响应
                chunk, is_complete = await loop.run_in_executor(executor, get_next_chunk, generator)

                if is_complete:
                    # 流结束
                    end_message = json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False)
                    yield f"data: {end_message}\n\n"
                    break

                # 计算增量，与原代码保持一致
                delta_ans = chunk[len(last_ans):]  # 计算增量
                if not delta_ans:  # 如果没有新内容，跳过
                    continue

                last_ans = chunk  # 更新累加内容
                sse_data = json.dumps({"retcode": 0, "retmsg": "", "data": last_ans}, ensure_ascii=False)
                yield f"data: {sse_data}\n\n"  # 注意这里返回的是累加的内容，与原代码一致

        except Exception as e:
            error_message = json.dumps(
                {"retcode": 500, "retmsg": str(e), "data": {"answer": f"**ERROR**: {str(e)}"}},
                ensure_ascii=False
            )
            yield f"data: {error_message}\n\n"
            # 流结束标记
            end_message = json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False)
            yield f"data: {end_message}\n\n"

    # 根据是否流式调用选择合适的方法
    if req["stream"]:
        return StreamingResponse(stream_response(), media_type="text/event-stream")
    else:
        # 在线程池中运行同步代码
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(executor, process_non_streaming_request)
        return get_json_result(data=data)
# @router.post('/chat_service_sse', summary="模型对话服务", response_description="成功调用对话模型")
# def chat_service_sse(request: LLMServiceRequest, db: Session = Depends(get_db), user=Depends(manager)):
#     """
#     ### POST `/v1/llm/chat_service` 模型对话服务
#
# **功能描述**:
# 此接口用于调用对话模型，基于用户提供的输入生成对应的响应内容。支持文本生成、图像到文本转换、消息处理等多种模型类型，接口根据请求体中的配置，选择适当的模型及生成方式，提供流式和非流式响应模式。
#
# ---
#
# ### 请求体 (Request Body)
#
# | 字段         | 类型                | 必填 | 描述                                                                                  |
# |--------------|---------------------|------|---------------------------------------------------------------------------------------|
# | `prompt`     | `string`           | 否   | 用户提供的提示内容，用于引导对话模型生成响应。                                        |
# | `messages`   | `list[dict]`       | 是   | 对话消息列表，包含用户与模型之间的对话历史，格式为 `{ "role": "user/assistant", "content": "..." }`。|
# | `llm_name`   | `string`           | 是   | 模型名称，用于指定所调用的语言模型。                                                 |
# | `stream`     | `boolean`          | 是   | 指定是否使用流式响应，`true` 表示流式响应，`false` 表示非流式响应。                   |
# | `gen_conf`   | `object`           | 否   | 生成配置，控制对话生成行为，例如温度值、生成长度等（具体配置视模型能力而定）。         |
# | `image`      | `string (Base64)`  | 否   | Base64 编码的图像数据，适用于图像到文本的转换模型。                                   |
#
# ---
#
# ### 响应 (Response)
#
# #### 成功响应 (200)
#
# - **流式响应**:
#     - **`Content-Type: text/event-stream`**
#     - 数据按块流式返回，每条消息以 `data:` 开头，并以两个换行符 `\\n\\n` 结束。
#
#     **示例**:
#
#     ```plaintext
#     data: {"retcode": 0, "retmsg": "", "data": "你好"}
#
#     data: {"retcode": 0, "retmsg": "", "data": "你好👋！"}
#
#     data: {"retcode": 0, "retmsg": "", "data": "你好👋！我是人工智能助手"}
#
#     data: {"retcode": 0, "retmsg": "", "data": true}
#     ```
#
# - **非流式响应**:
#     - **`Content-Type: application/json`**
#     - **示例**:
#     ```json
#     {
#         "retcode": 0,
#         "retmsg": "success",
#         "data": "你好👋！我是人工智能助手，很高兴见到你！欢迎问我任何问题。"
#     }
#     ```
#
# ---
#
# ### 错误响应
#
# #### **404: Tenant not found**
# - **描述**: 当根据用户ID查找租户信息失败时，返回此错误。
# - **示例**:
#     ```json
#     {
#         "detail": "Tenant not found!"
#     }
#     ```
#
# #### **404: Model not found**
# - **描述**: 当指定的模型名称在用户租户可用模型列表中未找到时，返回此错误。
# - **示例**:
#     ```json
#     {
#         "detail": "Model glm-4-airx not found in the list."
#     }
#     ```
#
# #### **500: 内部错误**
# - **描述**: 当发生意外错误时，返回此错误。
# - **示例**:
#     - **流式响应错误**:
#         ```plaintext
#         data: {"retcode": 500, "retmsg": "Internal server error", "data": {"answer": "**ERROR**: Internal error"}}
#         ```
#     - **非流式响应错误**:
#         ```json
#         {
#             "retcode": 500,
#             "retmsg": "Internal server error",
#             "data": {"answer": "**ERROR**: Internal error"}
#         }
#         ```
#
# ---
#
# ### 主要流程
#
# 1. 从请求中提取用户输入的内容、模型名称和配置信息。
# 2. 通过用户信息获取租户信息，确保用户的租户身份；如果未找到租户信息，返回404错误。
# 3. 获取用户租户关联的模型列表，确定模型类型 (`llm_type`)。
# 4. 根据 `llm_type` 判断是否需要传入 `image` 参数，构建生成请求。
# 5. 根据 `stream` 参数选择流式或非流式的生成方法，调用模型获取对话响应内容。
# 6. 返回生成的对话结果。
#
# ---
#
# ### 注意事项
#
# - **模型选择**:
#     - 仅当 `llm_type` 为 `image2text` 时传递 `image` 参数，以确保在需要图像到文本转换时能处理Base64编码的图像数据。
#     - 支持多种模型类型 (如文本生成、图像到文本、消息对话)，请根据需求选择适当的 `llm_name` 和 `llm_type`。
# - **流式调用**:
#     - 若 `stream` 参数为 `True`，将返回流式响应，用于实时数据生成；若为 `False`，返回完整的响应数据。
# - **数据格式**:
#     - 返回数据格式可能因模型及请求内容不同而有所变化；默认返回JSON格式的结构化数据或文本响应。
# - **流式响应结束标记**:
#     - 流式响应的最后一条消息为:
#       ```plaintext
#       data: {"retcode": 0, "retmsg": "", "data": true}
#       ```
#
#     """
#     req = request.model_dump()
#     tenants = TenantService.get_info_by(db, user.id)
#     if not tenants:
#         raise HTTPException(status_code=404, detail="Tenant not found!")
#
#     my_llms = TenantLLMService.get_my_llms(db, tenants[0]["tenant_id"])
#
#     def get_llm_type(model_name, my_llms):
#         for row in my_llms:
#             if row[4] == model_name:  # 这里的第5个元素是 model_name
#                 return row[-3]  # 倒数第三个元素是 llm_type
#         return None  # 如果找不到，返回 None
#
#     llm_type = get_llm_type(req["llm_name"], my_llms)
#     if llm_type:
#         logging.debug(f"The llm_type for model {req['llm_name']} is: {llm_type}")
#     else:
#         raise HTTPException(status_code=404, detail=f"Model {req['llm_name']} not found in the list.")
#
#     chat_mdl = LLMBundle(db, tenants[0]["tenant_id"], llm_type, req["llm_name"])
#     # 构建调用参数
#     call_params = {
#         "system": req["prompt"],
#         "history": req["messages"],
#         "gen_conf": req["gen_conf"]
#     }
#
#     # 如果llm_type为image2text，添加image参数
#     if llm_type == 'image2text':
#         call_params["image"] = req["image"]
#
#     async def stream_response():
#         """
#         Stream SSE response to the client.
#         """
#         try:
#             last_ans = ""  # 初始化累加变量
#             for ans in chat_mdl.chat_streamly(**call_params):
#                 delta_ans = ans[len(last_ans):]  # 计算增量
#                 if not delta_ans:  # 如果没有新内容，跳过
#                     continue
#                 last_ans = ans  # 更新累加内容
#                 sse_data = json.dumps({"retcode": 0, "retmsg": "", "data": last_ans}, ensure_ascii=False)
#                 yield f"data: {sse_data}\n\n"  # SSE 格式：data: 数据\n\n
#         except Exception as e:
#             error_message = json.dumps({"retcode": 500, "retmsg": str(e), "data": {"answer": f"**ERROR**: {str(e)}"}},
#                                        ensure_ascii=False)
#             yield f"data: {error_message}\n\n"
#         finally:
#             # 流结束标记
#             end_message = json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False)
#             yield f"data: {end_message}\n\n"
#
#     # 根据是否流式调用选择合适的方法
#     if req["stream"]:
#         # data = chat_mdl.chat_streamly(**call_params)
#         return StreamingResponse(stream_response(), media_type="text/event-stream")
#     else:
#         data = chat_mdl.chat(**call_params)
#
#         return get_json_result(data=data)


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
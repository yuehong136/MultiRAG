# coding=utf-8
"""
@project: multirag
@Author：龙
@file： dialog_app.py
@date：2024/8/12 16:00
@desc: 对话管理接口
"""
from typing import Annotated, Literal, Any

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, Discriminator, model_validator, field_validator, ConfigDict

from api.apps import manager
from api.db.db_models import get_db
from api.db.services.dialog_service import DialogService
from api.db import StatusEnum
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import TenantLLMService
from api.db.services.user_service import TenantService, UserTenantService
from api import settings
from api.utils.api_utils import server_error_response, get_data_error_result
from api.utils import get_uuid
from api.utils.api_utils import get_json_result


router = APIRouter()

class SparseSearchMode(BaseModel):
    type: Literal["sparse"] = "sparse"

class DenseSearchMode(BaseModel):
    type: Literal["dense"] = "dense"

class HybridSearchMode(BaseModel):
    type: Literal["hybrid"] = "hybrid"
    weight_dense: float = Field(default=0.7, ge=0.0, le=1.0, description="向量检索权重")
    weight_sparse: float = Field(default=0.3, ge=0.0, le=1.0, description="全文检索权重")

    @model_validator(mode='after')
    def validate_weights(self) -> 'HybridSearchMode':
        """确保权重和为1，如果不是则自动调整"""
        total = self.weight_dense + self.weight_sparse
        if abs(total - 1.0) > 0.001:
            self.weight_dense = self.weight_dense / total
            self.weight_sparse = self.weight_sparse / total
        return self

class FusionSearchMode(BaseModel):
    type: Literal["fusion"] = "fusion"
    weights: str = Field(default="0.05,0.95", description="文本权重,向量权重")

    @model_validator(mode='after')
    def validate_weights_format(self) -> 'FusionSearchMode':
        """验证weights格式"""
        try:
            parts = self.weights.split(',')
            if len(parts) != 2:
                raise ValueError("weights must contain exactly two comma-separated values")
            float(parts[0].strip())
            float(parts[1].strip())
        except (ValueError, AttributeError):
            raise ValueError("weights must be in format 'float,float' (e.g., '0.05,0.95')")
        return self

# 搜索模式联合类型
SearchModeType = Annotated[
    SparseSearchMode | DenseSearchMode | HybridSearchMode | FusionSearchMode,
    Discriminator('type')
]

# 提示配置的子模型
class PromptParameter(BaseModel):
    key: str = Field(..., description="参数键名")
    optional: bool = Field(default=False, description="是否为可选参数")

class PromptConfig(BaseModel):
    system: str = Field(
        default="""你是一个智能助手，请总结知识库的内容来回答问题，请列举知识库中的数据详细回答。当所有知识库内容都与问题无关时，你的回答必须包括"知识库中未找到您要的答案！"这句话。回答需要考虑聊天历史。
以下是知识库：
{knowledge}
以上是知识库。""",
        description="系统提示模板"
    )
    prologue: str = Field(
        default="您好，我是您的助手小樱，长得可爱又善良，can I help you?",
        description="开场白"
    )
    parameters: list[PromptParameter] = Field(
        default=[{"key": "knowledge", "optional": False}],
        description="参数列表"
    )
    empty_response: str = Field(
        default="Sorry! 知识库中未找到相关内容！",
        description="空响应消息"
    )


# 改进后的 DialogRequest
class DialogRequest(BaseModel):
    dialog_id: str | None = Field(None, description="对话的唯一标识符，如果为空则表示创建新对话")
    name: str = Field(default="New Dialog", max_length=100, description="对话的名称")
    description: str = Field(default="A helpful dialog", max_length=500, description="对话的描述")
    icon: str = Field(default="", description="对话的图标URL")
    top_n: int = Field(
        default=6,
        ge=1,
        le=50,
        description="从知识库中返回给用户的最大条目数"
    )
    top_k: int = Field(
        default=1024,
        ge=1,
        le=10000,
        description="从知识库中检索的最大条目数"
    )
    rerank_id: str | None = Field(
        default=None,
        description="重新排序模型的ID，用于对检索结果进行重新排序"
    )
    similarity_threshold: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="相似度阈值，低于此阈值的结果将被过滤"
    )
    vector_similarity_weight: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="向量相似度权重，用于重新排序阶段"
    )
    llm_id: str | None = Field(
        default=None,
        description="大语言模型的ID，如果不指定则使用租户默认模型"
    )
    llm_setting: dict[str, Any] | None = Field(
        default=None,
        description="大语言模型的配置参数"
    )
    prompt_config: dict[str, Any] | None = Field(
        default=None,
        description="""提示配置字典，建议包含以下字段：
        - system: 系统提示模板，支持参数插值，例如 {knowledge}
        - prologue: 对话开场白
        - parameters: 参数列表，每项包含 key 和 optional 属性
        - empty_response: 未找到相关内容时的回复
        """
    )
    kb_ids: list[str] | None = Field(
        default=None,
        description="知识库的ID列表"
    )
    # 新增的搜索模式参数
    search_mode: SearchModeType | None = Field(
        default=None,
        description="检索模式配置，决定对话中检索知识库时使用的策略"
    )

    # 字段验证器 - 验证name字段
    @field_validator('name')
    def validate_name(cls, v: str) -> str:  # ✅ 直接使用 cls，Pydantic会自动处理
        """验证对话名称"""
        if v.strip() == "":
            raise ValueError("Dialog name can't be empty.")

        if len(v.encode("utf-8")) > 255:
            raise ValueError(f"Dialog name length is {len(v)} which is larger than 255")

        return v

    # 模型验证器
    @model_validator(mode='after')
    def validate_top_n_not_greater_than_top_k(self) -> 'DialogRequest':
        """确保 top_n 不大于 top_k"""
        if self.top_n > self.top_k:
            raise ValueError("top_n cannot be greater than top_k")
        return self

    # 验证 prompt_config 中的参数
    @model_validator(mode='after')
    def validate_prompt_config(self) -> 'DialogRequest':
        """验证提示配置中的参数使用情况"""
        if not self.prompt_config:
            return self

        # 检查必要的字段
        if "system" not in self.prompt_config:
            raise ValueError("prompt_config must contain 'system' field")

        if "parameters" not in self.prompt_config:
            return self

        # 验证参数的使用
        system_prompt = self.prompt_config.get("system", "")
        for param in self.prompt_config.get("parameters", []):
            if isinstance(param, dict) and "key" in param and "optional" in param:
                key = param["key"]
                optional = param["optional"]
                if not optional and system_prompt.find(f"{{{key}}}") < 0:
                    raise ValueError(f"Required parameter '{key}' is not used in system prompt")

        return self

    # 转换函数
    def get_search_mode_dict(self) -> dict[str, Any] | None:
        """将搜索模式转换为字典格式供底层函数使用"""
        if self.search_mode is None:
            return None

        mode_data = self.search_mode.model_dump()
        mode_type = mode_data.pop('type')
        return {mode_type: mode_data}


class RemoveDialogRequest(BaseModel):
    dialog_ids: list[str]
    """要删除的对话ID列表。"""


class ListDialogsRequest(BaseModel):
    owner_ids: list[int] = Field(default_factory=list)

class ListDialogsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")  # 允许 dialogs 内部是任意字段字典
    dialogs: list[dict[str, Any]]
    total: int


def get_kb_names(kb_ids, db: Session):
    ids, nms = [], []
    for kid in kb_ids:
        kb = KnowledgebaseService.get_by_id(db, kid)
        if not kb or kb.status != StatusEnum.VALID.value:
            continue
        ids.append(kid)
        nms.append(kb.name)
    return ids, nms

@router.post('/set', summary="设置对话", response_description="成功设置对话")
def set_dialog(request: DialogRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
        设置对话

        该接口用于创建或更新对话信息，包括检索配置、模型设置和搜索模式。

        ## 参数说明

        ### 基础信息
        - **dialog_id**: 对话的唯一标识符，如果为空则创建新对话，否则更新已有对话
        - **name**: 对话名称，最大长度100个字符，默认为 'New Dialog'
        - **description**: 对话描述，最大长度500个字符，默认为 'A helpful dialog'
        - **icon**: 对话图标URL，最大长度200个字符

        ### 检索配置
        - **kb_ids**: 知识库ID列表，指定对话可以访问的知识库
        - **top_n**: 返回给用户的最大结果数 (1-50)，默认为6
        - **top_k**: 检索阶段的最大召回数 (1-10000)，默认为1024
        - **similarity_threshold**: 相似度阈值 (0.0-1.0)，低于此值的结果将被过滤，默认为0.1
        - **vector_similarity_weight**: 向量相似度权重 (0.0-1.0)，用于重排序，默认为0.3

        ### 模型配置
        - **llm_id**: 大语言模型ID，如果不指定则使用租户默认模型
        - **llm_setting**: 模型参数配置，如温度、最大长度等
        - **rerank_id**: 重排序模型ID，用于优化检索结果排序

        ### 提示配置 (prompt_config)
        提示配置是一个灵活的字典，建议包含以下字段：
        ```json
        {
            "system": "你是一个智能助手，请总结知识库的内容来回答问题，请列举知识库中的数据详细回答。当所有知识库内容都与问题无关时，你的回答必须包括"知识库中未找到您要的答案！"这句话。回答需要考虑聊天历史。\n以下是知识库：\n{knowledge}\n以上是知识库。",
            "prologue": "您好，我是您的助手小樱，长得可爱又善良，can I help you?",
            "parameters": [
                {"key": "knowledge", "optional": false}
            ],
            "empty_response": "Sorry! 知识库中未找到相关内容！"
        }
        ```
        - **system**: 必填，系统提示模板，支持参数插值，例如 {knowledge}
        - **prologue**: 可选，对话开场白
        - **parameters**: 可选，参数列表，每项包含 key 和 optional 属性
        - **empty_response**: 可选，未找到相关内容时的回复
        - 可以包含其他自定义字段，系统不会做特殊处理

        ### 搜索模式 (search_mode)
        控制对话中检索知识库时使用的策略：

        #### 1. 混合检索（推荐）
        ```json
        {
            "type": "hybrid",
            "weight_dense": 0.7,
            "weight_sparse": 0.3
        }
        ```

        #### 2. 纯语义检索
        ```json
        {
            "type": "dense"
        }
        ```

        #### 3. 纯关键词检索
        ```json
        {
            "type": "sparse"
        }
        ```

        #### 4. 融合检索
        ```json
        {
            "type": "fusion",
            "weights": "0.05,0.95"
        }
        ```

        ## 使用示例

        ### 创建新对话（带自定义提示配置）
        ```json
        {
            "name": "技术支持助手",
            "description": "专门用于技术问题解答的对话",
            "kb_ids": ["tech_kb_001", "faq_kb_002"],
            "search_mode": {
                "type": "hybrid",
                "weight_dense": 0.6,
                "weight_sparse": 0.4
            },
            "prompt_config": {
                "system": "你是一位专业的技术支持专家。基于以下知识库内容回答用户问题：\\n{knowledge}\\n{history}",
                "prologue": "您好！我是技术支持助手，请问有什么技术问题需要帮助吗？",
                "parameters": [
                    {"key": "knowledge", "optional": false},
                    {"key": "history", "optional": true}
                ],
                "empty_response": "很抱歉，我没有找到相关的技术资料。请提供更多细节，我会尽力帮助您。",
                "custom_field": "可以添加自定义字段"
            }
        }
        ```
        """
    try:
        # 获取默认提示配置
        default_prompt_config = {
            "system": """你是一个智能助手，请总结知识库的内容来回答问题，请列举知识库中的数据详细回答。当所有知识库内容都与问题无关时，你的回答必须包括"知识库中未找到您要的答案！"这句话。回答需要考虑聊天历史。
        以下是知识库：
        {knowledge}
        以上是知识库。""",
            "prologue": "您好，我是您的助手小樱，长得可爱又善良，can I help you?",
            "parameters": [
                {"key": "knowledge", "optional": False}
            ],
            "empty_response": "Sorry! 知识库中未找到相关内容！"
        }
        is_create = not request.dialog_id
        if not is_create:
            # 使用提供的配置或默认配置
            prompt_config = request.prompt_config or default_prompt_config

            if not request.kb_ids and not prompt_config.get("tavily_api_key") and "{knowledge}" in prompt_config['system']:
                return get_data_error_result(retmsg="Please remove `{knowledge}` in system prompt since no knowledge base/Tavily used here.")

            # 验证系统提示中是否包含所有必需参数
            for p in prompt_config["parameters"]:
                if p["optional"]:
                    continue
                if prompt_config["system"].find("{%s}" % p["key"]) < 0:
                    return get_data_error_result(
                        retmsg="Parameter '{}' is not used".format(p["key"]))

        tenant = TenantService.get_by_id(db, user.id)
        if not tenant:
            return get_data_error_result(retmsg="Tenant not found!")

        kbs = KnowledgebaseService.get_by_ids(db, request.kb_ids or [])
        embd_ids = [TenantLLMService.split_model_name_and_factory(kb.embd_id)[0] for kb in kbs]  # remove vendor suffix for comparison
        embd_count = len(set(embd_ids))
        if embd_count > 1:
            return get_data_error_result(retmsg=f'Datasets use different embedding models: {[kb.embd_id for kb in kbs]}"')

        llm_id = request.llm_id or tenant.llm_id

        # 使用实例方法获取搜索模式
        search_mode_dict = request.get_search_mode_dict()

        if not request.dialog_id:
            dia = {
                "id": get_uuid(),
                "tenant_id": user.id,
                "name": request.name,
                "kb_ids": request.kb_ids or [],
                "description": request.description,
                "llm_id": llm_id,
                "llm_setting": request.llm_setting,
                "prompt_config": prompt_config,
                "top_n": request.top_n,
                "top_k": request.top_k,
                "rerank_id": request.rerank_id,
                "similarity_threshold": request.similarity_threshold,
                "vector_similarity_weight": request.vector_similarity_weight,
                "icon": request.icon,
                "search_mode": search_mode_dict
            }
            if not DialogService.save(db, **dia):
                return get_data_error_result(retmsg="Fail to new a dialog!")

            dia = DialogService.get_by_id(db, dia["id"])
            if not dia:
                return get_data_error_result(retmsg="Fail to new a dialog!")

            dia = dia.to_dict()
            return get_json_result(data=dia)
        else:
            update_data = request.model_dump(exclude_unset=True, exclude_none=True)
            if "search_mode" in update_data:
                update_data["search_mode"] = search_mode_dict
            del update_data["dialog_id"]
            if "kb_names" in update_data:
                del update_data["kb_names"]
            if not DialogService.update_by_id(db, request.dialog_id, update_data):
                return get_data_error_result(retmsg="Dialog not found!")
            dia = DialogService.get_by_id(db, request.dialog_id)
            if not dia:
                return get_data_error_result(retmsg="Fail to update a dialog!")
            dia = dia.to_dict()
            dia["kb_ids"], dia["kb_names"] = get_kb_names(dia["kb_ids"], db)
            return get_json_result(data=dia)
    except Exception as e:
        return server_error_response(e)

@router.get('/get', summary="获取对话", response_description="成功获取对话")
async def get(dialog_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取对话

    该接口用于获取指定对话的信息。

    参数:
    - dialog_id: str 对话的唯一标识符

    返回:
    - 成功时返回包含对话信息的JSON结果
    - 失败时返回错误信息
    """
    try:
        dia = DialogService.get_by_id(db, dialog_id)
        if not dia:
            return get_data_error_result(retmsg="Dialog not found!")
        dia = dia.to_dict()
        dia["kb_ids"], dia["kb_names"] = get_kb_names(dia["kb_ids"], db)
        return get_json_result(data=dia)
    except Exception as e:
        return server_error_response(e)

@router.get('/list', summary="列出对话", response_description="成功列出对话")
async def list_dialogs(db: Session = Depends(get_db), user=Depends(manager)):
    """
    列出对话

    该接口用于列出当前用户的所有对话。

    返回:
    - 成功时返回包含对话列表的JSON结果
    - 失败时返回错误信息
    """
    try:
        diags = DialogService.query(
            db,
            tenant_id=user.id,
            status=StatusEnum.VALID.value,
            reverse=True,
            order_by="create_time")
        diags = [d.to_dict() for d in diags]
        for d in diags:
            d["kb_ids"], d["kb_names"] = get_kb_names(d["kb_ids"], db)
        return get_json_result(data=diags)
    except Exception as e:
        return server_error_response(e)


@router.post("/next", response_model=ListDialogsResponse, summary="List dialogs (next)")
def list_dialogs_next(
    # Query 参数（保持与你原逻辑一致的默认值与行为）
    keywords: Annotated[str, Query(alias="keywords", description="关键词模糊搜索")] = "",
    page_number: Annotated[int, Query(alias="page", ge=0, description="页码（从1开始；为0则不分页）")] = 0,
    items_per_page: Annotated[int, Query(alias="page_size", ge=0, description="每页大小（为0则不分页）")] = 0,
    parser_id: Annotated[str | None, Query(alias="parser_id", description="parser 过滤")] = None,
    orderby: Annotated[str, Query(alias="orderby", description="排序字段")] = "create_time",
    # 原逻辑：默认 true；当传入字符串 "false" 时才为 False。
    # 在 FastAPI 中直接使用 bool 会自动解析 ?desc=false 为 False，因此等价且更安全。
    desc: Annotated[bool, Query(alias="desc", description="是否降序")] = True,
    body: ListDialogsRequest = Depends(),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    try:
        owner_ids = body.owner_ids or []
        if not owner_ids:
            # tenants = TenantService.get_joined_tenants_by_user_id(db, user.id)
            # tenants = [tenant["tenant_id"] for tenant in tenants]
            tenants: list[int] = []  # 与原注释保持一致：keep it here
            dialogs, total = DialogService.get_by_tenant_ids(
                db,
                tenants,                      # joined_tenant_ids
                user.id,              # user_id
                page_number,                  # page_number
                items_per_page,               # items_per_page
                orderby,                      # orderby
                desc,                         # desc
                keywords,                     # keywords
                parser_id                     # parser_id
            )
        else:
            tenants = owner_ids
            # 与原逻辑一致：不分页取全量
            dialogs, _ = DialogService.get_by_tenant_ids(
                db,
                tenants,
                user.id,
                0,            # page_number=0 -> 不分页
                0,            # items_per_page=0 -> 不分页
                orderby,
                desc,
                keywords,
                parser_id
            )
            # 过滤 tenant_id
            dialogs = [d for d in dialogs if d.get("tenant_id") in tenants]
            total = len(dialogs)
            # 手动分页
            if page_number and items_per_page:
                start = (page_number - 1) * items_per_page
                end = page_number * items_per_page
                dialogs = dialogs[start:end]

        return get_json_result(data=ListDialogsResponse(dialogs=dialogs, total=total))

    except HTTPException:
        # 透传框架级异常
        raise
    except Exception as e:
        return server_error_response(e)


@router.post('/rm', summary="删除对话应用", response_description="成功删除对话应用")
async def rm(request: RemoveDialogRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    删除对话

    该接口用于删除指定的对话。

    参数:
    - request: RemoveDialogRequest对象，包含要删除的对话ID列表
        - dialog_ids: List[str] 要删除的对话ID列表

    返回:
    - 成功时返回成功删除的JSON结果
    - 失败时返回错误信息
    """
    dialog_list=[]
    tenants = UserTenantService.query(db, user_id=user.id)
    try:
        for id in request.dialog_ids:
            for tenant in tenants:
                if DialogService.query(db, tenant_id=tenant.tenant_id, id=id):
                    break
            else:
                return get_json_result(
                    data=False, retmsg=f'Only owner of dialog authorized for this operation.',
                    retcode=settings.RetCode.OPERATING_ERROR)
            dialog_list.append({"id": id, "status": StatusEnum.INVALID.value})
        DialogService.update_many_by_id(db, dialog_list)
        # DialogService.delete_by_id(db, id)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)
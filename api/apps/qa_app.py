"""
教师科研考核问答系统 - API接口
包含所有Pydantic Schema定义和接口实现
"""
import logging
from datetime import datetime
from typing import Any
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from api import settings
from api.db.db_models import get_db
from api.db.services.user_service import UserTenantService
from api.db.services.qa_service import (
    QATemplateStorageService,
    QATemplateMatchingService,
    StatelessSlotExtractionService,
    ClarificationService,
    StatelessQAService,
    LLMScoringService,
    RAGService
)
from api.apps import manager
from api.utils.api_utils import get_json_result, server_error_response, get_data_error_result


logger = logging.getLogger(__name__)
router = APIRouter()

# 初始化服务实例
template_storage = QATemplateStorageService()
template_matcher = QATemplateMatchingService()
slot_extractor = StatelessSlotExtractionService()
clarification_generator = ClarificationService()
stateless_qa_service = StatelessQAService()
llm_scorer = LLMScoringService()
rag_service = RAGService()


def get_user_tenant_id(db: Session, user_id: str) -> str:
    """获取用户的租户ID"""
    tenants = UserTenantService.query(db, user_id=user_id)
    if not tenants:
        raise HTTPException(status_code=404, detail="User tenant not found")
    return tenants[0].tenant_id


# ================================
# Pydantic Schema 定义
# ================================

class QAParamDefinition(BaseModel):
    """QA参数定义"""
    name: str = Field(..., description="参数名称")
    data_type: str = Field(default="string", description="数据类型：string, integer, float, boolean, date")
    description: str | None = Field(None, description="参数描述")
    required: bool = Field(default=True, description="是否必需")

class QATemplate(BaseModel):
    """QA模板数据结构 - 保持不变"""
    qa_id: str = Field(..., description="唯一标识符")
    question_canonical: str = Field(..., description="标准问法")
    paraphrases: list[str] = Field(default=[], description="同义句列表")
    needed_params: list[str] = Field(..., description="需要的参数列表")
    sql_template: str = Field(..., description="SQL模板，使用命名参数")
    rule_id: str | None = Field(None, description="评分规则ID，可为空")

class QATemplateV2(BaseModel):
    """QA模板数据结构 - 支持带类型的参数定义"""
    qa_id: str = Field(..., description="唯一标识符")
    question_canonical: str = Field(..., description="标准问法")
    paraphrases: list[str] = Field(default=[], description="同义句列表")
    needed_params_v2: list[QAParamDefinition] = Field(..., description="带类型的参数定义列表")
    sql_template: list[str] = Field(..., description="SQL模板列表，支持多个SQL模板，使用命名参数")
    rule_id: str | None = Field(None, description="评分规则ID，可为空")
    
    # 向后兼容
    @property
    def needed_params(self) -> list[str]:
        """向后兼容的参数名列表"""
        return [param.name for param in self.needed_params_v2]

class TableSchema(BaseModel):
    """数据库表结构信息 - 保持不变"""
    table_name: str = Field(..., description="表名")
    columns: list[dict[str, str]] = Field(..., description="列信息：{name, type, description}")

class StoreTemplatesRequest(BaseModel):
    """存储QA模板请求 - 保持不变"""
    templates: list[QATemplate] = Field(..., description="QA模板列表")
    clear_existing: bool = Field(default=False, description="是否清空现有模板")

class StoreTemplatesResponse(BaseModel):
    """存储QA模板响应 - 保持不变"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(..., description="操作结果信息")
    template_count: int | None = Field(None, description="存储的模板数量")
    record_count: int | None = Field(None, description="实际存储的记录数量")


class DialogRound(BaseModel):
    """单轮对话数据"""
    round_id: int = Field(..., description="轮次ID")
    user_input: str = Field(..., description="用户输入")
    timestamp: str = Field(..., description="时间戳")


class DialogContext(BaseModel):
    """对话上下文（调用端维护）"""
    session_id: str = Field(..., description="会话ID")
    initial_query: str = Field(..., description="初始查询")
    rounds: list[DialogRound] = Field(default_factory=list, description="对话轮次")
    matched_template: dict[str, Any] | None = Field(None, description="已匹配的模板")
    accumulated_params: dict[str, Any] = Field(default_factory=dict, description="累积参数")
    missing_params: list[str] = Field(default_factory=list, description="缺失参数")
    metadata: dict[str, Any] = Field(default_factory=dict, description="额外元数据")


class StatelessInterpretRequest(BaseModel):
    """无状态查询解释请求"""
    current_input: str = Field(..., description="当前用户输入")
    dialog_context: DialogContext | None = Field(None, description="对话上下文（多轮时提供）")
    table_schemas: list[TableSchema] = Field(..., description="数据库表结构")

    # 配置参数
    system_date: str | None = Field(None, description="系统日期")
    similarity_threshold: float = Field(default=0.3, description="模板匹配阈值")
    hybrid_weight: float = Field(default=0.7, description="混合检索权重")
    llm_name: str | None = Field(None, description="指定LLM模型")

    # 行为控制
    force_new_template: bool = Field(default=False, description="强制重新匹配模板")
    enable_slot_merge: bool = Field(default=True, description="启用参数合并")


class StatelessInterpretResponse(BaseModel):
    """无状态查询解释响应"""
    status: str = Field(..., description="处理状态：OK, NEED_CLARIFY, ERROR")

    # 核心结果
    qa_id: str | None = Field(None, description="匹配的QA模板ID")
    sql_template: list[str] | str | None = Field(None, description="SQL模板（V2为数组格式，V1为字符串格式）")
    complete_params: dict[str, Any] = Field(default_factory=dict, description="完整参数")
    rule_id: str | None = Field(None, description="评分规则ID")

    # 追问相关
    missing_params: list[str] = Field(default_factory=list, description="缺失参数")
    clarify_message: str | None = Field(None, description="追问文案")

    # 元信息
    confidence: float = Field(..., description="置信度")
    processing_info: dict[str, Any] = Field(default_factory=dict, description="处理信息")

    # 更新后的上下文（供调用端保存）
    updated_context: DialogContext | None = Field(None, description="更新后的上下文")


class QuickInterpretRequest(BaseModel):
    """快速解释请求（单轮）"""
    user_query: str = Field(..., description="用户查询")
    table_schemas: list[TableSchema] = Field(..., description="表结构")
    llm_name: str | None = Field(None, description="LLM模型")
    similarity_threshold: float = Field(default=0.3, description="模板匹配阈值")
    hybrid_weight: float = Field(default=0.7, description="混合检索权重")


class QuickInterpretResponse(BaseModel):
    """快速解释响应"""
    status: str = Field(..., description="状态：OK, NEED_CLARIFY, ERROR")
    qa_id: str | None = Field(None, description="匹配的QA模板ID")
    sql_template: str | None = Field(None, description="SQL模板")
    complete_params: dict[str, Any] = Field(default_factory=dict, description="完整参数")
    missing_params: list[str] = Field(default_factory=list, description="缺失参数")
    clarify_message: str | None = Field(None, description="追问文案")
    confidence: float = Field(..., description="置信度")
    message: str | None = Field(None, description="错误信息")


class CalcScoreRequest(BaseModel):
    """评分计算请求"""
    rule_description: str = Field(..., description="评分规则描述文本")
    data: list[dict[str, Any]] = Field(..., description="SQL查询结果数据")
    context: dict[str, Any] | None = Field(None, description="评分上下文信息")
    llm_name: str | None = Field(None, description="指定用于评分的LLM模型")


class CalcScoreResponse(BaseModel):
    """评分计算响应"""
    score: float | None = Field(None, description="最终得分（如果能提取数值）")
    score_text: str = Field(..., description="LLM生成的完整评分结果文本")
    analysis: str = Field(..., description="评分分析过程")
    suggestions: str | None = Field(None, description="改进建议")
    data_summary: dict[str, Any] = Field(..., description="数据汇总信息")


class RAGAnswerRequest(BaseModel):
    """RAG回答请求"""
    query: str = Field(..., description="用户查询")
    kb_id: str = Field(..., description="知识库ID")
    top_k: int = Field(default=5, description="检索top-k文档")
    llm_name: str | None = Field(None, description="指定LLM模型")


class RAGAnswerResponse(BaseModel):
    """RAG回答响应"""
    answer: str = Field(..., description="生成的回答")
    sources: list[dict[str, Any]] = Field(..., description="引用来源")
    confidence: float = Field(..., description="回答置信度")


class DeleteTemplateRequest(BaseModel):
    """删除QA模板请求"""
    qa_ids: str | list[str] = Field(..., description="要删除的QA模板ID，可以是单个ID或ID列表")


class DeleteTemplateResponse(BaseModel):
    """删除QA模板响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(..., description="操作结果信息")
    deleted_count: int | None = Field(None, description="删除的记录数量")
    failed_qa_ids: list[str] | None = Field(None, description="删除失败的qa_id列表")


class StoreTemplatesV2Request(BaseModel):
    """存储QA模板V2请求 - 支持类型化参数"""
    templates: list[QATemplateV2] = Field(..., description="QA模板V2列表")
    clear_existing: bool = Field(default=False, description="是否清空现有模板")


class CollectionStatusResponse(BaseModel):
    """集合状态响应"""
    v1_collection_exists: bool = Field(..., description="V1集合是否存在")
    v2_collection_exists: bool = Field(..., description="V2集合是否存在")
    v1_collection_name: str = Field(..., description="V1集合名称")
    v2_collection_name: str = Field(..., description="V2集合名称")
    current_active_collection: str | None = Field(None, description="当前活跃的集合")
    collection_version: str | None = Field(None, description="当前使用的集合版本")
    v1_record_count: int = Field(default=0, description="V1集合记录数量")
    v2_record_count: int = Field(default=0, description="V2集合记录数量")
    needs_migration: bool = Field(..., description="是否需要迁移")
    migration_suggestions: list[str] = Field(default_factory=list, description="迁移建议")


# ================================
# API接口实现
# ================================

@router.post("/store_templates", summary="存储QA模板", response_description="将QA模板存储到Milvus")
def store_templates(
        request: StoreTemplatesRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    存储QA模板到Milvus集合

    **功能描述:**
    - 将提供的QA模板列表存储到固定的Milvus集合 `bl_qa_template`
    - 自动为标准问法和同义句创建密集向量和稀疏向量（BM25）
    - 支持混合检索以提高匹配准确性
    - 按租户隔离存储

    **参数说明:**
    - templates: QA模板列表，每个模板包含标准问法、同义句、参数等信息
    - clear_existing: 是否先清空该租户的现有模板，默认false

    **返回值:**
    - success: 操作是否成功
    - message: 操作结果信息
    - template_count: 存储的模板数量
    - record_count: 实际存储的记录数量（包括标准问法和同义句）

    **注意事项:**
    - 每个模板会为标准问法创建一条记录
    - 每个同义句也会创建独立的记录
    - 所有记录都支持密集向量和BM25稀疏向量检索
    - 数据按租户隔离，不会互相干扰
    """
    try:
        tenant_id = get_user_tenant_id(db, user.id)

        # 参数验证
        if not request.templates:
            return get_data_error_result(retmsg="模板列表不能为空")

        # 如果需要清空现有模板
        if request.clear_existing:
            clear_result = template_storage.clear_tenant_templates(tenant_id)
            if not clear_result["success"]:
                return get_data_error_result(retmsg=f"清空现有模板失败: {clear_result['message']}")

        # 转换模板数据
        templates_dict = [template.model_dump() for template in request.templates]

        # 存储模板
        result = template_storage.store_templates(
            db=db,
            templates=templates_dict,
            tenant_id=tenant_id
        )

        if result["success"]:
            return get_json_result(data=result)
        else:
            return get_data_error_result(retmsg=result["message"])

    except Exception as e:
        logger.error(f"Error in store_templates: {e}")
        return server_error_response(e)


@router.post("/clear_templates", summary="清空QA模板", response_description="清空当前租户的所有QA模板")
def clear_templates(
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    清空当前租户的所有QA模板

    **功能描述:**
    - 删除当前租户在Milvus中的所有QA模板记录
    - 不影响其他租户的数据

    **返回值:**
    - success: 操作是否成功
    - message: 操作结果信息
    """
    try:
        tenant_id = get_user_tenant_id(db, user.id)

        result = template_storage.clear_tenant_templates(tenant_id)

        if result["success"]:
            return get_json_result(data=result)
        else:
            return get_data_error_result(retmsg=result["message"])

    except Exception as e:
        logger.error(f"Error in clear_templates: {e}")
        return server_error_response(e)


@router.post("/delete_template", summary="删除QA模板", response_description="根据qa_id删除指定的QA模板（支持单个或批量）")
def delete_template(
        request: DeleteTemplateRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    根据qa_id删除指定的QA模板（支持单个或批量删除）

    **功能描述:**
    - 删除当前租户中指定qa_id的QA模板记录
    - 支持单个qa_id或qa_id列表进行批量删除
    - 会删除该模板的所有相关记录（标准问法和同义句）
    - 只影响当前租户的数据

    **参数说明:**
    - qa_ids: 要删除的QA模板ID，可以是：
      - 单个字符串：删除一个模板
      - 字符串列表：批量删除多个模板

    **返回值:**
    - success: 操作是否成功（批量删除时，只要有一个成功就为true）
    - message: 操作结果信息
    - deleted_count: 删除的总记录数量
    - failed_qa_ids: 删除失败的qa_id列表（仅批量删除时）

    **使用示例:**
    ```json
    // 删除单个模板
    {"qa_ids": "template_001"}
    
    // 批量删除多个模板
    {"qa_ids": ["template_001", "template_002", "template_003"]}
    ```

    **注意事项:**
    - 删除操作不可逆，请谨慎使用
    - 只能删除当前租户下的模板
    - 批量删除时，部分成功也会返回成功状态
    - 如果qa_id不存在，会在failed_qa_ids中返回
    """
    try:
        tenant_id = get_user_tenant_id(db, user.id)

        # 处理qa_ids参数，统一转换为列表
        if isinstance(request.qa_ids, str):
            if not request.qa_ids.strip():
                return get_data_error_result(retmsg="qa_ids不能为空")
            qa_ids_list = [request.qa_ids.strip()]
        elif isinstance(request.qa_ids, list):
            if not request.qa_ids:
                return get_data_error_result(retmsg="qa_ids列表不能为空")
            qa_ids_list = [qa_id.strip() for qa_id in request.qa_ids if qa_id.strip()]
            if not qa_ids_list:
                return get_data_error_result(retmsg="qa_ids列表中没有有效的ID")
        else:
            return get_data_error_result(retmsg="qa_ids必须是字符串或字符串列表")

        # 调用删除服务
        result = template_storage.delete_templates_by_qa_ids(
            qa_ids=qa_ids_list,
            tenant_id=tenant_id
        )

        if result["success"]:
            return get_json_result(data=result)
        else:
            return get_data_error_result(retmsg=result["message"])

    except Exception as e:
        logger.error(f"Error in delete_template: {e}")
        return server_error_response(e)


@router.post("/interpret_stateless", summary="无状态查询解释", response_description="解释查询（无状态版本）")
def interpret_stateless(
        request: StatelessInterpretRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    无状态查询解释接口

    **设计理念:**
    - 服务端无状态，不存储任何对话信息
    - 调用端负责维护和传递上下文
    - 接口更通用，适用于各种场景
    - 松耦合设计，便于复用和扩展

    **使用方式:**
    1. 首次调用：只传current_input，不传dialog_context
    2. 后续调用：传入上次返回的updated_context作为dialog_context
    3. 调用端自行决定何时开始新对话（不传context即可）

    **返回格式说明:**
    - sql_template: V2模板返回数组格式，V1模板返回字符串格式
    - 支持多个SQL模板，便于业务端根据场景选择合适的SQL

    **优势:**
    - 服务端简单，易于扩展和维护
    - 调用端灵活，可以实现复杂的业务逻辑
    - 接口通用，适用于不同的集成场景
    """
    try:
        tenant_id = get_user_tenant_id(db, user.id)

        if not request.current_input.strip():
            return get_data_error_result(retmsg="当前输入不能为空")

        # 转换输入参数
        dialog_context_dict = None
        if request.dialog_context:
            dialog_context_dict = request.dialog_context.model_dump()

        table_schemas_dict = [schema.model_dump() for schema in request.table_schemas]

        # 调用无状态服务
        result = stateless_qa_service.interpret(
            db=db,
            current_input=request.current_input,
            dialog_context=dialog_context_dict,
            table_schemas=table_schemas_dict,
            tenant_id=tenant_id,
            system_date=request.system_date,
            similarity_threshold=request.similarity_threshold,
            hybrid_weight=request.hybrid_weight,
            llm_name=request.llm_name,
            force_new_template=request.force_new_template,
            enable_slot_merge=request.enable_slot_merge
        )

        return get_json_result(data=result)

    except Exception as e:
        logger.error(f"Error in interpret_stateless: {e}")
        return server_error_response(e)


@router.post("/quick_interpret", summary="快速解释（单轮）", response_description="快速单轮查询解释")
def quick_interpret(
        request: QuickInterpretRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    快速单轮查询解释（完全无状态）

    **适用场景:**
    - 简单的单轮查询
    - 不需要追问的场景
    - 快速原型验证

    **特点:**
    - 强制单轮模式
    - 不返回上下文信息
    - 响应更轻量
    """
    try:
        tenant_id = get_user_tenant_id(db, user.id)

        if not request.user_query.strip():
            return get_data_error_result(retmsg="用户查询不能为空")

        table_schemas_dict = [schema.model_dump() for schema in request.table_schemas]

        result = stateless_qa_service.interpret(
            db=db,
            current_input=request.user_query,
            dialog_context=None,  # 强制单轮
            table_schemas=table_schemas_dict,
            tenant_id=tenant_id,
            similarity_threshold=request.similarity_threshold,
            hybrid_weight=request.hybrid_weight,
            llm_name=request.llm_name,
            enable_slot_merge=False  # 禁用多轮合并
        )

        # 简化返回（不包含上下文）
        simplified_result = {
            "status": result["status"],
            "qa_id": result.get("qa_id"),
            "sql_template": result.get("sql_template"),
            "complete_params": result.get("complete_params", {}),
            "missing_params": result.get("missing_params", []),
            "clarify_message": result.get("clarify_message"),
            "confidence": result.get("confidence", 0.0),
            "message": result.get("message")
        }

        return get_json_result(data=simplified_result)

    except Exception as e:
        logger.error(f"Error in quick_interpret: {e}")
        return server_error_response(e)


@router.post("/store_templates_v2", summary="存储QA模板V2", response_description="存储支持类型化参数的QA模板到Milvus")
def store_templates_v2(
        request: StoreTemplatesV2Request,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    存储QA模板V2到Milvus（支持类型化参数定义和多SQL模板）

    **新功能:**
    - 支持带数据类型的参数定义
    - 支持多个SQL模板（sql_template为数组格式）
    - 自动类型验证和转换
    - 向后兼容现有API

    **参数定义格式:**
    ```json
    {
        "name": "teacher_age",
        "data_type": "integer",
        "description": "教师年龄",
        "required": true
    }
    ```

    **SQL模板格式:**
    - 支持传入多个SQL模板，用于不同的查询场景
    - 每个模板都使用相同的命名参数
    - 系统会根据业务逻辑选择合适的模板执行

    **支持的数据类型:**
    - string: 字符串类型
    - integer: 整数类型  
    - float: 浮点数类型
    - boolean: 布尔类型
    - date: 日期类型 (YYYY-MM-DD格式)
    """
    try:
        tenant_id = get_user_tenant_id(db, user.id)

        # 参数验证
        if not request.templates:
            return get_data_error_result(retmsg="模板列表不能为空")

        # 如果需要清空现有模板
        if request.clear_existing:
            clear_result = template_storage.clear_tenant_templates(tenant_id)
            if not clear_result["success"]:
                return get_data_error_result(retmsg=f"清空现有模板失败: {clear_result['message']}")

        # 转换V2模板数据为兼容格式
        templates_dict = []
        for template in request.templates:
            template_dict = template.model_dump()
            # 转换为V1兼容格式，同时保留V2信息
            template_dict["needed_params"] = [param["name"] for param in template_dict["needed_params_v2"]]
            # 将类型化参数信息存储为JSON字符串
            template_dict["needed_params_typed"] = json.dumps(template_dict["needed_params_v2"], ensure_ascii=False)
            templates_dict.append(template_dict)

        # 存储模板
        result = template_storage.store_templates_v2(
            db=db,
            templates=templates_dict,
            tenant_id=tenant_id
        )

        if result["success"]:
            return get_json_result(data=result)
        else:
            return get_data_error_result(retmsg=result["message"])

    except Exception as e:
        logger.error(f"Error in store_templates_v2: {e}")
        return server_error_response(e)


@router.post("/calc_score", summary="计算评分", response_description="使用LLM根据规则和数据计算评分")
def calculate_score(
        request: CalcScoreRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    使用LLM根据规则描述和数据计算评分

    **功能描述:**
    - 接收调用方提供的评分规则描述文本
    - 根据SQL查询结果数据，调用LLM进行智能评分
    - 返回详细的评分结果和分析

    **参数说明:**
    - rule_description: 评分规则的文本描述，由调用方提供
    - data: SQL查询结果数据列表
    - context: 评分上下文信息，可选
    - llm_name: 指定用于评分的LLM模型，可选

    **返回值:**
    - score: 提取的数值得分（如果可以提取）
    - score_text: LLM生成的完整评分结果文本
    - analysis: 详细的评分分析过程
    - suggestions: 改进建议（可选）
    - data_summary: 数据汇总信息

    **示例:**
    ```json
    {
        "rule_description": "根据工作量计算得分：教学每学时0.1分，科研每学时0.15分，总分不超过100分",
        "data": [{"teacher_name": "张三", "teaching_hours": 240, "research_hours": 160}]
    }
    ```
    """
    try:
        tenant_id = get_user_tenant_id(db, user.id)

        # 验证请求数据
        if not request.rule_description.strip():
            return get_data_error_result(retmsg="评分规则描述不能为空")

        if not isinstance(request.data, list):
            return get_data_error_result(retmsg="data必须是列表格式")

        # 调用LLM评分服务
        score_result = llm_scorer.calculate_score(
            db=db,
            rule_description=request.rule_description,
            data=request.data,
            context=request.context,
            tenant_id=tenant_id,
            llm_name=request.llm_name
        )

        return get_json_result(data=score_result)

    except Exception as e:
        logger.error(f"Error in calculate_score: {e}")
        return server_error_response(e)


@router.post("/rag_answer", summary="RAG回答", response_description="基于知识库检索生成回答")
def rag_answer(
        request: RAGAnswerRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    基于知识库检索生成回答

    **功能描述:**
    - 在指定知识库中检索相关文档
    - 使用LLM基于检索内容生成回答
    - 返回答案和引用来源

    **参数说明:**
    - query: 用户查询
    - kb_id: 知识库ID
    - top_k: 检索文档数量，默认5
    - llm_name: 指定LLM模型，可选

    **返回值:**
    - answer: 生成的回答
    - sources: 引用来源列表
    - confidence: 回答置信度
    """
    try:
        tenant_id = get_user_tenant_id(db, user.id)

        # 验证请求数据
        if not request.query.strip():
            return get_data_error_result(retmsg="查询内容不能为空")

        if not request.kb_id:
            return get_data_error_result(retmsg="知识库ID不能为空")

        # 调用RAG服务
        rag_result = rag_service.generate_answer(
            db=db,
            query=request.query,
            kb_id=request.kb_id,
            tenant_id=tenant_id,
            top_k=request.top_k,
            llm_name=request.llm_name
        )

        return get_json_result(data=rag_result)

    except Exception as e:
        logger.error(f"Error in rag_answer: {e}")
        return server_error_response(e)


@router.get("/system_info", summary="获取系统信息", response_description="获取当前系统的配置信息")
def get_system_info(
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    获取系统信息

    **返回信息:**
    - 支持的功能列表
    - 配置参数说明
    - 使用建议
    """
    try:
        system_info = {
            "version": "2.1.0-stateless-typed",
            "features": {
                "stateless_design": True,
                "template_storage": True,
                "template_storage_v2": True,
                "typed_parameters": True,
                "hybrid_search": True,
                "multi_round_dialog": True,
                "llm_scoring": True,
                "rag_answer": True
            },
            "supported_operations": {
                "template_management": [
                    "POST /store_templates - 存储QA模板（V1兼容）",
                    "POST /store_templates_v2 - 存储支持类型化参数的QA模板",
                    "POST /clear_templates - 清空所有模板",
                    "POST /delete_template - 删除指定模板（支持单个或批量）"
                ],
                "query_interpretation": [
                    "POST /interpret_stateless - 无状态查询解释（支持多轮）",
                    "POST /quick_interpret - 快速单轮查询"
                ],
                "scoring_and_rag": [
                    "POST /calc_score - LLM评分计算",
                    "POST /rag_answer - RAG回答生成"
                ],
                "collection_management": [
                    "GET /collection_status - 检查集合状态和迁移建议"
                ]
            },
            "collection_changes": {
                "description": "V2版本对Milvus集合结构进行了调整",
                "changes": [
                    "V1集合：bl_qa_template - 原有集合结构",
                    "V2集合：bl_qa_template_v2 - 新增类型化参数支持",
                    "系统自动检测并优先使用V2集合",
                    "V1和V2集合可以共存，保证向后兼容"
                ],
                "new_fields": [
                    "needed_params_typed - 类型化参数定义（JSON）",
                    "template_version - 模板版本标记（v1/v2）"
                ],
                "migration_strategy": [
                    "现有V1集合继续工作，无需立即迁移",
                    "使用V2接口存储新模板时会自动创建V2集合",
                    "查询接口自动选择最优集合（优先V2）",
                    "使用 GET /collection_status 检查当前状态"
                ]
            },
            "v2_new_features": {
                "typed_parameters": {
                    "description": "支持为QA模板参数指定数据类型",
                    "supported_types": ["string", "integer", "float", "boolean", "date"],
                    "benefits": [
                        "LLM能更准确地输出正确类型的参数值",
                        "自动类型验证和转换",
                        "更好的参数处理精度"
                    ]
                },
                "example_v2_template": {
                    "qa_id": "teacher_query_001",
                    "question_canonical": "查询教师考核数据",
                    "paraphrases": ["教师考核情况", "老师绩效数据"],
                    "needed_params_v2": [
                        {
                            "name": "teacher_id",
                            "data_type": "string",
                            "description": "教师ID",
                            "required": True
                        },
                        {
                            "name": "year",
                            "data_type": "integer", 
                            "description": "考核年度",
                            "required": True
                        },
                        {
                            "name": "score_threshold",
                            "data_type": "float",
                            "description": "分数阈值",
                            "required": False
                        },
                        {
                            "name": "is_active",
                            "data_type": "boolean",
                            "description": "是否在职",
                            "required": False
                        },
                        {
                            "name": "start_date",
                            "data_type": "date",
                            "description": "开始日期",
                            "required": False
                        }
                    ],
                    "sql_template": [
                        "SELECT * FROM teacher_performance WHERE teacher_id = {{teacher_id}} AND year = {{year}}",
                        "SELECT teacher_id, performance_score, evaluation_date FROM teacher_performance WHERE teacher_id = {{teacher_id}} AND year = {{year}} ORDER BY evaluation_date DESC"
                    ],
                    "rule_id": "rule_001"
                }
            },
            "configuration": {
                "similarity_threshold": {
                    "default": 0.3,
                    "range": "0.0-1.0",
                    "description": "模板匹配相似度阈值"
                },
                "hybrid_weight": {
                    "default": 0.7,
                    "range": "0.0-1.0",
                    "description": "混合检索中密集向量权重"
                }
            },
            "usage_tips": [
                "使用V2模板获得更精确的参数类型控制",
                "首次查询时不传dialog_context",
                "多轮对话时传入上次返回的updated_context",
                "调用端负责保存和管理对话状态",
                "quick_interpret适用于简单单轮查询",
                "interpret_stateless支持复杂多轮对话",
                "V2模板自动向后兼容V1接口"
            ],
            "migration_guide": {
                "from_v1_to_v2": [
                    "1. 将needed_params列表转换为needed_params_v2对象列表",
                    "2. 为每个参数添加data_type字段",
                    "3. 使用/store_templates_v2接口存储模板",
                    "4. 现有的查询接口无需修改，自动支持类型化参数"
                ]
            }
        }

        return get_json_result(data=system_info)

    except Exception as e:
        logger.error(f"Error in get_system_info: {e}")
        return server_error_response(e)


@router.get("/collection_status", summary="检查集合状态", response_description="检查QA模板集合的V1/V2状态")
def check_collection_status(
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    检查QA模板集合的状态
    
    **功能:**
    - 检查V1和V2集合是否存在
    - 统计各集合的记录数量
    - 提供迁移建议
    """
    try:
        tenant_id = get_user_tenant_id(db, user.id)
        
        from api.db.services.qa_service import QA_TEMPLATE_COLLECTION
        
        v1_collection = QA_TEMPLATE_COLLECTION
        v2_collection = f"{QA_TEMPLATE_COLLECTION}_v2"
        
        # 检查集合存在性
        v1_exists = settings.docStoreConn.has_collection(v1_collection)
        v2_exists = settings.docStoreConn.has_collection(v2_collection)
        
        # 统计记录数量
        v1_count = 0
        v2_count = 0
        
        if v1_exists:
            try:
                v1_results = settings.docStoreConn.query(
                    collection_name=v1_collection,
                    filter=f'tenant_id == "{tenant_id}"',
                    output_fields=["id"]
                )
                v1_count = len(v1_results) if v1_results else 0
            except Exception as e:
                logger.warning(f"无法统计V1集合记录数: {e}")
        
        if v2_exists:
            try:
                v2_results = settings.docStoreConn.query(
                    collection_name=v2_collection,
                    filter=f'tenant_id == "{tenant_id}"',
                    output_fields=["id"]
                )
                v2_count = len(v2_results) if v2_results else 0
            except Exception as e:
                logger.warning(f"无法统计V2集合记录数: {e}")
        
        # 确定当前活跃的集合
        current_active = None
        collection_version = None
        if v2_exists and v2_count > 0:
            current_active = v2_collection
            collection_version = "v2"
        elif v1_exists and v1_count > 0:
            current_active = v1_collection
            collection_version = "v1"
        
        # 判断是否需要迁移
        needs_migration = v1_exists and v1_count > 0 and (not v2_exists or v2_count == 0)
        
        # 生成迁移建议
        suggestions = []
        if not v1_exists and not v2_exists:
            suggestions.append("当前没有任何QA模板集合，建议使用V2接口创建新模板")
        elif v1_exists and v1_count > 0 and not v2_exists:
            suggestions.append("检测到V1集合有数据，建议使用V2接口重新存储模板以获得类型化参数支持")
        elif v1_exists and v2_exists and v1_count > 0 and v2_count == 0:
            suggestions.append("V1和V2集合都存在，但V2集合为空，建议使用V2接口存储新模板")
        elif v2_exists and v2_count > 0:
            suggestions.append("V2集合已有数据，系统将优先使用V2集合，享受类型化参数功能")
        
        if v1_exists and v1_count > 0:
            suggestions.append("V1集合数据仍然兼容，查询接口可以正常使用")
        
        result = CollectionStatusResponse(
            v1_collection_exists=v1_exists,
            v2_collection_exists=v2_exists,
            v1_collection_name=v1_collection,
            v2_collection_name=v2_collection,
            current_active_collection=current_active,
            collection_version=collection_version,
            v1_record_count=v1_count,
            v2_record_count=v2_count,
            needs_migration=needs_migration,
            migration_suggestions=suggestions
        )
        
        return get_json_result(data=result.model_dump())
        
    except Exception as e:
        logger.error(f"Error checking collection status: {e}")
        return server_error_response(e)
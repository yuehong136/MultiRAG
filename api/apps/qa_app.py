"""
教师科研考核问答系统 - API接口
包含所有Pydantic Schema定义和接口实现
"""
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

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

class QATemplate(BaseModel):
    """QA模板数据结构 - 保持不变"""
    qa_id: str = Field(..., description="唯一标识符")
    question_canonical: str = Field(..., description="标准问法")
    paraphrases: list[str] = Field(default=[], description="同义句列表")
    needed_params: list[str] = Field(..., description="需要的参数列表")
    sql_template: str = Field(..., description="SQL模板，使用命名参数")
    rule_id: str | None = Field(None, description="评分规则ID，可为空")

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
    sql_template: str | None = Field(None, description="SQL模板")
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
#
# @router.post("/interpret", summary="查询解释", response_description="解释用户查询并返回SQL模板或追问")
# def interpret_query(
#         request: InterpretRequest,
#         db: Session = Depends(get_db),
#         user=Depends(manager)
# ):
#     """
#     主要查询解释接口，从Milvus中匹配模板并进行槽位抽取
#
#     **功能流程:**
#     1. 使用混合检索（密集向量+BM25）从Milvus中找到最匹配的QA模板
#     2. 使用LLM从用户查询中抽取所需参数
#     3. 如果参数不完整，返回追问文案
#     4. 如果参数完整，返回SQL模板、参数和rule_id
#
#     **参数说明:**
#     - user_query: 用户查询文本
#     - table_schemas: 数据库表结构信息，用于槽位抽取
#     - system_date: 系统当前日期，用于时间推理
#     - similarity_threshold: 模板匹配相似度阈值
#     - hybrid_weight: 混合检索中密集向量的权重（0-1），稀疏向量权重为1-hybrid_weight
#
#     **返回值:**
#     - 成功：status=OK, sql_template, params, rule_id
#     - 需要追问：status=NEED_CLARIFY, missing_params, clarify_message
#     - 失败：status=ERROR, message
#
#     **注意:**
#     - 使用前需要先调用 /store_templates 存储QA模板
#     - 混合检索结合了语义相似度和关键词匹配，提高匹配准确性
#     - 支持自动时间推理和实体转换
#     """
#     try:
#         tenant_id = get_user_tenant_id(db, user.id)
#
#         # 参数验证
#         if not request.user_query.strip():
#             return get_data_error_result(retmsg="用户查询不能为空")
#
#         # 设置默认系统日期
#         if not request.system_date:
#             request.system_date = datetime.now().strftime("%Y-%m-%d")
#
#         # 1. 从Milvus检索匹配的模板
#         best_match = template_matcher.find_best_template(
#             db=db,
#             user_query=request.user_query,
#             tenant_id=tenant_id,
#             threshold=request.similarity_threshold,
#             hybrid_weight=request.hybrid_weight
#         )
#
#         if not best_match:
#             return get_json_result(data=InterpretResponse(
#                 status="ERROR",
#                 message="未找到匹配的QA模板，请检查查询内容或先上传相关模板",
#                 confidence=0.0
#             ).model_dump())
#
#         # 2. 槽位抽取
#         table_schemas_dict = [schema.model_dump() for schema in request.table_schemas]
#
#         slot_result = slot_extractor.extract_slots(
#             db=db,
#             user_query=request.user_query,
#             needed_params=best_match['needed_params'],
#             table_schemas=table_schemas_dict,
#             system_date=request.system_date,
#             tenant_id=tenant_id,
#             llm_name=request.llm_name
#         )
#
#         # 3. 检查是否需要追问
#         if slot_result['missing_params']:
#             clarify_message = clarification_generator.generate_clarification(
#                 db=db,
#                 user_query=request.user_query,
#                 missing_params=slot_result['missing_params'],
#                 table_schemas=table_schemas_dict,
#                 tenant_id=tenant_id,
#                 llm_name=request.llm_name
#             )
#
#             return get_json_result(data=InterpretResponse(
#                 status="NEED_CLARIFY",
#                 qa_id=best_match['qa_id'],
#                 missing_params=slot_result['missing_params'],
#                 sql_template=best_match['sql_template'],
#                 params=slot_result['extracted_params'],
#                 clarify_message=clarify_message,
#                 rule_id=best_match.get('rule_id'),  # 可能为空
#                 confidence=slot_result['confidence']
#             ).model_dump())
#
#         # 4. 返回完整结果
#         return get_json_result(data=InterpretResponse(
#             status="OK",
#             qa_id=best_match['qa_id'],
#             sql_template=best_match['sql_template'],
#             params=slot_result['extracted_params'],
#             rule_id=best_match.get('rule_id'),  # 可能为空
#             confidence=slot_result['confidence']
#         ).model_dump())
#
#     except Exception as e:
#         logger.error(f"Error in interpret_query: {e}")
#         return server_error_response(e)


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
            "version": "2.0.0-stateless",
            "features": {
                "stateless_design": True,
                "template_storage": True,
                "hybrid_search": True,
                "multi_round_dialog": True,
                "llm_scoring": True,
                "rag_answer": True
            },
            "supported_operations": {
                "template_management": [
                    "POST /store_templates - 存储QA模板",
                    "POST /clear_templates - 清空模板"
                ],
                "query_interpretation": [
                    "POST /interpret_stateless - 无状态查询解释（支持多轮）",
                    "POST /quick_interpret - 快速单轮查询"
                ],
                "scoring_and_rag": [
                    "POST /calc_score - LLM评分计算",
                    "POST /rag_answer - RAG回答生成"
                ]
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
                "首次查询时不传dialog_context",
                "多轮对话时传入上次返回的updated_context",
                "调用端负责保存和管理对话状态",
                "quick_interpret适用于简单单轮查询",
                "interpret_stateless支持复杂多轮对话"
            ]
        }

        return get_json_result(data=system_info)

    except Exception as e:
        logger.error(f"Error in get_system_info: {e}")
        return server_error_response(e)
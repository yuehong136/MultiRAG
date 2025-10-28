import logging
from enum import Enum
from typing import List, Any, Dict, Optional

from fastapi import APIRouter, Depends, Body, BackgroundTasks, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from fastapi.responses import StreamingResponse

from api.db.db_models import get_db
from api.apps import manager
from api.service.askdata_service.askdata_service import AskdataService, get_askdata_service
from api.service.askdata_service.event.event_handlers import create_sse_response
from api.service.askdata_service.util.sql_retry_handler import SQLRetryHandler
from api.service.askdata_service.stop_request_manager import stop_request_manager
from api.service.nl2sql_service.query_data_from_zt_by_sql import query_data_with_params

router = APIRouter()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class StatusEnum(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class ResponseSchema(BaseModel):
    status: StatusEnum = StatusEnum.SUCCESS
    message: str | None = None
    data: Any | None = None


class GetSqlAndTableConfigReq(BaseModel):
    """自然语言转初始SQL请求的基础模型"""
    user_query: str = Field(..., title="查询文本", description="用户提出的自然语言查询文本")
    dataset_id_list: List[str] = Field([], title="数据集ID列表", description="数据集ID列表")
    llm_name: str = Field("gpt-4", title="LLM模型名称", description="用于将自然语言转换为SQL的LLM模型名称")
    conversation_id: str = Field(None, title="conversation_id", description="conversation_id")
    ask_id: str = Field(None, title="ask_id", description="用户的提问ID")
    semantic_layer: Dict[str, Any] = Field({}, title="语义层", description="语义层")


@router.post("/get-sql-and-table-config", response_model=ResponseSchema)
async def get_sql_and_table_config(
        body: GetSqlAndTableConfigReq = Body(...),
        db: Session = Depends(get_db),
        user=Depends(manager),
        service: AskdataService = Depends(get_askdata_service)
):
    logging.info(f"get-sql-and-table-config请求体：{body.model_dump_json()}")

    try:
        # 1. 调用Service层获取包含SQL及其组件的完整结果
        sql_generation_result = await service.nlq_to_initial_sql(
            user_query=body.user_query,
            llm_name=body.llm_name,
            semantic_layer=body.semantic_layer.get('processed_semantic_layer', {}),
            recommended_chart=body.semantic_layer.get('recommended_chart'),
            ask_id=body.ask_id
        )

        if not sql_generation_result:
            logger.error("未能从Service层获取有效的SQL生成结果。")
            return ResponseSchema(
                status=StatusEnum.ERROR,
                message="SQL生成失败或LLM返回格式不正确"
            )

        if sql_generation_result.get("status") == "failed":
            return ResponseSchema(
                # 虽然无法生成SQL，但这里要返回成功的状态，因为中台接口只有在收到成功的状态才能将data返回给前端。
                status=StatusEnum.SUCCESS,
                message=f"SQL生成失败: {sql_generation_result.get('errorMessage')}",
                data={
                    "status": StatusEnum.ERROR,
                    "message": sql_generation_result.get("errorMessage"),
                }
            )

        if not sql_generation_result.get("sql"):
            logger.error("未能从Service层获取有效的SQL生成结果。")
            return ResponseSchema(
                status=StatusEnum.ERROR,
                message="SQL生成失败或LLM返回格式不正确"
            )

        sql = sql_generation_result["sql"]
        used_models = sql_generation_result["usedModels"]
        query_complexity = sql_generation_result["queryComplexity"]

        # 构建使用到的模型和表的详情字典
        used_model_detail_dict, used_table_detail_dict, model_list, intersection_dataset_ids = \
            await service.get_model_details_and_determine_dataset(
                model_ids=body.semantic_layer.get('model_ids', []),
                used_models=used_models,
                dataset_id_list=body.dataset_id_list
            )

        # 检查是否在构建模型详情后被停止（如果未被停止异常捕获）
        if body.ask_id:
            try:
                service._check_if_stopped(body.ask_id)
            except Exception as e:
                if "已被用户停止" in str(e):
                    return ResponseSchema(
                        # 虽然无法生成SQL，但这里要返回成功的状态，因为中台接口只有在收到成功的状态才能将data返回给前端。
                        status=StatusEnum.SUCCESS,
                        message=f"SQL生成失败: {str(e)}",
                        data={
                            "status": StatusEnum.ERROR,
                            "message": str(e),
                        }
                    )

        # 确定数据集ID
        if not intersection_dataset_ids:
            logger.error("无法确定数据集ID")
            return ResponseSchema(
                status=StatusEnum.ERROR,
                message="无法确定查询的数据集"
            )
        dataset_id = list(intersection_dataset_ids)[0]

        # ========== 使用 SQLRetryHandler 执行和修复 ==========

        # 配置重试次数（可以从配置文件或环境变量读取）
        MAX_RETRY_TIMES = 3  # 或者 int(os.getenv("SQL_MAX_RETRY_TIMES", "3"))

        # 创建重试处理器
        retry_handler = SQLRetryHandler(max_retries=MAX_RETRY_TIMES)

        # 准备修复函数需要的参数
        fix_params = {
            "user_query": body.user_query,
            "semantic_layer": body.semantic_layer.get('processed_semantic_layer', {}),
            "llm_name": body.llm_name,
            "used_models": used_models
        }

        # 执行SQL with retry
        execution_result = await retry_handler.execute_with_retry(
            execute_func=query_data_with_params,
            fix_func=service.fix_sql_query_with_components,
            sql=sql,
            dataset_id=dataset_id,
            fix_params=fix_params
        )

        # 处理执行结果
        if execution_result["status"] == "error":
            logger.error(f"查询失败（尝试{execution_result['retry_times'] + 1}次）: {execution_result['message']}")
            return ResponseSchema(
                status=StatusEnum.ERROR,
                message=f"查询数据失败（尝试{execution_result['retry_times'] + 1}次）: {execution_result['message']}"
            )

        sql = execution_result["final_sql"]
        result_data = execution_result["data"]
        data_count = len(result_data.get('data', []))

        # 如果SQL被修复过且used_models发生变化，重新构建模型详情
        if execution_result.get("was_fixed") and execution_result.get("used_models"):
            final_used_models = execution_result["used_models"]
            if final_used_models != used_models:
                logger.info("修复后的SQL使用了不同的模型，重新构建模型详情")
                used_model_detail_dict, used_table_detail_dict, model_list, new_intersection_dataset_ids = \
                    await service.get_model_details_and_determine_dataset(
                        model_ids=body.semantic_layer.get('model_ids', []),
                        used_models=final_used_models,
                        dataset_id_list=body.dataset_id_list
                    )

                # 检查新的数据集ID
                if not new_intersection_dataset_ids:
                    logger.error("修复后无法确定数据集ID")
                    return ResponseSchema(
                        status=StatusEnum.ERROR,
                        message="修复后无法确定查询的数据集"
                    )
                dataset_id = list(new_intersection_dataset_ids)[0]
                used_models = final_used_models

        # 复杂查询，直接返回结果
        if query_complexity == "complex":
            logger.info(f"当前SQL查询复杂度为：{query_complexity}")
            response_data = {
                "sql": sql,
                "query_complexity": query_complexity,
                "result": result_data,
                "was_fixed": execution_result.get("was_fixed", False),
                "retry_times": execution_result.get("retry_times", 0),
                "execution_history": execution_result.get("execution_history", [])  # 可选：返回执行历史
            }
            return ResponseSchema(
                status=StatusEnum.SUCCESS,
                message="生成初始SQL及配置成功",
                data=response_data
            )

        page_size = 20
        pagination_sql = None
        pagination_info = None
        # 数据量过大就需要进行分页
        if data_count > page_size:
            # 生成分页的sql
            pagination_sql = await service.sql_pagination_converter.convert_to_pagination(sql,
                                                                                          database_type="PostgreSQL",
                                                                                          llm_name=body.llm_name)
            sql_components = await service.sql_components_extractor.extract_sql_components(pagination_sql,
                                                                                           body.llm_name)
            pass
        else:
            sql_components = await service.sql_components_extractor.extract_sql_components(sql, body.llm_name)
            pass

        if pagination_sql:
            result = await query_data_with_params(pagination_sql, int(dataset_id), [])
            result_data = result["data"]
            pagination_info = {
                "data_count": data_count,
                "page_size": page_size,
                "page_count": (data_count + page_size - 1) // page_size
            }

        # 检查是否在SQL执行完成后被停止
        if body.ask_id:
            try:
                service._check_if_stopped(body.ask_id)
            except Exception as e:
                if "已被用户停止" in str(e):
                    return ResponseSchema(
                        # 虽然无法生成SQL，但这里要返回成功的状态，因为中台接口只有在收到成功的状态才能将data返回给前端。
                        status=StatusEnum.SUCCESS,
                        message=f"SQL生成失败: {str(e)}",
                        data={
                            "status": StatusEnum.ERROR,
                            "message": str(e),
                        }
                    )

        # 2. 生成表格配置
        model_table_alias_mapping_list, table_config = await service.generate_table_config(
            used_table_detail_dict=used_table_detail_dict,
            model_list=model_list,
            sql_components=sql_components,
            recommended_chart=body.semantic_layer.get('recommended_chart')
        )

        logger.info(f"model_table_alias_mapping_list:{model_table_alias_mapping_list}")

        # 4. 构建返回给前端的数据结构
        response_data = {
            "sql": sql,
            "pagination_sql": pagination_sql,
            "query_complexity": query_complexity,
            "result": result_data,
            "table_config": table_config,
            "sql_components": sql_components,
            "model_table_alias_mapping_list": model_table_alias_mapping_list,
            "dataset_id": dataset_id,
            "was_fixed": execution_result.get("was_fixed", False),
            "retry_times": execution_result.get("retry_times", 0),
            "pagination_info": pagination_info,
            # "execution_history": execution_result.get("execution_history", [])  # 可选
        }

        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="生成初始SQL及配置成功",
            data=response_data
        )

    except Exception as e:
        logger.exception("get-sql-and-table-config 发生异常")
        return ResponseSchema(
            # 虽然无法生成SQL，但这里要返回成功的状态，因为中台接口只有在收到成功的状态才能将data返回给前端。
            status=StatusEnum.SUCCESS,
            message=f"SQL生成失败: {str(e)}",
            data={
                "status": StatusEnum.ERROR,
                "message": str(e),
            }
        )


@router.get("/events/{event_id}")
async def subscribe_to_event(request: Request, event_id: str):
    """
    订阅指定事件ID的SSE端点

    Args:
        request: FastAPI请求对象
        event_id: 事件ID

    Returns:
        StreamingResponse: SSE流响应
    """
    return create_sse_response(request, event_id)


class AnalyzeUserQueryRequest(BaseModel):
    conversation_id: str = Field(..., description="会话ID")
    ask_id: str = Field(..., description="用户的提问ID")
    user_query: str = Field(..., description="用户查询")
    llm_name: Optional[str] = Field(default=None, description="指定使用的模型名称")
    dataset_id_list: List[str] = Field([], title="数据集ID列表", description="数据集ID列表")
    semantic_layer: Dict[str, Any] = Field(..., title="语义层", description="语义层")
    round_id: Optional[str] = Field(None, title="多轮对话的分组ID",
                                    description="如果是多轮对话，那么同一组对话所享有的ID")
    rewritten_question: Optional[str] = Field(None, title="重写的问题", description="重写的问题")


class AnalyzeUserQueryResponse(BaseModel):
    event_id: str = Field(..., description="事件ID，用于监听流式输出")
    subscribe_url: str = Field(..., description="SSE订阅地址")
    chat_status: str = Field(default="started", description="聊天状态")


async def analyze_user_query_background_task(
        event_id: str,
        request: AnalyzeUserQueryRequest,
        db: Session,
        user
) -> None:
    """
    通过将逻辑委托给服务层来处理流式聊天的后台任务。
    """
    try:
        service = get_askdata_service(db, user)

        await service.analyze_user_query_stream(
            event_id=event_id,
            user_query=request.user_query,
            rewritten_question=request.rewritten_question,
            semantic_layer=request.semantic_layer["processed_semantic_layer"],
            round_id=request.round_id,
            llm_name=request.llm_name,
            tenant_id=user.id,
            recommended_chart=request.semantic_layer["recommended_chart"],
            recommendation_reason=request.semantic_layer["recommendation_reason"],
            ask_id=request.ask_id
        )

    except Exception as e:
        logger.exception(f"后台聊天任务失败，event_id {event_id}: {e}")
        # 错误报告逻辑保留在此处，因为它是一个横切关注点（发布到事件管理器）
        try:
            from api.service.nl2sql_service.event.event_manager import event_manager
            await event_manager.publish(
                event_id=event_id,
                data={
                    "message": f"后台任务失败: {str(e)}",
                    "error": str(e),
                    "status": "task_error"
                },
                event_type="chat_error"
            )
        except Exception as publish_error:
            logger.error(f"为 {event_id} 发送错误事件失败: {publish_error}")


@router.post("/analyze-user-query-streaming/{custom_event_id}", response_model=ResponseSchema,
             summary="使用自定义事件ID启动流式聊天")
async def analyze_user_query_streaming(
        custom_event_id: str,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
        user=Depends(manager),
        body: AnalyzeUserQueryRequest = Body(
            ...,
            title="流式输出对于用户问题的分析",
            description="流式输出对于用户问题的分析"
        )
) -> ResponseSchema:
    """
    使用自定义事件ID启动流式聊天
    """
    logger.info(f"使用自定义事件ID {custom_event_id} 启动流式聊天：{body}")

    try:
        # 添加后台任务，调用重构后的后台任务函数
        background_tasks.add_task(
            analyze_user_query_background_task,
            custom_event_id,
            body,
            db,
            user
        )

        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="聊天请求已提交，请监听事件流获取实时结果",
            data=AnalyzeUserQueryResponse(
                event_id=custom_event_id,
                subscribe_url=f"/events/{custom_event_id}",
                chat_status="started"
            )
        )

    except Exception as e:
        logger.exception("使用自定义事件ID启动流式聊天失败")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"启动聊天失败：{str(e)}"
        )


class SemanticLayerRequest(BaseModel):
    conversation_id: str = Field(..., description="会话ID")
    ask_id: str = Field(..., description="用户的提问ID")
    user_query: str = Field(..., description="用户查询")
    llm_name: str = Field("", title="LLM模型名称", description="")
    dataset_id_list: List[str] = Field(
        [],
        title="数据集ID列表",
        description="数据集ID列表",
    )
    # 启用深度搜索，会对使用分词去高基数维度进行探查
    enable_deep_search: bool = Field(
        False,
        title="启用深度搜索",
        description="是否启用深度搜索功能"
    )
    # userid，后续需要去获取该用户语义层的权限。
    userid: str = Field(
        "",
        title="用户ID",
        description="用户ID，后续需要去获取该用户语义层的权限。"
    )
    enable_multi_rounds_question: bool = Field(
        False,
        title="启用多轮问答",
        description="是否启用多轮问答功能"
    )
    round_id: Optional[str] = Field(
        "",
        title="多轮对话的分组ID",
        description="如果是多轮对话，那么同一组对话所享有的ID"
    )


@router.post("/get-semantic-layer-streaming/{custom_event_id}", response_model=ResponseSchema,
             summary="获得语义层信息")
async def get_semantic_layer_streaming(
        custom_event_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager),
        body: SemanticLayerRequest = Body(
            ...,
            title="获得语义层信息",
            description="获得语义层信息"
        ),
        service: AskdataService = Depends(get_askdata_service)
) -> ResponseSchema:
    logger.info(f"使用自定义事件ID {custom_event_id} 获得语义层信息，参数：{body}")

    original_user_query = body.user_query
    final_user_query = original_user_query
    llm_rewritten_question = None

    if body.enable_multi_rounds_question:
        # 将历史提问改写逻辑下沉到Service，保持路由层简洁
        final_user_query, llm_rewritten_question = await service.prepare_user_query_for_semantic_layer(
            round_id=body.round_id,
            original_user_query=original_user_query,
            llm_name=body.llm_name
        )

    try:
        processed_semantic_layer, model_ids, recommended_chart, recommendation_reason = await service.generate_semantic_layer(
            user_query=final_user_query,
            dataset_id_list=body.dataset_id_list,
            userid=body.userid,
            event_id=custom_event_id,
            enable_deep_search=body.enable_deep_search,
            llm_name=body.llm_name,
            ask_id=body.ask_id)

        logger.info(f"processed_semantic_layer:{processed_semantic_layer}")
        logger.info(f"model_ids:{model_ids}")
        logger.info(f"recommended_chart:{recommended_chart}")
        logger.info(f"recommendation_reason:{recommendation_reason}")

        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="获得语义层信息成功",
            data={"processed_semantic_layer": processed_semantic_layer,
                  "model_ids": model_ids,
                  "rewritten_question": llm_rewritten_question,
                  "recommended_chart": recommended_chart,
                  "recommendation_reason": recommendation_reason}
        )

    except Exception as e:
        logger.exception("获得语义层信息失败")
        return ResponseSchema(
            # 虽然无法生成SQL，但这里要返回成功的状态，因为中台接口只有在收到成功的状态才能将data返回给前端。
            status=StatusEnum.SUCCESS,
            message=f"获得语义层信息失败：{str(e)}",
            data={
                "status": StatusEnum.ERROR,
                "message": str(e),
            }
        )


class AddHistoryRequest(BaseModel):
    conversation_id: str = Field(..., description="会话ID")
    ask_id: str = Field(..., description="询问ID")
    userid: str = Field(..., description="用户ID")
    data: str = Field(..., description="历史记录内容, 可以是JSON字符串")
    round_id: str = Field("", description="对话轮次ID")
    user_origin_question: str = Field("", description="用户原始问题")
    rewritten_question: Optional[str] = Field("", description="重写后的问题")
    processed_semantic_layer: str = Field("", description="处理后的语义层")


@router.post("/add-history", response_model=ResponseSchema, summary="新增历史记录")
async def add_history(
        body: AddHistoryRequest,
        service: AskdataService = Depends(get_askdata_service)
):
    """
    新增一条问数历史记录。
    """
    try:
        result = await service.add_ask_data_history(
            conversation_id=body.conversation_id,
            ask_id=body.ask_id,
            user_id=body.userid,
            data=body.data,
            round_id=body.round_id,
            user_origin_question=body.user_origin_question,
            rewritten_question=body.rewritten_question,
            processed_semantic_layer=body.processed_semantic_layer
        )
        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="历史记录添加成功",
            data={"history_id": result.id}
        )
    except Exception as e:
        logger.exception("新增历史记录失败")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"新增历史记录失败: {e}"
        )


class GetHistoryRequest(BaseModel):
    userid: str = Field(..., description="用户ID")


@router.post("/get-history/{conversation_id}", response_model=ResponseSchema, summary="查询历史记录")
async def get_history(
        conversation_id: str,
        body: GetHistoryRequest = Body(
            ...,
            title="查询历史记录"
        ),
        service: AskdataService = Depends(get_askdata_service)
):
    """
    根据对话ID查询相关的历史记录。
    """
    try:
        history_records = await service.get_ask_data_history(conversation_id, body.userid)
        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="历史记录查询成功",
            data=history_records
        )
    except Exception as e:
        logger.exception(f"查询历史记录失败 (conversation_id: {conversation_id})")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"查询历史记录失败: {e}"
        )


class DeleteHistoryByConversationRequest(BaseModel):
    conversation_id: str = Field(..., description="对话ID")
    userid: str = Field(..., description="用户ID")


@router.post("/delete-history/conversation", response_model=ResponseSchema, summary="根据对话ID删除历史记录")
async def delete_history_by_conversation(
        body: DeleteHistoryByConversationRequest = Body(
            ...,
            title="根据对话ID删除历史记录"
        ),
        service: AskdataService = Depends(get_askdata_service)
):
    """
    根据对话ID和用户ID删除相关的历史记录（软删除）。
    """
    try:
        deleted_count = await service.delete_ask_data_history_by_conversation(body.conversation_id, body.userid)
        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message=f"历史记录删除成功，共删除 {deleted_count} 条记录",
            data={"deleted_count": deleted_count}
        )
    except Exception as e:
        logger.exception(f"删除历史记录失败 (conversation_id: {body.conversation_id})")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"删除历史记录失败: {e}"
        )


class DeleteHistoryByAskIdRequest(BaseModel):
    ask_id: str = Field(..., description="问题ID")
    userid: str = Field(..., description="用户ID")


@router.post("/delete-history/ask", response_model=ResponseSchema, summary="根据询问ID删除历史记录")
async def delete_history_by_ask_id(
        body: DeleteHistoryByAskIdRequest = Body(
            ...,
            title="根据询问ID删除历史记录"
        ),
        service: AskdataService = Depends(get_askdata_service)
):
    """
    根据询问ID和用户ID删除相关的历史记录（软删除）。
    """
    try:
        deleted_count = await service.delete_ask_data_history_by_ask_id(body.ask_id, body.userid)
        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message=f"历史记录删除成功，共删除 {deleted_count} 条记录",
            data={"deleted_count": deleted_count}
        )
    except Exception as e:
        logger.exception(f"删除历史记录失败 (ask_id: {body.ask_id})")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"删除历史记录失败: {e}"
        )


class ReQueryRequest(BaseModel):
    conversation_id: str = Field(..., description="会话ID")
    ask_id: str = Field(..., description="用户的提问ID")
    chart_type: str = Field(..., description="图表类型")
    table_config: Dict[str, Any] = Field(..., description="表配置")
    sql_components: Dict[str, Any] = Field(..., description="SQL组件")
    model_table_alias_mapping_list: List[Dict[str, Any]] = Field(..., description="模型表别名映射列表")
    dataset_id: str = Field(..., description="数据集ID")
    pagination_info: Optional[Dict[str, Any]] = Field(None, description="分页信息")

    class Config:
        protected_namespaces = ()


@router.post("/re-query", response_model=ResponseSchema,
             summary="在前端调整后的查询")
async def re_query(
        db: Session = Depends(get_db),
        user=Depends(manager),
        body: ReQueryRequest = Body(
            ...,
            title="获得语义层信息",
            description="获得语义层信息"
        ),
        service: AskdataService = Depends(get_askdata_service)
) -> ResponseSchema:
    logger.info(
        f"re-query chart_type: {body.chart_type}\n table_config: {body.table_config} \n sql_components: {body.sql_components} \n model_table_alias_mapping_list: {body.model_table_alias_mapping_list}")

    try:
        requery_sql_result = await service.generate_requery_sql(body.chart_type, body.table_config,
                                                                sql_components=body.sql_components,
                                                                model_table_alias_mapping_list=body.model_table_alias_mapping_list,
                                                                pagination_info=body.pagination_info)
        logger.info("re-query sql result: {}".format(requery_sql_result))

        if body.pagination_info:
            result = await query_data_with_params(requery_sql_result["count_sql"], int(body.dataset_id),
                                                  requery_sql_result["count_sql_params"])
            if result["status"] == "error":
                logger.error(f"查询数据失败: {result['message']}")
                return ResponseSchema(
                    status=StatusEnum.ERROR,
                    message=f"查询数据失败: {result['message']}"
                )
            count = result.get("data", {}).get("data", [])[0].get("count", {})
            result = await query_data_with_params(requery_sql_result["sql"], int(body.dataset_id),
                                                  requery_sql_result["params"])
            if result["status"] == "error":
                logger.error(f"查询数据失败: {result['message']}")
                return ResponseSchema(
                    status=StatusEnum.ERROR,
                    message=f"查询数据失败: {result['message']}"
                )
            return ResponseSchema(
                status=StatusEnum.SUCCESS,
                message="生成re-query SQL成功",
                data={"result": result["data"], "pagination_info": {
                    "page_size": body.pagination_info["page_size"],
                    "page_index": body.pagination_info["page_index"],
                    "data_count": count
                }}
            )
        else:
            result = await query_data_with_params(requery_sql_result["sql"], int(body.dataset_id),
                                                  requery_sql_result["params"])
            if result["status"] == "error":
                logger.error(f"查询数据失败: {result['message']}")
                return ResponseSchema(
                    status=StatusEnum.ERROR,
                    message=f"查询数据失败: {result['message']}"
                )

            return ResponseSchema(
                status=StatusEnum.SUCCESS,
                message="生成re-query SQL成功",
                data={"result": result["data"]}
            )

    except Exception as e:
        logger.exception("生成re-query SQL失败")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"生成re-query SQL失败：{str(e)}"
        )


class GetHCDimValuesByDimValueRequest(BaseModel):
    """获取高基数维度值请求模型"""
    keyword: str = Field(..., title="搜索关键词", description="用于搜索维度值的关键词")
    dimension_id: str = Field(..., title="维度ID", description="高基数维度的ID")
    page_index: int = Field(1, title="页码", description="页码，从1开始", ge=1)
    page_size: int = Field(20, title="页面大小", description="每页返回的记录数", ge=1, le=1000)
    fuzzy_match: Optional[bool] = Field(True, title="模糊匹配", description="是否启用模糊匹配")
    userid: str = Field(
        "",
        title="用户ID",
        description="用户ID，后续需要去获取该用户语义层的权限。"
    )


@router.post("/get-hc-dim-values-by-dim-value", response_model=ResponseSchema,
             summary="根据关键词在高基数维度中搜索维度值")
async def get_hc_dim_values_by_dim_value(
        body: GetHCDimValuesByDimValueRequest = Body(
            ...,
            title="高基数维度值搜索请求",
            description="根据关键词在指定的高基数维度中搜索匹配的维度值，支持指定页码和每页数量"
        ),
        db: Session = Depends(get_db),
        user=Depends(manager),
        service: AskdataService = Depends(get_askdata_service)
) -> ResponseSchema:
    """
    根据关键词在高基数维度中搜索维度值

    通过提供关键词和维度ID，在指定的高基数维度中搜索匹配的维度值。
    支持模糊匹配和分页查询，只返回指定页面的数据。
    """
    logger.info(f"收到高基数维度值搜索请求: {body}")

    try:
        # 参数验证
        if not body.keyword.strip():
            return ResponseSchema(
                status=StatusEnum.ERROR,
                message="搜索关键词不能为空"
            )

        if not body.dimension_id.strip():
            return ResponseSchema(
                status=StatusEnum.ERROR,
                message="维度ID不能为空"
            )

        # 调用Service层方法
        result = await service.get_hc_dim_values_by_dim_value(
            keyword=body.keyword.strip(),
            dimension_id=body.dimension_id.strip(),
            page_index=body.page_index,
            page_size=body.page_size,
            fuzzy_match=body.fuzzy_match,
            user_id=body.userid
        )

        # 解析返回结果
        data_info = result.get("data", {})
        dimension_values = data_info.get("data", [])
        total = int(data_info.get("total", 0))
        sql = data_info.get("sql", "")

        logger.info(f"成功获取到第{body.page_index}页的 {len(dimension_values)} 条高基数维度值，总计 {total} 条")

        # 计算分页信息
        total_pages = (total + body.page_size - 1) // body.page_size if total > 0 else 0
        has_next_page = body.page_index < total_pages
        has_prev_page = body.page_index > 1

        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message=f"成功获取到第{body.page_index}页的 {len(dimension_values)} 条高基数维度值",
            data={
                "dimension_values": dimension_values,
                "pagination": {
                    "current_page": body.page_index,
                    "page_size": body.page_size,
                    "total_count": total,
                    "total_pages": total_pages,
                    "has_next_page": has_next_page,
                    "has_prev_page": has_prev_page
                },
                "dimension_id": body.dimension_id,
                "search_keyword": body.keyword,
                "fuzzy_match": body.fuzzy_match,
                "sql": sql
            }
        )

    except Exception as e:
        logger.exception("获取高基数维度值失败")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"获取高基数维度值失败: {str(e)}"
        )


class GenerateWideTableSqlReq(BaseModel):
    """自然语言转初始SQL请求的基础模型"""
    user_query: str = Field(..., title="查询文本", description="用户提出的自然语言查询文本")
    dataset_id: str = Field(..., title="数据集ID", description="数据集ID")
    llm_name: str = Field("gpt-4", title="LLM模型名称", description="用于将自然语言转换为SQL的LLM模型名称")
    conversation_id: str = Field(None, title="conversation_id", description="conversation_id")
    ask_id: str = Field(None, title="ask_id", description="用户的提问ID")
    userid: str = Field(
        "",
        title="用户ID",
        description="用户ID，后续需要去获取该用户语义层的权限。"
    )


@router.post("/generate-wide-table-sql", response_model=ResponseSchema)
async def generate_wide_table_sql(
        body: GenerateWideTableSqlReq = Body(
            ...
        ),
        db: Session = Depends(get_db), user=Depends(manager),
        service: AskdataService = Depends(get_askdata_service)
):
    try:
        # 调用service生成宽表SQL
        sql = await service.generate_widetable_sql(
            dataset_id=body.dataset_id,
            user_id=body.userid)

        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="生成初始SQL及配置成功",
            data={"sql": sql}
        )

    except Exception as e:
        logger.exception("generate-wide-table-sql 发生异常")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"处理请求失败：{str(e)}"
        )


@router.post("/stop-request/{ask_id}", response_model=ResponseSchema, summary="停止请求处理")
async def stop_request(ask_id: str) -> ResponseSchema:
    """
    停止指定ask_id的请求处理

    Args:
        ask_id: 要停止的请求ID

    Returns:
        ResponseSchema: 停止操作的结果
    """
    logger.info(f"收到停止请求，ask_id: {ask_id}")

    if not ask_id or not ask_id.strip():
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message="ask_id不能为空"
        )

    try:
        # 调用停止请求管理器停止请求
        success = stop_request_manager.stop_request(ask_id.strip())

        if success:
            logger.info(f"请求 {ask_id} 已成功标记为停止")
            return ResponseSchema(
                status=StatusEnum.SUCCESS,
                message=f"请求 {ask_id} 已停止",
                data={
                    "ask_id": ask_id,
                    "stopped": True,
                    "timestamp": None  # 可以添加时间戳如果需要
                }
            )
        else:
            logger.warning(f"停止请求 {ask_id} 失败")
            return ResponseSchema(
                status=StatusEnum.ERROR,
                message=f"停止请求 {ask_id} 失败"
            )

    except Exception as e:
        logger.exception(f"停止请求 {ask_id} 时发生异常")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"停止请求失败：{str(e)}"
        )
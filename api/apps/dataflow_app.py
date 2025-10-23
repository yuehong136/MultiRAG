#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
from __future__ import annotations

import json
import re
import sys
import time
from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, File, Query, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.component import LLM
from api.apps import manager
from api.db import CanvasCategory, FileType
from api.db.db_models import get_db
from api.db.services.canvas_service import CanvasTemplateService, UserCanvasService
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from api.db.services.task_service import queue_dataflow
from api.db.services.user_canvas_version import UserCanvasVersionService
from api.db.services.user_service import TenantService
from api.settings import RetCode
from api.utils import get_uuid
from api.utils.api_utils import get_data_error_result, get_json_result, server_error_response
from api.utils.file_utils import filename_type, read_potential_broken_pdf
from core.flow.pipeline import Pipeline

router = APIRouter()


class RemoveCanvasRequest(BaseModel):
    """删除画布请求模型"""
    canvas_ids: list[str] = Field(..., description="画布ID列表")


class SaveCanvasRequest(BaseModel):
    """保存画布请求模型"""
    title: str = Field(..., description="画布标题")
    dsl: dict | str = Field(..., description="画布DSL定义")
    id: str | None = Field(None, description="画布ID，创建时为空")
    
    @field_validator("dsl")
    @classmethod
    def _ensure_dict(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                raise ValueError("dsl must be a JSON object or a JSON string")
        return v


class RunDataFlowRequest(BaseModel):
    """运行数据流请求模型"""
    id: str = Field(..., description="数据流ID")
    doc_id: str = Field(..., description="文档ID")
    user_id: str | None = Field(None, description="用户ID")


class ResetDataFlowRequest(BaseModel):
    """重置数据流请求模型"""
    id: str = Field(..., description="数据流ID")
    task_id: str | None = Field(None, description="任务ID")


class DebugRequest(BaseModel):
    """调试组件请求模型"""
    id: str = Field(..., description="画布ID")
    component_id: str = Field(..., description="组件ID")
    params: dict = Field(..., description="调试参数")


class SettingRequest(BaseModel):
    """更新设置请求模型"""
    id: str = Field(..., description="画布ID")
    title: str = Field(..., description="标题")
    permission: str = Field(..., description="权限设置")
    description: str | None = Field(None, description="描述")
    avatar: str | None = Field(None, description="头像")


@router.get("/templates", summary="获取数据流模板列表")
def templates(db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """获取数据流模板列表"""
    try:
        data = [c.to_dict() for c in CanvasTemplateService.query(db, canvas_category=CanvasCategory.DataFlow)]
        return get_json_result(data=data)
    except Exception as e:
        return server_error_response(e)


@router.get("/list", summary="获取我的数据流列表")
def canvas_list(db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """获取当前用户的数据流列表，按更新时间倒序排列"""
    try:
        canvases = [c.to_dict() for c in UserCanvasService.query(db, user_id=user.id, canvas_category=CanvasCategory.DataFlow)]
        canvases_sorted = sorted(canvases, key=lambda x: x["update_time"] * -1)
        return get_json_result(data=canvases_sorted)
    except Exception as e:
        return server_error_response(e)


@router.post("/rm", summary="删除数据流（批量）")
def rm(request: RemoveCanvasRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    批量删除数据流
    
    Args:
        request: 删除请求参数
        db: 数据库会话
        user: 当前用户信息
        
    Returns:
        dict[str, Any]: 删除结果
    """
    try:
        for canvas_id in request.canvas_ids:
            if not UserCanvasService.accessible(db, canvas_id, user.id):
                return get_json_result(
                    data=False,
                    retmsg="Only owner of canvas authorized for this operation.",
                    retcode=RetCode.OPERATING_ERROR
                )
            UserCanvasService.delete_by_id(db, canvas_id)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post("/set", summary="创建/更新数据流")
def save(request: SaveCanvasRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    创建或更新数据流
    
    Args:
        request: 保存请求参数
        db: 数据库会话
        user: 当前用户信息
        
    Returns:
        dict[str, Any]: 保存结果
    """
    try:
        req = request.model_dump()
        dsl_obj = req["dsl"]  # 已在 validator 中转为 dict
        req["canvas_category"] = CanvasCategory.DataFlow
        
        if "id" not in req or not req["id"]:
            # 创建新数据流
            req["user_id"] = user.id
            if UserCanvasService.query(db, user_id=user.id, title=req["title"].strip(), canvas_category=CanvasCategory.DataFlow):
                return get_data_error_result(retmsg=f"{req['title'].strip()} already exists.")
            req["id"] = get_uuid()
            
            # 存储 dsl 需要序列化为 JSON 字符串
            to_save = {**req, "dsl": json.dumps(dsl_obj, ensure_ascii=False)}
            if not UserCanvasService.save(db, **to_save):
                return get_data_error_result(retmsg="Fail to save canvas.")
        else:
            # 更新现有数据流
            if not UserCanvasService.accessible(db, req["id"], user.id):
                return get_json_result(
                    data=False,
                    retmsg="Only owner of canvas authorized for this operation.",
                    retcode=RetCode.OPERATING_ERROR
                )
            UserCanvasService.update_by_id(db, req["id"], req)
        
        # 保存版本
        UserCanvasVersionService.insert(
            db,
            user_canvas_id=req["id"],
            dsl=dsl_obj,
            title=f"{req['title']}_{time.strftime('%Y_%m_%d_%H_%M_%S')}"
        )
        UserCanvasVersionService.delete_all_versions(db, req["id"])
        
        return get_json_result(data=req)
    except Exception as e:
        return server_error_response(e)


@router.get("/get/{canvas_id}", summary="获取数据流详情")
def get(canvas_id: str, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    获取指定数据流的详细信息
    
    Args:
        canvas_id: 画布ID
        db: 数据库会话
        user: 当前用户信息
        
    Returns:
        dict[str, Any]: 数据流详情
    """
    try:
        if not UserCanvasService.accessible(db, canvas_id, user.id):
            return get_data_error_result(retmsg="canvas not found.")
        c = UserCanvasService.get_by_tenant_id(db, canvas_id)
        return get_json_result(data=c)
    except Exception as e:
        return server_error_response(e)


@router.post("/run", summary="运行数据流")
def run(request: RunDataFlowRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    运行数据流处理任务
    
    Args:
        request: 运行请求参数
        db: 数据库会话
        user: 当前用户信息
        
    Returns:
        dict[str, Any]: 任务执行结果，包含 task_id 和 flow_id
    """
    try:
        flow_id = request.id
        doc_id = request.doc_id
        
        if not DocumentService.get_by_id(db, doc_id):
            return get_data_error_result(retmsg=f"Document for {doc_id} not found.")
        
        user_id = request.user_id or user.id
        if not UserCanvasService.accessible(db, flow_id, user.id):
            return get_json_result(
                data=False,
                retmsg="Only owner of canvas authorized for this operation.",
                retcode=RetCode.OPERATING_ERROR
            )
        
        cvs = UserCanvasService.get_by_id(db, flow_id)
        if not cvs:
            return get_data_error_result(retmsg="canvas not found.")
        
        # 确保 dsl 是字符串格式
        if not isinstance(cvs.dsl, str):
            cvs.dsl = json.dumps(cvs.dsl, ensure_ascii=False)
        
        task_id = get_uuid()
        
        ok, error_message = queue_dataflow(
            dsl=cvs.dsl,
            tenant_id=user_id,
            doc_id=doc_id,
            task_id=task_id,
            flow_id=flow_id,
            priority=0
        )
        if not ok:
            return server_error_response(error_message)
        
        return get_json_result(data={"task_id": task_id, "flow_id": flow_id})
    except Exception as e:
        return server_error_response(e)


@router.post("/reset", summary="重置数据流状态")
def reset(request: ResetDataFlowRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    重置数据流的执行状态
    
    Args:
        request: 重置请求参数
        db: 数据库会话
        user: 当前用户信息
        
    Returns:
        dict[str, Any]: 重置后的 DSL
    """
    try:
        flow_id = request.id
        
        if not UserCanvasService.accessible(db, flow_id, user.id):
            return get_json_result(
                data=False,
                retmsg="Only owner of canvas authorized for this operation.",
                retcode=RetCode.OPERATING_ERROR
            )
        
        task_id = request.task_id or ""
        
        user_canvas = UserCanvasService.get_by_id(db, request.id)
        if not user_canvas:
            return get_data_error_result(retmsg="canvas not found.")
        
        dataflow = Pipeline(
            dsl=json.dumps(user_canvas.dsl),
            tenant_id=user.id,
            flow_id=flow_id,
            task_id=task_id
        )
        dataflow.reset()
        new_dsl = json.loads(str(dataflow))
        UserCanvasService.update_by_id(db, request.id, {"dsl": new_dsl})
        return get_json_result(data=new_dsl)
    except Exception as e:
        return server_error_response(e)


@router.post("/upload/{canvas_id}", summary="上传文件/URL到数据流")
async def upload(
    canvas_id: str,
    file: UploadFile | None = File(default=None),
    url: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    上传文件或抓取URL内容到数据流
    
    Args:
        canvas_id: 画布ID
        file: 上传的文件（可选）
        url: 要抓取的URL（可选）
        db: 数据库会话
        
    Returns:
        dict[str, Any]: 上传结果，包含文件元数据
    """
    try:
        cvs = UserCanvasService.get_by_tenant_id(db, canvas_id)
        if not cvs:
            return get_data_error_result(retmsg="canvas not found.")
        
        user_id = cvs["user_id"]
        
        def structured(filename: str, filetype: str, blob: bytes, content_type: str):
            nonlocal user_id
            if filetype == FileType.PDF.value:
                blob = read_potential_broken_pdf(blob)
            
            location = get_uuid()
            FileService.put_blob(db, user_id, location, blob)
            
            return {
                "id": location,
                "name": filename,
                "size": sys.getsizeof(blob),
                "extension": filename.split(".")[-1].lower(),
                "mime_type": content_type,
                "created_by": user_id,
                "created_at": time.time(),
                "preview_url": None,
            }
        
        # 处理 URL 抓取
        if url:
            from crawl4ai import (
                AsyncWebCrawler,
                BrowserConfig,
                CrawlerRunConfig,
                CrawlResult,
                DefaultMarkdownGenerator,
                PruningContentFilter,
            )
            
            try:
                filename = re.sub(r"\?.*", "", url.split("/")[-1]) or "download"
                
                async def adownload():
                    browser_config = BrowserConfig(headless=True, verbose=False)
                    async with AsyncWebCrawler(config=browser_config) as crawler:
                        crawler_config = CrawlerRunConfig(
                            markdown_generator=DefaultMarkdownGenerator(content_filter=PruningContentFilter()),
                            pdf=True,
                            screenshot=False
                        )
                        result: CrawlResult = await crawler.arun(url=url, config=crawler_config)
                        return result
                
                page = await adownload()
                if page.pdf:
                    if filename.split(".")[-1].lower() != "pdf":
                        filename += ".pdf"
                    return get_json_result(
                        data=structured(
                            filename,
                            "pdf",
                            page.pdf,
                            page.response_headers.get("content-type", "application/pdf")
                        )
                    )
                
                # HTML/Markdown 内容
                blob = str(page.markdown).encode("utf-8")
                return get_json_result(
                    data=structured(
                        filename,
                        "html",
                        blob,
                        page.response_headers.get("content-type", "text/html")
                    )
                )
            except Exception as e:
                return server_error_response(e)
        
        # 处理文件上传
        if not file:
            return get_data_error_result(retmsg="No file or url provided.")
        
        try:
            content = await file.read()
            DocumentService.check_doc_health(user_id, file.filename)
            return get_json_result(
                data=structured(
                    file.filename,
                    filename_type(file.filename),
                    content,
                    file.content_type or "application/octet-stream"
                )
            )
        except Exception as e:
            return server_error_response(e)
    except Exception as e:
        return server_error_response(e)


@router.get("/input_form", summary="获取组件输入表单描述")
def input_form(
    id: str = Query(..., description="数据流ID"),
    component_id: str = Query(..., description="组件ID"),
    db: Session = Depends(get_db),
    user=Depends(manager),
) -> dict[str, Any]:
    """
    获取指定组件的输入表单描述
    
    Args:
        id: 数据流ID
        component_id: 组件ID
        db: 数据库会话
        user: 当前用户信息
        
    Returns:
        dict[str, Any]: 组件输入表单描述
    """
    try:
        user_canvas = UserCanvasService.get_by_id(db, id)
        if not user_canvas:
            return get_data_error_result(retmsg="canvas not found.")
        if not UserCanvasService.query(db, user_id=user.id, id=id):
            return get_json_result(
                data=False,
                retmsg="Only owner of canvas authorized for this operation.",
                retcode=RetCode.OPERATING_ERROR
            )
        
        dataflow = Pipeline(
            dsl=json.dumps(user_canvas.dsl),
            tenant_id=user.id,
            flow_id=id,
            task_id=""
        )
        
        return get_json_result(data=dataflow.get_component_input_form(component_id))
    except Exception as e:
        return server_error_response(e)


@router.post("/debug", summary="调试组件执行")
def debug(request: DebugRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    调试执行指定组件
    
    Args:
        request: 调试请求参数
        db: 数据库会话
        user: 当前用户信息
        
    Returns:
        dict[str, Any]: 组件执行输出
    """
    try:
        if not UserCanvasService.accessible(db, request.id, user.id):
            return get_json_result(
                data=False,
                retmsg="Only owner of canvas authorized for this operation.",
                retcode=RetCode.OPERATING_ERROR
            )
        
        user_canvas = UserCanvasService.get_by_id(db, request.id)
        canvas = Canvas(json.dumps(user_canvas.dsl), user.id)
        canvas.reset()
        canvas.message_id = get_uuid()
        component = canvas.get_component(request.component_id)["obj"]
        component.reset()
        
        if isinstance(component, LLM):
            component.set_debug_inputs(request.params)
        
        component.invoke(**{k: o["value"] for k, o in request.params.items()})
        outputs = component.output()
        
        # 处理流式输出
        for k in list(outputs.keys()):
            if isinstance(outputs[k], partial):
                txt = ""
                for c in outputs[k]():
                    txt += c
                outputs[k] = txt
        
        return get_json_result(data=outputs)
    except Exception as e:
        return server_error_response(e)


@router.get("/getlistversion/{canvas_id}", summary="获取数据流版本列表")
def getlistversion(canvas_id: str, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    获取指定数据流的版本历史列表
    
    Args:
        canvas_id: 画布ID
        db: 数据库会话
        user: 当前用户信息
        
    Returns:
        dict[str, Any]: 版本列表
    """
    try:
        version_list = sorted(
            [c.to_dict() for c in UserCanvasVersionService.list_by_canvas_id(db, canvas_id)],
            key=lambda x: x["update_time"] * -1
        )
        return get_json_result(data=version_list)
    except Exception as e:
        return get_data_error_result(retmsg=f"Error getting history files: {e}")


@router.get("/getversion/{version_id}", summary="获取指定版本")
def getversion(version_id: str, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    获取指定版本的详细信息
    
    Args:
        version_id: 版本ID
        db: 数据库会话
        user: 当前用户信息
        
    Returns:
        dict[str, Any]: 版本详情
    """
    try:
        version = UserCanvasVersionService.get_by_id(db, version_id)
        if version:
            return get_json_result(data=version.to_dict())
        return get_json_result(data=None)
    except Exception as e:
        return get_json_result(data=f"Error getting history file: {e}")


@router.get("/listteam", summary="获取团队数据流列表")
def list_canvas(
    keywords: str = Query("", description="关键词搜索"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(150, ge=1, le=500, description="每页数量"),
    orderby: str = Query("create_time", description="排序字段"),
    desc: bool = Query(True, description="是否降序"),
    db: Session = Depends(get_db),
    user=Depends(manager),
) -> dict[str, Any]:
    """
    获取团队/共享空间下的数据流列表
    
    Args:
        keywords: 搜索关键词
        page: 页码
        page_size: 每页数量
        orderby: 排序字段
        desc: 是否降序
        db: 数据库会话
        user: 当前用户信息
        
    Returns:
        dict[str, Any]: 数据流列表和总数
    """
    try:
        tenants = TenantService.get_joined_tenants_by_user_id(db, user.id)
        tenant_ids = [m["tenant_id"] for m in tenants]
        canvas, total = UserCanvasService.get_by_tenant_ids(
            db,
            tenant_ids,
            user.id,
            page,
            page_size,
            orderby,
            desc,
            keywords,
            canvas_category=CanvasCategory.DataFlow
        )
        return get_json_result(data={"canvas": canvas, "total": total})
    except Exception as e:
        return server_error_response(e)


@router.post("/setting", summary="更新数据流设置")
def setting(request: SettingRequest, db: Session = Depends(get_db), user=Depends(manager)) -> dict[str, Any]:
    """
    更新数据流的基本设置
    
    Args:
        request: 设置请求参数
        db: 数据库会话
        user: 当前用户信息
        
    Returns:
        dict[str, Any]: 更新结果
    """
    try:
        req = request.model_dump()
        req["user_id"] = user.id
        
        if not UserCanvasService.accessible(db, req["id"], user.id):
            return get_json_result(
                data=False,
                retmsg="Only owner of canvas authorized for this operation.",
                retcode=RetCode.OPERATING_ERROR
            )
        
        flow = UserCanvasService.get_by_id(db, req["id"])
        if not flow:
            return get_data_error_result(retmsg="canvas not found.")
        
        flow_dict = flow.to_dict()
        flow_dict["title"] = req["title"]
        
        for key in ("description", "permission", "avatar"):
            if value := req.get(key):
                flow_dict[key] = value
        
        num = UserCanvasService.update_by_id(db, req["id"], flow_dict)
        return get_json_result(data=num)
    except Exception as e:
        return server_error_response(e)


@router.get("/trace", summary="获取数据流执行追踪日志")
def trace(
    dataflow_id: str = Query(..., description="数据流ID"),
    task_id: str = Query(..., description="任务ID"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    获取数据流的执行追踪日志
    
    Args:
        dataflow_id: 数据流ID
        task_id: 任务ID
        db: 数据库会话
        
    Returns:
        dict[str, Any]: 执行日志
    """
    try:
        dataflow_canvas = UserCanvasService.get_by_id(db, dataflow_id)
        if not dataflow_canvas:
            return get_data_error_result(retmsg="dataflow not found.")
        
        dsl_str = json.dumps(dataflow_canvas.dsl, ensure_ascii=False)
        dataflow = Pipeline(
            dsl=dsl_str,
            tenant_id=dataflow_canvas.user_id,
            flow_id=dataflow_id,
            task_id=task_id
        )
        log = dataflow.fetch_logs()
        
        return get_json_result(data=log)
    except Exception as e:
        return server_error_response(e)
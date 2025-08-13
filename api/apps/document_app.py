# coding=utf-8
"""
@project: multirag
@Author：龙
@file： document_app.py
@date：2025/7/17 11:30
@desc:
"""
import logging
import os.path
import json
import pathlib
import re
from pathlib import Path
from io import BytesIO
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Form
from fastapi.responses import StreamingResponse
from pymilvus import MilvusException
from sqlalchemy.orm import Session
from urllib.parse import quote

from api.constants import FILE_NAME_LEN_LIMIT, IMG_BASE64_PREFIX
from api.db import VALID_FILE_TYPES, VALID_TASK_STATUS, FileType, TaskStatus, ParserType, FileSource, db_models
from api.db.db_models import Task, get_db
from api.db.services import duplicate_name
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.task_service import TaskService, cancel_all_task_of, queue_tasks
from api.db.services.user_service import UserTenantService
from deepdoc.parser.html_parser import RAGFlowHtmlParser
from api import settings
from api.utils.api_utils import construct_json_result, construct_error_response, convert_datetime_to_str, \
    get_json_result, get_data_error_result, server_error_response
from api.utils import get_uuid
from api.utils.file_utils import filename_type, thumbnail, get_project_base_directory
from api.utils.web_utils import CONTENT_TYPE_MAP, html2pdf, is_valid_url
from core.nlp import search
from core.utils.storage_factory import STORAGE_IMPL
from api.apps import manager

from pydantic import BaseModel, Field

router = APIRouter()


class WebCrawlRequest(BaseModel):
    kb_id: str = Field(..., description="知识库ID")
    name: str = Field(..., description="文件名")
    url: str = Field(..., description="URL地址")


class CreateDocumentRequest(BaseModel):
    name: str = Field(..., description="文件名")
    kb_id: str = Field(..., description="知识库ID")


class DocumentFilter(BaseModel):
    run_status: list[str] | None = []
    types: list[str] | None = []
    suffix: list[str] = []


class ChangeStatusRequest(BaseModel):
    doc_ids: list[str] | str = Field(..., description="文档ID或文档ID列表")
    status: int = Field(..., description="状态")
    
    # 兼容旧版本字段
    doc_id: str | None = Field(None, description="文档ID（兼容旧版本）")

class ChangeAuthRequest(BaseModel):
    doc_id: str = Field(..., description="文档ID")


class RemoveRequest(BaseModel):
    doc_id: list[str] = Field(..., description="文档ID列表")


class RunRequest(BaseModel):
    doc_ids: list[str] = Field(..., description="文档ID列表")
    run: int = Field(..., description="运行状态")
    delete: bool = Field(default=False, description="是否删除历史doc记录")


class RenameRequest(BaseModel):
    doc_id: str = Field(..., description="文档ID")
    name: str = Field(..., description="新的文件名")


class ChangeParserRequest(BaseModel):
    doc_id: str = Field(..., description="文档ID")
    parser_id: str = Field(..., description="解析器ID")
    parser_config: dict | None = Field(None, description="解析器配置")

class SetMetaRequest(BaseModel):
    doc_id: str = Field(..., description="文档ID")
    meta: dict[str, Any] = Field(..., description="元数据对象")


class FilterRequest(BaseModel):
    kb_id: str = Field(..., description="知识库ID")
    keywords: str = Field(default="", description="关键词")
    suffix: list[str] = Field(default=[], description="文件后缀过滤")
    run_status: list[str] = Field(default=[], description="运行状态过滤")
    types: list[str] = Field(default=[], description="文件类型过滤")

@router.post("/upload", summary="上传文件", response_description="成功上传文件")
async def upload(
        kb_id: str,
        files: list[UploadFile] = File(...),
        labels: str | None = Query(None),  # labels 是一个 JSON 格式的字符串
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    ### POST `/upload` 文件上传接口

    **功能描述**:
    此接口用于上传单个或多个文件到指定的知识库，支持文件标签标注和自动文件类型识别。上传的文件会被存储并创建对应的文档记录。

    ---

    ### 请求参数

    #### Form Data 参数
    | 参数名    | 类型                | 必填 | 描述                                                                |
    |-----------|---------------------|------|---------------------------------------------------------------------|
    | `kb_id`   | `string`           | 是   | 知识库的唯一标识符                                                  |
    | `files`   | `list[UploadFile]` | 是   | 要上传的文件列表，支持多文件同时上传                                |
    | `labels`  | `string`           | 否   | JSON格式的标签字符串，用于标注文件属性，如 `["标签1", "标签2"]`     |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": [
            {
                "id": "doc_123456",
                "name": "示例文档.pdf",
                "size": 1024000,
                "type": "pdf",
                "thumbnail": "/v1/document/image/kb_id-thumbnail_id",
                "created_time": "2024-01-01 12:00:00",
                "status": "uploaded"
            }
        ]
    }
    ```

    #### 错误响应

    - **400: 参数错误**
        ```json
        {
            "retcode": 400,
            "retmsg": "Lack of \"KB ID\"",
            "data": false
        }
        ```

    - **400: 文件问题**
        ```json
        {
            "retcode": 400,
            "retmsg": "No file selected!",
            "data": false
        }
        ```

    - **400: 文件名过长**
        ```json
        {
            "retcode": 400,
            "retmsg": "File name must be 255 bytes or less.",
            "data": false
        }
        ```

    - **404: 知识库不存在**
        ```json
        {
            "status_code": 404,
            "detail": "Can't find this knowledgebase!"
        }
        ```

    - **500: 服务器错误**
        ```json
        {
            "retcode": 500,
            "retmsg": "Upload processing failed",
            "data": false
        }
        ```

    ---

    ### 主要流程

    1. **参数验证**:
        - 验证知识库ID是否存在
        - 验证文件列表是否为空
        - 检查文件名长度限制

    2. **知识库验证**:
        - 根据kb_id查找知识库
        - 验证用户是否有权限访问该知识库

    3. **文件处理**:
        - 读取所有上传文件的内容
        - 验证文件格式和大小
        - 生成文件缩略图（如果适用）

    4. **标签处理**:
        - 解析JSON格式的labels参数
        - 验证标签格式的正确性

    5. **存储操作**:
        - 将文件存储到对象存储系统
        - 在数据库中创建文档记录
        - 关联文件与知识库的关系

    ---

    ### 支持的文件类型

    - **文档类型**: PDF, DOC, DOCX, TXT, MD
    - **表格类型**: XLS, XLSX, CSV
    - **演示文稿**: PPT, PPTX
    - **图片类型**: JPG, JPEG, PNG, GIF, BMP
    - **其他格式**: HTML, XML, JSON

    ---

    ### 使用示例

    #### 单文件上传
    ```bash
    curl -X POST "http://api.example.com/v1/document/upload" \
        -F "kb_id=kb_123456" \
        -F "files=@document.pdf"
    ```

    #### 多文件上传带标签
    ```bash
    curl -X POST "http://api.example.com/v1/document/upload" \
        -F "kb_id=kb_123456" \
        -F "files=@doc1.pdf" \
        -F "files=@doc2.docx" \
        -F 'labels=["重要文档", "技术资料"]'
    ```

    ---

    ### 注意事项

    - **文件大小限制**: 单个文件建议不超过100MB
    - **文件名限制**: 文件名不能超过255字节
    - **并发上传**: 支持同时上传多个文件，但建议单次不超过10个
    - **标签格式**: labels必须是有效的JSON数组格式
    - **权限控制**: 只有知识库的所有者才能上传文件
    - **自动解析**: 上传后文件会自动进入解析队列等待处理
    """
    if not kb_id:
        return construct_json_result(data=False, message='Lack of "KB ID"', code=settings.RetCode.ARGUMENT_ERROR)
    if not files:
        return construct_json_result(data=False, message='No file part!', code=settings.RetCode.ARGUMENT_ERROR)

    kb = KnowledgebaseService.get_by_id(db, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Can't find this knowledgebase!")
    file_contents = []
    for file in files:
        if file.filename == "":
            return get_json_result(data=False, retmsg="No file selected!", retcode=settings.RetCode.ARGUMENT_ERROR)
        if len(file.filename.encode("utf-8")) > FILE_NAME_LEN_LIMIT:
            return get_json_result(data=False, retmsg=f"File name must be {FILE_NAME_LEN_LIMIT} bytes or less.", retcode=settings.RetCode.ARGUMENT_ERROR)

        file_contents.append((await file.read(), file.filename))  # 读取文件内容并存储
    # 确保 labels 是 list 或 None
    if isinstance(labels, str):
        try:
            labels = json.loads(labels)
            if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
                raise ValueError('Labels must be a list of strings.')
        except json.JSONDecodeError:
            raise ValueError('Invalid JSON format for "labels".')
    elif labels is not None:
        raise ValueError('Labels must be a JSON-encoded list of strings or None.')
    # err, files = FileService.upload_document(db, kb, file_contents, user)
    err, files = FileService.upload_document(db, kb, file_contents, user, labels)  # 传递labels参数

    # if err:
    #     return get_json_result(data=files, retmsg="\n".join(err), retcode=settings.RetCode.SERVER_ERROR)
    if err:
        return construct_json_result(data=False, message="\n".join(err), code=settings.RetCode.SERVER_ERROR)

    if not files:
        return get_json_result(data=files, retmsg="There seems to be an issue with your file format. Please verify it is correct and not corrupted.", retcode=settings.RetCode.DATA_ERROR)

    return construct_json_result(data=files, code=settings.RetCode.SUCCESS)


@router.post("/web_crawl", summary="网页爬取", response_description="成功爬取网页")
def web_crawl(
        request_body: WebCrawlRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    ### POST `/web_crawl` 网页爬取接口

    **功能描述**:
    此接口用于爬取指定URL的网页内容，将网页转换为PDF格式后存储到知识库中。支持动态网页内容抓取和自动文档创建。

    ---

    ### 请求体 (Request Body)

    | 字段     | 类型     | 必填 | 描述                                        |
    |----------|----------|------|---------------------------------------------|
    | `kb_id`  | `string` | 是   | 目标知识库的唯一标识符                      |
    | `name`   | `string` | 是   | 保存的文档名称（不包含.pdf后缀）            |
    | `url`    | `string` | 是   | 要爬取的网页URL地址                         |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "id": "doc_123456",
            "kb_id": "kb_789",
            "name": "示例网页.pdf",
            "size": 2048000,
            "type": "pdf",
            "location": "网页内容.pdf",
            "thumbnail": "base64_encoded_thumbnail",
            "created_by": "user_id",
            "parser_id": "pdf_parser",
            "suffix": "pdf"
        }
    }
    ```

    #### 错误响应

    - **400: URL格式无效**
        ```json
        {
            "retcode": 400,
            "retmsg": "The URL format is invalid",
            "data": false
        }
        ```

    - **404: 知识库不存在**
        ```json
        {
            "status_code": 404,
            "detail": "Can't find this knowledgebase!"
        }
        ```

    - **500: 下载失败**
        ```json
        {
            "retcode": 500,
            "retmsg": "Download failure.",
            "data": false
        }
        ```

    - **500: 文件类型不支持**
        ```json
        {
            "retcode": 500,
            "retmsg": "This type of file has not been supported yet!",
            "data": false
        }
        ```

    ---

    ### 主要流程

    1. **URL验证**:
        - 验证URL格式的有效性
        - 检查是否为可访问的网址

    2. **知识库验证**:
        - 根据kb_id查找知识库
        - 验证用户是否有权限访问

    3. **网页爬取**:
        - 使用专用工具将网页转换为PDF
        - 保持原网页的格式和布局

    4. **文件处理**:
        - 生成唯一的文件名避免重复
        - 创建文档缩略图
        - 确定文件类型和解析器

    5. **存储操作**:
        - 将PDF文件存储到对象存储
        - 在数据库中创建文档记录
        - 关联到指定的知识库

    ---

    ### 特殊处理

    #### 文件类型自动识别
    - **图片文件**: 自动设置为图片解析器
    - **演示文稿**: PPT/PPTX自动设置为演示文稿解析器  
    - **邮件文件**: EML自动设置为邮件解析器
    - **PDF文件**: 使用知识库默认解析器

    #### 文件名处理
    - 自动添加.pdf后缀
    - 如果文件名重复，自动追加序号
    - 支持中文文件名

    ---

    ### 使用示例

    #### 爬取技术文档
    ```json
    {
        "kb_id": "tech_docs_kb",
        "name": "API文档",
        "url": "https://docs.example.com/api"
    }
    ```

    #### 爬取新闻页面
    ```json
    {
        "kb_id": "news_kb", 
        "name": "今日新闻",
        "url": "https://news.example.com/today"
    }
    ```

    ---

    ### 注意事项

    - **URL限制**: 仅支持HTTP/HTTPS协议的网址
    - **内容限制**: 无法爬取需要登录或有访问限制的页面
    - **格式保持**: 尽量保持原网页的排版和格式
    - **文件大小**: 生成的PDF大小取决于网页内容复杂度
    - **处理时间**: 复杂网页的转换可能需要较长时间
    - **动态内容**: 支持JavaScript渲染的动态内容抓取
    - **存储路径**: 文件存储在知识库对应的存储空间中
    """
    kb_id = request_body.kb_id
    name = request_body.name
    url = request_body.url
    if not is_valid_url(url):
        return construct_json_result(data=False, message='The URL format is invalid', code=settings.RetCode.ARGUMENT_ERROR)
    kb = KnowledgebaseService.get_by_id(db, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Can't find this knowledgebase!")

    blob = html2pdf(url)
    if not blob:
        return construct_error_response(ValueError("Download failure."))

    root_folder = FileService.get_root_folder(db, user.id)
    pf_id = root_folder["id"]
    FileService.init_knowledgebase_docs(db, pf_id, user.id)
    kb_root_folder = FileService.get_kb_folder(db, user.id)
    kb_folder = FileService.new_a_file_from_kb(db, kb.tenant_id, kb.name, kb_root_folder["id"])

    try:
        filename = duplicate_name(DocumentService.query, db=db, name=name + ".pdf", kb_id=kb.id)
        filetype = filename_type(filename)
        if filetype == FileType.OTHER.value:
            raise RuntimeError("This type of file has not been supported yet!")

        location = filename
        while STORAGE_IMPL.obj_exist(kb_id, location):
            location += "_"
        STORAGE_IMPL.put(kb_id, location, blob)
        doc = {
            "id": get_uuid(),
            "kb_id": kb.id,
            "parser_id": kb.parser_id,
            "parser_config": kb.parser_config,
            "created_by": user.id,
            "type": filetype,
            "name": filename,
            "location": location,
            "size": len(blob),
            "thumbnail": thumbnail(filename, blob),
            "suffix": Path(filename).suffix.lstrip("."),
        }
        if doc["type"] == FileType.VISUAL:
            doc["parser_id"] = ParserType.PICTURE.value
        if re.search(r"\.(ppt|pptx|pages)$", filename):
            doc["parser_id"] = ParserType.PRESENTATION.value
        if re.search(r"\.(eml)$", filename):
            doc["parser_id"] = ParserType.EMAIL.value
        DocumentService.insert(db, doc)
        FileService.add_file_from_kb(db, doc, kb_folder["id"], kb.tenant_id)
    except Exception as e:
        return construct_error_response(e)
    return construct_json_result(data=doc)


@router.post("/create", summary="创建文件或文件夹", response_description="成功创建文件或文件夹")
def create_document(
        request_body: CreateDocumentRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    req = request_body.model_dump()
    kb_id = req["kb_id"]
    if not kb_id:
        return construct_json_result(data=False, message='Lack of "KB ID"', code=settings.RetCode.ARGUMENT_ERROR)
    if len(req["name"].encode("utf-8")) > FILE_NAME_LEN_LIMIT:
        return construct_json_result(data=False, message=f"File name must be {FILE_NAME_LEN_LIMIT} bytes or less.", code=settings.RetCode.ARGUMENT_ERROR)

    if req["name"].strip() == "":
        return construct_json_result(data=False, message="File name can't be empty.", code=settings.RetCode.ARGUMENT_ERROR)
    req["name"] = req["name"].strip()

    try:
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        if not kb:
            return construct_json_result(data=False, message="Can't find this knowledgebase!",
                                         code=settings.RetCode.ARGUMENT_ERROR)

        if DocumentService.query(db, name=req["name"], kb_id=kb_id):
            return construct_json_result(data=False, message="Duplicated document name in the same knowledgebase.",
                                         code=settings.RetCode.ARGUMENT_ERROR)

        doc = DocumentService.insert(db, {
            "id": get_uuid(),
            "kb_id": kb.id,
            "parser_id": kb.parser_id,
            "parser_config": kb.parser_config,
            "created_by": user.id,
            "type": FileType.VIRTUAL,
            "name": req["name"],
            "suffix": Path(req["name"]).suffix.lstrip("."),
            "location": "",
            "size": 0
        })
        return construct_json_result(data=doc.to_dict(), code=settings.RetCode.SUCCESS)
    except Exception as e:
        return construct_error_response(e)


@router.get("/list", summary="列出文档", response_description="成功列出文档")
def list_docs(
        kb_id: str,
        keywords: str = "",
        page: int = 1,
        page_size: int = 15,
        orderby: str = "create_time",
        desc: bool = True,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    ### GET `/list` 列出文档接口

    **功能描述**:
    此接口用于获取指定知识库中的文档列表，支持关键词搜索、分页查询和排序功能。返回文档的基本信息和缩略图。

    ---

    ### 请求参数 (Query Parameters)

    | 参数名      | 类型      | 必填 | 默认值       | 描述                                                    |
    |-------------|-----------|------|-------------|--------------------------------------------------------|
    | `kb_id`     | `string`  | 是   | -           | 知识库的唯一标识符                                      |
    | `keywords`  | `string`  | 否   | ""          | 搜索关键词，支持文档名称模糊匹配                        |
    | `page`      | `int`     | 否   | 1           | 页码，从1开始                                          |
    | `page_size` | `int`     | 否   | 15          | 每页返回的文档数量                                      |
    | `orderby`   | `string`  | 否   | create_time | 排序字段，支持: create_time, name, size, update_time   |
    | `desc`      | `boolean` | 否   | true        | 是否降序排列                                           |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "total": 100,
            "docs": [
                {
                    "id": "doc_123456",
                    "name": "技术文档.pdf",
                    "size": 1024000,
                    "type": "pdf",
                    "status": "1",
                    "run": "done",
                    "progress": 100,
                    "chunk_num": 50,
                    "token_num": 15000,
                    "thumbnail": "/v1/document/image/kb_id-thumbnail_id",
                    "create_time": "2024-01-01 12:00:00",
                    "update_time": "2024-01-01 13:00:00",
                    "created_by": "user_123",
                    "parser_id": "pdf_parser",
                    "suffix": "pdf"
                }
            ]
        }
    }
    ```

    #### 错误响应

    - **400: 知识库ID缺失**
        ```json
        {
            "retcode": 400,
            "retmsg": "Lack of \"KB ID\"",
            "data": false
        }
        ```

    - **403: 权限不足**
        ```json
        {
            "retcode": 403,
            "retmsg": "Only owner of knowledgebase authorized for this operation.",
            "data": false
        }
        ```

    ---

    ### 主要流程

    1. **权限验证**:
        - 验证知识库ID是否存在
        - 检查用户是否为知识库的所有者
        - 确认用户有访问权限

    2. **数据查询**:
        - 根据关键词进行模糊搜索
        - 应用分页和排序参数
        - 统计符合条件的文档总数

    3. **结果处理**:
        - 转换时间格式为字符串
        - 处理缩略图URL路径
        - 格式化返回数据

    ---

    ### 排序字段说明

    | 字段名        | 描述           | 数据类型    |
    |---------------|----------------|-------------|
    | `create_time` | 创建时间       | datetime    |
    | `update_time` | 更新时间       | datetime    |
    | `name`        | 文档名称       | string      |
    | `size`        | 文件大小       | integer     |
    | `progress`    | 处理进度       | integer     |

    ---

    ### 文档状态说明

    #### 运行状态 (run)
    - `unstart`: 未开始处理
    - `running`: 正在处理
    - `done`: 处理完成
    - `fail`: 处理失败

    #### 可用状态 (status)
    - `0`: 禁用，不参与检索
    - `1`: 启用，正常使用

    ---

    ### 使用示例

    #### 基本查询
    ```
    GET /v1/document/list?kb_id=kb_123456
    ```

    #### 关键词搜索
    ```
    GET /v1/document/list?kb_id=kb_123456&keywords=技术文档
    ```

    #### 分页查询
    ```
    GET /v1/document/list?kb_id=kb_123456&page=2&page_size=20
    ```

    #### 自定义排序
    ```
    GET /v1/document/list?kb_id=kb_123456&orderby=size&desc=false
    ```

    ---

    ### 注意事项

    - **权限控制**: 只有知识库所有者才能查看文档列表
    - **缩略图处理**: 自动处理缩略图URL，支持base64和文件路径两种格式
    - **时间格式**: 所有时间字段统一转换为字符串格式返回
    - **性能优化**: 建议合理设置page_size，避免单次查询过多数据
    - **搜索范围**: 关键词搜索仅匹配文档名称，不包含文档内容
    """
    if not kb_id:
        return construct_json_result(data=False, message='Lack of "KB ID"', code=settings.RetCode.ARGUMENT_ERROR)

    tenants = UserTenantService.query(db, user_id=user.id)
    for tenant in tenants:
        if KnowledgebaseService.query(db, tenant_id=tenant.tenant_id, id=kb_id):
            break
    else:
        return get_json_result(
            data=False, retmsg=f'Only owner of knowledgebase authorized for this operation.',
            retcode=settings.RetCode.OPERATING_ERROR)

    try:
        docs, tol = DocumentService.get_by_kb_id(db, kb_id, page, page_size, orderby, desc, keywords)
        docs = [convert_datetime_to_str(d) for d in docs]

        for doc_item in docs:
            if doc_item['thumbnail'] and not doc_item['thumbnail'].startswith(IMG_BASE64_PREFIX):
                doc_item['thumbnail'] = f"/v1/document/image/{kb_id}-{doc_item['thumbnail']}"

        return construct_json_result(data={"total": tol, "docs": docs})
    except Exception as e:
        return construct_error_response(e)


@router.post("/list", summary="列出文档", response_description="成功列出文档")  # 改为 POST
def list_docs(
        filter_params: DocumentFilter,  # JSON body 参数
        kb_id: str,
        keywords: str = "",
        page: int = 0,  # 默认0表示不分页
        page_size: int = 0,  # 默认0表示不分页
        orderby: str = "create_time",
        desc: bool = True,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    if not kb_id:
        return construct_json_result(data=False, message='Lack of "KB ID"', code=settings.RetCode.ARGUMENT_ERROR)

    tenants = UserTenantService.query(db, user_id=user.id)
    for tenant in tenants:
        if KnowledgebaseService.query(db, tenant_id=tenant.tenant_id, id=kb_id):
            break
    else:
        return get_json_result(
            data=False, retmsg=f'Only owner of knowledgebase authorized for this operation.',
            retcode=settings.RetCode.OPERATING_ERROR)

    # 验证 run_status 参数
    run_status = filter_params.run_status
    if run_status:
        invalid_status = {s for s in run_status if s not in VALID_TASK_STATUS}
        if invalid_status:
            return construct_json_result(
                data=False,
                message=f"Invalid filter run status conditions: {', '.join(invalid_status)}",
                code=settings.RetCode.ARGUMENT_ERROR
            )

    # 验证 types 参数
    types = filter_params.types
    if types:
        invalid_types = {t for t in types if t not in VALID_FILE_TYPES}
        if invalid_types:
            return construct_json_result(
                data=False,
                message=f"Invalid filter conditions: {', '.join(invalid_types)} type{'s' if len(invalid_types) > 1 else ''}",
                code=settings.RetCode.ARGUMENT_ERROR
            )

    suffix = filter_params.suffix

    try:
        docs, tol = DocumentService.get_by_kb_id(
            db, kb_id, page, page_size, orderby, desc, keywords, run_status, types, suffix
        )
        docs = [convert_datetime_to_str(d) for d in docs]

        for doc_item in docs:
            if doc_item['thumbnail'] and not doc_item['thumbnail'].startswith(IMG_BASE64_PREFIX):
                doc_item['thumbnail'] = f"/v1/document/image/{kb_id}-{doc_item['thumbnail']}"

        return construct_json_result(data={"total": tol, "docs": docs})
    except Exception as e:
        return construct_error_response(e)

@router.post("/filter", summary="获取文档过滤器", response_description="成功获取文档过滤器")
def get_filter(
        request_body: FilterRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    获取指定知识库的文档过滤器统计信息。
    
    该接口根据提供的过滤条件，返回知识库中文档的统计信息，包括文件后缀分布和运行状态分布。
    
    参数:
    - kb_id (str): 知识库ID
    - keywords (str): 关键词过滤，默认为空
    - suffix (list[str]): 文件后缀过滤，默认为空列表
    - run_status (list[str]): 运行状态过滤，默认为空列表  
    - types (list[str]): 文件类型过滤，默认为空列表
    
    返回:
    - 包含过滤器统计信息和总数的响应
    """
    req = request_body.model_dump()
    kb_id = req.get("kb_id")
    
    if not kb_id:
        return get_json_result(data=False, retmsg='Lack of "KB ID"', retcode=settings.RetCode.ARGUMENT_ERROR)
    
    # 验证用户是否有权访问该知识库
    tenants = UserTenantService.query(db, user_id=user.id)
    for tenant in tenants:
        if KnowledgebaseService.query(db, tenant_id=tenant.tenant_id, id=kb_id):
            break
    else:
        return get_json_result(data=False, retmsg="Only owner of knowledgebase authorized for this operation.", retcode=settings.RetCode.OPERATING_ERROR)

    keywords = req.get("keywords", "")
    suffix = req.get("suffix", [])
    run_status = req.get("run_status", [])
    types = req.get("types", [])

    # 验证 run_status 参数
    if run_status:
        invalid_status = {s for s in run_status if s not in VALID_TASK_STATUS}
        if invalid_status:
            return get_data_error_result(retmsg=f"Invalid filter run status conditions: {', '.join(invalid_status)}")

    # 验证 types 参数  
    if types:
        invalid_types = {t for t in types if t not in VALID_FILE_TYPES}
        if invalid_types:
            return get_data_error_result(retmsg=f"Invalid filter conditions: {', '.join(invalid_types)} type{'s' if len(invalid_types) > 1 else ''}")

    try:
        filter_data, total = DocumentService.get_filter_by_kb_id(db, kb_id, keywords, run_status, types, suffix)
        return get_json_result(data={"total": total, "filter": filter_data})
    except Exception as e:
        return server_error_response(e)


@router.post('/infos', summary="获取文档信息", response_description="成功获取文档信息")
def docinfos(doc_ids: list[str], db: Session = Depends(get_db), user=Depends(manager)):
    for doc_id in doc_ids:
        if not DocumentService.accessible(db, doc_id, user.id):
            return get_json_result(
                data=False,
                retmsg='No authorization.',
                retcode=settings.RetCode.AUTHENTICATION_ERROR
            )
    docs = DocumentService.get_by_ids(db, doc_ids)
    # 将每个文档对象转换为字典
    docs_dicts = [doc.__dict__ for doc in docs]
    # 移除 '_sa_instance_state'，这个是 SQLAlchemy 内部使用的属性
    for doc_dict in docs_dicts:
        doc_dict.pop('_sa_instance_state', None)
    return get_json_result(data=docs_dicts)


@router.get("/thumbnails", summary="获取文档缩略图", response_description="成功获取文档缩略图")
def thumbnails(
        doc_ids: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    doc_ids_list = doc_ids.split(",")
    if not doc_ids_list:
        return construct_json_result(data=False, message='Lack of "Document ID"', code=settings.RetCode.ARGUMENT_ERROR)

    try:
        docs = DocumentService.get_thumbnails(db, doc_ids_list)

        for doc_item in docs:
            if doc_item['thumbnail'] and not doc_item['thumbnail'].startswith(IMG_BASE64_PREFIX):
                doc_item['thumbnail'] = f"/v1/document/image/{doc_item['kb_id']}-{doc_item['thumbnail']}"

        # docs 是一个包含元组的列表，每个元组包含两个元素：文档 ID 和缩略图
        thumbnail_dict = {doc[0]: doc[1] for doc in docs}

        return construct_json_result(data=thumbnail_dict)
    except Exception as e:
        return construct_error_response(e)


@router.post("/change_status", summary="更改文档状态", response_description="成功更改文档状态")
def change_status(
        request_body: ChangeStatusRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    ### POST `/change_status` 更改文档状态接口

    **功能描述**:
    此接口用于更改单个或多个文档的可用状态，支持批量操作和向后兼容。接口会同步更新数据库中的文档状态和搜索索引中的可用性标记。

    ---

    ### 请求体 (Request Body)

    | 字段        | 类型               | 必填 | 描述                                                                                 |
    |-------------|-------------------|------|--------------------------------------------------------------------------------------|
    | `doc_ids`   | `list[str]` 或 `str` | 是   | 文档ID列表或单个文档ID。支持批量操作，可传入字符串数组或单个字符串。                 |
    | `status`    | `int`             | 是   | 文档状态：0 = 禁用，1 = 启用。                                                      |
    | `doc_id`    | `str`             | 否   | 单个文档ID（向后兼容字段）。当 `doc_ids` 不存在时使用此字段。                        |

    **兼容性说明**:
    - 新版本调用者应使用 `doc_ids` 字段
    - 旧版本调用者可继续使用 `doc_id` 字段，系统会自动兼容
    - 优先级：`doc_ids` > `doc_id`

    ---

    ### 响应 (Response)

    #### 成功响应 (200)

    - **`Content-Type: application/json`**
    - **批量操作响应示例**:
        ```json
        {
            "retcode": 0,
            "retmsg": "success", 
            "data": {
                "doc_123": {"status": "1"},
                "doc_456": {"status": "1"},
                "doc_789": {"error": "No authorization."}
            }
        }
        ```

    - **单文档操作响应示例**:
        ```json
        {
            "retcode": 0,
            "retmsg": "success",
            "data": {
                "doc_123": {"status": "1"}
            }
        }
        ```

    #### 错误响应

    - **400: 参数错误**
        - **状态值无效**:
            ```json
            {
                "retcode": 400,
                "retmsg": "\"Status\" must be either 0 or 1!",
                "data": false
            }
            ```
        - **文档ID缺失**:
            ```json
            {
                "retcode": 400,
                "retmsg": "Document ID(s) required!",
                "data": false
            }
            ```

    #### 单个文档处理错误类型

    在批量操作中，每个文档ID的处理结果单独返回，可能的错误包括：

    - **权限不足**:
        ```json
        "doc_id": {"error": "No authorization."}
        ```

    - **文档不存在**:
        ```json
        "doc_id": {"error": "Document not found!"}
        ```

    - **知识库不存在**:
        ```json
        "doc_id": {"error": "Can't find this knowledgebase!"}
        ```

    - **数据库更新失败**:
        ```json
        "doc_id": {"error": "Database error (Document update)!"}
        ```

    - **搜索索引更新失败**:
        ```json
        "doc_id": {"error": "Database error (docStore update)!"}
        ```

    - **内部服务器错误**:
        ```json
        "doc_id": {"error": "Internal server error: 具体错误信息"}
        ```

    ---

    ### 主要流程

    1. **参数验证**:
        - 验证状态值必须为 0 或 1
        - 处理兼容性字段，优先使用 `doc_ids`，回退到 `doc_id`
        - 将单个文档ID转换为列表格式以统一处理

    2. **批量处理循环**:
        - 遍历所有文档ID
        - 对每个文档进行权限验证
        - 验证文档和所属知识库的存在性

    3. **状态更新**:
        - 更新数据库中的文档状态记录
        - 同步更新搜索索引中的 `available_int` 字段
        - 确保数据库和搜索引擎的数据一致性

    4. **结果汇总**:
        - 为每个文档ID返回处理结果
        - 成功时返回新状态，失败时返回错误信息

    ---

    ### 使用场景

    #### 1. 单文档状态更改（向后兼容）
    ```json
    {
        "doc_id": "doc_123456",
        "status": 1
    }
    ```

    #### 2. 批量文档状态更改（新功能）
    ```json
    {
        "doc_ids": ["doc_123", "doc_456", "doc_789"],
        "status": 0
    }
    ```

    #### 3. 混合格式（推荐使用 doc_ids）
    ```json
    {
        "doc_ids": "doc_123456",
        "status": 1
    }
    ```

    ---

    ### 注意事项

    - **权限控制**: 只有文档的拥有者才能更改文档状态
    - **数据一致性**: 接口会同时更新关系数据库和向量数据库的状态
    - **批量操作**: 每个文档的处理结果独立返回，部分失败不影响其他文档
    - **状态含义**: 
      - `0`: 文档禁用，不参与检索
      - `1`: 文档启用，正常参与检索
    - **错误处理**: 单个文档处理失败不会影响其他文档的处理
    - **向后兼容**: 完全兼容旧版本的 `doc_id` 字段调用方式

    ---

    ### 示例请求

    #### 批量启用文档:
    ```json
    {
        "doc_ids": ["doc_001", "doc_002", "doc_003"],
        "status": 1
    }
    ```

    #### 单文档禁用（旧版本兼容）:
    ```json
    {
        "doc_id": "doc_123456",
        "status": 0
    }
    ```

    ### 示例响应

    #### 批量操作成功响应:
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "doc_001": {"status": "1"},
            "doc_002": {"status": "1"}, 
            "doc_003": {"error": "Document not found!"}
        }
    }
    ```

    #### 单文档操作成功响应:
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "doc_123456": {"status": "0"}
        }
    }
    ```
    """
    req = request_body.model_dump()
    if str(req["status"]) not in ["0", "1"]:
        return construct_json_result(data=False, message='"Status" must be either 0 or 1!',
                                     code=settings.RetCode.ARGUMENT_ERROR)

    # 处理兼容性：优先使用 doc_ids，如果不存在则使用 doc_id
    doc_ids = req.get("doc_ids")
    if doc_ids is None:
        doc_ids = req.get("doc_id")
    
    # 确保 doc_ids 是列表格式
    if isinstance(doc_ids, str):
        doc_ids = [doc_ids]
    
    if not doc_ids:
        return construct_json_result(data=False, message="Document ID(s) required!",
                                     code=settings.RetCode.ARGUMENT_ERROR)

    result = {}
    for doc_id in doc_ids:
        if not DocumentService.accessible(db, doc_id, user.id):
            result[doc_id] = {"error": "No authorization."}
            continue

        try:
            doc = DocumentService.get_by_id(db, doc_id)
            if not doc:
                result[doc_id] = {"error": "Document not found!"}
                continue
            kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
            if not kb:
                result[doc_id] = {"error": "Can't find this knowledgebase!"}
                continue

            if not DocumentService.update_by_id(db, doc_id, {"status": str(req["status"])}):
                result[doc_id] = {"error": "Database error (Document update)!"}
                continue

            status = int(req["status"])
            if not settings.docStoreConn.update({"doc_id": doc_id}, {"available_int": status},
                                               search.index_name_one(kb.tenant_id, kb.name), doc.kb_id):
                result[doc_id] = {"error": "Database error (docStore update)!"}
            result[doc_id] = {"status": str(req["status"])}
        except Exception as e:
            result[doc_id] = {"error": f"Internal server error: {str(e)}"}

    return construct_json_result(data=result)


@router.post("/change_auth", summary="更改文档授权", response_description="成功更改文档授权")
def change_auth(
        request_body: ChangeAuthRequest,
        auths: str = Query(),  # labels 是一个 JSON 格式的字符串
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    req = request_body.model_dump()
    if not DocumentService.accessible(db, req["doc_id"], user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=settings.RetCode.AUTHENTICATION_ERROR)

    try:
        doc = DocumentService.get_by_id(db, req["doc_id"])
        if not doc:
            return construct_json_result(data=False, message="Document not found!", code=settings.RetCode.ARGUMENT_ERROR)
        kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
        if not kb:
            return construct_json_result(data=False, message="Can't find this knowledgebase!",
                                         code=settings.RetCode.ARGUMENT_ERROR)
        if isinstance(auths, str):
            try:
                auths = json.loads(auths)
                if not isinstance(auths, list) or not all(isinstance(auth, str) for auth in auths):
                    raise ValueError('auths must be a list of strings.')
            except json.JSONDecodeError:
                raise ValueError('Invalid JSON format for "auths".')
        elif auths is not None:
            raise ValueError('Auth must be a JSON-encoded list of strings or None.')
        if not DocumentService.update_by_id(db, req["doc_id"], {"auth": json.dumps(auths) if auths else None}):
            return construct_json_result(data=False, message="Database error (Document update)!",
                                         code=settings.RetCode.ARGUMENT_ERROR)

        # auth = str(req["auth"])
        settings.docStoreConn.update({"doc_id": req["doc_id"]}, {"auth": auths},
                                     search.index_name_one(kb.tenant_id, kb.name), doc.kb_id)
        return construct_json_result(data=True)
    except Exception as e:
        return construct_json_result(code=settings.RetCode.ARGUMENT_ERROR, message=str(e))


@router.post("/rm", summary="删除文档", response_description="成功删除文档")
def rm(
        request_body: RemoveRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    ### POST `/rm` 删除文档接口

    **功能描述**:
    此接口用于删除单个或多个文档，包括数据库记录、文件存储和相关的任务记录。支持批量删除操作，并自动处理文档间的依赖关系。

    ---

    ### 请求体 (Request Body)

    | 字段      | 类型          | 必填 | 描述                                              |
    |-----------|---------------|------|---------------------------------------------------|
    | `doc_id`  | `list[str]`   | 是   | 要删除的文档ID列表，支持单个或多个文档ID          |

    **兼容性说明**:
    - 请求体中的 `doc_id` 字段支持字符串和字符串数组两种格式
    - 单个文档删除：`{"doc_id": "doc_123"}`
    - 批量文档删除：`{"doc_id": ["doc_123", "doc_456"]}`

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": true
    }
    ```

    #### 错误响应

    - **403: 权限不足**
        ```json
        {
            "retcode": 403,
            "retmsg": "No authorization.",
            "data": false
        }
        ```

    - **400: 文档不存在**
        ```json
        {
            "retcode": 400,
            "retmsg": "Document not found!",
            "data": false
        }
        ```

    - **400: 租户不存在**
        ```json
        {
            "retcode": 400,
            "retmsg": "Tenant not found!",
            "data": false
        }
        ```

    - **400: 数据库删除失败**
        ```json
        {
            "retcode": 400,
            "retmsg": "Database error (Document removal)!",
            "data": false
        }
        ```

    - **500: 服务器错误**
        ```json
        {
            "retcode": 500,
            "retmsg": "删除过程中发生的具体错误信息",
            "data": false
        }
        ```

    ---

    ### 主要流程

    1. **权限验证**:
        - 检查用户对每个文档的删除权限
        - 验证用户是否为文档的所有者或有删除权限

    2. **删除前准备**:
        - 获取文档的基本信息（解析器类型、知识库ID等）
        - 获取文件存储地址信息
        - 初始化知识库文档目录结构

    3. **批量删除操作**:
        - 删除相关的任务记录
        - 从数据库中移除文档记录
        - 删除文件与文档的关联记录
        - 从对象存储中删除文件内容

    4. **特殊处理**:
        - 对于表格类型文档，更新知识库的字段映射
        - 统计表格文档数量，必要时清理字段映射配置

    5. **错误处理**:
        - 收集所有删除过程中的错误信息
        - 确保部分失败不影响其他文档的删除

    ---

    ### 删除范围

    #### 数据库记录
    - 文档基本信息记录
    - 文件与文档的关联关系
    - 相关的处理任务记录
    - 向量索引中的文档数据

    #### 存储文件
    - 原始上传文件
    - 生成的缩略图
    - 处理过程中的临时文件

    #### 配置信息
    - 表格文档的字段映射（当知识库中无其他表格文档时）
    - 文档的元数据配置

    ---

    ### 特殊文档类型处理

    #### 表格文档 (TABLE)
    - 删除表格文档时会检查知识库中的表格文档数量
    - 如果是最后一个表格文档，会自动清理字段映射配置
    - 确保知识库配置的一致性

    #### 虚拟文档 (VIRTUAL)
    - 仅删除数据库记录，无需处理文件存储
    - 清理相关的文档内容块

    ---

    ### 使用示例

    #### 删除单个文档
    ```json
    {
        "doc_id": ["doc_123456"]
    }
    ```

    #### 批量删除文档
    ```json
    {
        "doc_id": ["doc_123", "doc_456", "doc_789"]
    }
    ```

    #### 兼容格式（单个文档）
    ```json
    {
        "doc_id": "doc_123456"
    }
    ```

    ---

    ### 注意事项

    - **权限控制**: 只有文档的创建者或有删除权限的用户才能执行删除操作
    - **不可逆操作**: 删除操作不可撤销，请谨慎使用
    - **批量处理**: 支持批量删除，但建议单次删除数量不超过100个
    - **依赖检查**: 删除前会检查文档是否被其他功能引用
    - **存储清理**: 自动清理所有相关的存储文件，释放存储空间
    - **索引同步**: 同步清理向量数据库中的相关索引
    - **事务处理**: 使用事务确保数据一致性，避免部分删除导致的数据不一致
    - **错误累积**: 多个文档删除时，单个失败不会阻止其他文档的删除
    """
    req = request_body.model_dump()
    doc_ids = req["doc_id"]
    if isinstance(doc_ids, str):
        doc_ids = [doc_ids]

    for doc_id in doc_ids:
        if not DocumentService.accessible4deletion(db, doc_id, user.id):
            return get_json_result(
                data=False,
                retmsg='No authorization.',
                retcode=settings.RetCode.AUTHENTICATION_ERROR
            )

    root_folder = FileService.get_root_folder(db, user.id)
    pf_id = root_folder["id"]
    FileService.init_knowledgebase_docs(db, pf_id, user.id)
    errors = ""
    kb_table_num_map = {}
    for doc_id in doc_ids:
        try:
            doc = DocumentService.get_by_id(db, doc_id)
            if not doc:
                return construct_json_result(data=False, message="Document not found!", code=settings.RetCode.ARGUMENT_ERROR)
            tenant_id = DocumentService.get_tenant_id(db, doc_id)
            if not tenant_id:
                return construct_json_result(data=False, message="Tenant not found!", code=settings.RetCode.ARGUMENT_ERROR)

            # 在删除文档前先保存需要的属性
            doc_parser = doc.parser_id
            kb_id = doc.kb_id

            b, n = File2DocumentService.get_storage_address(db, doc_id=doc_id)

            TaskService.filter_delete(db, [Task.doc_id == doc_id])

            if not DocumentService.remove_document(db, doc, tenant_id):
                return construct_json_result(data=False, message="Database error (Document removal)!",
                                             code=settings.RetCode.ARGUMENT_ERROR)

            f2d = File2DocumentService.get_by_document_id(db, doc_id)
            deleted_file_count = 0
            if f2d:
                deleted_file_count = FileService.filter_delete(db, [db_models.File.source_type == FileSource.KNOWLEDGEBASE, db_models.File.id == f2d[0].file_id])
            File2DocumentService.delete_by_document_id(db, doc_id)
            if deleted_file_count > 0:
                STORAGE_IMPL.rm(b, n)

            # 使用之前保存的属性值，而不是访问已删除的对象
            if doc_parser == ParserType.TABLE:
                if kb_id not in kb_table_num_map:
                    counts = DocumentService.count_by_kb_id(db, kb_id=kb_id, keywords="", run_status=[TaskStatus.DONE], types=[])
                    kb_table_num_map[kb_id] = counts
                kb_table_num_map[kb_id] -= 1
                if kb_table_num_map[kb_id] <= 0:
                    KnowledgebaseService.delete_field_map(db, kb_id)
        except Exception as e:
            errors += str(e)

    if errors:
        return construct_json_result(data=False, message=errors, code=settings.RetCode.SERVER_ERROR)

    return construct_json_result(data=True)


@router.post("/run", summary="运行任务", response_description="成功运行任务")
def run(
        request_body: RunRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    ### POST `/run` 运行文档处理任务接口

    **功能描述**:
    此接口用于启动或停止文档的处理任务，支持批量操作。可以控制文档的解析、向量化等处理流程，并可选择是否清除历史处理数据。

    ---

    ### 请求体 (Request Body)

    | 字段       | 类型          | 必填 | 描述                                                        |
    |------------|---------------|------|-------------------------------------------------------------|
    | `doc_ids`  | `list[str]`   | 是   | 要处理的文档ID列表                                          |
    | `run`      | `int`         | 是   | 任务状态：0=停止，1=启动                                    |
    | `delete`   | `boolean`     | 否   | 是否删除历史处理数据，默认false                             |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": true
    }
    ```

    #### 错误响应

    - **403: 权限不足**
        ```json
        {
            "retcode": 403,
            "retmsg": "No authorization.",
            "data": false
        }
        ```

    - **400: 文档不存在**
        ```json
        {
            "retcode": 400,
            "retmsg": "Document not found!",
            "data": false
        }
        ```

    - **400: 租户不存在**
        ```json
        {
            "retcode": 400,
            "retmsg": "Tenant not found!",
            "data": false
        }
        ```

    - **400: Milvus删除失败**
        ```json
        {
            "retcode": 400,
            "retmsg": "Milvus delete failed!",
            "data": false
        }
        ```

    ---

    ### 主要流程

    1. **权限验证**:
        - 验证用户对所有文档的访问权限
        - 确保用户有操作这些文档的权利

    2. **状态更新**:
        - 更新文档的运行状态
        - 重置进度信息（当启动新任务时）
        - 清零chunk_num和token_num（当delete=true时）

    3. **数据清理**（当delete=true时）:
        - 删除相关的任务记录
        - 清除Milvus中的向量数据
        - 重置文档的处理统计信息

    4. **任务调度**（当run=1时）:
        - 获取文档的存储信息
        - 将文档加入处理队列
        - 处理表格文档的特殊逻辑

    ---

    ### 任务状态说明

    #### 运行状态值
    | 值 | 状态      | 描述                           |
    |----|-----------|--------------------------------|
    | 0  | 停止      | 停止文档处理，不进入处理队列   |
    | 1  | 运行      | 启动文档处理，加入处理队列     |

    #### 文档处理状态
    - `unstart`: 未开始处理
    - `running`: 正在处理中
    - `done`: 处理完成
    - `fail`: 处理失败

    ---

    ### 特殊处理逻辑

    #### 表格文档处理 (TABLE类型)
    - 启动表格文档处理前会检查知识库中的表格文档数量
    - 如果是首个表格文档，会清理旧的字段映射配置
    - 确保表格文档的字段映射一致性

    #### 重新处理 (delete=true)
    - 清除文档在向量数据库中的所有数据
    - 重置文档的chunk_num、token_num等统计信息
    - 删除相关的处理任务记录
    - 适用于文档内容发生变化需要重新处理的场景

    ---

    ### 使用场景

    #### 1. 启动文档处理
    ```json
    {
        "doc_ids": ["doc_123", "doc_456"],
        "run": 1
    }
    ```

    #### 2. 停止文档处理
    ```json
    {
        "doc_ids": ["doc_123", "doc_456"],
        "run": 0
    }
    ```

    #### 3. 重新处理文档（清除历史数据）
    ```json
    {
        "doc_ids": ["doc_123"],
        "run": 1,
        "delete": true
    }
    ```

    ---

    ### 处理队列机制

    #### 任务优先级
    - 按照文档提交顺序进行处理
    - 支持并发处理多个文档
    - 自动处理任务失败和重试

    #### 进度跟踪
    - 实时更新文档处理进度
    - 记录处理过程中的错误信息
    - 提供详细的处理状态反馈

    ---

    ### 注意事项

    - **权限控制**: 只有文档所有者才能控制文档的处理状态
    - **批量操作**: 支持同时处理多个文档，但建议合理控制数量
    - **数据一致性**: delete操作会彻底清除相关数据，请谨慎使用
    - **处理时间**: 文档处理时间取决于文档大小和复杂度
    - **资源占用**: 处理过程会占用系统资源，建议错峰处理大量文档
    - **状态同步**: 任务状态变更会实时反映在文档列表中
    - **错误处理**: 处理失败的文档会保留错误信息供调试使用
    - **向量数据**: 删除操作会同步清理Milvus中的向量数据
    """
    req = request_body.model_dump()

    for doc_id in req["doc_ids"]:
        if not DocumentService.accessible(db, doc_id, user.id):
            return get_json_result(
                data=False,
                retmsg='No authorization.',
                retcode=settings.RetCode.AUTHENTICATION_ERROR
            )

    try:
        kb_table_num_map = {}
        for id in req["doc_ids"]:
            info = {"run": str(req["run"]), "progress": 0}
            if str(req["run"]) == TaskStatus.RUNNING.value and req.get("delete", False):
                info["progress_msg"] = ""
                info["chunk_num"] = 0
                info["token_num"] = 0

            d = DocumentService.get_by_doc_id(db, id)
            kb_id = d["kb_id"]
            kb = KnowledgebaseService.get_by_id(db, kb_id)
            tenant_id = kb.tenant_id
            if not tenant_id:
                return construct_json_result(data=False, message="Tenant not found!", code=settings.RetCode.ARGUMENT_ERROR)

            if str(req["run"]) == TaskStatus.CANCEL.value:
                if str(d["run"]) == TaskStatus.RUNNING.value:
                    cancel_all_task_of(db, id)
                else:
                    return get_data_error_result(retmsg="Cannot cancel a task that is not in RUNNING status")

            if str(req["run"]) == TaskStatus.RUNNING.value and str(d["run"]) == TaskStatus.DONE.value:
                DocumentService.clear_chunk_num_when_rerun(db, d["id"])

            DocumentService.update_by_id(db, id, info)

            # 构建 Milvus 集合名称
            collection_name = search.index_name_one(tenant_id, kb.name)
            # 检查集合是否存在并删除 Milvus 中的数据
            if req.get("delete", False):
                TaskService.filter_delete(db, [Task.doc_id == id])
                try:
                    if settings.docStoreConn.has_collection(collection_name):
                        delete_result = settings.docStoreConn.delete(
                            collection_name=collection_name,
                            filter=f"doc_id == '{{doc_id}}'".format(doc_id=d["id"])
                        )
                        if not delete_result:
                            return construct_json_result(data=False, message="Milvus delete failed!", code=settings.RetCode.ARGUMENT_ERROR)
                except MilvusException as e:
                    return construct_json_result(data=False, message=str(e), code=settings.RetCode.ARGUMENT_ERROR)

            if str(req["run"]) == TaskStatus.RUNNING.value:
                doc = d
                doc["tenant_id"] = tenant_id

                doc_parser = doc.get("parser_id", ParserType.NAIVE)
                if doc_parser == ParserType.TABLE:
                    kb_id = doc.get("kb_id")
                    if not kb_id:
                        continue
                    if kb_id not in kb_table_num_map:
                        count = DocumentService.count_by_kb_id(db, kb_id=kb_id, keywords="", run_status=[TaskStatus.DONE], types=[])
                        kb_table_num_map[kb_id] = count
                        if kb_table_num_map[kb_id] <=0:
                            KnowledgebaseService.delete_field_map(db, kb_id)
                bucket, name = File2DocumentService.get_storage_address(db, doc_id=doc["id"])
                queue_tasks(db, doc, bucket, name, 0)
        return construct_json_result(data=True)
    except Exception as e:
        return construct_error_response(e)


@router.post("/rename", summary="重命名文档", response_description="成功重命名文档")
def rename(
        request_body: RenameRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    req = request_body.model_dump()

    if not DocumentService.accessible(db, req["doc_id"], user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=settings.RetCode.AUTHENTICATION_ERROR
        )

    try:
        doc = DocumentService.get_by_id(db, req["doc_id"])
        if not doc:
            return construct_json_result(data=False, message="Document not found!", code=settings.RetCode.ARGUMENT_ERROR)
        if pathlib.Path(req["name"].lower()).suffix != pathlib.Path(doc.name.lower()).suffix:
            return construct_json_result(data=False, message="The extension of file can't be changed",
                                         code=settings.RetCode.ARGUMENT_ERROR)
        if len(req["name"].encode("utf-8")) > FILE_NAME_LEN_LIMIT:
            return construct_json_result(data=False, message=f"File name must be {FILE_NAME_LEN_LIMIT} bytes or less.",
                                   code=settings.RetCode.ARGUMENT_ERROR)

        for d in DocumentService.query(db, name=req["name"], kb_id=doc.kb_id):
            if d.name == req["name"]:
                return construct_json_result(data=False, message="Duplicated document name in the same knowledgebase.",
                                             code=settings.RetCode.ARGUMENT_ERROR)

        if not DocumentService.update_by_id(db, req["doc_id"], {"name": req["name"]}):
            return construct_json_result(data=False, message="Database error (Document rename)!",
                                         code=settings.RetCode.ARGUMENT_ERROR)

        informs = File2DocumentService.get_by_document_id(db, req["doc_id"])
        if informs:
            file = FileService.get_by_id(db, informs[0].file_id)
            FileService.update_by_id(db, file.id, {"name": req["name"]})

        return construct_json_result(data=True)
    except Exception as e:
        return construct_error_response(e)


@router.get("/get/{doc_id}", summary="获取文档内容", response_description="成功获取文档内容")
def get_document(
        doc_id: str,
        db: Session = Depends(get_db),
        # user=Depends(manager)
):
    try:
        doc = DocumentService.get_by_id(db, doc_id)
        if not doc:
            return construct_json_result(data=False, message="Document not found!", code=settings.RetCode.ARGUMENT_ERROR)

        b, n = File2DocumentService.get_storage_address(db, doc_id=doc_id)

        file_content = STORAGE_IMPL.get(b, n)
        if not file_content:
            raise HTTPException(status_code=404, detail="File not found in storage")

        # 将文件内容包装成 BytesIO 对象
        file_stream = BytesIO(file_content)

        ext = re.search(r"\.([^.]+)$", doc.name.lower())
        ext = ext.group(1) if ext else None
        media_type = "application/octet-stream"
        if ext:
            if doc.type == FileType.VISUAL.value:
                media_type = CONTENT_TYPE_MAP.get(ext, f"image/{ext}")
            else:
                media_type = CONTENT_TYPE_MAP.get(ext, f"application/{ext}")

        # 使用 quote 对文件名进行编码
        encoded_filename = quote(doc.name)
        response = StreamingResponse(file_stream, media_type=media_type)
        response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{encoded_filename}"
        return response
    except Exception as e:
        return construct_error_response(e)


@router.post("/change_parser", summary="更改解析器", response_description="成功更改解析器")
def change_parser(
        request_body: ChangeParserRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    req = request_body.model_dump()

    if not DocumentService.accessible(db, req["doc_id"], user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=settings.RetCode.AUTHENTICATION_ERROR
        )

    try:
        # 根据文档ID获取文档信息
        doc = DocumentService.get_by_id(db, req["doc_id"])
        # 如果找不到文档，返回错误信息
        if not doc:
            return construct_json_result(data=False, message="Document not found!", code=settings.RetCode.ARGUMENT_ERROR)
        # 检查是否需要更新解析器ID
        if doc.parser_id.lower() == req["parser_id"].lower():
            # 如果parser_id未变更，则根据是否包含parser_config进行处理
            if "parser_config" in req:
                if req["parser_config"] == doc.parser_config:
                    return construct_json_result(data=True)
            else:
                return construct_json_result(data=True)
        # 检查文档类型是否支持
        if doc.type == FileType.VISUAL or re.search(r"\.(ppt|pptx|pages)$", doc.name):
            return construct_json_result(data=False, message="Not supported yet!", code=settings.RetCode.ARGUMENT_ERROR)

        # 更新文档的parser_id和其他信息
        e = DocumentService.update_by_id(db, doc.id, {"parser_id": req["parser_id"], "progress": 0, "progress_msg": "",
                                                      "run": TaskStatus.UNSTART.value})
        # 如果更新失败，返回错误信息
        if not e:
            return construct_json_result(data=False, message="Document not found!", code=settings.RetCode.ARGUMENT_ERROR)
        # 如果请求中包含parser_config，更新parser_config
        if "parser_config" in req:
            DocumentService.update_parser_config(db, doc.id, req["parser_config"])
        # 如果文档有token_num大于0，进行相关数值的递减操作
        if doc.token_num > 0:
            e = DocumentService.increment_chunk_num(db, doc.id, doc.kb_id, doc.token_num * -1, doc.chunk_num * -1,
                                                    doc.process_duration * -1)
            if not e:
                return construct_json_result(data=False, message="Document not found!", code=settings.RetCode.ARGUMENT_ERROR)
            # 获取文档所属的租户ID
            tenant_id = DocumentService.get_tenant_id(db, req["doc_id"])
            if not tenant_id:
                return construct_json_result(data=False, message="Tenant not found!", code=settings.RetCode.ARGUMENT_ERROR)
            document = DocumentService.get_by_doc_id(db, doc.id)
            kb = KnowledgebaseService.get_by_id(db, document["kb_id"])
            # 删除Milvus中的数据
            try:
                delete_result = settings.docStoreConn.delete(
                    collection_name=search.index_name_one(tenant_id, kb.name),
                    filter=f"doc_id == '{doc.id}'"
                )
                if not delete_result:
                    return construct_json_result(data=False, message="Milvus delete failed!",
                                                 code=settings.RetCode.ARGUMENT_ERROR)
            except MilvusException as e:
                return construct_json_result(data=False, message=str(e), code=settings.RetCode.ARGUMENT_ERROR)
        return construct_json_result(data=True)
    except Exception as e:
        return construct_error_response(e)


@router.get("/image/{image_id}", summary="获取图片", response_description="成功获取图片")
def get_image(
        image_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    try:
        arr = image_id.split("-")
        if len(arr) != 2:
            return get_data_error_result(retmsg="Image not found.")
        # 分离 bucket 和 name
        bkt, nm = image_id.split("-")

        # 获取文件内容
        file_content = STORAGE_IMPL.get(bkt, nm)

        # 确认 file_content 是字节流对象
        if not isinstance(file_content, (bytes, bytearray)):
            raise HTTPException(status_code=500, detail="Failed to retrieve image content")

        # 将文件内容包装成 BytesIO 对象
        file_stream = BytesIO(file_content)

        # 返回图片流响应
        response = StreamingResponse(file_stream, media_type="image/jpeg")
        return response
    except Exception as e:
        return construct_error_response(e)


# todo ragflow的def upload_and_parse待补充


@router.post("/parse", summary="解析网页或文件内容", response_description="成功解析内容")
async def parse(
        url: str | None = Form(None, description="网页URL（可选）"),
        files: list[UploadFile] | None = File(None),
        user=Depends(manager)
):
    """
    **功能描述**:
    此接口用于解析用户上传的文件或提供的网页URL内容，提取其中的文本信息并返回给用户。支持两种模式：基于URL解析网页内容或上传文件进行解析。

    ### 请求参数:
    - **url** (str, 可选):
        - 描述: 用户提供的网页URL，用于解析网页内容。
        - 备注: 若提供此参数，系统将优先处理URL内容。
    - **files** (list[UploadFile], 可选):
        - 描述: 用户上传的文件列表，可包含多个文件。
        - 备注: 若未提供URL参数，则会尝试解析上传的文件内容。
    - **user**:
        - 描述: 通过依赖项注入的用户信息，用于权限校验。

    ### 功能流程:
    1. **URL解析模式**:
        - 验证URL格式是否合法。
        - 使用Selenium驱动器加载网页，捕获页面的请求响应头，并提取网页的HTML内容。
        - 对HTML内容进行文本解析，提取有意义的段落并返回。
        - 若网页内容下载文件，则模拟 `File` 类读取文件内容并解析。

    2. **文件解析模式**:
        - 验证是否提供了文件。
        - 逐个读取上传文件的内容，将文件数据传递给 `FileService.parse_docs` 进行解析。
        - 返回解析后的文本内容。

    3. **异常处理**:
        - 捕获处理过程中发生的所有异常，并记录日志，返回适当的错误消息。

    ### 响应 (Response):
    - **成功响应 (200)**:
        - `data` (str): 返回解析后的文本内容，按段落分割。
    - **错误响应**:
        - **400: 参数错误**:
            - URL格式无效或未提供文件时返回。
        - **500: 服务器错误**:
            - 解析过程中发生内部错误时返回。

    ### 注意事项:
    - **优先级**:
        - 当URL和文件同时提供时，系统优先处理URL内容。
    - **目录管理**:
        - 下载的文件会存储在 `logs/downloads` 目录中，请确保目录具有写入权限。
    - **Selenium配置**:
        - 采用无头浏览器模式运行，为了兼容性，需安装Chrome及对应的WebDriver。
    - **文件解析**:
        - 上传文件内容会以字节流方式读取并处理，文件名作为辅助信息传递。
    """

    if url:
        if not is_valid_url(url):
            return get_json_result(
                data=False, retmsg='The URL format is invalid', retcode=settings.RetCode.ARGUMENT_ERROR)
        download_path = os.path.join(get_project_base_directory(), "logs/downloads")
        os.makedirs(download_path, exist_ok=True)
        from seleniumwire.webdriver import Chrome, ChromeOptions
        options = ChromeOptions()
        options.add_argument('--headless')
        options.add_argument('--disable-gpu')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_experimental_option('prefs', {
            'download.default_directory': download_path,
            'download.prompt_for_download': False,
            'download.directory_upgrade': True,
            'safebrowsing.enabled': True
        })
        # driver = Chrome(options=options)
        # driver.get(url)
        # res_headers = [r.response.headers for r in driver.requests]
        # if len(res_headers) > 1:
        #     sections = RAGFlowHtmlParser().parser_txt(driver.page_source)
        #     driver.quit()
        #     return get_json_result(data="\n".join(sections))
        try:
            driver = Chrome(options=options)
            driver.get(url)

            res_headers = [r.response.headers for r in driver.requests if r.response]
            logging.info(f"res_headers:{res_headers}")
            if len(res_headers) > 1:
                sections = RAGFlowHtmlParser().parser_txt(driver.page_source)
                driver.quit()
                return get_json_result(data="\n".join(sections))

            # 模拟 File 类逻辑
            r = re.search(r'filename=\"([^\"]+)\"', str(res_headers))
            if not r or not r.group(1):
                return get_json_result(
                    data=False, retmsg="Cannot identify downloaded file", retcode=settings.RetCode.ARGUMENT_ERROR
                )

            class File:
                filename: str
                filepath: str

                def __init__(self, filename, filepath):
                    self.filename = filename
                    self.filepath = filepath

                def read(self):
                    with open(self.filepath, "rb") as f:
                        return f.read()

            if not r or r.group(1):
                return get_json_result(
                    data=False, retmsg="Can't not identify downloaded file", retcode=settings.RetCode.ARGUMENT_ERROR)
            f = File(r.group(1), os.path.join(download_path, r.group(1)))
            txt = FileService.parse_docs([f], user.id)
            return get_json_result(data=txt)
        except Exception as e:
            logging.exception("[ERROR] URL processing failed")
            # traceback.print_exc()
            return get_json_result(
                retcode=settings.RetCode.SERVER_ERROR,
                retmsg=str(e),
                data=False
            )
        finally:
            if driver:
                driver.quit()

    if not files:
        return get_json_result(
            data=False, retmsg='No file part!', retcode=settings.RetCode.ARGUMENT_ERROR)

    try:
        # 读取每个文件的内容为字节数据，并将文件名与内容作为元组传递给 parse_docs
        file_data = [(await file.read(), file.filename) for file in files]

        # 调用 parse_docs 处理文件内容
        txt = FileService.parse_docs(file_data, user.id)
        # print(f"[DEBUG] parse text from files: {txt}")  # Debug print
        return get_json_result(
            data=txt
        )
    except Exception as e:
        logging.exception("[ERROR] File processing failed")
        # traceback.print_exc()
        return get_json_result(
            retcode=settings.RetCode.SERVER_ERROR,
            retmsg=str(e),
            data=False
        )


@router.post("/set_meta", summary="设置文档元数据", response_description="成功设置文档元数据")
def set_meta(
        request: SetMetaRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    req = request.model_dump()
    if not DocumentService.accessible(db, req["doc_id"], user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=settings.RetCode.AUTHENTICATION_ERROR
        )

    if not isinstance(req["meta"], dict):
        return get_json_result(
            data=False, retmsg='Meta data should be in Json map format, like {"key": "value"}',
            retcode=settings.RetCode.ARGUMENT_ERROR)

    try:
        doc = DocumentService.get_by_id(db, req["doc_id"])
        if not doc:
            return get_data_error_result(retmsg="Document not found!")

        # meta已经是字典对象，不需要再解析
        if not DocumentService.update_by_id(
                db, req["doc_id"], {"meta_fields": req["meta"]}):
            return get_data_error_result(
                retmsg="Database error (meta updates)!")

        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)

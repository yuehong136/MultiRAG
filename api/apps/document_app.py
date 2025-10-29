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
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Form, Body, Request
from fastapi.responses import StreamingResponse
from pymilvus import MilvusException
from sqlalchemy.orm import Session
from urllib.parse import quote
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    HTTP_500_INTERNAL_SERVER_ERROR,
)

from api.constants import FILE_NAME_LEN_LIMIT, IMG_BASE64_PREFIX
from api.db import VALID_FILE_TYPES, VALID_TASK_STATUS, FileType, TaskStatus, ParserType, FileSource, db_models
from api.db.db_models import Task, get_db
from api.db.services import duplicate_name
from api.db.services.document_service import DocumentService
from api.db.services.document_analysis_service import DocumentAnalysisService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.task_service import TaskService, cancel_all_task_of, queue_tasks, queue_dataflow
from api.db.services.user_service import UserTenantService
from deepdoc.parser.html_parser import RAGFlowHtmlParser
from api import settings
from api.common.check_team_permission import check_kb_team_permission
from api.utils.api_utils import construct_json_result, construct_error_response, convert_datetime_to_str, \
    get_json_result, get_data_error_result, server_error_response
from api.utils import get_uuid
from api.utils.file_utils import filename_type, thumbnail, get_project_base_directory
from api.utils.web_utils import CONTENT_TYPE_MAP, html2pdf, is_valid_url
from core.nlp import search
from core.utils.storage_factory import STORAGE_IMPL
from api.apps import manager

from pydantic import BaseModel, Field, ValidationError, field_validator

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
    parser_id: str | None = Field(None, description="解析器ID")
    parser_config: dict | None = Field(None, description="解析器配置")
    pipeline_id: str | None = Field(None, description="Pipeline ID")

class SetMetaRequest(BaseModel):
    doc_id: str = Field(..., description="文档ID")
    meta: dict[str, Any] = Field(..., description="元数据对象")


class FilterRequest(BaseModel):
    kb_id: str = Field(..., description="知识库ID")
    keywords: str = Field(default="", description="关键词")
    suffix: list[str] = Field(default=[], description="文件后缀过滤")
    run_status: list[str] = Field(default=[], description="运行状态过滤")
    types: list[str] = Field(default=[], description="文件类型过滤")


class PreviewChunksRequest(BaseModel):
    doc_id: str | None = Field(default=None, description="文档ID（与 file 二选一）")
    parser_config: dict | None = Field(default=None, description="解析配置覆盖（可选）")
    limit: int | None = Field(default=50, description="最多返回的切片条数（可选，非批次模式）")
    batch_size: int | None = Field(default=None, description="批次大小（启用批次模式时必填）")
    batch_id: str | None = Field(default=None, description="批次会话ID（续取时携带）")
    parser_id: str | None = Field(default=None, description="手动指定解析器（可选，受文件类型支持列表校验）")
    batch_index: int | None = Field(default=None, description="并发批次号（从0开始）；指定则按批次号取片段，不推进会话offset")


class WebParseOptions(BaseModel):
    # 通用站点抓取选项（为未来 provider 预留）
    crawl_sub_pages: bool | None = Field(default=None)
    only_main_content: bool | None = Field(default=None)
    includes: str | None = Field(default=None)
    excludes: str | None = Field(default=None)
    limit: int | None = Field(default=None, description="抓取的最大页面数量（仅部分 provider 支持）")
    max_depth: int | None = Field(default=None, description="抓取子页面的最大深度（仅部分 provider 支持）")
    use_sitemap: bool | None = Field(default=None)
    # Tavily Extract 专属可选项（与 core/utils/tavily_conn.py 对齐）
    include_images: bool | None = Field(default=None, description="是否在响应中包含图片URL列表（默认 False）")
    extract_depth: Literal["basic", "advanced"] | None = Field(
        default=None,
        description=(
            "提取深度：basic（默认，低延迟，成功率适中，1信用/成功5URL）| advanced（更高成功率与数据量，如表格/嵌入内容，2信用/成功5URL）"
        ),
    )
    format: Literal["markdown", "text"] | None = Field(
        default=None,
        description="提取内容格式：markdown（默认）| text（纯文本，可能增加延迟）",
    )
    timeout: int | None = Field(
        default=None, ge=1, le=60,
        description="超时时间（秒），1~60。未指定时：basic=10s，advanced=30s",
    )
    include_favicon: bool | None = Field(default=None, description="是否包含 favicon（默认 False）")

    @field_validator("limit", "max_depth", "timeout", mode="before")
    @classmethod
    def _empty_str_to_none_and_cast_int(cls, v):
        if v is None:
            return v
        if isinstance(v, str):
            vv = v.strip()
            if vv == "":
                return None
            try:
                return int(vv)
            except ValueError:
                return v
        return v


class WebParseCredentials(BaseModel):
    # 针对不同 provider 的凭据，如 Tavily 的 api_key
    api_key: str | None = Field(default=None)


class WebParseRequest(BaseModel):
    url: str = Field(..., description="要解析的网页 URL")
    provider: Literal["tavily", "jinareader"] = Field(
        ..., description="解析提供方：tavily | jinareader（后者待集成）"
    )
    options: WebParseOptions | None = Field(default=None, description="解析选项")
    credentials: WebParseCredentials | None = Field(default=None, description="第三方凭据，如 API Key")


class RaptorConfig(BaseModel):
    """RAPTOR配置"""
    max_cluster: int = Field(default=64, ge=1, le=128, description="最大聚类数")
    max_token: int = Field(default=512, ge=128, le=2048, description="摘要最大token数")
    threshold: float = Field(default=0.1, ge=0.0, le=1.0, description="聚类阈值")
    random_seed: int = Field(default=42, description="随机种子")
    prompt: str | None = Field(default=None, description="自定义prompt")


class DocumentAnalysisRequest(BaseModel):
    """文档分析请求"""
    doc_id: str = Field(..., description="文档ID")
    include_summary: bool = Field(default=True, description="是否生成摘要")
    include_tags: bool = Field(default=True, description="是否生成标签")
    summary_type: str = Field(default="short", description="摘要类型: short|long")
    raptor_config: RaptorConfig | None = Field(default=None, description="RAPTOR配置")
    use_cache: bool = Field(default=True, description="是否使用缓存")


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
    if not check_kb_team_permission(db, kb, user.id):
        return get_json_result(data=False, retmsg='No authorization.', retcode=settings.RetCode.AUTHENTICATION_ERROR)

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
    if not check_kb_team_permission(db, kb, user.id):
        return get_json_result(data=False, retmsg='No authorization.', retcode=settings.RetCode.AUTHENTICATION_ERROR)

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
            "pipeline_id": kb.pipeline_id,
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

        kb_root_folder = FileService.get_kb_folder(db, kb.tenant_id)
        if not kb_root_folder:
            return get_data_error_result(retmsg="Cannot find the root folder.")
        kb_folder = FileService.new_a_file_from_kb(
            db,
            kb.tenant_id,
            kb.name,
            kb_root_folder["id"],
        )
        if not kb_folder:
            return get_data_error_result(retmsg="Cannot find the kb folder for this file.")

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
        kb_id: str = Query(..., description="知识库 ID"),
        keywords: str = Query("", description="关键字"),
        page: int = Query(0, description="分页页码"),
        page_size: int = Query(0, description="分页大小"),
        orderby: str = Query("create_time", description="排序字段"),
        desc: bool = Query(True, description="是否倒序"),
        create_time_from: int | None = Query(0, description="创建时间起（时间戳）"),
        create_time_to: int | None = Query(0, description="创建时间止（时间戳）"),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    ### POST `/list` 列出文档接口

    **功能描述**:
    此接口用于获取指定知识库中的文档列表，支持关键词搜索、分页查询、排序及多条件过滤（运行状态、文件类型、文件后缀、创建时间范围）。返回文档的基本信息和缩略图。

    ---

    ### 请求参数

    #### Query Parameters
    | 参数名              | 类型      | 必填 | 默认值       | 描述                                                    |
    |---------------------|-----------|------|-------------|--------------------------------------------------------|
    | `kb_id`             | `string`  | 是   | -           | 知识库的唯一标识符                                      |
    | `keywords`          | `string`  | 否   | ""          | 搜索关键词，支持文档名称模糊匹配                        |
    | `page`              | `int`     | 否   | 0           | 页码，从0开始，0表示不分页                              |
    | `page_size`         | `int`     | 否   | 0           | 每页返回的文档数量，0表示不分页                          |
    | `orderby`           | `string`  | 否   | create_time | 排序字段，支持: create_time, name, size, update_time   |
    | `desc`              | `boolean` | 否   | true        | 是否降序排列                                           |
    | `create_time_from`  | `int`     | 否   | 0           | 创建时间范围起始（Unix时间戳，0表示不限制）             |
    | `create_time_to`    | `int`     | 否   | 0           | 创建时间范围结束（Unix时间戳，0表示不限制）             |

    #### JSON Body (DocumentFilter)
    | 字段名        | 类型          | 必填 | 默认值 | 描述                                            |
    |---------------|--------------|------|--------|------------------------------------------------|
    | `run_status`  | `list[string]` | 否   | []     | 运行状态过滤，支持: unstart, running, done, fail |
    | `types`       | `list[string]` | 否   | []     | 文件类型过滤，需在系统支持类型列表中            |
    | `suffix`      | `list[string]` | 否   | []     | 文件后缀名过滤                                  |

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

    - **400: 过滤条件无效**
        ```json
        {
            "retcode": 400,
            "retmsg": "Invalid filter run status conditions: abc",
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

    2. **参数校验**:
        - 校验 `run_status` 是否在有效范围
        - 校验 `types` 是否在支持的文件类型列表中

    3. **数据查询**:
        - 根据关键词进行模糊搜索
        - 按分页与排序参数查询文档
        - 根据 `create_time_from` 和 `create_time_to` 过滤时间范围
        - 统计符合条件的文档总数

    4. **结果处理**:
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
    POST /v1/document/list?kb_id=kb_123456
    Body: {}
    ```

    #### 关键词搜索
    ```
    POST /v1/document/list?kb_id=kb_123456&keywords=技术文档
    Body: {}
    ```

    #### 分页查询
    ```
    POST /v1/document/list?kb_id=kb_123456&page=2&page_size=20
    Body: {}
    ```

    #### 自定义排序
    ```
    POST /v1/document/list?kb_id=kb_123456&orderby=size&desc=false
    Body: {}
    ```

    #### 按时间范围筛选
    ```
    POST /v1/document/list?kb_id=kb_123456&create_time_from=1700000000&create_time_to=1700500000
    Body: {}
    ```

    #### 多条件过滤
    ```
    POST /v1/document/list?kb_id=kb_123456
    Body: {
        "run_status": ["done", "running"],
        "types": ["pdf", "docx"],
        "suffix": ["pdf"]
    }
    ```

    ---

    ### 注意事项

    - **权限控制**: 只有知识库所有者才能查看文档列表
    - **缩略图处理**: 自动处理缩略图URL，支持base64和文件路径两种格式
    - **时间格式**: 所有时间字段统一转换为字符串格式返回
    - **性能优化**: 建议合理设置page_size，避免单次查询过多数据
    - **搜索范围**: 关键词搜索仅匹配文档名称，不包含文档内容
    - **时间过滤**: `create_time_from` 和 `create_time_to` 为 Unix 时间戳，0 表示不限制
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

        # === 新增的时间范围过滤逻辑 ===
        if create_time_from or create_time_to:
            docs = [
                doc for doc in docs
                if (create_time_from == 0 or doc.get("create_time", 0) >= create_time_from)
                and (create_time_to == 0 or doc.get("create_time", 0) <= create_time_to)
            ]
        # 处理缩略图路径
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


@router.post("/web_parse", summary="解析网页（按 provider 调用）", response_description="返回网页文本与原始响应")
def web_parse(
        request: WebParseRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    ### POST `/web_parse` 解析网页（多 Provider）

    **功能描述**:
    按 `provider` 调用对应的网页解析能力，将网页内容解析为文本并返回，同时附带原始响应数据，便于排障与二次处理。

    ---

    ### 请求体 (Request Body)

    | 字段          | 类型                                   | 必填 | 描述                                                                 |
    |---------------|----------------------------------------|------|----------------------------------------------------------------------|
    | `url`         | `string`                               | 是   | 要解析的网页 URL                                                      |
    | `provider`    | `"tavily" | "jinareader"`              | 是   | 解析提供方。当前已接入 `tavily`；`jinareader` 预留待集成             |
    | `options`     | `WebParseOptions`                      | 否   | 解析选项。通用字段 + 各 provider 专属字段（见下表）                   |
    | `credentials` | `WebParseCredentials`                  | 否   | 第三方凭据。`tavily` 需 `api_key`。未来可从凭据表自动读取（可不传）   |

    #### WebParseOptions（通用 + Tavily 专属）
    | 字段               | 类型                           | 默认值     | 说明                                                                                   |
    |--------------------|--------------------------------|------------|----------------------------------------------------------------------------------------|
    | `crawl_sub_pages`  | `boolean`                      | `null`     | 是否抓取子页面（部分 provider 支持）                                                   |
    | `only_main_content`| `boolean`                      | `null`     | 仅解析主内容（部分 provider 支持）                                                     |
    | `includes`         | `string`                       | `null`     | 包含匹配（部分 provider 支持）                                                         |
    | `excludes`         | `string`                       | `null`     | 排除匹配（部分 provider 支持）                                                         |
    | `limit`            | `int`                          | `null`     | 抓取的最大页面数量（部分 provider 支持）                                               |
    | `max_depth`        | `int`                          | `null`     | 抓取子页面的最大深度（部分 provider 支持）                                             |
    | `use_sitemap`      | `boolean`                      | `null`     | 是否使用 sitemap（部分 provider 支持）                                                 |
    | `include_images`   | `boolean`                      | `False`    | Tavily：是否在响应中包含图片 URL 列表                                                   |
    | `extract_depth`    | `"basic" | "advanced"`        | `basic`    | Tavily：提取深度。basic 低延迟/1信用/成功5URL；advanced 成功率更高/更多数据/2信用/5URL |
    | `format`           | `"markdown" | "text"`        | `markdown`| Tavily：返回格式。markdown 更快；text 为纯文本，可能增加延迟                            |
    | `timeout`          | `int (1~60)`                   | `自动`     | Tavily：最大等待秒数。未指定时：basic=10s，advanced=30s                                 |
    | `include_favicon`  | `boolean`                      | `False`    | Tavily：是否包含 favicon                                                                |

    #### WebParseCredentials
    | 字段       | 类型     | 必填 | 说明                          |
    |------------|----------|------|-------------------------------|
    | `api_key`  | `string` | 否   | 第三方 API Key（Tavily 需要） |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
      "retcode": 0,
      "retmsg": "success",
      "data": {
        "provider": "tavily",
        "url": "https://example.com",
        "texts": ["解析出的纯文本1", "解析出的纯文本2"],
        "raw": {
          "results": [
            {"url": "https://example.com", "raw_content": "...", "images": [], "favicon": "..."}
          ],
          "failed_results": [],
          "response_time": 1.23,
          "request_id": "uuid"
        }
      }
    }
    ```

    #### 错误响应
    - **400: 参数错误/未配置凭据**
      ```json
      {"retcode": 400, "retmsg": "Tavily requires credentials.api_key", "data": false}
      ```
    - **400: 不支持的 provider**
      ```json
      {"retcode": 400, "retmsg": "Unsupported provider: xxx", "data": false}
      ```
    - **500: 上游服务错误或内部错误**
      ```json
      {"retcode": 500, "retmsg": "具体错误信息", "data": false}
      ```

    ---

    ### 主要流程
    1. 解析请求体，校验 `provider`。
    2. Service 层根据 `provider` 调用对应适配器（当前支持 Tavily Extract）。
    3. 透传 `options` 中与该 provider 相关的字段；凭据来自 `credentials.api_key`。
    4. 统一组装输出：`texts` 为纯文本数组，`raw` 为原始响应（含 `request_id/response_time/failed_results`）。

    ---

    ### 使用示例
    ```json
    {
      "url": "https://jw.dhu.edu.cn/2025/0623/c22070a363167/page.htm",
      "provider": "tavily",
      "options": {
        "extract_depth": "advanced",
        "format": "markdown",
        "include_images": false,
        "include_favicon": false,
        "timeout": 20
      },
      "credentials": {"api_key": "tvly-***"}
    }
    ```

    ---

    ### 注意事项
    - `provider = tavily` 需有效 `api_key`；未来支持从凭据表自动注入后可不在请求体传递。
    - `extract_depth/format/timeout` 语义遵循 Tavily 官方文档；`timeout` 范围 1~60 秒。
    - `texts` 返回的是清洗后的纯文本；原始字段请在 `raw.results[*].raw_content` 获取。
    - `jinareader` 为预留 provider，集成后在同一接口下直接可用。
    """
    try:
        url = request.url
        provider = request.provider
        options = request.options.model_dump(exclude_none=True) if request.options else None
        credentials = request.credentials.model_dump(exclude_none=True) if request.credentials else None

        result = DocumentService.parse_web_by_provider(
            provider=provider,
            url=url,
            options=options,
            credentials=credentials,
        )
        return get_json_result(data=result)
    except Exception as e:
        return construct_error_response(e)


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
        doc_ids: list[str] = Query(..., description="文档ID列表，例如 ?doc_ids=1&doc_ids=2"),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    if not doc_ids:
        return construct_json_result(data=False, message='Lack of "Document ID"', code=settings.RetCode.ARGUMENT_ERROR)

    try:
        docs = DocumentService.get_thumbnails(db, doc_ids)

        for doc_item in docs:
            if doc_item['thumbnail'] and not doc_item['thumbnail'].startswith(IMG_BASE64_PREFIX):
                doc_item['thumbnail'] = f"/v1/document/image/{doc_item['kb_id']}-{doc_item['thumbnail']}"

        return get_json_result(data={d["id"]: d["thumbnail"] for d in docs})
    except Exception as e:
        return server_error_response(e)


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

            if all([("delete" not in req or req["delete"]), str(req["run"]) == TaskStatus.RUNNING.value, str(d["run"]) == TaskStatus.DONE.value]):
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
                if doc.get("pipeline_id", ""):
                    queue_dataflow(db, tenant_id, flow_id=doc["pipeline_id"], task_id=get_uuid(), doc_id=id)
                else:
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
    """
    更改文档的解析器或Pipeline配置
    
    概要：允许用户修改文档的解析器类型（parser_id）、解析器配置（parser_config）或Pipeline配置（pipeline_id），并重置文档处理状态。
    
    参数：
    - **request_body**: 请求体，包含：
        - doc_id: 文档ID（必填）
        - parser_id: 解析器ID（可选），如 "naive", "paper", "book", "laws", "presentation", "manual", "qa", "table", "resume", "picture", "one", "knowledge_graph", "email"
        - parser_config: 解析器配置（可选），JSON对象，包含解析器的各种参数
        - pipeline_id: Pipeline ID（可选），指定使用哪个Pipeline进行处理
    
    返回：
    - dict: 操作结果
        - data: True 表示更改成功
    
    功能：
    1. 验证用户对文档的访问权限
    2. 获取文档信息
    3. 根据请求类型执行不同的操作：
       - 如果包含 pipeline_id：更新Pipeline配置并重置文档
       - 如果包含 parser_id：更新解析器配置并重置文档
    4. 重置文档时会：
       - 清空处理进度和状态
       - 递减知识库的统计数据（token_num、chunk_num等）
       - 删除向量数据库中的文档chunks
    
    内部函数 reset_doc()：
    - 更新文档的parser_id、进度和状态
    - 如果文档已有tokens，则：
      - 递减知识库的token_num、chunk_num和process_duration
      - 从向量数据库中删除该文档的所有chunks
    
    业务场景：
    1. **更换解析器**：
       - 发现当前解析器效果不佳，切换到更合适的解析器
       - 例如：从 "naive" 切换到 "paper" 以更好地解析学术论文
    
    2. **调整解析参数**：
       - 修改chunk_token_num、delimiter等参数优化分块效果
       - 调整layout_recognize选择不同的版面识别引擎
    
    3. **切换Pipeline**：
       - 更换处理流程（如从简单处理切换到包含GraphRAG的复杂流程）
       - 适配不同的业务需求
    
    验证逻辑：
    - 如果更新Pipeline：检查pipeline_id是否与当前相同，相同则直接返回成功
    - 如果更新解析器：
      - 检查parser_id和parser_config是否与当前完全相同
      - 检查文档类型是否支持指定的解析器
      - VISUAL类型文档只能使用 "picture" 解析器
      - PPT/PPTX/Pages文档只能使用 "presentation" 解析器
    
    权限要求：
    - 用户必须对该文档有访问权限（accessible检查）
    
    异常处理：
    - 如果用户无权限，返回 AUTHENTICATION_ERROR
    - 如果文档不存在，返回 "Document not found!"
    - 如果文档类型不支持指定解析器，返回 "Not supported yet!"
    - 如果租户不存在，返回 "Tenant not found!"
    - 如果向量数据库删除失败，返回 "Milvus delete failed!"
    - 其他异常返回服务器错误
    
    注意：
    - 更改解析器会清空文档的处理结果，需要重新运行解析任务
    - 如果文档已经处理过（token_num > 0），会删除所有已生成的chunks
    - 操作不可逆，请确认后再执行
    - parser_id和pipeline_id至少需要提供一个
    - 更新parser_config不会触发文档重置（如果parser_id未变）
    
    使用示例：
    1. 更换解析器：{"doc_id": "xxx", "parser_id": "paper"}
    2. 调整参数：{"doc_id": "xxx", "parser_id": "naive", "parser_config": {"chunk_token_num": 512}}
    3. 切换Pipeline：{"doc_id": "xxx", "pipeline_id": "yyy"}
    """
    req = request_body.model_dump()

    if not DocumentService.accessible(db, req["doc_id"], user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=settings.RetCode.AUTHENTICATION_ERROR
        )

    doc = DocumentService.get_by_id(db, req["doc_id"])
    if not doc:
        return get_data_error_result(retmsg="Document not found!")

    def reset_doc():
        """重置文档的处理状态和数据"""
        e = DocumentService.update_by_id(
            db, doc.id,
            {
                "parser_id": req.get("parser_id", doc.parser_id),
                "progress": 0,
                "progress_msg": "",
                "run": TaskStatus.UNSTART.value
            }
        )
        if not e:
            return get_data_error_result(retmsg="Document not found!")
        
        if doc.token_num > 0:
            e = DocumentService.increment_chunk_num(
                db, doc.id, doc.kb_id,
                doc.token_num * -1,
                doc.chunk_num * -1,
                doc.process_duration * -1
            )
            if not e:
                return get_data_error_result(retmsg="Document not found!")
            
            tenant_id = DocumentService.get_tenant_id(db, req["doc_id"])
            if not tenant_id:
                return get_data_error_result(retmsg="Tenant not found!")
            
            document = DocumentService.get_by_doc_id(db, doc.id)
            kb = KnowledgebaseService.get_by_id(db, document["kb_id"])
            
            # 删除向量数据库中的数据
            try:
                delete_result = settings.docStoreConn.delete(
                    collection_name=search.index_name_one(tenant_id, kb.name),
                    filter=f"doc_id == '{doc.id}'"
                )
                if not delete_result:
                    return get_data_error_result(retmsg="Milvus delete failed!")
            except MilvusException as e:
                return get_data_error_result(retmsg=str(e))
        
        return None

    try:
        # 处理 pipeline_id 更新
        if "pipeline_id" in req and req["pipeline_id"] is not None:
            if doc.pipeline_id == req["pipeline_id"]:
                return get_json_result(data=True)
            
            DocumentService.update_by_id(db, doc.id, {"pipeline_id": req["pipeline_id"]})
            error = reset_doc()
            if error:
                return error
            return get_json_result(data=True)

        # 处理 parser_id 更新
        if req.get("parser_id"):
            if doc.parser_id.lower() == req["parser_id"].lower():
                if "parser_config" in req and req["parser_config"] is not None:
                    if req["parser_config"] == doc.parser_config:
                        return get_json_result(data=True)
                else:
                    return get_json_result(data=True)

            # 检查文档类型是否支持指定的解析器
            if (doc.type == FileType.VISUAL and req["parser_id"] != "picture") or (re.search(r"\.(ppt|pptx|pages)$", doc.name) and req["parser_id"] != "presentation"):
                return get_data_error_result(retmsg="Not supported yet!")
            
            # 更新parser_config（如果提供）
            if "parser_config" in req and req["parser_config"] is not None:
                DocumentService.update_parser_config(db, doc.id, req["parser_config"])
            
            error = reset_doc()
            if error:
                return error
        
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


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
    """为指定文档写入自定义元数据键值对。

    - **request.doc_id**: 文档ID。
    - **request.meta**: 仅支持字符串、整数、浮点数类型的 value。
    - **返回值**: `true` 表示写入成功。
    """
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

    for value in req["meta"].values():
        if not isinstance(value, (str, int, float)):
            return get_json_result(
                data=False,
                retmsg=f"The type is not supported: {value}",
                retcode=settings.RetCode.ARGUMENT_ERROR,
            )

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


@router.post("/preview_chunks", summary="仅切片预览（不向量化/不入库，支持批次与直传文件）")
async def preview_chunks(
        request_body: PreviewChunksRequest | None = Body(None),
        request_form: str | None = Form(None, alias="request", description="请求JSON（multipart时使用）"),
        file: UploadFile | None = File(None, description="直传单文件（可选）"),
        raw_req: Request = None,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    ### POST `/preview_chunks` 仅切片预览接口（支持批次）

    **功能描述**:
    该接口对指定文档执行“解析+切片”，仅返回切片后的文本内容用于预览与调参验证。
    不进行向量化、也不写入向量库/数据库，不改变任何统计字段。

    ---

    ### 请求体 (Request Body)

    | 字段            | 类型      | 必填 | 描述                                                                 |
    |-----------------|-----------|------|----------------------------------------------------------------------|
    | `doc_id`        | `string`  | 否   | 文档ID；与 `file` 二选一                                            |
    | `parser_config` | `object`  | 否   | 解析配置覆盖（与文档配置合并，含 from_page/to_page 等）             |
    | `limit`         | `int`     | 否   | 最多返回的切片条数，默认 `50`（非批次模式使用）                      |
    | `batch_size`    | `int`     | 否   | 批次大小；若传入则启用批次模式                                      |
    | `batch_id`      | `string`  | 否   | 批次会话ID；续取下一批时传入                                         |
    | `file`          | `file`    | 否   | 直传单个文件；与 `doc_id` 二选一                                      |
    | `parser_id`     | `string`  | 否   | 手动指定解析器，如 `naive`/`paper` 等；会校验与文件类型兼容性         |
    | `batch_index`   | `int`     | 否   | 并发批次号（0-based）；指定则按批次号取片段，不推进offset            |

    ---

    ### 响应 (Response)

    #### 成功响应 (200) 非批次模式
    ```json
    {
      "retcode": 0,
      "retmsg": "success",
      "data": {
        "chunks": ["切片文本1", "切片文本2", "..."],
        "count": 2
      }
    }
    ```

    #### 成功响应 (200) 批次模式（v2.0）
    ```json
    {
      "retcode": 0,
      "retmsg": "success",
      "data": {
        "batch_id": "2f7e...",
        "chunks": ["切片文本1", "切片文本2", ...],
        "count": 20,                // 当前批次数量
        "total": 150,               // 当前已解析的chunks总数（动态增长）
        "has_more": true,
        "batch_index": 3,
        "total_batches": 8,         // 基于当前total动态计算（150/20≈8）
        "status": "parsing",        // parsing | completed | error
        "progress": 0.35,           // 解析进度 0.0-1.0
        "parsed_page_range": "0-35",  // 已解析页面范围
        "total_pages": 100          // 总页数（PDF专用）
      }
    }
    ```

    #### 错误响应

    - **400: 参数错误 / 解析器不兼容**
      ```json
      {
        "retcode": 101,
        "retmsg": "Invalid JSON: ... 或 Unsupported parser_id ...",
        "data": false
      }
      ```

    - **403: 权限不足**
      ```json
      {
        "retcode": 403,
        "retmsg": "No authorization.",
        "data": false
      }
      ```

    - **404: 文档或文件缺失**
      ```json
      {
        "retcode": 404,
        "retmsg": "Document not found.",
        "data": false
      }
      ```

    - **415: 文件类型不支持**
      ```json
      {
        "retcode": 415,
        "retmsg": "file type not supported yet(...)",
        "data": false
      }
      ```

    - **500: 服务器错误**
      ```json
      {
        "retcode": 500,
        "retmsg": "Internal Server Error",
        "data": false
      }
      ```

    ---

    ### 响应字段说明（v2.0 渐进式解析）

    #### 核心变化
    
    从 v2.0 开始，**PDF文件**支持渐进式解析，边解析边返回数据，无需等待全部解析完成。
    
    #### 字段说明
    
    | 字段 | 类型 | 说明 | 版本 |
    |------|------|------|------|
    | `batch_id` | string | 批次会话ID | v1.0 |
    | `chunks` | array | 当前批次的切片文本数组 | v1.0 |
    | `count` | int | 当前批次返回的chunks数量（例如batch_size=20，最后一批可能只有4个） | v1.0 |
    | `total` | int | 当前已解析的chunks总数（动态增长） | v1.0/v2.0 |
    | `has_more` | bool | 是否还有更多数据（包括正在解析的） | v1.0 |
    | `batch_index` | int | 当前批次索引（从0开始） | v1.0 |
    | `total_batches` | int | 当前总批次数（基于当前total动态计算） | v1.0/v2.0 |
    | `status` | string | 解析状态：`parsing` / `completed` / `error` | **v2.0** |
    | `progress` | float | 解析进度（0.0-1.0，基于页数计算） | **v2.0** |
    | `parsed_page_range` | string | 已解析的页面范围（如"0-24"，表示已解析第0到24页）| **v2.0** |
    | `total_pages` | int | 总页数（PDF专用） | **v2.0** |
    
    #### 📄 chunks字段说明
    
    **chunks结构**：返回纯文本数组
    ```json
    ["切片文本1", "切片文本2", "切片文本3", ...]
    ```
    
    **顺序保证**：
    - ✅ chunks数组严格按照PDF原始文档顺序排列
    - ✅ 第1页的chunks → 第2页的chunks → ... 依次排序
    - ✅ 每批解析完成后按顺序追加到数组，不会乱序
    - ✅ 用户通过offset顺序获取，保证不重复、不跳过
    
    **页面来源说明**：
    - 虽然chunks是纯文本，但通过 `parsed_page_range` 可以知道当前所有chunks的来源页面范围
    - 例如：`parsed_page_range="0-24"` 表示当前所有chunks来自PDF的第0-24页
    - 如果需要精确的每个chunk的页码，可以考虑后续扩展返回元数据
    
    #### 📊 字段详解
    
    **`count`（当前批次数量）**：
    - 表示当前返回的chunks数量
    - 通常等于 `batch_size`，最后一批可能更少
    - 示例：`batch_size=20`，最后一批只有4个，则 `count=4`
    
    **`total`（当前总数）**：
    - ⚠️ **重要变化**：v2.0中此字段会动态增长
    - **解析中**：随着后台解析进度增长（60 → 120 → 62）
    - **解析完成**：最终确定值（62）
    - **用途**：配合 `status` 判断，了解当前已有多少chunks可取
    
    **`total_batches`（总批次数）**：
    - 基于当前 `total` 动态计算：`(total + batch_size - 1) / batch_size`
    - **解析中**：会随着total增长而变化（3批 → 6批 → 4批）
    - **解析完成**：最终确定值（4批）
    - **用途**：显示"第X批/共Y批"（但解析中不准确）
    
    #### 使用建议
    
    **使用示例**：
    ```javascript
    // 轮询获取所有chunks
    let allChunks = [];
    
    while (data.has_more) {
      // 追加当前批次
      allChunks.push(...data.chunks);
      
      // 显示进度
      if (data.status === "parsing") {
        console.log(`解析中: ${(data.progress * 100).toFixed(0)}%`);
        console.log(`已获取: ${allChunks.length}/${data.total} chunks`);
        console.log(`已解析页面: ${data.parsed_page_range}/${data.total_pages}`);
        // 输出示例：
        // 解析中: 24%
        // 已获取: 40/160 chunks
        // 已解析页面: 0-24/100
      } else if (data.status === "completed") {
        console.log(`解析完成: 共${data.total}个chunks`);
        console.log(`页面范围: ${data.parsed_page_range}`);
        // 输出示例：
        // 解析完成: 共62个chunks
        // 页面范围: 0-30
      }
      
      // 续取下一批
      if (data.has_more) {
        response = await fetch('/api/preview_chunks', {
          method: 'POST',
          body: JSON.stringify({ batch_id: data.batch_id, batch_size: 20 })
        });
        data = await response.json().data;
      }
    }
    
    console.log(`最终获取${allChunks.length}个chunks，顺序有保证`);
    ```
    
    #### PDF渐进式解析特性（v2.0最终版）
    
    **首次请求行为**：
    - ⏱️ **等待策略**：首次请求会**等待直到有数据**才返回，保证 `chunks` 非空（避免破坏调用者逻辑）
    - 📊 **返回时机**：通常等待2-5秒，返回首批已解析的chunks（50-200个）
    - ✅ **数据保证**：首次返回必有数据，`chunks.length > 0`（除非解析失败或空文档）
    
    **后台解析机制**：
    - 📄 **分批解析**：按配置的 `task_page_size` 分批解析
      - 默认解析器：12页/批
      - paper解析器：22页/批
      - 用户可通过 `parser_config.task_page_size` 自定义
    - 🔄 **增量存储**：每解析完一批，立即追加到Redis，用户轮询时可获取
    - 📈 **实时进度**：`progress`、`current_page` 字段实时更新
    
    **轮询获取特性**：
    - ✨ **渐进式获取**：每次轮询都能获取到**新解析的chunks**
    - 🎯 **offset自动推进**：无论解析是否完成，读取位置都会正确更新
    - 🚫 **不会跳过数据**：修复了v1.0的bug，保证获取到所有chunks
    - ⏳ **可随时续取**：用户可以晚点续取（1分钟后），不会丢失中间chunks
    
    **特殊情况处理**：
    - 🔀 **特殊解析器**：`one`、`knowledge_graph` 或非DeepDOC布局自动回退到一次性解析
    - ❌ **解析失败**：立即返回 `status="error"`，不会一直等待
    - 📄 **空文档**：立即返回 `status="completed"` + 空数组
    
    ---

    ### 支持的模式

    1) 非批次模式（不传 `batch_size`）：
       - 适合小文档或快速预览；一次性返回全部切片（可结合 `limit` 截断）。
       - 不创建会话，无 Redis 残留。

    2) 批次顺序模式（传 `batch_size`，不传 `batch_index`）：
       - 首次请求返回 `batch_id` 及首批数据，内部按 offset 依次推进。
       - 当本次响应 `has_more=false`（最后一批）时，服务端立即删除 Redis 会话。

    3) 批次并发模式（传 `batch_size` + `batch_index`）：
       - 可用相同 `batch_id` 并发拉取不同 `batch_index` 的批次。
       - 当任一请求返回 `has_more=false`（该批为最后一批）时，服务端立即删除 Redis 会话。

    4) 文件直传 vs. doc_id：
       - 直传文件：`file` + `request`（表单字段，JSON 字符串）或纯 JSON + multipart 混用。
       - doc_id：只需 `doc_id` + `parser_config`（可选），不上传文件。

    ---

    ### 主要流程

    #### 非PDF文件流程（原有逻辑）

    1. **权限验证**：校验 `doc_id` 权限或接受 `file` 直传
    2. **读取文件**：从对象存储或上传流中读取文件二进制
    3. **合并配置**：将 `parser_config` 与默认配置合并
    4. **一次性解析**：调用对应解析器，完整解析文件
    5. **提取文本**：统一提取 `content_with_weight` 作为文本切片
    6. **批次返回**：
       - 非批次模式：一次性返回全部（可用 `limit` 截断）
       - 批次模式：按 `batch_size` 分批返回，存储到Redis

    #### PDF文件流程（v2.0渐进式解析）

    **首次请求（创建会话）**：

    1. **权限验证** → 读取PDF文件 → 合并配置
    2. **获取PDF元信息**：
       - 总页数（`total_pages`）
       - 解析页范围（`from_page` - `to_page`）
       - 分批大小（`task_page_size`，默认12页）
    3. **创建Redis会话**：
       ```json
       {
         "batch_id": "uuid",
         "status": "parsing",
         "chunks": [],           // 初始为空
         "total": 0,             // 当前已解析数量（动态增长）
         "progress": 0.0,
         "parsed_page_range": "0-0",
         "total_pages": 100
       }
       ```
       内部字段（不返回给用户）：
       - `estimated_total`: 400（预估总数，仅用于内部计算）
    4. **启动后台解析线程**：
       - 每次解析12页（或22页，取决于解析器）
       - 解析完一批，立即追加到 `session.chunks`
       - 更新 `progress`、`current_page`
       - 写回Redis（TTL 30分钟）
    5. **主线程等待首批数据**：
       - 每500ms检查一次Redis会话
       - 当 `chunks.length > 0` 时，立即返回
       - 返回首批 `batch_size` 个chunks
       - **关键**：更新 `session.offset = batch_size`
    
    **后续请求（续取批次）**：
    
    1. **从Redis读取会话**（通过 `batch_id`）
    2. **读取当前批次**：
       - `start = session.offset`（上次读到哪里）
       - `end = start + batch_size`
       - `batch = session.chunks[start:end]`
    3. **判断是否还有更多**：
       - 如果 `end < len(chunks)` → `has_more = true`
       - 如果 `status == "parsing"` → `has_more = true`（后台还在解析）
       - 否则 → `has_more = false`
    4. **更新offset**（关键修复）：
       - 如果 `has_more == true`：
         - 更新 `session.offset = end`
         - 写回Redis（保证下次续取不重复）
       - 如果 `has_more == false`：
         - 删除Redis会话（释放资源）
    5. **返回当前批次**：
       - `chunks`：当前批次数据
       - `total`：当前已解析总数
       - `status`：parsing / completed / error
       - `progress`：解析进度（0.0-1.0）
       - `parsed_page_range`：已解析页面范围

    **关键特性**：
    - ✅ 首次请求保证返回有数据（等待策略）
    - ✅ offset在parsing期间也会更新（修复了v1.0 bug）
    - ✅ 用户晚点续取不会跳过chunks（offset正确保存）
    - ✅ 每次轮询都能获取新解析的chunks（渐进式体验）

    ---

    ### 使用示例

    #### 示例1：非批次模式（小文档快速预览）

    ```bash
    curl -X POST "http://api.example.com/v1/document/preview_chunks" \
      -H "Content-Type: application/json" \
      -d '{
            "doc_id": "doc_123",
            "parser_config": {"chunk_token_num": 512, "delimiter": "\n!?。；！？"},
            "limit": 50
          }'
    ```

    #### 示例2：PDF批次模式（v2.0渐进式解析）

    **首次请求（上传100页PDF）**：
    ```bash
    curl -X POST "http://api.example.com/v1/document/preview_chunks" \
      -H "Content-Type: application/json" \
      -d '{
            "doc_id": "doc_pdf_100_pages",
            "batch_size": 50,
            "parser_config": {"task_page_size": 12}
          }'
    
    # 响应（等待2-5秒后返回）：
    {
      "retcode": 0,
      "retmsg": "success",
      "data": {
        "batch_id": "abc123-def456",
        "chunks": [
          "第1页的内容...",
          "第2页的内容...",
          "第3页的内容...",
          ...  // 共50个纯文本
        ],
        "count": 50,
        "total": 80,               // 当前已解析80个chunks（动态增长）
        "has_more": true,          // 还有更多数据
        "batch_index": 0,
        "total_batches": 4,        // 当前总批次数（基于当前total=80计算）
        "status": "parsing",       // 正在解析中
        "progress": 0.12,          // 已解析12%
        "parsed_page_range": "0-12",  // 📌 已解析页面范围：0-12页
        "total_pages": 100         // 总共100页
      }
    }
    ```

    **第2次请求（1秒后续取）**：
    ```bash
    curl -X POST "http://api.example.com/v1/document/preview_chunks" \
      -H "Content-Type: application/json" \
      -d '{
            "batch_id": "abc123-def456",
            "batch_size": 50
          }'
    
    # 响应（立即返回）：
    {
      "data": {
        "batch_id": "abc123-def456",
        "chunks": [
          "第51个chunk...",
          "第52个chunk...",
          ...  // 共50个纯文本
        ],
        "count": 20,
        "total": 160,              // 后台已解析到160个chunks（动态增长）
        "has_more": true,
        "status": "parsing",
        "progress": 0.24,          // 已解析24%
        "parsed_page_range": "0-24",  // 📌 已解析页面范围：0-24页
        "total_batches": 8         // 基于当前total=160计算（160/20=8批）
      }
    }
    ```

    **第N次请求（解析完成后）**：
    ```bash
    # 继续用batch_id续取
    
    # 响应：
    {
      "data": {
        "batch_id": "abc123-def456",
        "chunks": ["最后一批..."],
        "count": 2,                // 最后一批只有2个（62 % 20 = 2）
        "total": 62,               // 实际总数62个（确定值）
        "has_more": false,         // 没有更多了
        "status": "completed",     // 解析完成
        "progress": 1.0,           // 100%
        "parsed_page_range": "0-100",  // 📌 已解析页面范围：0-100页（全部）
        "total_batches": 4,        // 总共4批（62/20=4批）
        "total_pages": 100
      }
    }
    ```

    #### 示例3：文件直传 + 批次模式

    ```bash
    curl -X POST "http://api.example.com/v1/document/preview_chunks" \
      -F 'file=@/path/to/document.pdf' \
      -F 'request={"batch_size": 50, "parser_id": "paper"}'
    
    # 首次返回batch_id，后续用batch_id续取
    ```

    #### 示例4：指定解析器和自定义分批大小

    ```bash
    curl -X POST "http://api.example.com/v1/document/preview_chunks" \
      -H "Content-Type: application/json" \
      -d '{
            "doc_id": "doc_paper",
            "batch_size": 100,
            "parser_id": "paper",
            "parser_config": {
              "task_page_size": 20,    // 每批解析20页（覆盖默认的22页）
              "chunk_token_num": 512
            }
          }'
    ```

    ---

    ### 注意事项

    **通用注意事项**：
    - 本接口不会触发向量化与入库，也不会修改文档/知识库的 `chunk_num`、`token_num` 等统计。
    - 页/行范围与具体解析器有关：PDF 为页区间，表格/文本等为行或分段区间。
    - `parser_config` 的键需与对应解析器支持的配置项一致，未识别的键将被忽略。
    - 返回仅包含文本切片；如需图像/表格截图等，请通过其他接口获取对应图片资源。
    - 批次会话存活时间默认 30 分钟；超时将被清理，需重新发起首批请求。

    **v2.0 PDF渐进式解析注意事项**：
    - ⏱️ **首次请求会等待**：首次请求会等待直到有数据才返回（通常2-5秒），不会返回空数组。
    - 🔄 **需要持续轮询**：用户需要持续用 `batch_id` 轮询获取后续数据，直到 `has_more=false`。
    - 📊 **实时进度反馈**：通过 `status`、`progress`、`parsed_page_range` 字段可以了解解析进度。
    - 📄 **页面范围说明**：
      - `parsed_page_range`（如"0-24"）直观显示已解析页面0到24页的内容
      - 这比单个数字更清楚，避免误解为"只解析了第24页"
      - 可通过解析范围获取起止页：`const [from, to] = range.split('-').map(Number)`
    - 🎯 **offset自动推进**：每次续取后，offset会自动更新，保证不重复、不跳过数据。
    - ⏳ **可延迟续取**：用户可以晚点续取（例如1分钟后），不会丢失中间chunks。
    - 🔀 **自动回退**：特殊解析器（`one`、`knowledge_graph`）或非DeepDOC布局会自动回退到一次性解析。
    - 💾 **Redis存储完整数据**：
      - 所有已解析的chunks都存储在Redis中，直到会话过期或用户读完删除
      - ⚠️ **重要**：即使后台解析完成（status=completed），Redis会话仍会保留
      - 只有当用户读完最后一批（has_more=false）或TTL过期（30分钟）时才删除
      - 用户可以随时回来继续读取（30分钟内）
    - 🚀 **并发友好**：使用后台线程解析，不会阻塞API线程池。
    
    **兼容性说明**：
    - ⚠️ **重要变化**：v2.0中 `total` 字段会动态增长（PDF解析中）
    - ✅ 老客户端可以继续使用，但需注意 `total` 不再是固定值
    - ✅ 通过 `status` 字段可以判断解析状态
    - ⚠️ 如果调用者依赖 `chunks` 非空判断，v2.0保证首次返回必有数据（除非解析失败）
    
    **chunks顺序性保证（v2.0重要）**：
    - ✅ **严格有序**：chunks数组严格按照PDF原始文档顺序排列
    - 🔄 **渐进式追加**：后台解析时，每批解析完成后按顺序追加到Redis
    - 📄 **页面范围**：通过 `parsed_page_range` 字段可以知道当前所有chunks来自哪些页面
    - 💡 **使用方式**：
      ```javascript
      // 获取页面范围
      const [fromPage, toPage] = data.parsed_page_range.split('-').map(Number);
      console.log(`当前${data.total}个chunks来自第${fromPage}-${toPage}页`);
      // 输出：当前160个chunks来自第0-24页
      
      // 直接使用文本（保证顺序）
      data.chunks.forEach((text, index) => {
        console.log(`Chunk ${index}: ${text.substring(0, 50)}...`);
      });
      ```
    """
    def _error_response(retcode: int, retmsg: str, status_code: int, data: Any = False):
        response = get_json_result(retcode=retcode, retmsg=retmsg, data=data)
        response.status_code = status_code
        return response

    try:
        # 统一解析请求：multipart 用 request_form，application/json 用 request_body
        if request_form is not None:
            req = PreviewChunksRequest.model_validate_json(request_form)
        elif request_body is not None:
            req = request_body
        else:
            # 兜底：当路径同时声明了 File/Form 时，部分客户端以 application/json 发送会导致 request_body 为空
            # 这里直接从原始请求解析 JSON
            try:
                data = await raw_req.json()
            except json.JSONDecodeError as e:
                return _error_response(settings.RetCode.ARGUMENT_ERROR, f"Invalid JSON: {e.msg}", HTTP_400_BAD_REQUEST)
            except Exception:
                req = PreviewChunksRequest()
            else:
                try:
                    req = PreviewChunksRequest.model_validate(data)
                except ValidationError as e:
                    return _error_response(settings.RetCode.ARGUMENT_ERROR, str(e), HTTP_400_BAD_REQUEST)
                except Exception as e:
                    return _error_response(settings.RetCode.ARGUMENT_ERROR, str(e), HTTP_400_BAD_REQUEST)
        # 参数校验：支持 doc_id、文件上传或批次续取
        if file is None and not req.doc_id and not req.batch_id:
            return get_json_result(
                data=False,
                retmsg='Either `doc_id` or `file` or `batch_id` is required.',
                retcode=settings.RetCode.ARGUMENT_ERROR
            )

        # 分支一：文件预览（直传或批次续取）
        if not req.doc_id:
            # 批次模式（首次或续取）
            if req.batch_size or req.batch_id:
                file_bytes = await file.read() if file is not None else None
                filename = file.filename or "uploaded" if file is not None else None
                data = await DocumentService.preview_file_chunks_batched(
                    db,
                    filename=filename,
                    file_bytes=file_bytes,
                    parser_config_override=req.parser_config,
                    batch_size=req.batch_size,
                    batch_id=req.batch_id,
                    override_parser_id=req.parser_id,
                    batch_index=req.batch_index,
                    tenant_id=user.id,
                )
                return get_json_result(data=data)

            if file is None:
                return get_json_result(
                    data=False,
                    retmsg='Either supply `file` for preview or provide `batch_id/batch_size` for batch mode.',
                    retcode=settings.RetCode.ARGUMENT_ERROR
                )

            file_bytes = await file.read()
            filename = file.filename or "uploaded"
            chunks = DocumentService.preview_file_chunks(
                db,
                filename,
                file_bytes,
                parser_config_override=req.parser_config,
                override_parser_id=req.parser_id,
                tenant_id=user.id,
            )
            return get_json_result(data={"chunks": chunks, "count": len(chunks)})

        # 分支二：基于 doc_id
        if not DocumentService.accessible(db, req.doc_id, user.id):
            return get_json_result(
                data=False,
                retmsg='No authorization.',
                retcode=settings.RetCode.AUTHENTICATION_ERROR
            )
        if req.batch_size or req.batch_id:
            data = await DocumentService.preview_document_chunks_batched(
                db,
                doc_id=req.doc_id,
                parser_config_override=req.parser_config,
                batch_size=req.batch_size or 50,
                batch_id=req.batch_id,
                override_parser_id=req.parser_id,
                batch_index=req.batch_index,
            )
            return get_json_result(data=data)
        chunks = DocumentService.preview_document_chunks(
            db,
            doc_id=req.doc_id,
            parser_config_override=req.parser_config,
            limit=req.limit,
            override_parser_id=req.parser_id,
        )
        return get_json_result(data={"chunks": chunks, "count": len(chunks)})
    except HTTPException:
        raise
    except NotImplementedError as e:
        return _error_response(
            HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            str(e),
            HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            data=False,
        )
    except (LookupError, FileNotFoundError) as e:
        return _error_response(settings.RetCode.NOT_FOUND, str(e), HTTP_404_NOT_FOUND)
    except ValueError as e:
        return _error_response(settings.RetCode.ARGUMENT_ERROR, str(e), HTTP_400_BAD_REQUEST)
    except ValidationError as e:
        return _error_response(settings.RetCode.ARGUMENT_ERROR, str(e), HTTP_400_BAD_REQUEST)
    except Exception as e:
        return _error_response(settings.RetCode.SERVER_ERROR, str(e), HTTP_500_INTERNAL_SERVER_ERROR)


@router.post("/analyze", summary="文档智能分析", response_description="成功分析文档")
async def analyze_document(
    doc_id: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
    kb_id: str | None = Form(default=None),
    include_summary: bool = Form(default=True),
    include_tags: bool = Form(default=True),
    summary_type: str = Form(default="short"),
    raptor_config: str | None = Form(default=None),  # JSON字符串
    use_cache: bool = Form(default=True),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### POST `/analyze` 文档智能分析接口

    **功能描述**:
    对已解析的文档或上传的文件进行智能分析，生成语义标签、词频标签和文档摘要。

    ---

    ### 功能特性

    1. **语义标签生成**: 使用LLM生成2-3个高质量语义标签
    2. **词频标签提取**: 提取文档中Top 5高频关键词
    3. **文档摘要生成**: 支持短摘要(150-200词)和长摘要(300-500词)
    4. **智能聚合**:
       - 短文档(≤10 chunks): 直接合并处理
       - 长文档(>10 chunks): 使用RAPTOR分层聚类
    5. **自适应数据源**:
       - 已向量化文档: 从Milvus获取(性能优10-15倍)
       - 未向量化文档: 重新解析文件
       - 直传文件: 临时解析(不入库)

    ---

    ### 请求参数 (三种模式)

    #### 模式1: doc_id (已上传的文档)

    | 参数名 | 类型 | 必填 | 默认值 | 描述 |
    |--------|------|------|--------|------|
    | `doc_id` | string | 是 | - | 文档ID |
    | `kb_id` | string | 是 | - | 知识库ID |
    | `include_summary` | boolean | 否 | true | 是否生成摘要 |
    | `include_tags` | boolean | 否 | true | 是否生成标签 |
    | `summary_type` | string | 否 | "short" | 摘要类型: short/long |
    | `raptor_config` | string(JSON) | 否 | null | RAPTOR配置(可选) |
    | `use_cache` | boolean | 否 | true | 是否使用缓存 |

    #### 模式2: file (直传文件,临时分析)

    | 参数名 | 类型 | 必填 | 默认值 | 描述 |
    |--------|------|------|--------|------|
    | `file` | file | 是 | - | 上传的文件 |
    | `include_summary` | boolean | 否 | true | 是否生成摘要 |
    | `include_tags` | boolean | 否 | true | 是否生成标签 |
    | `summary_type` | string | 否 | "short" | 摘要类型: short/long |
    | `raptor_config` | string(JSON) | 否 | null | RAPTOR配置(可选) |
    | `use_cache` | boolean | 否 | true | 是否使用缓存 |

    #### RAPTOR配置参数 (JSON字符串)

    ```json
    {
        "max_cluster": 64,
        "max_token": 512,
        "threshold": 0.1,
        "random_seed": 42,
        "prompt": "..."
    }
    ```

    ---

    ### 响应示例

    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "doc_id": "abc123",
            "doc_name": "深度学习入门.pdf",
            "semantic_tags": ["深度学习", "神经网络", "计算机视觉"],
            "frequency_tags": ["模型", "训练", "数据", "算法", "网络"],
            "combined_tags": ["深度学习", "神经网络", "计算机视觉", "训练", "数据"],
            "short_summary": "本文系统介绍了深度学习的基础概念...",
            "metadata": {
                "chunk_count": 156,
                "use_raptor": true,
                "cluster_summary_count": 8,
                "processing_time_seconds": 12.5
            }
        }
    }
    ```

    ---

    ### 使用示例

    #### 模式1: 分析已上传的文档
    ```bash
    curl -X POST "http://api.example.com/v1/document/analyze" \
        -H "Authorization: Bearer YOUR_TOKEN" \
        -F "doc_id=abc123" \
        -F "kb_id=kb_456" \
        -F "include_summary=true" \
        -F "include_tags=true"
    ```

    #### 模式2: 直传文件临时分析
    ```bash
    curl -X POST "http://api.example.com/v1/document/analyze" \
        -H "Authorization: Bearer YOUR_TOKEN" \
        -F "file=@document.pdf" \
        -F "include_summary=true" \
        -F "include_tags=true"
    ```

    #### 自定义RAPTOR参数
    ```bash
    curl -X POST "http://api.example.com/v1/document/analyze" \
        -H "Authorization: Bearer YOUR_TOKEN" \
        -F "doc_id=abc123" \
        -F "kb_id=kb_456" \
        -F 'raptor_config={"max_cluster":32,"max_token":400}'
    ```
    """
    try:
        # 参数验证
        if not doc_id and not file:
            raise HTTPException(
                status_code=400,
                detail="Must provide either doc_id or file"
            )

        if doc_id and file:
            raise HTTPException(
                status_code=400,
                detail="Cannot provide both doc_id and file"
            )

        if doc_id and not kb_id:
            raise HTTPException(
                status_code=400,
                detail="kb_id is required when using doc_id"
            )

        # doc_id模式: 验证文档状态
        if doc_id:
            doc = DocumentService.get_by_id(db, doc_id)
            if not doc:
                raise HTTPException(status_code=404, detail="Document not found")

            if doc.status != "1":
                raise HTTPException(status_code=400, detail="Document not ready")

        # 解析RAPTOR配置
        raptor_config_dict = None
        if raptor_config:
            try:
                raptor_config_dict = json.loads(raptor_config)
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid raptor_config JSON")

        # 调用分析服务
        analysis_service = DocumentAnalysisService(db, user.id)
        result = await analysis_service.analyze_document(
            doc_id=doc_id,
            file=file,
            kb_id=kb_id,
            include_summary=include_summary,
            include_tags=include_tags,
            summary_type=summary_type,
            raptor_config=raptor_config_dict,
            use_cache=use_cache
        )

        return construct_json_result(data=result, code=200)

    except HTTPException:
        raise
    except Exception as e:
        logging.exception(f"Document analysis failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{doc_id}/tags", summary="获取文档标签", response_description="成功获取文档标签")
async def get_document_tags(
    doc_id: str,
    kb_id: str = Query(..., description="知识库ID"),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### GET `/{doc_id}/tags` 快速获取文档标签

    **功能描述**:
    快速获取文档的语义标签和词频标签，不生成摘要。

    ---

    ### 路径参数

    | 参数名 | 类型 | 必填 | 描述 |
    |--------|------|------|------|
    | `doc_id` | string | 是 | 文档ID |

    ### 查询参数

    | 参数名 | 类型 | 必填 | 描述 |
    |--------|------|------|------|
    | `kb_id` | string | 是 | 知识库ID |

    ---

    ### 响应示例

    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "doc_id": "abc123",
            "semantic_tags": ["深度学习", "神经网络"],
            "frequency_tags": ["模型", "训练", "数据", "算法", "网络"],
            "combined_tags": ["深度学习", "神经网络", "训练", "数据", "算法"]
        }
    }
    ```

    ---

    ### 使用示例

    ```bash
    curl -X GET "http://api.example.com/v1/document/abc123/tags?kb_id=kb_456" \
        -H "Authorization: Bearer YOUR_TOKEN"
    ```
    """
    try:
        analysis_service = DocumentAnalysisService(db, user.id)
        result = await analysis_service.analyze_document(
            doc_id=doc_id,
            kb_id=kb_id,
            include_summary=False,
            include_tags=True
        )

        return construct_json_result(
            data={
                "doc_id": doc_id,
                "semantic_tags": result.get("semantic_tags"),
                "frequency_tags": result.get("frequency_tags"),
                "combined_tags": result.get("combined_tags")
            }
        )

    except Exception as e:
        logging.exception(f"Get document tags failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{doc_id}/summary", summary="获取文档摘要", response_description="成功获取文档摘要")
async def get_document_summary(
    doc_id: str,
    kb_id: str = Query(..., description="知识库ID"),
    summary_type: str = Query(default="short", pattern="^(short|long)$"),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### GET `/{doc_id}/summary` 快速获取文档摘要

    **功能描述**:
    快速获取文档摘要，不生成标签。

    ---

    ### 路径参数

    | 参数名 | 类型 | 必填 | 描述 |
    |--------|------|------|------|
    | `doc_id` | string | 是 | 文档ID |

    ### 查询参数

    | 参数名 | 类型 | 必填 | 默认值 | 描述 |
    |--------|------|------|--------|------|
    | `kb_id` | string | 是 | - | 知识库ID |
    | `summary_type` | string | 否 | "short" | 摘要类型: short/long |

    ---

    ### 响应示例

    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "doc_id": "abc123",
            "summary": "本文系统介绍了深度学习的基础概念..."
        }
    }
    ```

    ---

    ### 使用示例

    #### 获取短摘要
    ```bash
    curl -X GET "http://api.example.com/v1/document/abc123/summary?kb_id=kb_456" \
        -H "Authorization: Bearer YOUR_TOKEN"
    ```

    #### 获取长摘要
    ```bash
    curl -X GET "http://api.example.com/v1/document/abc123/summary?kb_id=kb_456&summary_type=long" \
        -H "Authorization: Bearer YOUR_TOKEN"
    ```
    """
    try:
        analysis_service = DocumentAnalysisService(db, user.id)
        result = await analysis_service.analyze_document(
            doc_id=doc_id,
            kb_id=kb_id,
            include_summary=True,
            include_tags=False,
            summary_type=summary_type
        )

        return construct_json_result(
            data={
                "doc_id": doc_id,
                "summary": result.get("short_summary") if summary_type == "short" else result.get("long_summary")
            }
        )

    except Exception as e:
        logging.exception(f"Get document summary failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

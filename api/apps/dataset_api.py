# coding=utf-8
"""
@project: multirag
@Author：龙
@file： dataset_api.py
@date：2024/7/22 16:30
@desc:
"""
import base64
import json
import os
import pathlib
import re
import warnings
from datetime import datetime
from functools import partial
from io import BytesIO
from urllib.parse import quote

from PIL import Image
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status, UploadFile, File as Fe
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from api.contants import NAME_LENGTH_LIMIT
from api.db import FileType, ParserType, FileSource, TaskStatus
from api.db import StatusEnum
from api.db.database import get_db, SessionLocal
from api.db.db_models import File
from api.db.services import duplicate_name
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import TenantService
from api.settings import RetCode
from api.utils import get_uuid
from api.utils.api_utils import construct_json_result, construct_error_response, convert_datetime_to_str
from api.utils.api_utils import construct_result, validate_request
from api.utils.file_utils import filename_type, thumbnail
from core.app import book, laws, manual, naive, one, paper, presentation, qa, resume, table, picture, email
from core.nlp import search
from core.utils.milvus_conn import MILVUS_CONNECTION
from core.utils.storage_factory import STORAGE_IMPL
from api.db.database import get_db
from api.apps import manager
from pydantic import BaseModel, Field
from typing import Optional, List
import requests

MAXIMUM_OF_UPLOADING_FILES = 256

router = APIRouter()


class CreateDatasetRequest(BaseModel):
    name: str = Field(..., description="数据集名称")


class UpdateDatasetRequest(BaseModel):
    name: Optional[str] = Field(None, description="数据集名称")
    embedding_model_id: Optional[str] = Field(None, description="嵌入模型ID")
    chunk_method: Optional[str] = Field(None, description="分块方法")
    photo: Optional[str] = Field(None, description="图片")
    layout_recognize: Optional[bool] = Field(None, description="布局识别")
    language: Optional[str] = Field(None, description="语言")
    description: Optional[str] = Field(None, description="描述")
    permission: Optional[str] = Field(None, description="权限")
    token_num: Optional[int] = Field(None, description="令牌数量")
    template_type: Optional[str] = Field(None, description="模板类型")
    chunk_num: Optional[int] = Field(None, description="分块数量")


class UploadDocumentsRequest(BaseModel):
    files: List[UploadFile] = Field(..., description="上传的文件")


class UpdateDocumentRequest(BaseModel):
    name: Optional[str] = Field(None, description="文件名")
    enable: Optional[str] = Field(None, description="是否启用")
    template_type: Optional[str] = Field(None, description="模板类型")


class ParseDocumentsRequest(BaseModel):
    doc_ids: Optional[List[str]] = Field(None, description="文件ID列表")


# ------------------------------ create a dataset ---------------------------------------

@router.post("/", summary="创建数据集", response_description="成功创建数据集")
async def create_dataset(
        request_body: CreateDatasetRequest,
        db: Session = Depends(get_db),
        user=Depends(manager),
        req: Request = None
):
    authorization_token = req.headers.get("Authorization")
    if not authorization_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization header is missing.")
    tenant_id = user.id

    dataset_name = request_body.name.strip()

    if not dataset_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty dataset name")

    dataset_name_length = len(dataset_name)
    if dataset_name_length > NAME_LENGTH_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Dataset name: {dataset_name} with length {dataset_name_length} exceeds {NAME_LENGTH_LIMIT}!"
        )

    dataset_name = duplicate_name(
        KnowledgebaseService.query,
        db=db,
        name=dataset_name,
        tenant_id=tenant_id,
        status=StatusEnum.VALID.value
    )

    try:
        dataset_id = get_uuid()
        tenant = TenantService.get_by_id(db, tenant_id)
        if not tenant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
        embd_id = tenant.embd_id

        dataset_data = {
            "id": dataset_id,
            "tenant_id": tenant_id,
            "created_by": tenant_id,
            "name": dataset_name,
            "embd_id": embd_id,
        }

        if not KnowledgebaseService.save(db, **dataset_data):
            return construct_result()

        return construct_json_result(
            code=RetCode.SUCCESS,
            data={"dataset_name": dataset_name, "dataset_id": dataset_id}
        )
    except Exception as e:
        return construct_error_response(e)


# -----------------------------list datasets-------------------------------------------------------

@router.get("/", summary="列出数据集", response_description="成功列出数据集")
async def list_datasets(
        offset: int = 0,
        count: int = -1,
        orderby: str = "create_time",
        desc: bool = True,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
   列出当前用户可以访问的数据集。

   参数:
   - offset (int): 偏移量，默认值为0。
   - count (int): 返回数据集的数量，默认值为-1表示返回所有。
   - orderby (str): 排序字段，默认值为"create_time"。
   - desc (bool): 是否降序排列，默认值为True。

   返回:
   - JSON: 数据集列表的JSON响应。
   """
    try:
        tenants = TenantService.get_joined_tenants_by_user_id(db, user.id)
        datasets = KnowledgebaseService.get_by_tenant_ids_by_offset(
            db,
            [m["tenant_id"] for m in tenants],
            user.id,
            offset,
            count,
            orderby,
            desc
        )
        # 将 `datetime` 对象转换为字符串格式
        datasets = [convert_datetime_to_str(dataset) for dataset in datasets]
        return construct_json_result(data=datasets, code=RetCode.SUCCESS, message="List datasets successfully!")
    except Exception as e:
        return construct_error_response(e)


# ---------------------------------delete a dataset ----------------------------

@router.delete("/{dataset_id}", summary="删除数据集", response_description="成功删除数据集")
async def remove_dataset(
        dataset_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
   删除指定的数据集。

   参数:
   - dataset_id (str): 数据集ID。

   返回:
   - JSON: 删除操作的结果。
   """
    try:
        datasets = KnowledgebaseService.query(db, created_by=user.id, id=dataset_id)

        if not datasets:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="The dataset cannot be found for your current account.")

        for doc in DocumentService.query(db, kb_id=dataset_id):
            if not DocumentService.remove_document(db, doc, datasets[0].tenant_id):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="There was an error during the document removal process. Please check the status of the RAGFlow server and try the removal again."
                )
            f2d = File2DocumentService.get_by_document_id(db, doc.id)
            FileService.filter_delete(db, [File.source_type == FileSource.KNOWLEDGEBASE, File.id == f2d[0].file_id])
            File2DocumentService.delete_by_document_id(db, doc.id)

        if not KnowledgebaseService.delete_by_id(db, dataset_id):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="There was an error during the dataset removal process. Please check the status of the RAGFlow server and try the removal again."
            )

        return construct_json_result(code=RetCode.SUCCESS, message=f"Remove dataset: {dataset_id} successfully")
    except Exception as e:
        return construct_error_response(e)


# ------------------------------ get details of a dataset ----------------------------------------

@router.get("/{dataset_id}", summary="获取数据集详情", response_description="成功获取数据集详情")
async def get_dataset(dataset_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取指定数据集的详细信息。

    参数:
    - dataset_id (str): 数据集ID。

    返回:
    - JSON: 数据集详细信息的JSON响应。
    """
    try:
        dataset = KnowledgebaseService.get_detail(db, dataset_id)
        if not dataset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Can't find this dataset!")

        dataset_dict = convert_datetime_to_str(dataset)
        return construct_json_result(data=dataset_dict, code=RetCode.SUCCESS)
    except Exception as e:
        return construct_error_response(e)


# ------------------------------ update a dataset --------------------------------------------

@router.put("/{dataset_id}", summary="更新数据集", response_description="成功更新数据集")
async def update_dataset(
        dataset_id: str,
        request_body: UpdateDatasetRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    更新指定数据集的信息。

    参数:
    - dataset_id (str): 数据集ID。
    - request_body (UpdateDatasetRequest): 请求体，包含更新数据集的信息。
      - name (Optional[str]): 数据集名称。
      - embedding_model_id (Optional[str]): 嵌入模型ID。
      - chunk_method (Optional[str]): 分块方法。
      - photo (Optional[str]): 图片。
      - layout_recognize (Optional[bool]): 布局识别。
      - language (Optional[str]): 语言。
      - description (Optional[str]): 描述。
      - permission (Optional[str]): 权限。
      - token_num (Optional[int]): 令牌数量。
      - template_type (Optional[str]): 模板类型。
      - chunk_num (Optional[int]): 分块数量。

    返回:
    - JSON: 更新后的数据集详细信息的JSON响应。
    """
    req = request_body.model_dump(exclude_unset=True)
    try:
        # 请求不能为空
        if not req:
            return construct_json_result(code=RetCode.DATA_ERROR,
                                         message="Please input at least one parameter that you want to update!")

        # 检查是否可以找到数据集
        if not KnowledgebaseService.query(db, created_by=user.id, id=dataset_id):
            return construct_json_result(message="Only the owner of knowledgebase is authorized for this operation!",
                                         code=RetCode.OPERATING_ERROR)

        dataset = KnowledgebaseService.get_by_id(db, dataset_id)
        if not dataset:
            return construct_json_result(code=RetCode.DATA_ERROR, message="This dataset cannot be found!")

        # 检查名称是否重复
        if "name" in req:
            name = req["name"].strip()
            if name.lower() != dataset.name.lower() and len(
                    KnowledgebaseService.query(db, name=name, tenant_id=user.id, status=StatusEnum.VALID.value)) > 1:
                return construct_json_result(code=RetCode.DATA_ERROR,
                                             message=f"The name: {name.lower()} is already used by other datasets. Please choose a different name.")

        dataset_updating_data = {}
        chunk_num = req.get("chunk_num")

        # 更新嵌入模型ID
        if req.get("embedding_model_id"):
            if chunk_num == 0:
                dataset_updating_data["embd_id"] = req["embedding_model_id"]
            else:
                return construct_json_result(code=RetCode.DATA_ERROR,
                                             message="You have already parsed the document in this dataset, so you cannot change the embedding model.")

        # 更新分块方法
        if "chunk_method" in req:
            type_value = req["chunk_method"]
            if is_illegal_value_for_enum(type_value, ParserType):
                return construct_json_result(message=f"Illegal value {type_value} for 'chunk_method' field.",
                                             code=RetCode.DATA_ERROR)
            if chunk_num != 0:
                return construct_json_result(code=RetCode.DATA_ERROR,
                                             message="You have already parsed the document in this dataset, so you cannot change the chunk method.")
            dataset_updating_data["parser_id"] = req["template_type"]

        # 更新头像
        if req.get("photo"):
            dataset_updating_data["avatar"] = req["photo"]

        # 更新布局识别
        if "layout_recognize" in req:
            if "parser_config" not in dataset_updating_data:
                dataset_updating_data['parser_config'] = {}
            dataset_updating_data['parser_config']['layout_recognize'] = req['layout_recognize']

        # 更新其他字段
        for key in ["name", "language", "description", "permission", "id", "token_num"]:
            if key in req:
                dataset_updating_data[key] = req.get(key)

        # 更新数据集
        if not KnowledgebaseService.update_by_id(db, dataset.id, dataset_updating_data):
            return construct_json_result(
                code=RetCode.OPERATING_ERROR,
                message="Failed to update! Please check the status of RAGFlow server and try again!"
            )

        dataset = KnowledgebaseService.get_by_id(db, dataset.id)
        if not dataset:
            return construct_json_result(code=RetCode.DATA_ERROR,
                                         message="Failed to get the dataset using the dataset ID.")

        dataset_dict = dataset.to_dict()
        dataset_dict = convert_datetime_to_str(dataset_dict)

        return construct_json_result(data=dataset_dict, code=RetCode.SUCCESS)
    except Exception as e:
        return construct_error_response(e)


# ----------------------------upload files-----------------------------------------------------

@router.post("/{dataset_id}/documents/", summary="上传文件", response_description="成功上传文件")
async def upload_documents(
        dataset_id: str,  # 数据集ID，用于指定上传的文件所属的数据集
        files: List[UploadFile] = Fe(...),  # 上传的文件，可以是单个或多个
        db: Session = Depends(get_db),  # 依赖注入数据库会话
        user=Depends(manager)  # 依赖注入当前用户信息
):
    """
    上传文件到指定的数据集。

    参数:
    - dataset_id (str): 数据集ID。
    - files (UploadFile | None): 上传的文件。

    返回:
    - JSON: 上传文件的详细信息的JSON响应。
    """
    # 检查是否有文件上传，如果没有则返回错误
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="There is no file!")

    # 检查上传文件的数量是否超过限制
    num_file_objs = len(files)
    if num_file_objs > MAXIMUM_OF_UPLOADING_FILES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"You try to upload {num_file_objs} files, which exceeds the maximum number of uploading files: {MAXIMUM_OF_UPLOADING_FILES}"
        )

    # 根据数据集ID获取数据集信息
    dataset = KnowledgebaseService.get_by_id(db, dataset_id)
    if not dataset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Can't find this dataset")

    # 获取用户根文件夹，用于确定新上传文件的父文件夹
    root_folder = FileService.get_root_folder(db, user.id)
    parent_file_id = root_folder['id'] if isinstance(root_folder, dict) else root_folder.id
    # 初始化知识库文档
    FileService.init_knowledgebase_docs(db, parent_file_id, user.id)
    # 获取知识库根文件夹
    kb_root_folder = FileService.get_kb_folder(db, user.id)
    # 创建一个新的文件夹，用于存储上传到该数据集的文件
    kb_folder = FileService.new_a_file_from_kb(db, dataset.tenant_id, dataset.name, kb_root_folder["id"])

    err = []
    MAX_FILE_NUM_PER_USER = int(os.environ.get("MAX_FILE_NUM_PER_USER", 0))
    uploaded_docs_json = []

    # 遍历上传的每个文件进行处理
    for file in files:
        try:
            # 检查是否超过单个用户可上传文件的数量限制
            if MAX_FILE_NUM_PER_USER > 0 and DocumentService.get_doc_count(db,
                                                                           dataset.tenant_id) >= MAX_FILE_NUM_PER_USER:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="Exceed the maximum file number of a free user!")

            # 生成文件名，避免重复
            filename = duplicate_name(DocumentService.query, db=db, name=file.filename, kb_id=dataset.id)
            # 获取文件类型
            filetype = filename_type(filename)
            # 如果文件类型不受支持，则返回错误
            if filetype == FileType.OTHER.value:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="This type of file has not been supported yet!")

            # 生成文件在存储系统中的唯一位置
            location = filename
            while STORAGE_IMPL.obj_exist(dataset_id, location):
                location += "_"

            # 读取文件内容并上传到存储系统
            blob = await file.read()

            if blob == b'':
                warnings.warn(f"[WARNING]: The content of the file {filename} is empty.")

            STORAGE_IMPL.put(dataset_id, location, blob)

            # 构建文档数据库记录的字典
            doc = {
                "id": get_uuid(),
                "kb_id": dataset.id,
                "parser_id": dataset.parser_id,
                "parser_config": dataset.parser_config,
                "created_by": user.id,
                "type": filetype,
                "name": filename,
                "location": location,
                "size": len(blob),
                "thumbnail": thumbnail(filename, blob)
            }
            # 根据文件类型调整解析器ID
            if doc["type"] == FileType.VISUAL:
                doc["parser_id"] = ParserType.PICTURE.value
            if re.search(r"\.(ppt|pptx|pages)$", filename):
                doc["parser_id"] = ParserType.PRESENTATION.value
            if re.search(r"\.(eml)$", filename):
                doc["parser_id"] = ParserType.EMAIL.value
            # 插入文档记录到数据库
            DocumentService.insert(db, doc)

            # 在知识库文件夹中添加文件记录
            FileService.add_file_from_kb(db, doc, kb_folder["id"], dataset.tenant_id)
            uploaded_docs_json.append(doc)
        except Exception as e:
            # 记录处理过程中出现的错误
            err.append(file.filename + ": " + str(e))

    # 如果有错误发生，则返回错误信息
    if err:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="\n".join(err))

    # 返回成功响应，包含上传文件的信息
    return construct_json_result(data=uploaded_docs_json, code=RetCode.SUCCESS)


# ----------------------------delete a file-----------------------------------------------------

@router.delete("/{dataset_id}/documents/{document_id}", summary="删除文件", response_description="成功删除文件")
async def delete_document(
        dataset_id: str,
        document_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    删除指定的数据集中的文件。

    参数:
    - dataset_id (str): 数据集ID。
    - document_id (str): 文件ID。

    返回:
    - JSON: 删除操作的结果。
    """
    root_folder = FileService.get_root_folder(db, user.id)
    parent_file_id = root_folder.id
    FileService.init_knowledgebase_docs(db, parent_file_id, user.id)
    errors = ""
    try:
        document = DocumentService.get_by_id(db, document_id)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document {document_id} not found!")

        tenant_id = DocumentService.get_tenant_id(db, document_id)
        if not tenant_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"You cannot delete this document {document_id} due to the authorization reason!")

        real_dataset_id, location = File2DocumentService.get_storage_address(db, doc_id=document_id)
        if real_dataset_id != dataset_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"The document {document_id} is not in the dataset: {dataset_id}, but in the dataset: {real_dataset_id}.")

        if not DocumentService.remove_document(db, document, tenant_id):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="There was an error during the document removal process. Please check the status of the RAGFlow server and try the removal again."
            )

        file_to_doc = File2DocumentService.get_by_document_id(db, document_id)
        FileService.filter_delete(db, [File.source_type == FileSource.KNOWLEDGEBASE, File.id == file_to_doc[0].file_id])
        File2DocumentService.delete_by_document_id(db, document_id)

        STORAGE_IMPL.rm(dataset_id, location)
    except Exception as e:
        errors += str(e)

    if errors:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=errors)

    return construct_json_result(data=True, code=RetCode.SUCCESS)


# ----------------------------list files-----------------------------------------------------

@router.get('/{dataset_id}/documents/', summary="列出文件", response_description="成功列出文件")
async def list_documents(
        dataset_id: str,
        keywords: str = "",
        offset: int = 0,
        count: int = -1,
        order_by: str = "create_time",
        descend: bool = True,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    列出指定数据集中的文件。

    参数:
    - dataset_id (str): 数据集ID。
    - keywords (str): 关键词，默认值为空。
    - offset (int): 偏移量，默认值为0。
    - count (int): 返回文件的数量，默认值为-1表示返回所有。
    - order_by (str): 排序字段，默认值为"create_time"。
    - descend (bool): 是否降序排列，默认值为True。

    返回:
    - JSON: 文件列表的JSON响应。
    """
    if not dataset_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Lack of 'dataset_id'")

    try:
        docs, total = DocumentService.list_documents_in_dataset(db, dataset_id, offset, count, order_by, descend,
                                                                keywords)

        # 将每个文档的 datetime 字段转换为字符串格式
        serialized_docs = [convert_datetime_to_str(doc) for doc in docs]

        return construct_json_result(data={"total": total, "docs": serialized_docs}, code=RetCode.SUCCESS)
    except Exception as e:
        return construct_error_response(e)


# ----------------------------update: enable rename-----------------------------------------------------

@router.put("/{dataset_id}/documents/{document_id}", summary="更新文件", response_description="成功更新文件")
async def update_document(
        dataset_id: str,
        document_id: str,
        request: UpdateDocumentRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    更新指定数据集中的文件信息。

    参数:
    - dataset_id (str): 数据集ID。
    - document_id (str): 文件ID。
    - request (UpdateDocumentRequest): 更新请求对象，包含更新的详细信息。

    返回:
    - JSON: 更新后的文件详细信息的JSON响应。
    """
    req = request.model_dump(exclude_unset=True)
    try:
        legal_parameters = {"name", "enable", "template_type"}
        for key in req.keys():
            if key not in legal_parameters:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{key} is an illegal parameter.")

        if not req:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Please input at least one parameter that you want to update!")

        dataset = KnowledgebaseService.get_by_id(db, dataset_id)
        if not dataset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"This dataset {dataset_id} cannot be found!")

        document = DocumentService.get_by_id(db, document_id)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"This document {document_id} cannot be found!")

        updating_data = {}
        if "name" in req:
            new_name = req["name"].strip()
            updating_data["name"] = new_name

            if not new_name:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="There is no new name.")

            if pathlib.Path(new_name.lower()).suffix != pathlib.Path(document.name.lower()).suffix:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="The extension of file cannot be changed")

            for d in DocumentService.query(db, name=new_name, kb_id=document.kb_id):
                if d.name == new_name:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                        detail="Duplicated document name in the same dataset.")

        if "enable" in req:
            enable_value = req["enable"]
            if is_illegal_value_for_enum(enable_value, StatusEnum):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail=f"Illegal value {enable_value} for 'enable' field.")
            updating_data["status"] = enable_value

        if "template_type" in req:
            type_value = req["template_type"]
            if is_illegal_value_for_enum(type_value, ParserType):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail=f"Illegal value {type_value} for 'template_type' field.")
            updating_data["parser_id"] = req["template_type"]

        if not DocumentService.update_by_id(db, document_id, updating_data):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update document in the database! Please check the status of RAGFlow server and try again!"
            )

        if "name" in req:
            file_information = File2DocumentService.get_by_document_id(db, document_id)
            if file_information:
                file = FileService.get_by_id(db, file_information[0].file_id)
                FileService.update_by_id(db, file.id, {"name": req["name"]})

        document = DocumentService.get_by_id(db, document_id)
        return construct_json_result(data=document.to_dict(), message="Success", code=RetCode.SUCCESS)
    except Exception as e:
        return construct_error_response(e)


# ----------------------------download a file-----------------------------------------------------

@router.get("/{dataset_id}/documents/{document_id}", summary="下载文件", response_description="成功下载文件")
async def download_document(
        dataset_id: str,
        document_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    下载指定数据集中的文件。

    参数:
    - dataset_id (str): 数据集ID。
    - document_id (str): 文件ID。

    返回:
    - StreamingResponse: 文件流响应。
    """
    try:
        dataset = KnowledgebaseService.get_by_id(db, dataset_id)
        if not dataset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"This dataset '{dataset_id}' cannot be found!")

        document = DocumentService.get_by_id(db, document_id)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"This document '{document_id}' cannot be found!")

        doc_id, doc_location = File2DocumentService.get_storage_address(db, doc_id=document_id)
        file_stream = STORAGE_IMPL.get(doc_id, doc_location)
        if not file_stream:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This file is empty.")

        file = BytesIO(file_stream)
        # 使用 quote 对文件名进行编码
        encoded_filename = quote(document.name)
        return StreamingResponse(
            file,
            media_type='application/octet-stream',
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"}
        )

    except Exception as e:
        return construct_error_response(e)


# ----------------------------start parsing a document-----------------------------------------------------

def doc_parse_callback(doc_id, prog=None, msg="", db: Session = SessionLocal()):
    cancel = DocumentService.do_cancel(db, doc_id)
    if cancel:
        raise Exception("The parsing process has been cancelled!")


def doc_parse(binary, doc_name, parser_name, tenant_id, doc_id):
    match parser_name:
        case "book":
            book.chunk(doc_name, binary=binary, callback=partial(doc_parse_callback, doc_id))
        case "laws":
            laws.chunk(doc_name, binary=binary, callback=partial(doc_parse_callback, doc_id))
        case "manual":
            manual.chunk(doc_name, binary=binary, callback=partial(doc_parse_callback, doc_id))
        case "naive":
            return naive.chunk(doc_name, binary=binary, callback=partial(doc_parse_callback, doc_id))
        case "one":
            one.chunk(doc_name, binary=binary, callback=partial(doc_parse_callback, doc_id))
        case "paper":
            paper.chunk(doc_name, binary=binary, callback=partial(doc_parse_callback, doc_id))
        case "picture":
            picture.chunk(doc_name, binary=binary, tenant_id=tenant_id, lang="Chinese",
                          callback=partial(doc_parse_callback, doc_id))
        case "presentation":
            presentation.chunk(doc_name, binary=binary, callback=partial(doc_parse_callback, doc_id))
        case "qa":
            return qa.chunk(doc_name, binary=binary, callback=partial(doc_parse_callback, doc_id))
        case "resume":
            resume.chunk(doc_name, binary=binary, callback=partial(doc_parse_callback, doc_id))
        case "table":
            table.chunk(doc_name, binary=binary, callback=partial(doc_parse_callback, doc_id))
        case _:
            return False

    return True


@router.post("/{dataset_id}/documents/{document_id}/status", summary="开始解析文件",
             response_description="成功开始解析文件")
async def parse_document(
        dataset_id: str,
        document_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    开始解析指定数据集中的文件。

    参数:
    - dataset_id (str): 数据集ID。
    - document_id (str): 文件ID。

    返回:
    - JSON: 解析操作的结果。
    """
    try:
        dataset = KnowledgebaseService.get_by_id(db, dataset_id)
        if not dataset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"This dataset '{dataset_id}' cannot be found!")

        return await parsing_document_internal(db, document_id)
    except Exception as e:
        return construct_error_response(e)


@router.post("/{dataset_id}/documents/status", summary="开始解析多个文件", response_description="成功开始解析多个文件")
async def parse_documents(
        dataset_id: str,
        doc_ids: List[str],
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    开始解析指定数据集中的多个文件。

    参数:
    - dataset_id (str): 数据集ID。
    - doc_ids (List[str]): 文件ID列表。
    - db (Session): 数据库会话。
    - user: 当前用户信息。

    返回:
    - JSON: 解析操作的结果。
    """
    try:
        dataset = KnowledgebaseService.get_by_id(db, dataset_id)
        if not dataset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"This dataset '{dataset_id}' cannot be found!")

        if not doc_ids:
            docs, total = DocumentService.list_documents_in_dataset(db, dataset_id, 0, -1, "create_time", True, "")
            doc_ids = [doc["id"] for doc in docs]

        message = ""
        for id in doc_ids:
            res = await parsing_document_internal(db, id)
            res_body = res.json()
            if res_body["code"] == RetCode.SUCCESS:
                message += res_body["message"]
            else:
                return res
        return construct_json_result(data=True, code=RetCode.SUCCESS, message=message)
    except Exception as e:
        return construct_error_response(e)


def image_to_base64(image):
    buffered = BytesIO()
    image.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return img_str


async def parsing_document_internal(db: Session, id: str):
    message = ""
    try:
        document = DocumentService.get_by_id(db, id)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"This document '{id}' cannot be found!")

        d = DocumentService.get_by_doc_id(db, id)
        kb_id = d["kb_id"]
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        tenant_id = kb.tenant_id

        if not tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found!")

        info = {"run": "1", "progress": 0, "progress_msg": "", "chunk_num": 0, "token_num": 0}
        DocumentService.update_by_id(db, id, info)

        # ELASTICSEARCH.deleteByQuery(Q("match", doc_id=id), idxnm=search.index_name(tenant_id))
        # # 删除Milvus中的数据
        # try:
        #     kb_id = DocumentService.get_by_doc_id(db, id)
        #     kb = KnowledgebaseService.get_by_id(db, kb_id)
        #     delete_result = MILVUS_CONNECTION.delete(
        #         collection_name=search.index_name(tenant_id, kb.name),
        #         filter=f"doc_id == {id}"
        #     )
        #     if not delete_result:
        #         return construct_json_result(data=False, message="Milvus delete failed!",
        #                                      code=RetCode.ARGUMENT_ERROR)
        # except MilvusException as e:
        #     return construct_json_result(data=False, message=str(e), code=RetCode.ARGUMENT_ERROR)

        doc_attributes = DocumentService.get_by_id(db, id).to_dict()
        doc_id = doc_attributes["id"]
        bucket, doc_name = File2DocumentService.get_storage_address(db, doc_id=doc_id)
        binary = STORAGE_IMPL.get(bucket, doc_name)
        parser_name = doc_attributes["parser_id"]
        if binary:
            res = doc_parse(binary, doc_name, parser_name, tenant_id, doc_id)
            if not res:
                message += f"The parser id: {parser_name} of the document {doc_id} is not supported; "
        else:
            message += f"Empty data in the document: {doc_name}; "

        if doc_attributes["status"] == TaskStatus.FAIL.value:
            message += f"Failed in parsing the document: {doc_id}; "
        # # 确保 res 可序列化
        # if res and isinstance(res, dict):
        #     res = json.dumps(res)
        # else:
        #     res = str(res)
        # 将 res 中的图像对象转换为 base64 字符串
        if res:
            for item in res:
                if 'image' in item and isinstance(item['image'], Image.Image):
                    item['image'] = image_to_base64(item['image'])
        return construct_json_result(code=RetCode.SUCCESS, message=message, data=res)
    except Exception as e:
        return construct_error_response(e)


# ----------------------------stop parsing a doc-----------------------------------------------------

@router.delete("/{dataset_id}/documents/{document_id}/status", summary="停止解析文件",
               response_description="成功停止解析文件")
async def stop_parsing_document(
        dataset_id: str,
        document_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    停止解析指定数据集中的文件。

    参数:
    - dataset_id (str): 数据集ID。
    - document_id (str): 文件ID。
    - db (Session): 数据库会话。
    - user: 当前用户信息。

    返回:
    - JSON: 停止解析操作的结果。
    """
    try:
        dataset = KnowledgebaseService.get_by_id(db, dataset_id)
        if not dataset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"This dataset '{dataset_id}' cannot be found!")

        return await stop_parsing_document_internal(document_id, db)
    except Exception as e:
        return construct_error_response(e)


@router.delete("/{dataset_id}/documents/status", summary="停止解析多个文件",
               response_description="成功停止解析多个文件")
async def stop_parsing_documents(
        dataset_id: str,
        doc_ids: List[str],
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    停止解析指定数据集中的多个文件。

    参数:
    - dataset_id (str): 数据集ID。
    - doc_ids (List[str]): 文件ID列表。
    - db (Session): 数据库会话。
    - user: 当前用户信息。

    返回:
    - JSON: 停止解析操作的结果。
    """
    try:
        dataset = KnowledgebaseService.get_by_id(db, dataset_id)
        if not dataset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"This dataset '{dataset_id}' cannot be found!")

        if not doc_ids:
            docs, total = DocumentService.list_documents_in_dataset(db, dataset_id, 0, -1, "create_time", True, "")
            doc_ids = [doc["id"] for doc in docs]

        message = ""
        for id in doc_ids:
            res = await stop_parsing_document_internal(id, db)
            res_body = res.json()
            if res_body["code"] == RetCode.SUCCESS:
                message += res_body["message"]
            else:
                return res
        return construct_json_result(data=True, code=RetCode.SUCCESS, message=message)
    except Exception as e:
        return construct_error_response(e)


async def stop_parsing_document_internal(document_id: str, db: Session):
    try:
        document = DocumentService.get_by_id(db, document_id)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"This document '{document_id}' cannot be found!")

        doc_attributes = document.to_dict()
        if doc_attributes["status"] == TaskStatus.RUNNING.value:
            tenant_id = DocumentService.get_tenant_id(db, document_id)
            if not tenant_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found!")

            if not DocumentService.update_by_id(db, document_id, {"status": "2"}):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="There was an error during the stopping parsing the document process. Please check the status of the RAGFlow server and try the update again."
                )

            doc_attributes = DocumentService.get_by_id(db, document_id).to_dict()
            if doc_attributes["status"] == TaskStatus.RUNNING.value:
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                    detail=f"Failed in parsing the document: {document_id}; ")

        return construct_json_result(code=RetCode.SUCCESS, message="")
    except Exception as e:
        return construct_error_response(e)


# ----------------------------show the status of the file-----------------------------------------------------

@router.get("/{dataset_id}/documents/{document_id}/status", summary="显示文件解析状态",
            response_description="成功显示文件解析状态")
async def show_parsing_status(
        dataset_id: str,
        document_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    显示指定数据集中文件的解析状态。

    参数:
    - dataset_id (str): 数据集ID。
    - document_id (str): 文件ID。
    - db (Session): 数据库会话。
    - user: 当前用户信息。

    返回:
    - JSON: 文件解析状态的JSON响应。
    """
    try:
        dataset = KnowledgebaseService.get_by_id(db, dataset_id)
        if not dataset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"This dataset: '{dataset_id}' cannot be found!")

        document = DocumentService.get_by_id(db, document_id)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"This document: '{document_id}' is not a valid document.")

        doc_attributes = document.to_dict()
        return construct_json_result(
            data={"progress": doc_attributes["progress"], "status": TaskStatus(doc_attributes["status"]).name},
            code=RetCode.SUCCESS
        )
    except Exception as e:
        return construct_error_response(e)


def is_illegal_value_for_enum(value, enum_class):
    """
    检查给定值是否为指定枚举类的非法值。

    参数:
    - value: 要检查的值。
    - enum_class: 枚举类。

    返回:
    - bool: 如果值为非法值，则返回True；否则返回False。
    """
    return value not in enum_class.__members__.values()

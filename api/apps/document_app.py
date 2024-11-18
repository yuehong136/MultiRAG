# coding=utf-8
"""
@project: multirag
@Author：龙
@file： document_app.py
@date：2024/7/29 17:17
@desc:
"""
import logging
import os.path
import json
import pathlib
import re
import traceback
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Form
from fastapi.responses import StreamingResponse
from pymilvus import MilvusException
from sqlalchemy.orm import Session
from urllib.parse import quote

from api.constants import IMG_BASE64_PREFIX
from api.db import FileType, TaskStatus, ParserType, FileSource, db_models
from api.db.database import get_db
from api.db.db_models import Task
from api.db.services import duplicate_name
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.task_service import TaskService, queue_tasks
from api.db.services.user_service import UserTenantService
from deepdoc.parser.html_parser import RAGFlowHtmlParser
from api.settings import RetCode
from api.utils.api_utils import construct_json_result, construct_error_response, convert_datetime_to_str, \
    get_json_result
from api.utils import get_uuid
from api.utils.file_utils import filename_type, thumbnail, get_project_base_directory
from api.utils.web_utils import html2pdf, is_valid_url
from core.nlp import search
from core.utils.milvus_conn import MILVUS_CONNECTION
from core.utils.storage_factory import STORAGE_IMPL
from api.apps import manager

from pydantic import BaseModel, Field
from typing import Optional, List

router = APIRouter()


class WebCrawlRequest(BaseModel):
    kb_id: str = Field(..., description="知识库ID")
    name: str = Field(..., description="文件名")
    url: str = Field(..., description="URL地址")


class CreateDocumentRequest(BaseModel):
    name: str = Field(..., description="文件名")
    kb_id: str = Field(..., description="知识库ID")


class ChangeStatusRequest(BaseModel):
    doc_id: str = Field(..., description="文档ID")
    status: int = Field(..., description="状态")


class RemoveRequest(BaseModel):
    doc_id: List[str] = Field(..., description="文档ID列表")


class RunRequest(BaseModel):
    doc_ids: List[str] = Field(..., description="文档ID列表")
    run: int = Field(..., description="运行状态")


class RenameRequest(BaseModel):
    doc_id: str = Field(..., description="文档ID")
    name: str = Field(..., description="新的文件名")


class ChangeParserRequest(BaseModel):
    doc_id: str = Field(..., description="文档ID")
    parser_id: str = Field(..., description="解析器ID")
    parser_config: Optional[dict] = Field(None, description="解析器配置")


@router.post("/upload", summary="上传文件", response_description="成功上传文件")
async def upload(
        kb_id: str,
        files: List[UploadFile] = File(...),
        labels: Optional[str] = Query(None),  # labels 是一个 JSON 格式的字符串
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    上传文件到指定的知识库。

    该路由允许用户上传多个文件并将其关联到特定的知识库（Knowledgebase）。用户还可以选择传递
    `labels` 参数，该参数为一个 JSON 格式的字符串，用于标注文件的相关属性或责任。

    参数:
    - kb_id (str): 知识库的唯一标识符。
    - files (List[UploadFile]): 要上传的文件列表。用户可以一次上传多个文件。
    - labels (Optional[str]): 一个可选的 JSON 字符串，用于标注文件的属性或责任。示例：`["label1", "label2"]`。
    返回值:
    - JSON 响应对象，包含上传文件的处理结果。如果操作成功，返回已上传文件的信息；如果操作失败，
      返回错误消息和状态码。

    异常:
    - HTTPException: 如果 `kb_id` 对应的知识库不存在，返回 404 错误。
    - 其他服务器相关错误，如文件类型不支持或超过最大文件数量限制。

    逻辑流程:
    1. 验证 `kb_id` 和 `files` 是否存在，如果缺失则返回错误消息。
    2. 根据 `kb_id` 获取对应的知识库，如果找不到则返回 404 错误。
    3. 读取并存储每个文件的内容。
    4. 如果提供了 `labels` 参数，将其从 JSON 字符串转换为 Python 列表。
    5. 调用 `FileService.upload_document` 方法，将文件和 `labels` 一起上传。
    6. 如果上传过程中发生错误，返回错误消息；否则返回上传成功的文件信息。
    """
    if not kb_id:
        return construct_json_result(data=False, message='Lack of "KB ID"', code=RetCode.ARGUMENT_ERROR)
    if not files:
        return construct_json_result(data=False, message='No file part!', code=RetCode.ARGUMENT_ERROR)

    kb = KnowledgebaseService.get_by_id(db, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Can't find this knowledgebase!")
    file_contents = []
    for file in files:
        file_contents.append((await file.read(), file.filename))  # 读取文件内容并存储
    # 将 JSON 字符串转换为列表
    if labels:
        labels = json.loads(labels)
    # err, files = FileService.upload_document(db, kb, file_contents, user)
    err, files = FileService.upload_document(db, kb, file_contents, user, labels)  # 传递labels参数
    if err:
        return construct_json_result(data=False, message="\n".join(err), code=RetCode.SERVER_ERROR)
    return construct_json_result(data=files, code=RetCode.SUCCESS)


@router.post("/web_crawl", summary="网页爬取", response_description="成功爬取网页")
async def web_crawl(
        request_body: WebCrawlRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    kb_id = request_body.kb_id
    name = request_body.name
    url = request_body.url
    if not is_valid_url(url):
        return construct_json_result(data=False, message='The URL format is invalid', code=RetCode.ARGUMENT_ERROR)
    kb = KnowledgebaseService.get_by_id(db, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Can't find this knowledgebase!")

    blob = html2pdf(url)
    if not blob:
        return construct_error_response(ValueError("Download failure."))

    root_folder = FileService.get_root_folder(db, user.id)
    pf_id = root_folder.id
    FileService.init_knowledgebase_docs(db, pf_id, user.id)
    kb_root_folder = FileService.get_kb_folder(db, user.id)
    kb_folder = FileService.new_a_file_from_kb(db, kb.tenant_id, kb.name, kb_root_folder.id)

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
            "thumbnail": thumbnail(filename, blob)
        }
        if doc["type"] == FileType.VISUAL:
            doc["parser_id"] = ParserType.PICTURE.value
        if re.search(r"\.(ppt|pptx|pages)$", filename):
            doc["parser_id"] = ParserType.PRESENTATION.value
        if re.search(r"\.(eml)$", filename):
            doc["parser_id"] = ParserType.EMAIL.value
        DocumentService.insert(db, doc)
        FileService.add_file_from_kb(db, doc, kb_folder.id, kb.tenant_id)
    except Exception as e:
        return construct_error_response(e)
    return construct_json_result(data=True)


@router.post("/create", summary="创建文件或文件夹", response_description="成功创建文件或文件夹")
async def create_document(
        request_body: CreateDocumentRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    req = request_body.model_dump()
    kb_id = req["kb_id"]
    if not kb_id:
        return construct_json_result(data=False, message='Lack of "KB ID"', code=RetCode.ARGUMENT_ERROR)

    try:
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        if not kb:
            return construct_json_result(data=False, message="Can't find this knowledgebase!",
                                         code=RetCode.ARGUMENT_ERROR)

        if DocumentService.query(db, name=req["name"], kb_id=kb_id):
            return construct_json_result(data=False, message="Duplicated document name in the same knowledgebase.",
                                         code=RetCode.ARGUMENT_ERROR)

        doc = DocumentService.insert(db, {
            "id": get_uuid(),
            "kb_id": kb.id,
            "parser_id": kb.parser_id,
            "parser_config": kb.parser_config,
            "created_by": user.id,
            "type": FileType.VIRTUAL,
            "name": req["name"],
            "location": "",
            "size": 0
        })
        return construct_json_result(data=doc.to_dict(), code=RetCode.SUCCESS)
    except Exception as e:
        return construct_error_response(e)


@router.get("/list", summary="列出文档", response_description="成功列出文档")
async def list_docs(
        kb_id: str,
        keywords: str = "",
        page: int = 1,
        page_size: int = 15,
        orderby: str = "create_time",
        desc: bool = True,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    if not kb_id:
        return construct_json_result(data=False, message='Lack of "KB ID"', code=RetCode.ARGUMENT_ERROR)

    tenants = UserTenantService.query(db, user_id=user.id)
    for tenant in tenants:
        if KnowledgebaseService.query(db, tenant_id=tenant.tenant_id, id=kb_id):
            break
    else:
        return get_json_result(
            data=False, retmsg=f'Only owner of knowledgebase authorized for this operation.',
            retcode=RetCode.OPERATING_ERROR)

    try:
        docs, tol = DocumentService.get_by_kb_id(db, kb_id, page, page_size, orderby, desc, keywords)
        docs = [convert_datetime_to_str(d) for d in docs]

        for doc_item in docs:
            if doc_item['thumbnail'] and not doc_item['thumbnail'].startswith(IMG_BASE64_PREFIX):
                doc_item['thumbnail'] = f"/v1/document/image/{kb_id}-{doc_item['thumbnail']}"

        return construct_json_result(data={"total": tol, "docs": docs})
    except Exception as e:
        return construct_error_response(e)


@router.post('/infos', summary="获取文档信息", response_description="成功获取文档信息")
def docinfos(doc_ids: list[str], db: Session = Depends(get_db), user=Depends(manager)):
    for doc_id in doc_ids:
        if not DocumentService.accessible(db, doc_id, user.id):
            return get_json_result(
                data=False,
                retmsg='No authorization.',
                retcode=RetCode.AUTHENTICATION_ERROR
            )
    docs = DocumentService.get_by_ids(db, doc_ids)
    # 将每个文档对象转换为字典
    docs_dicts = [doc.__dict__ for doc in docs]
    # 移除 '_sa_instance_state'，这个是 SQLAlchemy 内部使用的属性
    for doc_dict in docs_dicts:
        doc_dict.pop('_sa_instance_state', None)
    return get_json_result(data=docs_dicts)


@router.get("/thumbnails", summary="获取文档缩略图", response_description="成功获取文档缩略图")
async def thumbnails(
        doc_ids: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    doc_ids_list = doc_ids.split(",")
    if not doc_ids_list:
        return construct_json_result(data=False, message='Lack of "Document ID"', code=RetCode.ARGUMENT_ERROR)

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
async def change_status(
        request_body: ChangeStatusRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    req = request_body.model_dump()
    if str(req["status"]) not in ["0", "1"]:
        return construct_json_result(data=False, message='"Status" must be either 0 or 1!',
                                     code=RetCode.ARGUMENT_ERROR)
    if not DocumentService.accessible(db, req["doc_id"], user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=RetCode.AUTHENTICATION_ERROR)

    try:
        doc = DocumentService.get_by_id(db, req["doc_id"])
        if not doc:
            return construct_json_result(data=False, message="Document not found!", code=RetCode.ARGUMENT_ERROR)
        kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
        if not kb:
            return construct_json_result(data=False, message="Can't find this knowledgebase!",
                                         code=RetCode.ARGUMENT_ERROR)

        if not DocumentService.update_by_id(db, req["doc_id"], {"status": str(req["status"])}):
            return construct_json_result(data=False, message="Database error (Document update)!",
                                         code=RetCode.ARGUMENT_ERROR)

        # 更新Milvus中的数据
        status_int = 0 if str(req["status"]) == "0" else 1

        try:
            update_result = MILVUS_CONNECTION.upsert(
                collection_name=search.index_name_one(kb.tenant_id, kb.name),
                data={"doc_id": req["doc_id"], "available_int": status_int}
            )
            if update_result["upsert_count"] == 0:
                return construct_json_result(code=RetCode.ARGUMENT_ERROR, message="Milvus update failed!")
        except MilvusException as e:
            return construct_json_result(code=RetCode.ARGUMENT_ERROR, message=str(e))

        return construct_json_result(data=True)
    except Exception as e:
        return construct_json_result(code=RetCode.ARGUMENT_ERROR, message=str(e))
    #
    #     if str(req["status"]) == "0":
    #         ELASTICSEARCH.updateScriptByQuery(Q("term", doc_id=req["doc_id"]),
    #                                           scripts="ctx._source.available_int=0;",
    #                                           idxnm=search.index_name(kb.tenant_id))
    #     else:
    #         ELASTICSEARCH.updateScriptByQuery(Q("term", doc_id=req["doc_id"]),
    #                                           scripts="ctx._source.available_int=1;",
    #                                           idxnm=search.index_name(kb.tenant_id))
    #     return construct_json_result(data=True)
    # except Exception as e:
    #     return construct_error_response(e)


@router.post("/rm", summary="删除文档", response_description="成功删除文档")
async def rm(
        request_body: RemoveRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    req = request_body.model_dump()
    doc_ids = req["doc_id"]
    if isinstance(doc_ids, str):
        doc_ids = [doc_ids]

    for doc_id in doc_ids:
        if not DocumentService.accessible4deletion(db, doc_id, user.id):
            return get_json_result(
                data=False,
                retmsg='No authorization.',
                retcode=RetCode.AUTHENTICATION_ERROR
            )

    root_folder = FileService.get_root_folder(db, user.id)
    pf_id = root_folder["id"]
    FileService.init_knowledgebase_docs(db, pf_id, user.id)
    errors = ""
    for doc_id in doc_ids:
        try:
            doc = DocumentService.get_by_id(db, doc_id)
            if not doc:
                return construct_json_result(data=False, message="Document not found!", code=RetCode.ARGUMENT_ERROR)
            tenant_id = DocumentService.get_tenant_id(db, doc_id)
            if not tenant_id:
                return construct_json_result(data=False, message="Tenant not found!", code=RetCode.ARGUMENT_ERROR)

            b, n = File2DocumentService.get_storage_address(db, doc_id=doc_id)

            if not DocumentService.remove_document(db, doc, tenant_id):
                return construct_json_result(data=False, message="Database error (Document removal)!",
                                             code=RetCode.ARGUMENT_ERROR)

            f2d = File2DocumentService.get_by_document_id(db, doc_id)
            FileService.filter_delete(db, [db_models.File.source_type == FileSource.KNOWLEDGEBASE,
                                           db_models.File.id == f2d[0].file_id])
            File2DocumentService.delete_by_document_id(db, doc_id)

            STORAGE_IMPL.rm(b, n)
        except Exception as e:
            errors += str(e)

    if errors:
        return construct_json_result(data=False, message=errors, code=RetCode.SERVER_ERROR)

    return construct_json_result(data=True)


@router.post("/run", summary="运行任务", response_description="成功运行任务")
async def run(
        request_body: RunRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    req = request_body.model_dump()

    for doc_id in req["doc_ids"]:
        if not DocumentService.accessible(db, doc_id, user.id):
            return get_json_result(
                data=False,
                retmsg='No authorization.',
                retcode=RetCode.AUTHENTICATION_ERROR
            )

    try:
        for id in req["doc_ids"]:
            info = {"run": str(req["run"]), "progress": 0}
            if str(req["run"]) == TaskStatus.RUNNING.value:
                info["progress_msg"] = ""
                info["chunk_num"] = 0
                info["token_num"] = 0
            DocumentService.update_by_id(db, id, info)
            d = DocumentService.get_by_doc_id(db, id)
            kb_id = d["kb_id"]
            kb = KnowledgebaseService.get_by_id(db, kb_id)
            tenant_id = kb.tenant_id
            if not tenant_id:
                return construct_json_result(data=False, message="Tenant not found!", code=RetCode.ARGUMENT_ERROR)

            # 构建 Milvus 集合名称
            collection_name = search.index_name_one(tenant_id, kb.name)
            # 检查集合是否存在并删除 Milvus 中的数据
            try:
                if MILVUS_CONNECTION.has_collection(collection_name):
                    delete_result = MILVUS_CONNECTION.delete(
                        collection_name=collection_name,
                        filter=f"doc_id == '{{doc_id}}'".format(doc_id=d["id"])
                        # filter=f"doc_id == '{d["id"]}'"
                    )
                    if not delete_result:
                        return construct_json_result(data=False, message="Milvus delete failed!",
                                                     code=RetCode.ARGUMENT_ERROR)
            except MilvusException as e:
                return construct_json_result(data=False, message=str(e), code=RetCode.ARGUMENT_ERROR)

            if str(req["run"]) == TaskStatus.RUNNING.value:
                TaskService.filter_delete(db, [Task.doc_id == id])
                doc = DocumentService.get_by_id(db, id).to_dict()
                doc["tenant_id"] = tenant_id
                bucket, name = File2DocumentService.get_storage_address(db, doc_id=doc["id"])
                queue_tasks(db, doc, bucket, name)
        return construct_json_result(data=True)
    except Exception as e:
        return construct_error_response(e)


@router.post("/rename", summary="重命名文档", response_description="成功重命名文档")
async def rename(
        request_body: RenameRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    req = request_body.model_dump()

    for doc_id in req["doc_ids"]:
        if not DocumentService.accessible(db, doc_id, user.id):
            return get_json_result(
                data=False,
                retmsg='No authorization.',
                retcode=RetCode.AUTHENTICATION_ERROR
            )

    try:
        doc = DocumentService.get_by_id(db, req["doc_id"])
        if not doc:
            return construct_json_result(data=False, message="Document not found!", code=RetCode.ARGUMENT_ERROR)
        if pathlib.Path(req["name"].lower()).suffix != pathlib.Path(doc.name.lower()).suffix:
            return construct_json_result(data=False, message="The extension of file can't be changed",
                                         code=RetCode.ARGUMENT_ERROR)
        for d in DocumentService.query(db, name=req["name"], kb_id=doc.kb_id):
            if d.name == req["name"]:
                return construct_json_result(data=False, message="Duplicated document name in the same knowledgebase.",
                                             code=RetCode.ARGUMENT_ERROR)

        if not DocumentService.update_by_id(db, req["doc_id"], {"name": req["name"]}):
            return construct_json_result(data=False, message="Database error (Document rename)!",
                                         code=RetCode.ARGUMENT_ERROR)

        informs = File2DocumentService.get_by_document_id(db, req["doc_id"])
        if informs:
            file = FileService.get_by_id(db, informs[0].file_id)
            FileService.update_by_id(db, file.id, {"name": req["name"]})

        return construct_json_result(data=True)
    except Exception as e:
        return construct_error_response(e)


@router.get("/get/{doc_id}", summary="获取文档内容", response_description="成功获取文档内容")
async def get_document(
        doc_id: str,
        db: Session = Depends(get_db),
        # user=Depends(manager)
):
    try:
        doc = DocumentService.get_by_id(db, doc_id)
        if not doc:
            return construct_json_result(data=False, message="Document not found!", code=RetCode.ARGUMENT_ERROR)

        b, n = File2DocumentService.get_storage_address(db, doc_id=doc_id)

        file_content = STORAGE_IMPL.get(b, n)
        if not file_content:
            raise HTTPException(status_code=404, detail="File not found in storage")

        # 将文件内容包装成 BytesIO 对象
        file_stream = BytesIO(file_content)

        ext = re.search(r"\.([^.]+)$", doc.name)
        media_type = "application/octet-stream"
        if ext:
            if doc.type == FileType.VISUAL.value:
                media_type = f'image/{ext.group(1)}'
            else:
                media_type = f'application/{ext.group(1)}'

        # 使用 quote 对文件名进行编码
        encoded_filename = quote(doc.name)
        response = StreamingResponse(file_stream, media_type=media_type)
        response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{encoded_filename}"
        return response
    except Exception as e:
        return construct_error_response(e)


@router.post("/change_parser", summary="更改解析器", response_description="成功更改解析器")
async def change_parser(
        request_body: ChangeParserRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    req = request_body.model_dump()

    for doc_id in req["doc_ids"]:
        if not DocumentService.accessible(db, doc_id, user.id):
            return get_json_result(
                data=False,
                retmsg='No authorization.',
                retcode=RetCode.AUTHENTICATION_ERROR
            )

    try:
        # 根据文档ID获取文档信息
        doc = DocumentService.get_by_id(db, req["doc_id"])
        # 如果找不到文档，返回错误信息
        if not doc:
            return construct_json_result(data=False, message="Document not found!", code=RetCode.ARGUMENT_ERROR)
        # 检查是否需要更新解析器ID
        if doc.parser_id.lower() == req["parser_id"].lower():
            # 如果parser_id未变更，则根据是否包含parser_config进行处理
            if "parser_config" in req and req["parser_config"] == doc.parser_config:
                return construct_json_result(data=True)
            else:
                return construct_json_result(data=True)
            # # 如果parser_id未变更，则根据是否包含parser_config进行处理
            # # if "parser_config" in req and req["parser_config"] == doc.parser_config:
            # #     return construct_json_result(data=True)
            # def convert_to_dict(obj):
            #     """
            #     将一个对象转换为字典。
            #     如果是自定义对象，使用对象的 __dict__ 属性来获取属性值。
            #     """
            #     if isinstance(obj, dict):
            #         return obj
            #     elif hasattr(obj, "__dict__"):
            #         return {key: convert_to_dict(value) for key, value in obj.__dict__.items()}
            #     else:
            #         return obj
            #
            # # 转换后进行比较
            # if "parser_config" in req and convert_to_dict(doc.parser_config) == req["parser_config"]:
            #     return construct_json_result(data=True)
            # else:
            #     # return construct_json_result(data=True)
            #     pass  # 继续往下执行后续代码

        # 检查文档类型是否支持
        if doc.type == FileType.VISUAL or re.search(r"\.(ppt|pptx|pages)$", doc.name):
            return construct_json_result(data=False, message="Not supported yet!", code=RetCode.ARGUMENT_ERROR)

        # 更新文档的parser_id和其他信息
        e = DocumentService.update_by_id(db, doc.id, {"parser_id": req["parser_id"], "progress": 0, "progress_msg": "",
                                                      "run": TaskStatus.UNSTART.value})
        # 如果更新失败，返回错误信息
        if not e:
            return construct_json_result(data=False, message="Document not found!", code=RetCode.ARGUMENT_ERROR)
        # 如果请求中包含parser_config，更新parser_config
        if "parser_config" in req:
            DocumentService.update_parser_config(db, doc.id, req["parser_config"])
        # 如果文档有token_num大于0，进行相关数值的递减操作
        if doc.token_num > 0:
            e = DocumentService.increment_chunk_num(db, doc.id, doc.kb_id, doc.token_num * -1, doc.chunk_num * -1,
                                                    doc.process_duration * -1)
            if not e:
                return construct_json_result(data=False, message="Document not found!", code=RetCode.ARGUMENT_ERROR)
            # 获取文档所属的租户ID
            tenant_id = DocumentService.get_tenant_id(db, req["doc_id"])
            if not tenant_id:
                return construct_json_result(data=False, message="Tenant not found!", code=RetCode.ARGUMENT_ERROR)
            document = DocumentService.get_by_doc_id(db, doc.id)
            kb = KnowledgebaseService.get_by_id(db, document["kb_id"])
            # 删除Milvus中的数据
            try:
                delete_result = MILVUS_CONNECTION.delete(
                    collection_name=search.index_name_one(tenant_id, kb.name),
                    filter=f"doc_id == '{doc.id}'"
                )
                if not delete_result:
                    return construct_json_result(data=False, message="Milvus delete failed!",
                                                 code=RetCode.ARGUMENT_ERROR)
            except MilvusException as e:
                return construct_json_result(data=False, message=str(e), code=RetCode.ARGUMENT_ERROR)
        return construct_json_result(data=True)
    except Exception as e:
        return construct_error_response(e)


@router.get("/image/{image_id}", summary="获取图片", response_description="成功获取图片")
async def get_image(
        image_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    try:
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
        url: Optional[str] = Form(None, description="网页URL（可选）"),
        files: Optional[List[UploadFile]] = File(None),
        user=Depends(manager)
):
    if url:
        if not is_valid_url(url):
            return get_json_result(
                data=False, retmsg='The URL format is invalid', retcode=RetCode.ARGUMENT_ERROR)
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

            res_headers = [r.response.headers for r in driver.requests]
            if len(res_headers) > 1:
                sections = RAGFlowHtmlParser().parser_txt(driver.page_source)
                driver.quit()
                return get_json_result(data="\n".join(sections))

            # 模拟 File 类逻辑
            r = re.search(r'filename=\"([^\"]+)\"', json.dumps(res_headers))
            if not r or not r.group(1):
                return get_json_result(
                    data=False, retmsg="Cannot identify downloaded file", retcode=RetCode.ARGUMENT_ERROR
                )

            class File:
                filename: str
                filepath: str

                def __init__(self, filename, filepath):
                    self.filename = filename
                    self.filepath = filepath

                def read(self):
                    with open(self.filepath, "r", encoding="utf-8") as f:
                        return f.read()

            if not r or r.group(1):
                return get_json_result(
                    data=False, retmsg="Can't not identify downloaded file", retcode=RetCode.ARGUMENT_ERROR)
            f = File(r.group(1), os.path.join(download_path, r.group(1)))
            txt = FileService.parse_docs([f], user.id)
            return get_json_result(data=txt)
        except Exception as e:
            logging.exception("[ERROR] URL processing failed")
            # traceback.print_exc()
            return get_json_result(
                retcode=RetCode.SERVER_ERROR,
                retmsg=str(e),
                data=False
            )
        finally:
            if driver:
                driver.quit()

    if not files:
        return get_json_result(
            data=False, retmsg='No file part!', retcode=RetCode.ARGUMENT_ERROR)

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
            retcode=RetCode.SERVER_ERROR,
            retmsg=str(e),
            data=False
        )

# coding=utf-8
"""
@project: multirag
@Author：龙
@file： document_app.py
@date：2024/7/29 17:17
@desc:
"""
import os
import pathlib
import re
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Request
from fastapi.responses import StreamingResponse
from pymilvus import MilvusException
from sqlalchemy.orm import Session
from urllib.parse import quote
from typing import List
# from elasticsearch_dsl import Q
# from core.nlp import search
# from core.utils.es_conn import ELASTICSEARCH
from api.db import FileType, TaskStatus, ParserType, FileSource, db_models
from api.db.database import get_db
from api.db.db_models import Task
from api.db.services import duplicate_name
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.task_service import TaskService, queue_tasks
from api.settings import RetCode, stat_logger
from api.utils.api_utils import construct_json_result, construct_error_response, convert_datetime_to_str, \
    get_json_result
from api.utils import get_uuid
from api.utils.file_utils import filename_type, thumbnail
from api.utils.web_utils import html2pdf, is_valid_url
from core.nlp import search
from core.utils.milvus_conn import MILVUS_CONNECTION
from core.utils.minio_conn import MINIO
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
        db: Session = Depends(get_db),
        user=Depends(manager)
):
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
    err, files = FileService.upload_document(db, kb, file_contents, user)
    # root_folder = FileService.get_root_folder(db, user.id)
    # pf_id = root_folder['id']
    # FileService.init_knowledgebase_docs(db, pf_id, user.id)
    # kb_root_folder = FileService.get_kb_folder(db, user.id)
    # kb_folder = FileService.new_a_file_from_kb(db, kb.tenant_id, kb.name, kb_root_folder["id"])
    #
    # err = []
    # uploaded_docs_json = []
    # for file in files:
    #     try:
    #         MAX_FILE_NUM_PER_USER = int(os.environ.get('MAX_FILE_NUM_PER_USER', 0))
    #         if 0 < MAX_FILE_NUM_PER_USER <= DocumentService.get_doc_count(db, kb.tenant_id):
    #             raise RuntimeError("Exceed the maximum file number of a free user!")
    #
    #         filename = duplicate_name(DocumentService.query, db=db, name=file.filename, kb_id=kb.id)
    #         filetype = filename_type(filename)
    #         if filetype == FileType.OTHER.value:
    #             raise RuntimeError("This type of file has not been supported yet!")
    #
    #         location = filename
    #         while MINIO.obj_exist(kb_id, location):
    #             location += "_"
    #         blob = await file.read()
    #         MINIO.put(kb_id, location, blob)
    #         doc = {
    #             "id": get_uuid(),
    #             "kb_id": kb.id,
    #             "parser_id": kb.parser_id,
    #             "parser_config": kb.parser_config,
    #             "created_by": user.id,
    #             "type": filetype,
    #             "name": filename,
    #             "location": location,
    #             "size": len(blob),
    #             "thumbnail": thumbnail(filename, blob)
    #         }
    #         if doc["type"] == FileType.VISUAL:
    #             doc["parser_id"] = ParserType.PICTURE.value
    #         if re.search(r"\.(ppt|pptx|pages)$", filename):
    #             doc["parser_id"] = ParserType.PRESENTATION.value
    #         DocumentService.insert(db, doc)
    #
    #         FileService.add_file_from_kb(db, doc, kb_folder["id"], kb.tenant_id)
    #         uploaded_docs_json.append(doc)
    #
    #     except Exception as e:
    #         err.append(file.filename + ": " + str(e))
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
        while MINIO.obj_exist(kb_id, location):
            location += "_"
        MINIO.put(kb_id, location, blob)
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
    try:
        docs, tol = DocumentService.get_by_kb_id(db, kb_id, page, page_size, orderby, desc, keywords)
        docs = [convert_datetime_to_str(d) for d in docs]
        return construct_json_result(data={"total": tol, "docs": docs})
    except Exception as e:
        return construct_error_response(e)


@router.post('/infos', summary="获取文档信息", response_description="成功获取文档信息")
def docinfos(doc_ids: list[str],db: Session = Depends(get_db), user=Depends(manager)):
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
async def remove_document(
        request_body: RemoveRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    req = request_body.model_dump()
    doc_ids = req["doc_id"]
    if isinstance(doc_ids, str):
        doc_ids = [doc_ids]
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

            b, n = File2DocumentService.get_minio_address(db, doc_id=doc_id)

            if not DocumentService.remove_document(db, doc, tenant_id):
                return construct_json_result(data=False, message="Database error (Document removal)!",
                                             code=RetCode.ARGUMENT_ERROR)

            f2d = File2DocumentService.get_by_document_id(db, doc_id)
            FileService.filter_delete(db, [db_models.File.source_type == FileSource.KNOWLEDGEBASE,
                                           db_models.File.id == f2d[0].file_id])
            File2DocumentService.delete_by_document_id(db, doc_id)

            MINIO.rm(b, n)
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
                bucket, name = File2DocumentService.get_minio_address(db, doc_id=doc["id"])
                queue_tasks(db, doc, bucket, name)
        return construct_json_result(data=True)
    except Exception as e:
        return construct_error_response(e)

    #         ELASTICSEARCH.deleteByQuery(Q("match", doc_id=id), idxnm=search.index_name(tenant_id))
    #
    #         if str(req["run"]) == TaskStatus.RUNNING.value:
    #             TaskService.filter_delete(db, [Task.doc_id == id])
    #             doc = DocumentService.get_by_id(db, id).to_dict()
    #             doc["tenant_id"] = tenant_id
    #             bucket, name = File2DocumentService.get_minio_address(db, doc_id=doc["id"])
    #             queue_tasks(doc, bucket, name)
    #
    #     return construct_json_result(data=True)
    # except Exception as e:
    #     return construct_error_response(e)


@router.post("/rename", summary="重命名文档", response_description="成功重命名文档")
async def rename(
        request_body: RenameRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    req = request_body.model_dump()
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
        user=Depends(manager)
):
    try:
        doc = DocumentService.get_by_id(db, doc_id)
        if not doc:
            return construct_json_result(data=False, message="Document not found!", code=RetCode.ARGUMENT_ERROR)

        b, n = File2DocumentService.get_minio_address(db, doc_id=doc_id)

        file_content = MINIO.get(b, n)
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
        file_content = MINIO.get(bkt, nm)

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

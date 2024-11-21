# coding=utf-8
"""
@project: multirag
@Author：龙
@file： api_app.py
@date：2024/7/22 16:02
@desc: API 管理接口
"""
import json
import os
import re
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.db import FileType, LLMType, ParserType, FileSource
from api.db.db_models import APIToken, Task, File
from api.db.services import duplicate_name
from api.db.services.api_service import APITokenService, API4ConversationService
from api.db.services.dialog_service import DialogService, chat, keyword_extraction
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.task_service import queue_tasks, TaskService
from api.db.services.user_service import UserTenantService
from api.db.services.llm_service import TenantLLMService

from api import settings
from api.utils import get_uuid, current_timestamp, datetime_format
from api.utils.api_utils import server_error_response, get_data_error_result, get_json_result, \
    generate_confirmation_token

from api.utils.file_utils import filename_type, thumbnail
from core.utils.storage_factory import STORAGE_IMPL
from api.db.services.canvas_service import UserCanvasService
from agent.canvas import Canvas
from functools import partial

from api.db.database import get_db
from api.apps import manager


class NewTokenRequest(BaseModel):
    tenant_id: str
    """租户的唯一标识符。"""

class NewConversationRequest(BaseModel):
    user_id: str
    """用户的唯一标识符。"""

class RemoveTokenRequest(BaseModel):
    tokens: list[str]
    """要删除的API令牌列表。"""

    tenant_id: str
    """租户的唯一标识符。"""

class CompletionRequest(BaseModel):
    conversation_id: str
    """对话的唯一标识符。"""

    messages: list[dict]
    """消息列表，每个消息包含角色和内容。"""

    quote: bool | None = False
    """是否引用，默认值为 False。"""

    stream: bool | None = True
    """是否使用流式响应，默认值为 True。"""

class DocumentUploadRequest(BaseModel):
    kb_name: str
    """知识库的名称。"""

    parser_id: str | None = None
    """解析器的ID，默认值为 None。"""

    run: int | None = None
    """是否立即运行，默认值为 None。"""

class ListKbDocsRequest(BaseModel):
    kb_name: str
    """知识库的名称。"""

    page: int | None = 1
    """分页页码，默认值为 1。"""

    page_size: int | None = 15
    """每页显示的文档数量，默认值为 15。"""

    orderby: str | None = "create_time"
    """排序字段，默认值为 "create_time"。"""

    desc: bool | None = True
    """是否按降序排序，默认值为 True。"""

    keywords: str | None = ""
    """搜索关键字，默认值为空字符串。"""

class DocumentRemoveRequest(BaseModel):
    doc_names: list[str]
    """要删除的文档名称列表。"""

    doc_ids: list[str]
    """要删除的文档ID列表。"""

class CompletionFAQRequest(BaseModel):
    Authorization: str
    """授权令牌。"""

    conversation_id: str
    """对话的唯一标识符。"""

    word: str
    """用户输入的关键词。"""


router = APIRouter()


@router.post('/new_token', summary="生成新的API令牌", response_description="成功生成新的API令牌")
async def new_token(request: NewTokenRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    生成新的API令牌

    该接口用于为指定租户生成新的API令牌。

    参数:
    - request: NewTokenRequest对象，包含租户的唯一标识符
        - tenant_id: str 租户的唯一标识符

    返回:
    - 成功时返回包含新API令牌的JSON结果
    - 失败时返回错误信息
    """
    try:
        tenants = UserTenantService.query(db, user_id=user.id)
        if not tenants:
            return get_data_error_result(retmsg="Tenant not found!")

        tenant_id = tenants[0].tenant_id
        obj = {"tenant_id": tenant_id, "token": generate_confirmation_token(tenant_id),
               "dialog_id": request.tenant_id,
               "create_time": current_timestamp(),
               "create_date": datetime_format(datetime.now()),
               "update_time": None,
               "update_date": None
               }
        if request.get("canvas_id"):
            obj["dialog_id"] = request["canvas_id"]
            obj["source"] = "agent"
        else:
            obj["dialog_id"] = request["dialog_id"]
        if not APITokenService.save(db, **obj):
            return get_data_error_result(retmsg="Fail to new a dialog!")

        return get_json_result(data=obj)
    except Exception as e:
        return server_error_response(e)


@router.get('/token_list', summary="获取API令牌列表", response_description="成功获取API令牌列表")
async def token_list(
    dialog_id: str | None = Query(None, alias="dialog_id"),
    canvas_id: str | None = Query(None, alias="canvas_id"),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    获取API令牌列表

    该接口用于获取指定对话的API令牌列表。

    参数:
    - dialog_id: str 对话的唯一标识符
    - canvas_id: str 画布的唯一标识符

    返回:
    - 成功时返回包含API令牌列表的JSON结果
    - 失败时返回错误信息
    """
    try:
        # 优先使用 dialog_id，如果 dialog_id 不存在，则使用 canvas_id
        id = dialog_id if dialog_id is not None else canvas_id
        if not id:
            raise HTTPException(status_code=400, detail="Either dialog_id or canvas_id must be provided")

        tenants = UserTenantService.query(db, user_id=user.id)
        if not tenants:
            return get_data_error_result(retmsg="Tenant not found!")

        objs = APITokenService.query(db, tenant_id=tenants[0].tenant_id, dialog_id=id)
        return get_json_result(data=[o.to_dict() for o in objs])
    except Exception as e:
        return server_error_response(e)


@router.post('/rm', summary="删除API令牌", response_description="成功删除API令牌")
async def rm(request: RemoveTokenRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
   删除API令牌

   该接口用于删除指定租户的API令牌。

   参数:
   - request: RemoveTokenRequest对象，包含要删除的API令牌和租户的唯一标识符
       - tokens: List[str] 要删除的API令牌列表
       - tenant_id: str 租户的唯一标识符

   返回:
   - 成功时返回成功删除的JSON结果
   - 失败时返回错误信息
   """
    try:
        for token in request.tokens:
            APITokenService.filter_delete(
                db,
                [APIToken.tenant_id == request.tenant_id, APIToken.token == token])
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.get('/stats', summary="获取API使用统计", response_description="成功获取API使用统计")
async def stats(from_date: str = None, to_date: str = None, canvas_id: str = None, db: Session = Depends(get_db), user=Depends(manager)):
    """
   获取API使用统计

   该接口用于获取API的使用统计信息。

   参数:
   - from_date: str 起始日期，格式为 YYYY-MM-DD，默认为过去7天
   - to_date: str 结束日期，格式为 YYYY-MM-DD，默认为当前日期

   返回:
   - 成功时返回包含API使用统计的JSON结果
   - 失败时返回错误信息
   """
    try:
        tenants = UserTenantService.query(db, user_id=user.id)
        if not tenants:
            return get_data_error_result(retmsg="Tenant not found!")
        from_date = from_date or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
        to_date = to_date or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        objs = API4ConversationService.stats(db, tenants[0].tenant_id, from_date, to_date,
                                             "agent" if canvas_id else None)
        res = {
            "pv": [(o["dt"], o["pv"]) for o in objs],
            "uv": [(o["dt"], o["uv"]) for o in objs],
            "speed": [(o["dt"], float(o["tokens"]) / (float(o["duration"] + 0.1))) for o in objs],
            "tokens": [(o["dt"], float(o["tokens"]) / 1000.) for o in objs],
            "round": [(o["dt"], o["round"]) for o in objs],
            "thumb_up": [(o["dt"], o["thumb_up"]) for o in objs]
        }
        return get_json_result(data=res)
    except Exception as e:
        return server_error_response(e)


@router.post('/new_conversation', summary="创建新对话", response_description="成功创建新对话")
async def set_conversation(request: NewConversationRequest, db: Session = Depends(get_db), req: Request = None):
    """
   创建新对话

   该接口用于创建新的对话。

   参数:
   - request: NewConversationRequest对象，包含用户的唯一标识符
       - user_id: str 用户的唯一标识符

   返回:
   - 成功时返回包含新对话信息的JSON结果
   - 失败时返回错误信息
   """
    # token = request.headers.get('Authorization').split()[1]
    auth_header = req.headers.get('Authorization')
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization header missing or invalid")

    token = auth_header.split()[1]

    try:
        print("Querying APIToken with token:", token)
        objs = APITokenService.query(db, token=token)  # 使用 APITokenService.query 来查询 token
        if not objs:
            print("No APIToken found with the provided token")
            return get_json_result(
                data=False, retmsg='Token is not valid!"', retcode=settings.RetCode.AUTHENTICATION_ERROR)

        print("APIToken found:", objs)
        if objs[0].source == "agent":
            cvs = UserCanvasService.get_by_id(db, objs[0].dialog_id)
            if not cvs:
                return server_error_response("canvas not found.")
            if not isinstance(cvs.dsl, str):
                cvs.dsl = json.dumps(cvs.dsl, ensure_ascii=False)
            canvas = Canvas(cvs.dsl, objs[0].tenant_id)
            conv = {
                "id": get_uuid(),
                "dialog_id": cvs.id,
                "user_id": request.args.get("user_id", ""),
                "message": [{"role": "assistant", "content": canvas.get_prologue()}],
                "source": "agent"
            }
            API4ConversationService.save(**conv)
            return get_json_result(data=conv)
        else:
            e, dia = DialogService.get_by_id(db, objs[0].dialog_id)
            if not e:
                return get_data_error_result(retmsg="Dialog not found")
            conv = {
                "id": get_uuid(),
                "dialog_id": dia.id,
                "user_id": request.args.get("user_id", ""),
                "message": [{"role": "assistant", "content": dia.prompt_config["prologue"]}]
            }
            API4ConversationService.save(**conv)
            return get_json_result(data=conv)
    except Exception as e:
        print("Exception occurred:", str(e))
        return server_error_response(e)


@router.post('/completion', summary="完成对话", response_description="成功完成对话")
async def completion(request: CompletionRequest, db: Session = Depends(get_db)):
    """
   完成对话

   该接口用于完成指定对话，生成对话内容。

   参数:
   - request: CompletionRequest对象，包含对话的详细信息
       - conversation_id: str 对话的唯一标识符
       - messages: List[dict] 消息列表，每个消息包含角色和内容
       - quote: Optional[bool] 是否引用，默认值为 False
       - stream: Optional[bool] 是否使用流式响应，默认值为 True

   返回:
   - 成功时返回生成的对话内容
   - 失败时返回错误信息
   """
    token = request.headers.get('Authorization').split()[1]
    objs = APITokenService.query(token=token)
    if not objs:
        return get_json_result(
            data=False, retmsg='Token is not valid!"', retcode=settings.RetCode.AUTHENTICATION_ERROR)
    req = request.model_dump()
    conv = API4ConversationService.get_by_id(db, req["conversation_id"])
    if not conv:
        return get_data_error_result(retmsg="Conversation not found!")
    if "quote" not in req: req["quote"] = False

    msg = []
    for m in req["messages"]:
        if m["role"] == "system":
            continue
        if m["role"] == "assistant" and not msg:
            continue
        msg.append(m)
    if not msg[-1].get("id"): msg[-1]["id"] = get_uuid()
    message_id = msg[-1]["id"]

    def fillin_conv(ans):
        nonlocal conv, message_id
        if not conv.reference:
            conv.reference.append(ans["reference"])
        else:
            conv.reference[-1] = ans["reference"]
        conv.message[-1] = {"role": "assistant", "content": ans["answer"], "id": message_id}
        ans["id"] = message_id

    def rename_field(ans):
        reference = ans['reference']
        if not isinstance(reference, dict):
            return
        for chunk_i in reference.get('chunks', []):
            if 'docnm_kwd' in chunk_i:
                chunk_i['doc_name'] = chunk_i['docnm_kwd']
                chunk_i.pop('docnm_kwd')

    try:
        if conv.source == "agent":
            stream = req.get("stream", True)
            conv.message.append(msg[-1])
            cvs = UserCanvasService.get_by_id(db, conv.dialog_id)
            if not cvs:
                return server_error_response("canvas not found.")
            del req["conversation_id"]
            del req["messages"]

            if not isinstance(cvs.dsl, str):
                cvs.dsl = json.dumps(cvs.dsl, ensure_ascii=False)

            if not conv.reference:
                conv.reference = []
            conv.message.append({"role": "assistant", "content": "", "id": message_id})
            conv.reference.append({"chunks": [], "doc_aggs": []})

            final_ans = {"reference": [], "content": ""}
            canvas = Canvas(cvs.dsl, objs[0].tenant_id)

            canvas.messages.append(msg[-1])
            canvas.add_user_input(msg[-1]["content"])
            answer = canvas.run(stream=stream)

            assert answer is not None, "Nothing. Is it over?"

            if stream:
                assert isinstance(answer, partial), "Nothing. Is it over?"

                def sse():
                    nonlocal answer, cvs, conv
                    try:
                        for ans in answer():
                            for k in ans.keys():
                                final_ans[k] = ans[k]
                            ans = {"answer": ans["content"], "reference": ans.get("reference", [])}
                            fillin_conv(ans)
                            rename_field(ans)
                            yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": ans},
                                                       ensure_ascii=False) + "\n\n"

                        canvas.messages.append({"role": "assistant", "content": final_ans["content"], "id": message_id})
                        if final_ans.get("reference"):
                            canvas.reference.append(final_ans["reference"])
                        cvs.dsl = json.loads(str(canvas))
                        API4ConversationService.append_message(conv.id, conv.to_dict())
                    except Exception as e:
                        yield "data:" + json.dumps({"retcode": 500, "retmsg": str(e),
                                                    "data": {"answer": "**ERROR**: " + str(e), "reference": []}},
                                                   ensure_ascii=False) + "\n\n"
                    yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False) + "\n\n"

                resp = Response(sse(), mimetype="text/event-stream")
                resp.headers.add_header("Cache-control", "no-cache")
                resp.headers.add_header("Connection", "keep-alive")
                resp.headers.add_header("X-Accel-Buffering", "no")
                resp.headers.add_header("Content-Type", "text/event-stream; charset=utf-8")
                return resp

            final_ans["content"] = "\n".join(answer["content"]) if "content" in answer else ""
            canvas.messages.append({"role": "assistant", "content": final_ans["content"], "id": message_id})
            if final_ans.get("reference"):
                canvas.reference.append(final_ans["reference"])
            cvs.dsl = json.loads(str(canvas))

            result = {"answer": final_ans["content"], "reference": final_ans.get("reference", [])}
            fillin_conv(result)
            API4ConversationService.append_message(conv.id, conv.to_dict())
            rename_field(result)
            return get_json_result(data=result)

        # ******************For dialog******************
        conv.message.append(msg[-1])
        e, dia = DialogService.get_by_id(conv.dialog_id)
        if not e:
            return get_data_error_result(retmsg="Dialog not found!")
        del req["conversation_id"]
        del req["messages"]

        if not conv.reference:
            conv.reference = []
        conv.message.append({"role": "assistant", "content": "", "id": message_id})
        conv.reference.append({"chunks": [], "doc_aggs": []})

        def stream():
            nonlocal dia, msg, req, conv
            try:
                for ans in chat(dia, msg, True, **req):
                    fillin_conv(ans)
                    rename_field(ans)
                    yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": ans},
                                               ensure_ascii=False) + "\n\n"
                API4ConversationService.append_message(conv.id, conv.to_dict())
            except Exception as e:
                yield "data:" + json.dumps({"retcode": 500, "retmsg": str(e),
                                            "data": {"answer": "**ERROR**: " + str(e), "reference": []}},
                                           ensure_ascii=False) + "\n\n"
            yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False) + "\n\n"

        if req.get("stream", True):
            resp = Response(stream(), mimetype="text/event-stream")
            resp.headers.add_header("Cache-control", "no-cache")
            resp.headers.add_header("Connection", "keep-alive")
            resp.headers.add_header("X-Accel-Buffering", "no")
            resp.headers.add_header("Content-Type", "text/event-stream; charset=utf-8")
            return resp

        answer = None
        for ans in chat(dia, msg, **req):
            answer = ans
            fillin_conv(ans)
            API4ConversationService.append_message(conv.id, conv.to_dict())
            break
        rename_field(answer)
        return get_json_result(data=answer)

    except Exception as e:
        return server_error_response(e)


@router.get('/conversation/{conversation_id}', summary="获取对话详情", response_description="成功获取对话详情")
async def get(conversation_id: str, db: Session = Depends(get_db)):
    """
   获取对话详情

   该接口用于获取指定对话的详细信息。

   参数:
   - conversation_id: str 对话的唯一标识符

   返回:
   - 成功时返回包含对话详情的JSON结果
   - 失败时返回错误信息
   """
    token = request.headers.get('Authorization').split()[1]
    objs = APITokenService.query(db, token=token)
    if not objs:
        return get_json_result(
            data=False, retmsg='Token is not valid!"', retcode=settings.RetCode.AUTHENTICATION_ERROR)

    try:
        conv = API4ConversationService.get_by_id(db, conversation_id)
        if not conv:
            return get_data_error_result(retmsg="Conversation not found!")

        conv = conv.to_dict()
        if token != APITokenService.query(db, dialog_id=conv['dialog_id'])[0].token:
            return get_json_result(data=False, retmsg='Token is not valid for this conversation_id!"',
                                   retcode=settings.RetCode.AUTHENTICATION_ERROR)

        for referenct_i in conv['reference']:
            if referenct_i is None or len(referenct_i) == 0:
                continue
            for chunk_i in referenct_i['chunks']:
                if 'docnm_kwd' in chunk_i.keys():
                    chunk_i['doc_name'] = chunk_i['docnm_kwd']
                    chunk_i.pop('docnm_kwd')
        return get_json_result(data=conv)
    except Exception as e:
        return server_error_response(e)


@router.post('/document/upload', summary="上传文档", response_description="成功上传文档")
async def upload(request: Request, db: Session = Depends(get_db)):
    token = request.headers.get('Authorization').split()[1]
    objs = APITokenService.query(token=token)
    if not objs:
        return get_json_result(
            data=False, retmsg='Token is not valid!"', retcode=settings.RetCode.AUTHENTICATION_ERROR)

    form = await request.form()
    kb_name = form.get("kb_name").strip()
    tenant_id = objs[0].tenant_id

    try:
        e, kb = KnowledgebaseService.get_by_name(db, kb_name, tenant_id)
        if not e:
            return get_data_error_result(retmsg="Can't find this knowledgebase!")
        kb_id = kb.id
    except Exception as e:
        return server_error_response(e)

    if 'file' not in form:
        return get_json_result(data=False, retmsg='No file part!', retcode=settings.RetCode.ARGUMENT_ERROR)

    file = form['file']
    if file.filename == '':
        return get_json_result(data=False, retmsg='No file selected!', retcode=settings.RetCode.ARGUMENT_ERROR)

    root_folder = FileService.get_root_folder(tenant_id)
    pf_id = root_folder["id"]
    FileService.init_knowledgebase_docs(pf_id, tenant_id)
    kb_root_folder = FileService.get_kb_folder(tenant_id)
    kb_folder = FileService.new_a_file_from_kb(kb.tenant_id, kb.name, kb_root_folder["id"])

    try:
        if DocumentService.get_doc_count(kb.tenant_id) >= int(os.environ.get('MAX_FILE_NUM_PER_USER', 8192)):
            return get_data_error_result(retmsg="Exceed the maximum file number of a free user!")

        filename = duplicate_name(DocumentService.query, name=file.filename, kb_id=kb_id)
        filetype = filename_type(filename)
        if not filetype:
            return get_data_error_result(retmsg="This type of file has not been supported yet!")

        location = filename
        while STORAGE_IMPL.obj_exist(kb_id, location):
            location += "_"
        blob = await file.read()
        STORAGE_IMPL.put(kb_id, location, blob)
        doc = {
            "id": get_uuid(),
            "kb_id": kb.id,
            "parser_id": kb.parser_id,
            "parser_config": kb.parser_config,
            "created_by": kb.tenant_id,
            "type": filetype,
            "name": filename,
            "location": location,
            "size": len(blob),
            "thumbnail": thumbnail(filename, blob)
        }

        if "parser_id" in form.keys():
            if form.get("parser_id").strip() in list(vars(ParserType).values())[1:-3]:
                doc["parser_id"] = form.get("parser_id").strip()
        if doc["type"] == FileType.VISUAL:
            doc["parser_id"] = ParserType.PICTURE.value
        if re.search(r"\.(ppt|pptx|pages)$", filename):
            doc["parser_id"] = ParserType.PRESENTATION.value
        if re.search(r"\.(eml)$", filename):
            doc["parser_id"] = ParserType.EMAIL.value

        doc_result = DocumentService.insert(db, doc)
        FileService.add_file_from_kb(db, doc, kb_folder["id"], kb.tenant_id)
    except Exception as e:
        return server_error_response(e)

    if "run" in form.keys():
        if form.get("run").strip() == "1":
            try:
                info = {"run": 1, "progress": 0}
                info["progress_msg"] = ""
                info["chunk_num"] = 0
                info["token_num"] = 0
                DocumentService.update_by_id(db, doc["id"], info)
                tenant_id = DocumentService.get_tenant_id(db, doc["id"])
                if not tenant_id:
                    return get_data_error_result(retmsg="Tenant not found!")

                TaskService.filter_delete(db, [Task.doc_id == doc["id"]])
                e, doc = DocumentService.get_by_id(db, doc["id"])
                doc = doc.to_dict()
                doc["tenant_id"] = tenant_id
                bucket, name = File2DocumentService.get_storage_address(db, doc_id=doc["id"])
                queue_tasks(db, doc, bucket, name)
            except Exception as e:
                return server_error_response(e)

    return get_json_result(data=doc_result.to_json())

@router.post('/list_chunks', summary="列出文档块", response_description="成功列出文档块")
async def list_chunks(request: Request, db: Session = Depends(get_db)):
    token = request.headers.get('Authorization').split()[1]
    objs = APITokenService.query(token=token)
    if not objs:
        return get_json_result(
            data=False, retmsg='Token is not valid!"', retcode=settings.RetCode.AUTHENTICATION_ERROR)

    req = await request.json()

    try:
        if "doc_name" in req.keys():
            tenant_id = DocumentService.get_tenant_id_by_name(db, req['doc_name'])
            doc_id = DocumentService.get_doc_id_by_doc_name(db, req['doc_name'])
        elif "doc_id" in req.keys():
            tenant_id = DocumentService.get_tenant_id(db, req['doc_id'])
            doc_id = req['doc_id']
        else:
            return get_json_result(data=False, retmsg="Can't find doc_name or doc_id")

        res = settings.retrievaler.chunk_list(doc_id=doc_id, tenant_id=tenant_id)
        res = [
            {
                "content": res_item["content_with_weight"],
                "doc_name": res_item["docnm_kwd"],
                "img_id": res_item["img_id"]
            } for res_item in res
        ]
    except Exception as e:
        return server_error_response(e)

    return get_json_result(data=res)

@router.post('/list_kb_docs', summary="列出知识库文档", response_description="成功列出知识库文档")
async def list_kb_docs(request: ListKbDocsRequest, db: Session = Depends(get_db)):
    """
   列出知识库文档

   该接口用于列出指定知识库的文档。

   参数:
   - request: ListKbDocsRequest对象，包含知识库的详细信息
       - kb_name: str 知识库的名称
       - page: Optional[int] 分页页码，默认值为 1
       - page_size: Optional[int] 每页显示的文档数量，默认值为 15
       - orderby: Optional[str] 排序字段，默认值为 "create_time"
       - desc: Optional[bool] 是否按降序排序，默认值为 True
       - keywords: Optional[str] 搜索关键字，默认值为空字符串

   返回:
   - 成功时返回包含文档列表的JSON结果
   - 失败时返回错误信息
   """
    token = request.headers.get('Authorization').split()[1]
    objs = APITokenService.query(db, token=token)
    if not objs:
        return get_json_result(
            data=False, retmsg='Token is not valid!"', retcode=settings.RetCode.AUTHENTICATION_ERROR)

    tenant_id = objs[0].tenant_id
    kb_name = request.kb_name.strip()

    try:
        e, kb = KnowledgebaseService.get_by_name(db, kb_name, tenant_id)
        if not e:
            return get_data_error_result(retmsg="Can't find this knowledgebase!")
        kb_id = kb.id
    except Exception as e:
        return server_error_response(e)

    try:
        docs, tol = DocumentService.get_by_kb_id(
            db, kb_id, request.page, request.page_size, request.orderby, request.desc, request.keywords)
        docs = [{"doc_id": doc['id'], "doc_name": doc['name']} for doc in docs]

        return get_json_result(data={"total": tol, "docs": docs})
    except Exception as e:
        return server_error_response(e)


@router.post('/infos', summary="获取文档信息", response_description="成功获取文档信息")
def docinfos(doc_ids: list[str],db: Session = Depends(get_db), user=Depends(manager)):
    docs = DocumentService.get_by_ids(db, doc_ids)
    # 将每个文档对象转换为字典
    docs_dicts = [doc.__dict__ for doc in docs]
    # 移除 '_sa_instance_state'，这个是 SQLAlchemy 内部使用的属性
    for doc_dict in docs_dicts:
        doc_dict.pop('_sa_instance_state', None)
    return get_json_result(data=docs_dicts)


@router.delete('/document', summary="删除文档", response_description="成功删除文档")
async def document_rm(request: DocumentRemoveRequest, db: Session = Depends(get_db)):
    token = request.headers.get('Authorization').split()[1]
    objs = APITokenService.query(db, token=token)
    if not objs:
        return get_json_result(
            data=False, retmsg='Token is not valid!"', retcode=settings.RetCode.AUTHENTICATION_ERROR)

    tenant_id = objs[0].tenant_id
    try:
        doc_ids = [DocumentService.get_doc_id_by_doc_name(db, doc_name) for doc_name in request.doc_names]
        for doc_id in request.doc_ids:
            if doc_id not in doc_ids:
                doc_ids.append(doc_id)

        if not doc_ids:
            return get_json_result(data=False, retmsg="Can't find doc_names or doc_ids")
    except Exception as e:
        return server_error_response(e)

    root_folder = FileService.get_root_folder(tenant_id)
    pf_id = root_folder["id"]
    FileService.init_knowledgebase_docs(pf_id, tenant_id)

    errors = ""
    for doc_id in doc_ids:
        try:
            e, doc = DocumentService.get_by_id(db, doc_id)
            if not e:
                return get_data_error_result(retmsg="Document not found!")
            tenant_id = DocumentService.get_tenant_id(db, doc_id)
            if not tenant_id:
                return get_data_error_result(retmsg="Tenant not found!")

            b, n = File2DocumentService.get_storage_address(doc_id=doc_id)

            if not DocumentService.remove_document(doc, tenant_id):
                return get_data_error_result(retmsg="Database error (Document removal)!")

            f2d = File2DocumentService.get_by_document_id(db, doc_id)
            FileService.filter_delete(db, [File.source_type == FileSource.KNOWLEDGEBASE, File.id == f2d[0].file_id])
            File2DocumentService.delete_by_document_id(db, doc_id)

            STORAGE_IMPL.rm(b, n)
        except Exception as e:
            errors += str(e)

    if errors:
        return get_json_result(data=False, retmsg=errors, retcode=settings.RetCode.SERVER_ERROR)

    return get_json_result(data=True)

@router.post('/completion_aibotk', summary="完成FAQ对话", response_description="成功完成FAQ对话")
async def completion_faq(request: CompletionFAQRequest, db: Session = Depends(get_db)):
    """
    完成FAQ对话

    该接口用于完成FAQ对话，生成对话内容。

    参数:
    - request: CompletionFAQRequest对象，包含对话的详细信息
        - Authorization: str 授权令牌
        - conversation_id: str 对话的唯一标识符
        - word: str 用户输入的关键词

    返回:
    - 成功时返回生成的对话内容
    - 失败时返回错误信息
    """
    import base64
    req = request.model_dump()

    token = req["Authorization"]
    objs = APITokenService.query(db, token=token)
    if not objs:
        return get_json_result(
            data=False, retmsg='Token is not valid!"', retcode=settings.RetCode.AUTHENTICATION_ERROR)

    conv = API4ConversationService.get_by_id(db, req["conversation_id"])
    if not conv:
        return get_data_error_result(retmsg="Conversation not found!")
    if "quote" not in req: req["quote"] = True

    msg = []
    msg.append({"role": "user", "content": req["word"]})

    if not msg[-1].get("id"): msg[-1]["id"] = get_uuid()
    message_id = msg[-1]["id"]

    def fillin_conv(ans):
        nonlocal conv, message_id
        if not conv.reference:
            conv.reference.append(ans["reference"])
        else:
            conv.reference[-1] = ans["reference"]
        conv.message[-1] = {"role": "assistant", "content": ans["answer"], "id": message_id}
        ans["id"] = message_id

    try:
        if conv.source == "agent":
            conv.message.append(msg[-1])
            e, cvs = UserCanvasService.get_by_id(db, conv.dialog_id)
            if not e:
                return server_error_response("canvas not found.")

            if not isinstance(cvs.dsl, str):
                cvs.dsl = json.dumps(cvs.dsl, ensure_ascii=False)

            if not conv.reference:
                conv.reference = []
            conv.message.append({"role": "assistant", "content": "", "id": message_id})
            conv.reference.append({"chunks": [], "doc_aggs": []})

            final_ans = {"reference": [], "doc_aggs": []}
            canvas = Canvas(cvs.dsl, objs[0].tenant_id)

            canvas.messages.append(msg[-1])
            canvas.add_user_input(msg[-1]["content"])
            answer = canvas.run(stream=False)

            assert answer is not None, "Nothing. Is it over?"

            data_type_picture = {
                "type": 3,
                "url": "base64 content"
            }
            data = [
                {
                    "type": 1,
                    "content": ""
                }
            ]
            final_ans["content"] = "\n".join(answer["content"]) if "content" in answer else ""
            canvas.messages.append({"role": "assistant", "content": final_ans["content"], "id": message_id})
            if final_ans.get("reference"):
                canvas.reference.append(final_ans["reference"])
            cvs.dsl = json.loads(str(canvas))

            ans = {"answer": final_ans["content"], "reference": final_ans.get("reference", [])}
            data[0]["content"] += re.sub(r'##\d\$\$', '', ans["answer"])
            fillin_conv(ans)
            API4ConversationService.append_message(db, conv.id, conv.to_dict())

            chunk_idxs = [int(match[2]) for match in re.findall(r'##\d\$\$', ans["answer"])]
            for chunk_idx in chunk_idxs[:1]:
                if ans["reference"]["chunks"][chunk_idx]["img_id"]:
                    try:
                        bkt, nm = ans["reference"]["chunks"][chunk_idx]["img_id"].split("-")
                        response = STORAGE_IMPL.get(bkt, nm)
                        data_type_picture["url"] = base64.b64encode(response).decode('utf-8')
                        data.append(data_type_picture)
                        break
                    except Exception as e:
                        return server_error_response(e)

            response = {"code": 200, "msg": "success", "data": data}
            return response

        # ******************For dialog******************
        conv.message.append(msg[-1])
        e, dia = DialogService.get_by_id(db, conv.dialog_id)
        if not e:
            return get_data_error_result(retmsg="Dialog not found!")
        del req["conversation_id"]

        if not conv.reference:
            conv.reference = []
        conv.message.append({"role": "assistant", "content": "", "id": message_id})
        conv.reference.append({"chunks": [], "doc_aggs": []})

        data_type_picture = {
            "type": 3,
            "url": "base64 content"
        }
        data = [
            {
                "type": 1,
                "content": ""
            }
        ]
        ans = ""
        for a in chat(dia, msg, stream=False, **req):
            ans = a
            break
        data[0]["content"] += re.sub(r'##\d\$\$', '', ans["answer"])
        fillin_conv(ans)
        API4ConversationService.append_message(conv.id, conv.to_dict())

        chunk_idxs = [int(match[2]) for match in re.findall(r'##\d\$\$', ans["answer"])]
        for chunk_idx in chunk_idxs[:1]:
            if ans["reference"]["chunks"][chunk_idx]["img_id"]:
                try:
                    bkt, nm = ans["reference"]["chunks"][chunk_idx]["img_id"].split("-")
                    response = STORAGE_IMPL.get(bkt, nm)
                    data_type_picture["url"] = base64.b64encode(response).decode('utf-8')
                    data.append(data_type_picture)
                    break
                except Exception as e:
                    return server_error_response(e)

        response = {"code": 200, "msg": "success", "data": data}
        return response

    except Exception as e:
        return server_error_response(e)


@router.post('/retrieval', summary="完成FAQ对话", response_description="成功完成FAQ对话")
def retrieval(request, question, db: Session = Depends(get_db),):
    token = request.headers.get('Authorization').split()[1]
    objs = APIToken.query(token=token)
    if not objs:
        return get_json_result(
            data=False, retmsg='Token is not valid!"', retcode=settings.RetCode.AUTHENTICATION_ERROR)

    req = request.json
    kb_ids = req.get("kb_id",[])
    doc_ids = req.get("doc_ids", [])
    question = req.get("question")
    page = int(req.get("page", 1))
    size = int(req.get("size", 30))
    similarity_threshold = float(req.get("similarity_threshold", 0.2))
    vector_similarity_weight = float(req.get("vector_similarity_weight", 0.3))
    top = int(req.get("top_k", 1024))

    try:
        kbs = KnowledgebaseService.get_by_ids(db, kb_ids)
        embd_nms = list(set([kb.embd_id for kb in kbs]))
        if len(embd_nms) != 1:
            return get_json_result(
                data=False, retmsg='Knowledge bases use different embedding models or does not exist."',
                retcode=settings.RetCode.AUTHENTICATION_ERROR)

        embd_mdl = TenantLLMService.model_instance(
            db, kbs[0].tenant_id, LLMType.EMBEDDING.value, llm_name=kbs[0].embd_id)
        rerank_mdl = None
        if req.get("rerank_id"):
            rerank_mdl = TenantLLMService.model_instance(
                db, kbs[0].tenant_id, LLMType.RERANK.value, llm_name=req["rerank_id"])
        if req.get("keyword", False):
            chat_mdl = TenantLLMService.model_instance(db, kbs[0].tenant_id, LLMType.CHAT)
            question += keyword_extraction(chat_mdl, question)
        ranks = settings.retrievaler.retrieval(question, embd_mdl, kbs[0].tenant_id, kb_ids, page, size,
                                      similarity_threshold, vector_similarity_weight, top,
                                      doc_ids, rerank_mdl=rerank_mdl)
        for c in ranks["chunks"]:
            if "vector" in c:
                del c["vector"]
        return get_json_result(data=ranks)
    except Exception as e:
        if str(e).find("not_found") > 0:
            return get_json_result(data=False, retmsg=f'No chunk found! Check the chunk status please!',
                                   retcode=settings.RetCode.DATA_ERROR)
        return server_error_response(e)
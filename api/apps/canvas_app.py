from __future__ import annotations

import json
import logging
import re
import sys
import time
from functools import partial
from typing import Any, AsyncIterator, Iterator

import trio
from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    Query,
    UploadFile
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from api import settings
from api.apps import manager  # 你现有的登录依赖，返回当前用户对象
from api.db.db_models import get_db, APIToken
from api.db import FileType
from api.db.services.canvas_service import (
    CanvasTemplateService,
    UserCanvasService,
    API4ConversationService
)
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from api.db.services.user_canvas_version import UserCanvasVersionService
from api.db.services.user_service import TenantService
from api.settings import RetCode
from api.utils import get_uuid
from api.utils.api_utils import (
    get_json_result,
    get_data_error_result,
    server_error_response
)
from api.utils.file_utils import filename_type, read_potential_broken_pdf
from core.utils.redis_conn import REDIS_CONN

# 运行期组件
from agent.component.llm import LLM
from agent.canvas import Canvas

# DB 测试
from peewee import MySQLDatabase, PostgresqlDatabase

router = APIRouter(prefix="/canvas", tags=["Canvas"])

# =========================
# Pydantic v2 Schemas
# =========================

class RemoveCanvasRequest(BaseModel):
    canvas_ids: list[str]

class SetCanvasRequest(BaseModel):
    title: str
    dsl: dict | str
    id: str | None = None
    description: str | None = None
    permission: str | None = None
    avatar: str | None = None

    @field_validator("dsl")
    @classmethod
    def _ensure_dict_or_json(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                raise ValueError("dsl must be a JSON object or a JSON string")
        return v

class CompletionRequest(BaseModel):
    id: str
    query: str | None = ""
    files: list[Any] | None = []
    inputs: dict | None = {}
    user_id: str | None = None

class ResetRequest(BaseModel):
    id: str

class DebugRequest(BaseModel):
    id: str
    component_id: str
    params: dict

class TestDBConnectRequest(BaseModel):
    db_type: str
    database: str
    username: str
    host: str
    port: int
    password: str

class SettingRequest(BaseModel):
    id: str
    title: str
    permission: str
    description: str | None = None
    avatar: str | None = None

# =========================
# 路由实现
# =========================

@router.get("/templates", summary="获取Canvas模板列表")
def templates(db: Session = Depends(get_db), user=Depends(manager)):
    try:
        data = [c.to_dict() for c in CanvasTemplateService.get_all(db)]
        return get_json_result(data=data)
    except Exception as e:
        return server_error_response(e)


@router.get("/list", summary="获取我的Canvas列表")
def canvas_list(db: Session = Depends(get_db), user=Depends(manager)):
    try:
        kbs = [c.to_dict() for c in UserCanvasService.query(db, user_id=user.id)]
        kbs_sorted = sorted(kbs, key=lambda x: x["update_time"] * -1)
        return get_json_result(data=kbs_sorted)
    except Exception as e:
        return server_error_response(e)


@router.post("/rm", summary="删除Canvas（批量）")
def rm(request: RemoveCanvasRequest, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        for cid in request.canvas_ids:
            if not UserCanvasService.accessible(db, cid, tenant_id=user.id):
                return get_json_result(
                    data=False,
                    retmsg="Only owner of canvas authorized for this operation.",
                    retcode=RetCode.OPERATING_ERROR
                )
            UserCanvasService.delete_by_id(db, cid)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post("/set", summary="创建/更新 Canvas")
def save(request: SetCanvasRequest, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        req = request.model_dump()
        dsl_obj = req["dsl"]  # 已在 validator 中转为 dict

        if "id" not in req or not req["id"]:
            req["user_id"] = user.id
            # 新建
            if UserCanvasService.query(db, user_id=user.id, title=req["title"].strip()):
                return get_data_error_result(retmsg=f"{req['title'].strip()} already exists.")
            req["id"] = get_uuid()
            # 存储 dsl 需要序列化为原库约定格式
            to_save = {**req, "dsl": json.dumps(dsl_obj, ensure_ascii=False)}
            ok = UserCanvasService.save(db, **to_save)
            if not ok:
                return get_data_error_result(retmsg="Fail to save canvas.")
        else:
            # 更新
            if not UserCanvasService.accessible(db, req["id"], user.id):
                return get_json_result(
                    data=False,
                    retmsg="Only owner of canvas authorized for this operation.",
                    retcode=RetCode.OPERATING_ERROR
                )
            to_update = {**req, "dsl": dsl_obj}
            UserCanvasService.update_by_id(db, req["id"], to_update)

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


@router.get("/get/{canvas_id}", summary="获取 Canvas 详情")
def get(canvas_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    if not UserCanvasService.accessible(db, canvas_id, user.id):
        return get_data_error_result(retmsg="canvas not found.")
    e, c = UserCanvasService.get_by_tenant_id(db, canvas_id)
    return get_json_result(data=c)


@router.get("/getsse/{canvas_id}", summary="SSE获取（供外部token使用）")
def getsse(
    canvas_id: str,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    try:
        if not authorization:
            return get_data_error_result(retmsg='Authorization is not valid!"')
        parts = authorization.split()
        if len(parts) != 2:
            return get_data_error_result(retmsg='Authorization is not valid!"')
        token = parts[1]
        objs = APIToken.query(db, beta=token)
        if not objs:
            return get_data_error_result(retmsg='Authentication error: API key is invalid!"')
        tenant_id = objs[0].tenant_id
        if not UserCanvasService.query(db, user_id=tenant_id, id=canvas_id):
            return get_json_result(
                data=False,
                retmsg='Only owner of canvas authorized for this operation.',
                retcode=RetCode.OPERATING_ERROR
            )
        e, c = UserCanvasService.get_by_id(db, canvas_id)
        if not e or c.user_id != tenant_id:
            return get_data_error_result(retmsg="canvas not found.")
        return get_json_result(data=c.to_dict())
    except Exception as e:
        return server_error_response(e)


@router.post("/completion", summary="运行 Canvas（SSE）")
def run(request: CompletionRequest, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        req = request.model_dump()
        query = req.get("query") or ""
        files = req.get("files") or []
        inputs = req.get("inputs") or {}
        user_id = req.get("user_id") or user.id

        e, cvs = UserCanvasService.get_by_id(db, req["id"])
        if not e:
            return get_data_error_result(retmsg="canvas not found.")

        if not UserCanvasService.accessible(db, req["id"], user.id):
            return get_json_result(
                data=False,
                retmsg="Only owner of canvas authorized for this operation.",
                retcode=RetCode.OPERATING_ERROR
            )

        e, cvs = UserCanvasService.get_by_id(db, req["id"])
        if not e:
            return get_data_error_result(retmsg="canvas not found.")

        dsl_str = cvs.dsl if isinstance(cvs.dsl, str) else json.dumps(cvs.dsl, ensure_ascii=False)

        # 使用生成器包装为 SSE
        def _iter() -> Iterator[bytes]:
            try:
                canvas = Canvas(dsl_str, user.id, req["id"])
                for ans in canvas.run(query=query, files=files, user_id=user_id, inputs=inputs):
                    yield ("data:" + json.dumps(ans, ensure_ascii=False) + "\n\n").encode("utf-8")

                # 运行结束后回写最新 DSL
                cvs.dsl = json.loads(str(canvas))
                UserCanvasService.update_by_id(db, req["id"], cvs.to_dict())
            except Exception as e:
                logging.exception(e)
                err = {"code": 500, "message": str(e), "data": False}
                yield ("data:" + json.dumps(err, ensure_ascii=False) + "\n\n").encode("utf-8")

        return StreamingResponse(
            _iter(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "Content-Type": "text/event-stream; charset=utf-8",
            },
        )
    except Exception as e:
        return server_error_response(e)


@router.post("/reset", summary="重置 Canvas 状态")
def reset(request: ResetRequest, db: Session = Depends(get_db), user=Depends(manager)):
    if not UserCanvasService.accessible(db, request.id, user.id):
        return get_json_result(
            data=False, retmsg='Only owner of canvas authorized for this operation.',
            retcode=RetCode.OPERATING_ERROR)
    try:
        e, user_canvas = UserCanvasService.get_by_id(db, request.id)
        if not e:
            return get_data_error_result(retmsg="canvas not found.")

        canvas = Canvas(json.dumps(user_canvas.dsl), user.id)
        canvas.reset()
        new_dsl = json.loads(str(canvas))
        UserCanvasService.update_by_id(db, request.id, {"dsl": new_dsl})
        return get_json_result(data=new_dsl)
    except Exception as e:
        return server_error_response(e)


@router.post("/upload/{canvas_id}", summary="上传文件/URL到 Canvas")
async def upload(
    canvas_id: str,
    file: UploadFile | None = File(default=None),
    url: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    try:
        e, cvs = UserCanvasService.get_by_tenant_id(db, canvas_id)
        if not e:
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

        if url:
            from crawl4ai import (
                AsyncWebCrawler,
                BrowserConfig,
                CrawlerRunConfig,
                DefaultMarkdownGenerator,
                PruningContentFilter,
                CrawlResult,
            )

            async def adownload():
                browser_config = BrowserConfig(headless=True, verbose=False)
                async with AsyncWebCrawler(config=browser_config) as crawler:
                    crawler_config = CrawlerRunConfig(
                        markdown_generator=DefaultMarkdownGenerator(content_filter=PruningContentFilter()),
                        pdf=True,
                        screenshot=False,
                    )
                    result: CrawlResult = await crawler.arun(url=url, config=crawler_config)
                    return result

            try:
                page = await adownload()
                filename = re.sub(r"\?.*", "", url.split("/")[-1]) or "download"
                if page.pdf:
                    if filename.split(".")[-1].lower() != "pdf":
                        filename += ".pdf"
                    return get_json_result(
                        data=structured(filename, "pdf", page.pdf, page.response_headers.get("content-type", "application/pdf"))
                    )
                # html/markdown 内容
                blob = str(page.markdown).encode("utf-8")
                return get_json_result(
                    data=structured(filename, "html", blob, page.response_headers.get("content-type", "text/html"))
                )
            except Exception as e:
                return server_error_response(e)

        if not file:
            return get_data_error_result(retmsg="No file or url provided.")

        # 常规文件上传
        try:
            content = await file.read()
            DocumentService.check_doc_health(user_id, file.filename)
            return get_json_result(
                data=structured(file.filename, filename_type(file.filename), content, file.content_type or "application/octet-stream")
            )
        except Exception as e:
            return server_error_response(e)
    except Exception as e:
        return server_error_response(e)


@router.get("/input_form", summary="获取组件输入表单描述")
def input_form(
    id: str = Query(..., description="canvas id"),
    component_id: str = Query(..., description="component id"),
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    try:
        e, user_canvas = UserCanvasService.get_by_id(db, id)
        if not e:
            return get_data_error_result(retmsg="canvas not found.")
        if not UserCanvasService.query(db, user_id=user.id, id=id):
            return get_json_result(
                data=False,
                retmsg="Only owner of canvas authorized for this operation.",
                retcode=RetCode.OPERATING_ERROR
            )
        canvas = Canvas(json.dumps(user_canvas.dsl), user.id)
        form_desc = canvas.get_component_input_form(component_id)
        return get_json_result(data=form_desc)
    except Exception as e:
        return server_error_response(e)


@router.post("/debug", summary="组件调试执行")
def debug(request: DebugRequest, db: Session = Depends(get_db), user=Depends(manager)):
    if not UserCanvasService.accessible(db, request.id, user.id):
        return get_json_result(
            data=False, retmsg='Only owner of canvas authorized for this operation.',
            retcode=RetCode.OPERATING_ERROR)
    try:
        e, user_canvas = UserCanvasService.get_by_id(db, request.id)
        canvas = Canvas(json.dumps(user_canvas.dsl), user.id)
        canvas.reset()
        canvas.message_id = get_uuid()
        component = canvas.get_component(request.component_id)["obj"]
        component.reset()
        if isinstance(component, LLM):
            component.set_debug_inputs(request.params)

        # 将 params 的 {k: {"value": ...}} 转为 kwargs
        kwargs = {k: o["value"] for k, o in request.params.items()}
        component.invoke(**kwargs)

        outputs = component.output()
        for k in list(outputs.keys()):
            if isinstance(outputs[k], partial):
                txt = ""
                for c in outputs[k]():
                    txt += c
                outputs[k] = txt
        return get_json_result(data=outputs)
    except Exception as e:
        return server_error_response(e)


@router.post("/test_db_connect", summary="测试数据库连通性")
def test_db_connect(request: TestDBConnectRequest, user=Depends(manager)):
    try:
        if request.db_type in ["mysql", "mariadb"]:
            db = MySQLDatabase(
                request.database,
                user=request.username,
                host=request.host,
                port=request.port,
                password=request.password
            )
            db.connect()
            db.close()
        elif request.db_type == "postgresql":
            db = PostgresqlDatabase(
                request.database,
                user=request.username,
                host=request.host,
                port=request.port,
                password=request.password
            )
            db.connect()
            db.close()
        elif request.db_type == "mssql":
            import pyodbc
            connection_string = (
                f"DRIVER={{ODBC Driver 17 for SQL Server}};"
                f"SERVER={request.host},{request.port};"
                f"DATABASE={request.database};"
                f"UID={request.username};"
                f"PWD={request.password};"
            )
            db = pyodbc.connect(connection_string)
            cursor = db.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            db.close()
        else:
            return server_error_response("Unsupported database type.")
        return get_json_result(data="Database Connection Successful!")
    except Exception as e:
        return server_error_response(e)


@router.get("/getlistversion/{canvas_id}", summary="获取 Canvas 版本列表")
def getlistversion(canvas_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        vlist = sorted(
            [c.to_dict() for c in UserCanvasVersionService.list_by_canvas_id(db, canvas_id)],
            key=lambda x: x["update_time"] * -1
        )
        return get_json_result(data=vlist)
    except Exception as e:
        return get_data_error_result(retmsg=f"Error getting history files: {e}")


@router.get("/getversion/{version_id}", summary="获取指定版本")
def getversion(version_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        e, version = UserCanvasVersionService.get_by_id(db, version_id)
        if version:
            return get_json_result(data=version.to_dict())
        return get_json_result(data=None)
    except Exception as e:
        return get_json_result(data=f"Error getting history file: {e}")


@router.get("/listteam", summary="获取团队/共享空间下的 Canvas")
def list_canvas(
    keywords: str = Query("", description="关键词"),
    page: int = Query(1, ge=1),
    page_size: int = Query(150, ge=1, le=500),
    orderby: str = Query("create_time"),
    desc: bool = Query(True),
    db: Session = Depends(get_db),
    user=Depends(manager),
):
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
            keywords
        )
        return get_json_result(data={"canvas": canvas, "total": total})
    except Exception as e:
        return server_error_response(e)


@router.post("/setting", summary="更新 Canvas 基本设置")
def setting(request: SettingRequest, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        req = request.model_dump()
        req["user_id"] = user.id

        if not UserCanvasService.accessible(db, req["id"], user.id):
            return get_json_result(
                data=False, retmsg='Only owner of canvas authorized for this operation.',
                retcode=RetCode.OPERATING_ERROR)

        e, flow = UserCanvasService.get_by_id(db, req["id"])
        if not e:
            return get_data_error_result(retmsg="canvas not found.")
        flow_dict = flow.to_dict()
        flow_dict["title"] = req["title"]
        if req.get("description"):
            flow_dict["description"] = req["description"]
        if req.get("permission"):
            flow_dict["permission"] = req["permission"]
        if req.get("avatar"):
            flow_dict["avatar"] = req["avatar"]

        num = UserCanvasService.update_by_id(db, req["id"], flow_dict)
        return get_json_result(data=num)
    except Exception as e:
        return server_error_response(e)


@router.get("/trace", summary="获取运行链路追踪日志")
def trace(
    canvas_id: str = Query(...),
    message_id: str = Query(...),
    user=Depends(manager),
):
    try:
        binv = REDIS_CONN.get(f"{canvas_id}-{message_id}-logs")
        if not binv:
            return get_json_result(data={})
        return get_json_result(data=json.loads(binv.encode("utf-8")))
    except Exception as e:
        logging.exception(e)
        return server_error_response(e)


@router.get("/{canvas_id}/sessions", summary="分页获取会话记录")
def sessions(
    canvas_id: str,
    user_id: str | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=200),
    keywords: str | None = Query(default=None),
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    orderby: str = Query("update_time"),
    desc: bool = Query(True),
    dsl: bool = Query(True, description="是否包含dsl"),
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    try:
        tenant_id = user.id
        if not UserCanvasService.accessible(db, canvas_id, tenant_id):
            return get_json_result(
                data=False, retmsg='Only owner of canvas authorized for this operation.',
                retcode=RetCode.OPERATING_ERROR)

        include_dsl = dsl  # 与原逻辑一致：除 false/False 外均为 True
        total, sess = API4ConversationService.get_list(
            db,
            canvas_id,
            tenant_id,
            page,
            page_size,
            orderby,
            desc,
            None,
            user_id,
            include_dsl,
            keywords,
            from_date,
            to_date
        )
        return get_json_result(data={"total": total, "sessions": sess})
    except Exception as e:
        return server_error_response(e)
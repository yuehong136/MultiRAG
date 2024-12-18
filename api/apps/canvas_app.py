# coding=utf-8
"""
@project: multirag
@Author：龙
@file： canvas_app.py
@date：2024/8/9 14:04
@desc:
"""
import logging
import json
from functools import partial

from fastapi import APIRouter, Depends, HTTPException, Response, Body
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api.db.services.canvas_service import CanvasTemplateService, UserCanvasService
from api import settings
from api.utils import get_uuid
from api.utils.api_utils import get_json_result, server_error_response, get_data_error_result
from api.db.database import get_db
from api.apps import manager
from agent.canvas import Canvas

router = APIRouter()


class RemoveCanvasRequest(BaseModel):
    canvas_ids: list[str]


class SaveCanvasRequest(BaseModel):
    id: str | None
    dsl: str
    title: str


class RunCanvasRequest(BaseModel):
    id: str
    message: str | None = None
    stream: bool | None = True


class ResetCanvasRequest(BaseModel):
    id: str


class DBConnectionRequest(BaseModel):
    db_type: str
    database: str
    username: str
    host: str
    port: int
    password: str

@router.get('/templates', summary="获取所有画布模板", response_description="成功获取所有画布模板")
async def templates(db: Session = Depends(get_db), user=Depends(manager)):
    return get_json_result(data=[c.to_dict() for c in CanvasTemplateService.get_all(db)])


@router.get('/list', summary="获取用户画布列表", response_description="成功获取用户画布列表")
async def canvas_list(db: Session = Depends(get_db), user=Depends(manager)):
    return get_json_result(data=sorted(
        [c.to_dict() for c in UserCanvasService.query(db, user_id=user.id)],
        key=lambda x: x["update_time"] * -1
    ))


@router.post('/rm', summary="删除画布", response_description="成功删除画布")
async def rm(request: RemoveCanvasRequest, db: Session = Depends(get_db), user=Depends(manager)):
    for canvas_id in request.canvas_ids:
        if not UserCanvasService.query(db, user_id=user.id,id=canvas_id):
            return get_json_result(
                data=False, retmsg=f'Only owner of canvas authorized for this operation.',
                retcode=settings.RetCode.OPERATING_ERROR)
        UserCanvasService.delete_by_id(db, canvas_id)
    return get_json_result(data=True)


@router.post('/set', summary="保存画布", response_description="成功保存画布")
async def save(request: SaveCanvasRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req_data = request.model_dump()
    req_data["user_id"] = user.id
    if not isinstance(req_data["dsl"], str):
        req_data["dsl"] = json.dumps(req_data["dsl"], ensure_ascii=False)

    req_data["dsl"] = json.loads(req_data["dsl"])

    if "id" not in req_data or not req_data["id"]:
        if UserCanvasService.query(db, user_id=user.id, title=req_data["title"].strip()):
            return server_error_response(ValueError("Duplicated title."))
        req_data["id"] = get_uuid()
        if not UserCanvasService.save(db, **req_data):
            return get_data_error_result(retmsg="Fail to save canvas.")
    else:
        if not UserCanvasService.query(db, user_id=user.id, id=req_data["id"]):
            return get_json_result(
                data=False, retmsg=f'Only owner of canvas authorized for this operation.',
                retcode=settings.RetCode.OPERATING_ERROR)
        UserCanvasService.update_by_id(db, req_data["id"], req_data)

    return get_json_result(data=req_data)


@router.get('/get/{canvas_id}', summary="获取画布详情", response_description="成功获取画布详情")
async def get(canvas_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    c = UserCanvasService.get_by_id(db, canvas_id)
    if not c:
        return get_data_error_result(retmsg="canvas not found.")
    return get_json_result(data=c.to_dict())


@router.post('/completion', summary="运行画布", response_description="成功运行画布")
async def run(request: RunCanvasRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req_data = request.model_dump()
    stream = req_data.get("stream", True)
    cvs = UserCanvasService.get_by_id(db, req_data["id"])
    if not cvs:
        return get_data_error_result(retmsg="canvas not found.")

    if not isinstance(cvs.dsl, str):
        cvs.dsl = json.dumps(cvs.dsl, ensure_ascii=False)

    final_ans = {"reference": [], "content": ""}
    message_id = req_data.get("message_id", get_uuid())
    try:
        canvas = Canvas(cvs.dsl, user.id)
        if "message" in req_data:
            canvas.messages.append({"role": "user", "content": req_data["message"], "id": message_id})
            if len([m for m in canvas.messages if m["role"] == "user"]) > 1:
                # ten = TenantService.get_info_by(db, user.id)[0]
                # req["message"] = full_question(db, ten["tenant_id"], ten["llm_id"], canvas.messages)
                pass
            canvas.add_user_input(req_data["message"])
        answer = canvas.run(stream=stream)
        logging.debug(canvas)
    except Exception as e:
        return server_error_response(e)

    assert answer is not None, "Nothing. Is it over?"

    if stream:
        assert isinstance(answer, partial), "Nothing. Is it over?"

        def sse():
            nonlocal answer, cvs
            try:
                for ans in answer():
                    for k in ans.keys():
                        final_ans[k] = ans[k]
                    ans = {"answer": ans["content"], "reference": ans.get("reference", [])}
                    yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": ans}, ensure_ascii=False) + "\n\n"

                canvas.messages.append({"role": "assistant", "content": final_ans["content"], "id": message_id})
                if final_ans.get("reference"):
                    canvas.reference.append(final_ans["reference"])
                cvs.dsl = json.loads(str(canvas))
                UserCanvasService.update_by_id(db, req_data["id"], cvs.to_dict())
            except Exception as e:
                yield "data:" + json.dumps({"retcode": 500, "retmsg": str(e),
                                            "data": {"answer": "**ERROR**: " + str(e), "reference": []}},
                                           ensure_ascii=False) + "\n\n"
            yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False) + "\n\n"

        return Response(sse(), media_type="text/event-stream")

    final_ans["content"] = "\n".join(answer["content"]) if "content" in answer else ""
    canvas.messages.append({"role": "assistant", "content": final_ans["content"], "id": message_id})
    if final_ans.get("reference"):
        canvas.reference.append(final_ans["reference"])
    cvs.dsl = json.loads(str(canvas))
    UserCanvasService.update_by_id(db, req_data["id"], cvs.to_dict())
    return get_json_result(data={"answer": final_ans["content"], "reference": final_ans.get("reference", [])})


@router.post('/reset', summary="重置画布", response_description="成功重置画布")
async def reset(request: ResetCanvasRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req_data = request.model_dump()
    try:
        user_canvas = UserCanvasService.get_by_id(db, req_data["id"])
        if not user_canvas:
            return get_data_error_result(retmsg="canvas not found.")

        if not UserCanvasService.query(user_id=user.id, id=req_data["id"]):
            return get_json_result(
                data=False, retmsg='Only owner of canvas authorized for this operation.',
                retcode=settings.RetCode.OPERATING_ERROR)
        canvas = Canvas(json.dumps(user_canvas.dsl), user.id)
        canvas.reset()
        req_data["dsl"] = json.loads(str(canvas))
        UserCanvasService.update_by_id(db, req_data["id"], {"dsl": req_data["dsl"]})
        return get_json_result(data=req_data["dsl"])
    except Exception as e:
        return server_error_response(e)


@router.post('/test_db_connect', summary="测试数据库连接", response_description="成功测试数据库连接")
def test_db_connect(
    request: DBConnectionRequest = Body(...),
    user=Depends(manager)
):
    try:
        # 根据 db_type 选择不同的数据库引擎
        if request.db_type in ["mysql", "mariadb"]:
            db_url = f"mysql+pymysql://{request.username}:{request.password}@{request.host}:{request.port}/{request.database}"
        elif request.db_type == 'postgresql':
            db_url = f"postgresql+psycopg2://{request.username}:{request.password}@{request.host}:{request.port}/{request.database}"
        else:
            raise HTTPException(status_code=400, detail="Unsupported database type")

        # 创建数据库引擎
        engine = create_engine(db_url)

        # 测试连接
        with engine.connect() as connection:
            connection.execute("SELECT 1")

        return {"data": "Database Connection Successful!"}
    except SQLAlchemyError as e:
        raise HTTPException(status_code=400, detail=str(e))
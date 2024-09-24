# coding=utf-8
"""
@project: multirag
@Author：龙
@file： xxx.py
@date：2024/7/9 9:00
@desc:
"""
import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import get_json_result
from api.versions import get_rag_version
from core.settings import SVR_QUEUE_NAME
from core.utils.storage_factory import STORAGE_IMPL, STORAGE_IMPL_TYPE
from timeit import default_timer as timer
from core.utils.redis_conn import REDIS_CONN
from api.db.database import get_db
from api.apps import manager

router = APIRouter()

@router.get("/version", summary="获取版本", response_description="成功获取版本")
async def version():
    return get_json_result(data=get_rag_version())

@router.get("/status", summary="获取系统状态", response_description="成功获取系统状态")
async def status(db: Session = Depends(get_db)):
    res = {}
    st = timer()
    try:
        res["es"] = ELASTICSEARCH.health()
        res["es"]["elapsed"] = "{:.1f}".format((timer() - st) * 1000.)
    except Exception as e:
        res["es"] = {"status": "red", "elapsed": "{:.1f}".format((timer() - st) * 1000.), "error": str(e)}

    st = timer()
    try:
        STORAGE_IMPL.health()
        res["storage"] = {"storage": STORAGE_IMPL_TYPE.lower(), "status": "green",
                          "elapsed": "{:.1f}".format((timer() - st) * 1000.)}
    except Exception as e:
        res["storage"] = {"storage": STORAGE_IMPL_TYPE.lower(), "status": "red",
                          "elapsed": "{:.1f}".format((timer() - st) * 1000.), "error": str(e)}

    st = timer()
    try:
        KnowledgebaseService.get_by_id("x")
        res["database"] = {"database": "postgres", "status": "green",
                           "elapsed": "{:.1f}".format((timer() - st) * 1000.)}
    except Exception as e:
        res["database"] = {"database": "postgres", "status": "red",
                           "elapsed": "{:.1f}".format((timer() - st) * 1000.), "error": str(e)}


    st = timer()
    try:
        if not REDIS_CONN.health():
            raise Exception("Lost connection!")
        res["redis"] = {"status": "green", "elapsed": "{:.1f}".format((timer() - st) * 1000.)}
    except Exception as e:
        res["redis"] = {"status": "red", "elapsed": "{:.1f}".format((timer() - st) * 1000.), "error": str(e)}

    try:
        v = REDIS_CONN.get("TASKEXE")
        if not v:
            raise Exception("No task executor running!")
        obj = json.loads(v)
        color = "green"
        for id in obj.keys():
            arr = obj[id]
            if len(arr) == 1:
                obj[id] = [0]
            else:
                obj[id] = [arr[i+1]-arr[i] for i in range(len(arr)-1)]
            elapsed = max(obj[id])
            if elapsed > 50: color = "yellow"
            if elapsed > 120: color = "red"
        res["task_executor"] = {"status": color, "elapsed": obj}
    except Exception as e:
        res["task_executor"] = {"status": "red", "error": str(e)}

    return get_json_result(data=res)

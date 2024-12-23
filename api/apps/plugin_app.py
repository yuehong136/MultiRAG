from fastapi import APIRouter, Depends, Body
from api.apps import manager
from api.db.database import get_db
from sqlalchemy.orm import Session

from api.service.plugin_service.plugin_service import PluginService
from api.service.script_scheduler_service.script_scheduler_service import ScriptSchedulerService

router = APIRouter()

from enum import Enum

from pydantic import BaseModel
from typing import Any, Optional, Dict


class StatusEnum(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class ResponseSchema(BaseModel):
    status: StatusEnum = StatusEnum.SUCCESS
    message: Optional[str] = None
    data: Optional[Any] = None


class RunTemporaryScriptRequest(BaseModel):
    script: str
    args: Dict[str, Any]


class RunPluginScriptRequest(BaseModel):
    plugin_id: str
    script: str
    args: Dict[str, Any]


class InstallDepRequest(BaseModel):
    plugin_id: str
    package_name: str
    package_version: Optional[str] = None


class UninstallDepRequest(BaseModel):
    plugin_id: str
    package_name: str


@router.post("/run-plugin-script", summary="运行插件脚本")
async def run_plugin_script(
        body: RunPluginScriptRequest = Body(...),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    result = await ScriptSchedulerService.run_plugin_script(
        plugin_id=body.plugin_id,
        script=body.script,
        args=body.args,
        user_id=user.id
    )
    return ResponseSchema(message="运行插件脚本成功", data=result)


@router.post("/install-dep", summary="安装插件依赖")
async def install_dep(
        body: InstallDepRequest = Body(...),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    result = await PluginService.install_dep(
        plugin_id=body.plugin_id,
        package_name=body.package_name,
        package_version=body.package_version
    )
    return ResponseSchema(message="安装依赖成功", data=result)


@router.post("/uninstall-dep", summary="卸载插件依赖")
async def uninstall_dep(
        body: UninstallDepRequest = Body(...),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    result = await PluginService.uninstall_dep(
        plugin_id=body.plugin_id,
        package_name=body.package_name
    )
    return ResponseSchema(message="卸载依赖成功", data=result)

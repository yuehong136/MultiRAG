from __future__ import annotations

from enum import Enum
from typing import Any

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel

from agent.plugin import GlobalPluginManager
from api.service.plugin_service.plugin_service import PluginService
from api.service.script_scheduler_service.script_scheduler_service import ScriptSchedulerService
from api.utils.api_utils import Principal, async_current_user, get_json_result

router = APIRouter()


class StatusEnum(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class ResponseSchema(BaseModel):
    status: StatusEnum = StatusEnum.SUCCESS
    message: str | None = None
    data: Any | None = None


class RunPluginScriptRequest(BaseModel):
    plugin_id: str
    script: str
    args: dict[str, Any]


class InstallDepRequest(BaseModel):
    plugin_id: str
    package_name: str
    package_version: str | None = None


class UninstallDepRequest(BaseModel):
    plugin_id: str
    package_name: str


@router.post("/plugin/run-plugin-script", summary="运行插件脚本")
async def run_plugin_script(
    body: RunPluginScriptRequest = Body(...),
    user: Principal = Depends(async_current_user),
) -> ResponseSchema:
    result = await ScriptSchedulerService.run_plugin_script(
        plugin_id=body.plugin_id,
        script=body.script,
        args=body.args,
        user_id=user.id,
    )
    return ResponseSchema(message="运行插件脚本成功", data=result)


@router.post("/plugin/install-dep", summary="安装插件依赖")
async def install_dep(
    body: InstallDepRequest = Body(...),
    _user: Principal = Depends(async_current_user),
) -> ResponseSchema:
    result = await PluginService.install_dep(
        plugin_id=body.plugin_id,
        package_name=body.package_name,
        package_version=body.package_version,
    )
    return ResponseSchema(message="安装依赖成功", data=result)


@router.post("/plugin/uninstall-dep", summary="卸载插件依赖")
async def uninstall_dep(
    body: UninstallDepRequest = Body(...),
    _user: Principal = Depends(async_current_user),
) -> ResponseSchema:
    result = await PluginService.uninstall_dep(plugin_id=body.plugin_id, package_name=body.package_name)
    return ResponseSchema(message="卸载依赖成功", data=result)


@router.get("/plugin/tools", summary="获取LLM工具列表", response_description="成功获取LLM工具列表")
async def llm_tools(_user: Principal = Depends(async_current_user)) -> Any:
    tools_metadata = [tool.get_metadata() for tool in GlobalPluginManager.get_llm_tools()]
    return get_json_result(data=tools_metadata)

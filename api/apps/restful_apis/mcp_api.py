"""RESTful MCP server management API.

Routes are mounted under ``/api/v1`` by ``api.apps.register_page``.  The
legacy ``/v1/mcp_server/*`` surface remains until its dedicated removal step.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import MCPServer, get_async_db
from api.db.services.mcp_server_service import MCPServerService
from api.db.services.user_service import TenantService
from api.utils.api_utils import Principal, async_current_user, get_data_error_result, get_json_result, get_mcp_tools, server_error_response
from api.utils.web_utils import get_float, safe_json_parse
from common.constants import VALID_MCP_SERVER_TYPES
from common.mcp_tool_call_conn import MCPToolCallSession, close_multiple_mcp_toolcall_sessions
from common.misc_utils import get_uuid, thread_pool_exec

router = APIRouter()

JsonObject = dict[str, Any]
JsonObjectInput = JsonObject | str


class CreateMCPServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    server_type: str
    description: str | None = None
    variables: JsonObjectInput | None = None
    headers: JsonObjectInput | None = None
    timeout: float = 10.0


class UpdateMCPServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    url: str | None = None
    server_type: str | None = None
    description: str | None = None
    variables: JsonObjectInput | None = None
    headers: JsonObjectInput | None = None
    timeout: float = 10.0


class ImportMCPServersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mcp_servers: dict[str, JsonObject] = Field(alias="mcpServers")
    timeout: float = 10.0


class TestMCPServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    server_type: str
    headers: JsonObjectInput | None = None
    variables: JsonObjectInput | None = None
    timeout: float = 10.0


class TestMCPToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: JsonObject
    timeout: float = 10.0


class CacheMCPToolsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools: list[JsonObject]


def _normalize_mcp_ids(mcp_ids: list[str] | None, mcp_id: str) -> list[str]:
    values = mcp_ids or ([mcp_id] if mcp_id else [])
    return [value for item in values for value in item.split(",") if value]


def _export_payload(db: Session, mcp_id: str, tenant_id: str) -> dict[str, JsonObject] | None:
    server = MCPServerService.get_by_id(db, mcp_id)
    if server is None or server.tenant_id != tenant_id:
        return None

    variables = server.variables or {}
    return {
        "mcpServers": {
            server.name: {
                "type": server.server_type,
                "url": server.url,
                "name": server.name,
                "authorization_token": variables.get("authorization_token", ""),
                "tools": variables.get("tools", {}),
            }
        }
    }


def _owned_server_payload(db: Session, mcp_id: str, tenant_id: str) -> JsonObject | None:
    server = MCPServerService.get_by_id(db, mcp_id)
    if server is None or server.tenant_id != tenant_id:
        return None
    return {
        "id": server.id,
        "name": server.name,
        "tenant_id": server.tenant_id,
        "url": server.url,
        "server_type": server.server_type,
        "description": server.description,
        "variables": server.variables or {},
        "headers": server.headers or {},
    }


@router.get("/mcp/servers", summary="获取MCP服务器列表", response_description="成功获取MCP服务器列表")
async def list_mcp(
    keywords: str = Query(""),
    page: int = Query(0, ge=0),
    page_size: int = Query(0, ge=0),
    orderby: str = Query("create_time"),
    desc: bool = Query(True),
    mcp_ids: Annotated[list[str] | None, Query()] = None,
    mcp_id: str = Query(""),
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    ids = _normalize_mcp_ids(mcp_ids, mcp_id)
    try:
        servers = await db.run_sync(  # TODO(async-phase4)
            lambda session: MCPServerService.get_servers(session, user.id, ids, 0, 0, orderby, desc, keywords) or []
        )
        total = len(servers)
        if page and page_size:
            servers = servers[(page - 1) * page_size : page * page_size]
        return get_json_result(data={"mcp_servers": servers, "total": total})
    except Exception as error:
        return server_error_response(error)


@router.get("/mcp/servers/{mcp_id}", summary="获取或导出MCP服务器", response_description="成功获取MCP服务器")
async def detail(
    mcp_id: str,
    mode: str = Query("preview"),
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    try:
        if mode == "download":
            payload = await db.run_sync(lambda session: _export_payload(session, mcp_id, user.id))  # TODO(async-phase4)
        else:
            payload = await db.run_sync(  # TODO(async-phase4)
                lambda session: server.to_dict() if (server := MCPServerService.get_or_none(session, id=mcp_id, tenant_id=user.id)) is not None else None
            )

        if payload is None:
            return get_data_error_result(retmsg=f"Cannot find MCP server {mcp_id} for user {user.id}")
        return get_json_result(data=payload)
    except Exception as error:
        return server_error_response(error)


@router.post("/mcp/servers", summary="创建MCP服务器", response_description="成功创建MCP服务器")
async def create(
    request: CreateMCPServerRequest,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    request_data = request.model_dump()
    server_type = request.server_type
    if server_type not in VALID_MCP_SERVER_TYPES:
        return get_data_error_result(retmsg="Unsupported MCP server type.")

    server_name = request.name
    if not server_name or len(server_name.encode("utf-8")) > 255:
        return get_data_error_result(retmsg=f"Invalid MCP name or length is {len(server_name)} which is large than 255.")
    if not request.url:
        return get_data_error_result(retmsg="Invalid url.")

    try:
        duplicate, tenant_exists = await db.run_sync(  # TODO(async-phase4)
            lambda session: (
                MCPServerService.get_by_name_and_tenant(session, name=server_name, tenant_id=user.id)[0],
                TenantService.get_by_id(session, user.id) is not None,
            )
        )
        if duplicate:
            return get_data_error_result(retmsg="Duplicated MCP server name.")
        if not tenant_exists:
            return get_data_error_result(retmsg="Tenant not found.")

        headers = safe_json_parse(request.headers or {})
        variables = safe_json_parse(request.variables or {})
        variables.pop("tools", None)
        timeout = get_float(request_data, "timeout", 10)

        probe = MCPServer(id=server_name, name=server_name, url=request.url, server_type=server_type, variables=variables, headers=headers)
        server_tools, error_message = await thread_pool_exec(get_mcp_tools, [probe], timeout)
        if error_message:
            return get_data_error_result(retmsg=error_message)

        tools = server_tools[server_name]
        variables["tools"] = {tool["name"]: tool for tool in tools if isinstance(tool, dict) and "name" in tool}
        create_data = {
            "id": get_uuid(),
            "tenant_id": user.id,
            "name": server_name,
            "url": request.url,
            "server_type": server_type,
            "description": request.description,
            "variables": variables,
            "headers": headers,
        }

        created = await db.run_sync(lambda session: MCPServerService.insert(session, **create_data))  # TODO(async-phase4)
        if not created:
            return get_data_error_result(retmsg="Failed to create MCP server.")
        return get_json_result(data=create_data)
    except Exception as error:
        return server_error_response(error)


@router.put("/mcp/servers/{mcp_id}", summary="更新MCP服务器", response_description="成功更新MCP服务器")
async def update(
    mcp_id: str,
    request: UpdateMCPServerRequest,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    try:
        existing = await db.run_sync(  # TODO(async-phase4)
            lambda session: server.to_dict() if (server := MCPServerService.get_by_id(session, mcp_id)) is not None and server.tenant_id == user.id else None
        )
        if existing is None:
            return get_data_error_result(retmsg=f"Cannot find MCP server {mcp_id} for user {user.id}")

        request_data = request.model_dump(exclude_unset=True)
        request_data.pop("timeout", None)
        server_type = request.server_type or existing["server_type"]
        if server_type not in VALID_MCP_SERVER_TYPES:
            return get_data_error_result(retmsg="Unsupported MCP server type.")

        server_name = request.name or existing["name"]
        if len(server_name.encode("utf-8")) > 255:
            return get_data_error_result(retmsg=f"Invalid MCP name or length is {len(server_name)} which is large than 255.")
        url = request.url or existing["url"]
        if not url:
            return get_data_error_result(retmsg="Invalid url.")

        headers = safe_json_parse(request.headers if request.headers is not None else existing.get("headers") or {})
        variables = safe_json_parse(request.variables if request.variables is not None else existing.get("variables") or {})
        variables.pop("tools", None)
        timeout = request.timeout

        probe = MCPServer(id=server_name, name=server_name, url=url, server_type=server_type, variables=variables, headers=headers)
        server_tools, error_message = await thread_pool_exec(get_mcp_tools, [probe], timeout)
        if error_message:
            return get_data_error_result(retmsg=error_message)

        tools = server_tools[server_name]
        variables["tools"] = {tool["name"]: tool for tool in tools if isinstance(tool, dict) and "name" in tool}
        request_data.update(
            {
                "id": mcp_id,
                "tenant_id": user.id,
                "name": server_name,
                "url": url,
                "server_type": server_type,
                "variables": variables,
                "headers": headers,
            }
        )

        def _update(session: Session) -> JsonObject | None:
            updated = MCPServerService.filter_update(
                session,
                [MCPServer.id == mcp_id, MCPServer.tenant_id == user.id],
                request_data,
            )
            if not updated:
                return None
            server = MCPServerService.get_by_id(session, mcp_id)
            return server.to_dict() if server is not None else None

        updated_payload = await db.run_sync(_update)  # TODO(async-phase4)
        if updated_payload is None:
            return get_data_error_result(retmsg="Failed to update MCP server.")
        return get_json_result(data=updated_payload)
    except Exception as error:
        return server_error_response(error)


@router.delete("/mcp/servers/{mcp_id}", summary="删除MCP服务器", response_description="成功删除MCP服务器")
async def remove(
    mcp_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    try:

        def _delete(session: Session) -> bool | None:
            server = MCPServerService.get_by_id(session, mcp_id)
            if server is None or server.tenant_id != user.id:
                return None
            return bool(MCPServerService.delete_by_ids(session, [mcp_id]))

        deleted = await db.run_sync(_delete)  # TODO(async-phase4)
        if deleted is None:
            return get_data_error_result(retmsg=f"Cannot find MCP server {mcp_id} for user {user.id}")
        if not deleted:
            return get_data_error_result(retmsg=f"Failed to delete MCP servers {[mcp_id]}")
        return get_json_result(data=True)
    except Exception as error:
        return server_error_response(error)


@router.post("/mcp/servers/import", summary="批量导入MCP服务器", response_description="成功导入MCP服务器")
async def import_multiple(
    request: ImportMCPServersRequest,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    if not request.mcp_servers:
        return get_data_error_result(retmsg="No MCP servers provided.")

    results: list[JsonObject] = []
    try:
        for server_name, config in request.mcp_servers.items():
            if not all(key in config for key in {"type", "url"}):
                results.append({"server": server_name, "success": False, "message": "Missing required fields (type or url)"})
                continue
            if not server_name or len(server_name.encode("utf-8")) > 255:
                results.append(
                    {
                        "server": server_name,
                        "success": False,
                        "message": f"Invalid MCP name or length is {len(server_name)} which is large than 255.",
                    }
                )
                continue

            base_name = server_name
            new_name = base_name
            counter = 0
            while await db.run_sync(  # TODO(async-phase4)
                lambda session, name=new_name: MCPServerService.get_by_name_and_tenant(session, name=name, tenant_id=user.id)[0]
            ):
                new_name = f"{base_name}_{counter}"
                counter += 1

            authorization_token = config.get("authorization_token", "")
            headers = {"authorization_token": authorization_token} if authorization_token else {}
            variables = {key: value for key, value in config.items() if key not in {"type", "url", "headers"}}
            probe = MCPServer(
                id=new_name,
                name=new_name,
                url=str(config["url"]),
                server_type=str(config["type"]),
                variables=variables,
                headers=headers,
            )
            server_tools, error_message = await thread_pool_exec(get_mcp_tools, [probe], request.timeout)
            if error_message:
                results.append({"server": base_name, "success": False, "message": error_message})
                continue

            tools = server_tools[new_name]
            create_data = {
                "id": get_uuid(),
                "tenant_id": user.id,
                "name": new_name,
                "url": config["url"],
                "server_type": config["type"],
                "variables": {
                    "authorization_token": authorization_token,
                    "tools": {tool["name"]: tool for tool in tools if isinstance(tool, dict) and "name" in tool},
                },
            }
            created = await db.run_sync(lambda session, data=create_data: MCPServerService.insert(session, **data))  # TODO(async-phase4)
            if not created:
                results.append({"server": server_name, "success": False, "message": "Failed to create MCP server."})
                continue

            result: JsonObject = {"server": server_name, "success": True, "action": "created", "id": create_data["id"], "new_name": new_name}
            if new_name != base_name:
                result["message"] = f"Renamed from '{base_name}' to '{new_name}' avoid duplication"
            results.append(result)

        return get_json_result(data={"results": results})
    except Exception as error:
        return server_error_response(error)


@router.post("/mcp/servers/{mcp_id}/test", summary="测试MCP连接", response_description="成功测试MCP连接")
async def test_mcp(
    mcp_id: str,
    request: TestMCPServerRequest,
    user: Principal = Depends(async_current_user),
):
    if not request.url:
        return get_data_error_result(retmsg="Invalid MCP url.")
    if request.server_type not in VALID_MCP_SERVER_TYPES:
        return get_data_error_result(retmsg="Unsupported MCP server type.")

    headers = safe_json_parse(request.headers or {})
    variables = safe_json_parse(request.variables or {})
    server = MCPServer(id=mcp_id, server_type=request.server_type, url=request.url, headers=headers, variables=variables)
    session = MCPToolCallSession(server, server.variables)
    try:
        try:
            tools = await thread_pool_exec(session.get_tools, request.timeout)
        except Exception as error:
            return get_data_error_result(retmsg=f"Test MCP error: {error}")
        finally:
            await thread_pool_exec(close_multiple_mcp_toolcall_sessions, [session])

        result = []
        for tool in tools:
            tool_payload = tool.model_dump()
            tool_payload["enabled"] = True
            result.append(tool_payload)
        return get_json_result(data=result)
    except Exception as error:
        return server_error_response(error)


@router.get("/mcp/servers/{mcp_id}/tools", summary="获取MCP工具列表", response_description="成功获取MCP工具列表")
async def list_tools(
    mcp_id: str,
    timeout: float = Query(10.0, gt=0),
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    try:
        payload = await db.run_sync(lambda session: _owned_server_payload(session, mcp_id, user.id))  # TODO(async-phase4)
        if payload is None:
            return get_data_error_result(retmsg=f"Cannot find MCP server {mcp_id} for user {user.id}")

        server = MCPServer(**payload)
        server_tools, error_message = await thread_pool_exec(get_mcp_tools, [server], timeout)
        if error_message:
            return get_data_error_result(retmsg=f"MCP list tools error: {error_message}")
        return get_json_result(data=server_tools[mcp_id])
    except Exception as error:
        return server_error_response(error)


@router.post(
    "/mcp/servers/{mcp_id}/tools/{tool_name}/test",
    summary="测试MCP工具",
    response_description="成功测试MCP工具",
)
async def test_tool(
    mcp_id: str,
    tool_name: str,
    request: TestMCPToolRequest,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    tool_call_session: MCPToolCallSession | None = None
    try:
        payload = await db.run_sync(lambda session: _owned_server_payload(session, mcp_id, user.id))  # TODO(async-phase4)
        if payload is None:
            return get_data_error_result(retmsg=f"Cannot find MCP server {mcp_id} for user {user.id}")

        server = MCPServer(**payload)
        tool_call_session = MCPToolCallSession(server, server.variables)
        result = await thread_pool_exec(tool_call_session.tool_call, tool_name, request.arguments, request.timeout)
        return get_json_result(data=result)
    except Exception as error:
        return server_error_response(error)
    finally:
        if tool_call_session is not None:
            await thread_pool_exec(close_multiple_mcp_toolcall_sessions, [tool_call_session])


@router.put("/mcp/servers/{mcp_id}/tools", summary="更新MCP工具配置", response_description="成功更新MCP工具配置")
async def cache_tools(
    mcp_id: str,
    request: CacheMCPToolsRequest,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    try:

        def _cache(session: Session) -> tuple[bool, bool, dict[str, JsonObject]]:
            payload = _owned_server_payload(session, mcp_id, user.id)
            if payload is None:
                return False, False, {}

            tools = {tool["name"]: tool for tool in request.tools if "name" in tool}
            variables = dict(payload["variables"] or {})
            variables["tools"] = tools
            updated = MCPServerService.filter_update(
                session,
                [MCPServer.id == mcp_id, MCPServer.tenant_id == user.id],
                {"variables": variables},
            )
            return True, bool(updated), tools

        found, updated, tools = await db.run_sync(_cache)  # TODO(async-phase4)
        if not found:
            return get_data_error_result(retmsg=f"Cannot find MCP server {mcp_id} for user {user.id}")
        if not updated:
            return get_data_error_result(retmsg="Failed to update MCP server tools.")
        return get_json_result(data=tools)
    except Exception as error:
        return server_error_response(error)

"""File snapshot HTTP contract shared by Files, Workspaces and Datasets."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.deps import get_storage
from api.db.db_models import get_async_db
from api.db.services.file_commit_service import CommitError, CreateCommit, FileCommitService, build_tree, diff_trees, public_commit
from api.identity.principal import Principal
from api.utils.api_utils import async_current_user


class CommitRoute(APIRoute):
    def get_route_handler(self) -> Any:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            from fastapi.responses import JSONResponse

            try:
                return await original(request)
            except CommitError as exc:
                return JSONResponse({"code": exc.status, "message": str(exc), "data": None}, status_code=exc.status)

        return handle


router = APIRouter(route_class=CommitRoute)
User = Annotated[Principal, Depends(async_current_user)]
DB = Annotated[AsyncSession, Depends(get_async_db)]


def service(db: DB, storage: Annotated[Any, Depends(get_storage)]) -> FileCommitService:
    return FileCommitService(db, storage)


Service = Annotated[FileCommitService, Depends(service)]


def success(data: Any) -> dict[str, Any]:
    return {"code": 0, "message": "success", "data": data}


async def create(request: Request, entity_id: str, body: CreateCommit, user: User, svc: Service) -> dict[str, Any]:
    return success(await svc.create(request.scope["route"].name.split(":")[0], entity_id, user.tenant_id, user.id, body))


async def history(request: Request, entity_id: str, user: User, svc: Service, page: int = Query(1, ge=1), page_size: int = Query(15, ge=1, le=100), desc: bool = True) -> dict[str, Any]:
    folder = await svc.resolve(request.scope["route"].name.split(":")[0], entity_id, user.tenant_id, user.id)
    return success(await svc.list_commits(folder, page, page_size, desc))


async def diff(request: Request, entity_id: str, user: User, svc: Service, from_id: str = Query(alias="from"), to_id: str = Query(alias="to")) -> dict[str, Any]:
    folder = await svc.resolve(request.scope["route"].name.split(":")[0], entity_id, user.tenant_id, user.id)
    before, after = await svc.commit(folder, from_id), await svc.commit(folder, to_id)
    return success(diff_trees(before.tree_state, after.tree_state))


async def changes(request: Request, entity_id: str, user: User, svc: Service) -> dict[str, Any]:
    folder = await svc.resolve(request.scope["route"].name.split(":")[0], entity_id, user.tenant_id, user.id)
    latest = await svc.latest(folder)
    state, _ = await svc.live(folder, await svc.rows(folder))
    return success([row for row in diff_trees(latest.tree_state if latest else {}, state) if row["file_id"] != folder.id])


async def detail(request: Request, entity_id: str, commit_id: str, user: User, svc: Service) -> dict[str, Any]:
    folder = await svc.resolve(request.scope["route"].name.split(":")[0], entity_id, user.tenant_id, user.id)
    commit = await svc.commit(folder, commit_id)
    return success({**public_commit(commit), "files": await svc.items(commit)})


async def files(request: Request, entity_id: str, commit_id: str, user: User, svc: Service) -> dict[str, Any]:
    folder = await svc.resolve(request.scope["route"].name.split(":")[0], entity_id, user.tenant_id, user.id)
    return success(await svc.items(await svc.commit(folder, commit_id)))


async def tree(request: Request, entity_id: str, commit_id: str, user: User, svc: Service) -> dict[str, Any]:
    folder = await svc.resolve(request.scope["route"].name.split(":")[0], entity_id, user.tenant_id, user.id)
    commit = await svc.commit(folder, commit_id)
    return success(build_tree(commit.tree_state, folder.id))


async def content(request: Request, entity_id: str, commit_id: str, file_id: str, user: User, svc: Service) -> dict[str, Any]:
    folder = await svc.resolve(request.scope["route"].name.split(":")[0], entity_id, user.tenant_id, user.id)
    data = await svc.content(await svc.commit(folder, commit_id), file_id)
    return success({"content": data.decode("utf-8", errors="replace")})


@router.get("/files/{file_id}/versions")
async def versions(file_id: str, user: User, svc: Service) -> dict[str, Any]:
    return success(await svc.versions(user.tenant_id, user.id, file_id))


# Static /diff must precede /{commit_id}. No skill or memory resolver exists.
for scope in ("folders", "workspaces", "workspace", "datasets"):
    for suffix, method, endpoint in (
        ("/commits", "POST", create),
        ("/commits", "GET", history),
        ("/commits/diff", "GET", diff),
        ("/changes", "GET", changes),
        ("/commits/{commit_id}", "GET", detail),
        ("/commits/{commit_id}/files", "GET", files),
        ("/commits/{commit_id}/tree", "GET", tree),
        ("/commits/{commit_id}/files/{file_id}/content", "GET", content),
    ):
        router.add_api_route(f"/{scope}/{{entity_id}}{suffix}", endpoint, methods=[method], name=f"{scope}:{endpoint.__name__}")

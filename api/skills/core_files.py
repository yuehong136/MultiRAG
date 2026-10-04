"""Authorized Files integration for Python directory Skills."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import File, PythonSkillCoreSpace
from api.skills.core_runtime import get_core_search
from api.skills.core_service import SkillCoreService
from api.skills.schemas import SkillError
from api.skills.storage import SkillStorage


def service(db: AsyncSession) -> SkillCoreService:
    from common.resources import storage

    return SkillCoreService(db, SkillStorage(storage()), get_core_search())


async def resolve(db: AsyncSession, tenant: str, identity: str | None, *, active: bool = True) -> SkillCoreService | None:
    if not identity:
        return None
    svc = service(db)
    return svc if await svc.file_space(tenant, identity, active=active) else None


async def listing(svc: SkillCoreService, tenant: str, parent: str, args: dict[str, Any]) -> dict[str, Any]:
    space = await svc.file_space(tenant, parent)
    if space is None:
        raise SkillError(404, "NOT_FOUND", "File not found")
    excluded = {identity for plan in space.cleanup.get("files", []) for identity in plan["ids"]}
    excluded.update(space.cleanup.get("uploads", {}).get("ids", []))
    rows = list(await svc.db.scalars(select(File).where(File.tenant_id == tenant, File.parent_id == parent, File.id != parent, File.id.not_in(excluded))))
    if args.get("keywords"):
        rows = [row for row in rows if args["keywords"].lower() in row.name.lower()]
    order = args.get("orderby", "create_time")
    if order not in ("name", "create_time", "update_time", "size", "type"):
        raise SkillError(422, "INVALID_SORT", "Invalid file ordering")
    rows.sort(key=lambda row: (getattr(row, order) or (0 if order in ("create_time", "update_time", "size") else ""), row.id), reverse=args.get("desc", True))
    total = len(rows)
    start = (args["page"] - 1) * args["page_size"]
    result = [row.to_dict() for row in rows[start : start + args["page_size"]]]
    for row in result:
        row["has_child_folder"] = bool(await svc.db.scalar(select(File.id).where(File.tenant_id == tenant, File.parent_id == row["id"], File.type == "folder").limit(1)))
        row["kbs_info"] = []
    folder = await svc.db.get(File, parent)
    return {"files": result, "total": total, "parent_folder": folder.to_dict() if folder else None}


async def delete_files(svc: SkillCoreService, tenant: str, ids: list[str]) -> tuple[bool, dict[str, Any]]:
    from api.apps.services.file_api_service import delete_files_async

    success_count = 0
    errors: list[str] = []
    ordinary: list[str] = []
    owned: list[tuple[str, str, bool]] = []
    for identity in dict.fromkeys(ids):
        space = await svc.file_space(tenant, identity, active=False)
        if space is None:
            rows = list(await svc.db.scalars(select(PythonSkillCoreSpace).where(PythonSkillCoreSpace.tenant_id == tenant, PythonSkillCoreSpace.state != "deleted")))
            space = next((row for row in rows if any(plan["root"] == identity for plan in row.cleanup.get("files", []))), None)
        if space is None:
            ordinary.append(identity)
        else:
            owned.append((identity, space.id, space.folder_id == identity))
    await svc.db.rollback()
    # Preserve the original batch's visited-set and partial-result behavior.
    if ordinary:
        _, result = await delete_files_async(tenant, ordinary)
        success_count += result["success_count"]
        errors.extend(result["errors"])
    completed: set[str] = set()
    for identity, space_id, entire in owned:
        if identity in completed:
            continue
        try:
            await svc.plan_delete(tenant, space_id, identity, entire=entire)
            space = await svc.space(tenant, space_id, active=False)
            planned = {value for plan in space.cleanup.get("files", []) for value in plan["ids"]}
            await svc.clean(tenant, space_id)
            success_count += len(planned - completed)
            completed.update(planned)
        except Exception:
            await svc.db.rollback()
            errors.append(f"Failed to delete file {identity}")
    return not errors, {"success_count": success_count, "errors": errors}

"""Transactional skill assets and durable operation registration.

The relational manifest is authoritative. Storage/index calls never borrow a DB
session; long effects run only after the operation has been committed.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import File, Skill, SkillOperation, SkillSearchConfig, SkillSpace, SkillVersion, SkillVersionFile
from api.skills.file_guard import SKILL_SOURCES
from api.skills.package import SkillPackage
from api.skills.schemas import ActivateVersion, CreateSpace, SkillError, UpdateConfig, UpdateSpace, default_fields, idempotency_key
from api.skills.storage import SkillStorage, zip_files

BACKEND = "python"


def new_id() -> str:
    return uuid.uuid4().hex


def request_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def folder(tenant: str, name: str, parent: str | None, source: str, identity: str | None = None) -> File:
    identity = identity or new_id()
    return File(id=identity, parent_id=parent or identity, tenant_id=tenant, created_by=tenant, name=name, type="folder", source_type=source, location="", size=0)


def public(model: Any) -> dict[str, Any]:
    fields: dict[type[Any], tuple[str, ...]] = {
        SkillSpace: ("id", "name", "description", "backend_owner", "state", "revision", "root_folder_id", "active_generation_id", "create_time", "update_time"),
        Skill: ("id", "space_id", "name", "description", "tags", "active_version_id", "state", "revision", "create_time", "update_time"),
        SkillVersion: ("id", "skill_id", "version", "content_digest", "state", "index_state", "file_count", "total_size", "create_time", "update_time"),
        SkillOperation: ("id", "kind", "state", "phase", "attempts", "resource_id", "progress", "result", "error", "create_time", "update_time"),
        SkillSearchConfig: ("revision", "embedding_model_id", "rerank_model_id", "top_k", "vector_weight", "similarity_threshold", "fields"),
    }
    result = {key: getattr(model, key) for key in fields[type(model)]}
    if isinstance(model, SkillSearchConfig):
        for key in ("embedding_model_id", "rerank_model_id"):
            result[key] = str(result[key]) if result[key] is not None else None
    return result


def accepted(operation: SkillOperation) -> dict[str, Any]:
    return {"operation_id": operation.id, "state": operation.state, "resource_id": operation.resource_id}


class SkillService:
    def __init__(self, db: AsyncSession, storage: SkillStorage, search: Any) -> None:
        self.db = db
        self.storage = storage
        self.search = search

    async def space(self, tenant: str, identity: str, *, write: bool = False, hidden: bool = False) -> SkillSpace:
        query = select(SkillSpace).where(SkillSpace.id == identity, SkillSpace.tenant_id == tenant, SkillSpace.backend_owner == BACKEND)
        if write:
            query = query.with_for_update()
        row = await self.db.scalar(query)
        if row is None or (not hidden and row.state != "active"):
            raise SkillError(404, "NOT_FOUND", "Space not found")
        if write and row.backend_owner != BACKEND:
            raise SkillError(409, "BACKEND_OWNER_MISMATCH", "Space belongs to another backend")
        return row

    async def skill(self, tenant: str, space_id: str, identity: str, *, hidden: bool = False) -> Skill:
        row = await self.db.scalar(select(Skill).where(Skill.id == identity, Skill.space_id == space_id, Skill.tenant_id == tenant))
        if row is None or (not hidden and row.state != "active"):
            raise SkillError(404, "NOT_FOUND", "Skill not found")
        return row

    async def version(self, tenant: str, space_id: str, identity: str, *, hidden: bool = False) -> SkillVersion:
        row = await self.db.scalar(
            select(SkillVersion)
            .join(Skill, Skill.id == SkillVersion.skill_id)
            .where(SkillVersion.id == identity, SkillVersion.tenant_id == tenant, Skill.space_id == space_id, Skill.tenant_id == tenant)
        )
        if row is None or (not hidden and row.state != "installed"):
            raise SkillError(404, "NOT_FOUND", "Version not found")
        skill = await self.skill(tenant, space_id, row.skill_id, hidden=hidden)
        if not hidden and skill.state != "active":
            raise SkillError(404, "NOT_FOUND", "Version not found")
        return row

    async def operation(self, tenant: str, identity: str) -> SkillOperation:
        row = await self.db.scalar(select(SkillOperation).where(SkillOperation.id == identity, SkillOperation.tenant_id == tenant, SkillOperation.backend_owner == BACKEND))
        if row is None:
            raise SkillError(404, "NOT_FOUND", "Operation not found")
        return row

    async def config(self, tenant: str, space_id: str) -> SkillSearchConfig:
        row = await self.db.scalar(select(SkillSearchConfig).where(SkillSearchConfig.space_id == space_id, SkillSearchConfig.tenant_id == tenant))
        if row is None:
            raise SkillError(404, "NOT_FOUND", "Configuration not found")
        return row

    async def create_space(self, tenant: str, request: CreateSpace) -> dict[str, Any]:
        # A transaction-scoped advisory lock also serializes creation of the tenant root.
        await self.db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("skills-root:" + tenant, 0))))
        root = await self.db.scalar(select(File).where(File.tenant_id == tenant, File.parent_id == File.id, or_(File.source_type.is_(None), File.source_type.not_in(SKILL_SOURCES))))
        if root is None:
            root = folder(tenant, "/", None, "")
            self.db.add(root)
        space_folder = folder(tenant, request.name, root.id, "skill_space")
        row = SkillSpace(
            id=new_id(),
            tenant_id=tenant,
            created_by=tenant,
            name=request.name,
            name_key=request.name.casefold(),
            description=request.description,
            root_folder_id=space_folder.id,
            state="active",
            backend_owner=BACKEND,
            revision=1,
        )
        self.db.add_all([space_folder, row])
        await self.db.flush()
        self.db.add(
            SkillSearchConfig(
                id=new_id(),
                tenant_id=tenant,
                space_id=row.id,
                embedding_model_id=None,
                rerank_model_id=None,
                top_k=10,
                vector_weight=0.3,
                similarity_threshold=0.2,
                fields=default_fields(),
                revision=1,
            )
        )
        await self.db.commit()
        return public(row)

    async def list_spaces(self, tenant: str, page: int, page_size: int, keywords: str) -> dict[str, Any]:
        where = [SkillSpace.tenant_id == tenant, SkillSpace.state == "active", SkillSpace.backend_owner == BACKEND]
        if keywords:
            where.append(SkillSpace.name.icontains(keywords, autoescape=True))
        total = await self.db.scalar(select(func.count()).select_from(SkillSpace).where(*where))
        rows = await self.db.scalars(select(SkillSpace).where(*where).order_by(SkillSpace.create_time.desc(), SkillSpace.id).offset((page - 1) * page_size).limit(page_size))
        return {"spaces": [public(row) for row in rows], "total": total, "page": page, "page_size": page_size}

    async def update_space(self, tenant: str, identity: str, request: UpdateSpace) -> dict[str, Any]:
        row = await self.space(tenant, identity, write=True)
        if row.revision != request.revision:
            raise SkillError(409, "REVISION_CONFLICT", "Space has changed")
        if "name" in request.model_fields_set:
            if request.name is None:
                raise SkillError(422, "INVALID_NAME", "Space name cannot be null")
            row.name = request.name
            row.name_key = request.name.casefold()
            file = await self.db.get(File, row.root_folder_id)
            if file is None or file.tenant_id != tenant:
                raise SkillError(409, "ASSET_INTEGRITY", "Space folder binding is invalid")
            file.name = row.name
        if "description" in request.model_fields_set:
            if request.description is None:
                raise SkillError(422, "INVALID_DESCRIPTION", "Description cannot be null")
            row.description = request.description
        row.revision += 1
        await self.db.commit()
        return public(row)

    async def list_skills(self, tenant: str, space_id: str, page: int, page_size: int, keywords: str, sort: str, descending: bool) -> dict[str, Any]:
        await self.space(tenant, space_id)
        where = [Skill.tenant_id == tenant, Skill.space_id == space_id, Skill.state == "active"]
        if keywords:
            where.append(Skill.name.icontains(keywords, autoescape=True))
        total = await self.db.scalar(select(func.count()).select_from(Skill).where(*where))
        order = Skill.name if sort == "name" else Skill.create_time
        rows = await self.db.scalars(select(Skill).where(*where).order_by(order.desc() if descending else order, Skill.id).offset((page - 1) * page_size).limit(page_size))
        return {"skills": [public(row) for row in rows], "total": total, "page": page, "page_size": page_size}

    async def get_skill(self, tenant: str, space_id: str, identity: str) -> dict[str, Any]:
        await self.space(tenant, space_id)
        row = await self.skill(tenant, space_id, identity)
        versions = await self.db.scalars(
            select(SkillVersion)
            .where(SkillVersion.skill_id == row.id, SkillVersion.tenant_id == tenant, SkillVersion.state.in_(["installed", "staging", "install_failed"]))
            .order_by(SkillVersion.create_time.desc(), SkillVersion.id)
        )
        return {"skill": public(row), "versions": [public(version) for version in versions]}

    async def replay(self, tenant: str, kind: str, key: str, body: dict[str, Any]) -> SkillOperation | None:
        # Both backends serialize the same key before looking up its primary or alias binding.
        await self.db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"), {"scope": f"skills-request:{tenant}:{kind}:{key}"})
        row = await self.db.scalar(
            select(SkillOperation).where(
                SkillOperation.tenant_id == tenant,
                SkillOperation.kind == kind,
                or_(SkillOperation.idempotency_key == key, SkillOperation.payload["idempotency_aliases"].op("?")(key)),
            )
        )
        if row is not None:
            if row.backend_owner != BACKEND:
                raise SkillError(409, "BACKEND_OWNER_MISMATCH", "Operation belongs to another backend")
            bound_hash = row.request_hash if row.idempotency_key == key else row.payload["idempotency_aliases"][key]
            if bound_hash != request_hash(body):
                raise SkillError(409, "IDEMPOTENCY_CONFLICT", "Idempotency key was used for a different request")
        return row

    def new_operation(self, tenant: str, kind: str, key: str, body: dict[str, Any], space_id: str | None, resource_id: str | None, *, phase: str = "sealed") -> SkillOperation:
        row = SkillOperation(
            id=new_id(),
            tenant_id=tenant,
            space_id=space_id,
            resource_id=resource_id,
            backend_owner=BACKEND,
            kind=kind,
            state="pending",
            phase=phase,
            idempotency_key=key,
            request_hash=request_hash(body),
            payload=body,
            progress={"completed": 0, "total": 1},
            result={"items": []},
            attempts=0,
            revision=1,
        )
        self.db.add(row)
        return row

    async def install(self, tenant: str, space_id: str, package: SkillPackage, activate: bool, key: str | None) -> dict[str, Any]:
        key = idempotency_key(key)
        body = {"space_id": space_id, "name": package.name, "version": package.version, "content_digest": package.content_digest, "activate": activate}
        previous = await self.replay(tenant, "install", key, body)
        if previous:
            return accepted(previous)
        space = await self.space(tenant, space_id, write=True)
        previous = await self.replay(tenant, "install", key, body)
        if previous:
            return accepted(previous)
        skill = await self.db.scalar(select(Skill).where(Skill.space_id == space_id, Skill.name == package.name, Skill.deleted_at.is_(None)))
        if skill is not None and skill.state != "active":
            raise SkillError(409, "RESOURCE_BUSY", "Skill is being deleted")
        if skill is None:
            skill_folder = folder(tenant, package.name, space.root_folder_id, "skill")
            skill = Skill(
                id=new_id(), tenant_id=tenant, space_id=space_id, folder_id=skill_folder.id, name=package.name, description=package.description, tags=package.tags, state="active", revision=1
            )
            self.db.add_all([skill_folder, skill])
            await self.db.flush()
        existing = await self.db.scalar(select(SkillVersion).where(SkillVersion.skill_id == skill.id, SkillVersion.version == package.version))
        if existing:
            if existing.content_digest != package.content_digest or existing.state == "deleted":
                raise SkillError(409, "VERSION_CONFLICT", "Version is immutable and cannot be replaced")
            original = await self.db.scalar(
                select(SkillOperation).where(SkillOperation.resource_id == existing.id, SkillOperation.kind == "install", SkillOperation.tenant_id == tenant).order_by(SkillOperation.create_time)
            )
            if original:
                original_id = original.id
                # Workers lock operation then space. Release our space lock before
                # locking the original operation to merge aliases without lost updates.
                await self.db.rollback()
                rebound = await self.replay(tenant, "install", key, body)
                if rebound:
                    return accepted(rebound)
                original = await self.db.scalar(select(SkillOperation).where(SkillOperation.id == original_id).with_for_update().execution_options(populate_existing=True))
                if original is None:
                    raise SkillError(409, "VERSION_CONFLICT", "Version operation is unavailable")
                if original.backend_owner != BACKEND:
                    raise SkillError(409, "BACKEND_OWNER_MISMATCH", "Operation belongs to another backend")
                if original.payload.get("activate") != activate:
                    raise SkillError(409, "VERSION_ALREADY_INSTALLED", "Version already exists; use the active-version endpoint to change activation")
                aliases = {**original.payload.get("idempotency_aliases", {}), key: request_hash(body)}
                original.payload = {**original.payload, "idempotency_aliases": aliases}
                await self.db.commit()
                return accepted(original)
            raise SkillError(409, "VERSION_CONFLICT", "Version already exists")
        version_folder = folder(tenant, package.version, skill.folder_id, "skill_version")
        version = SkillVersion(
            id=new_id(),
            tenant_id=tenant,
            skill_id=skill.id,
            folder_id=version_folder.id,
            version=package.version,
            content_digest=package.content_digest,
            manifest=package.manifest(),
            source_kind="local",
            state="staging",
            index_state="unindexed",
            file_count=len(package.files),
            total_size=sum(len(file.data) for file in package.files),
        )
        self.db.add_all([version_folder, version])
        await self.db.flush()
        operation = self.new_operation(tenant, "install", key, body, space_id, version.id, phase="staging")
        operation.payload = {
            **body,
            "version_id": version.id,
            "skill_id": skill.id,
            "skipped_binary_count": package.skipped_binary_count,
            "description": package.description,
            "tags": package.tags,
            "objects": [],
        }
        operation.progress = {"completed": 0, "total": len(package.files)}
        # Persist all intended object addresses before any external write.
        directories: dict[str, str] = {"": version_folder.id}
        objects: list[dict[str, Any]] = []
        for item in package.files:
            parts = item.path.split("/")
            for depth in range(1, len(parts)):
                path = "/".join(parts[:depth])
                if path not in directories:
                    parent = directories["/".join(parts[: depth - 1])]
                    directory = folder(tenant, parts[depth - 1], parent, "skill_version")
                    self.db.add(directory)
                    directories[path] = directory.id
            parent = directories["/".join(parts[:-1])]
            file_id = new_id()
            location = f"{operation.id}/{file_id}"
            self.db.add(File(id=file_id, parent_id=parent, tenant_id=tenant, created_by=tenant, name=parts[-1], location=location, size=len(item.data), type="other", source_type="skill_file"))
            self.db.add(
                SkillVersionFile(
                    id=new_id(), tenant_id=tenant, version_id=version.id, file_id=file_id, relative_path=item.path, content_digest=item.sha256, size=len(item.data), media_type=item.media_type
                )
            )
            objects.append({"path": item.path, "bucket": parent, "key": location, "file_id": file_id, "sha256": item.sha256, "size": len(item.data)})
        operation.payload = {**operation.payload, "objects": objects}
        space.revision += 1
        await self.db.commit()
        try:
            for count, (item, address) in enumerate(zip(package.files, objects, strict=True), 1):
                await self.storage.put(address["bucket"], address["key"], item)
                await self.db.refresh(operation)
                if operation.state != "pending" or operation.phase != "staging":
                    raise SkillError(409, "OPERATION_CHANGED", "Upload operation is no longer staging")
                operation.progress = {"completed": count, "total": len(objects)}
                await self.db.commit()
            # Taking the space lock prevents sealing a package into a deleted space.
            await self.space(tenant, space_id, write=True)
            await self.db.refresh(version)
            if version.state != "staging":
                raise SkillError(409, "OPERATION_CHANGED", "Version is no longer staging")
            operation.phase = "sealed"
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            await self.db.refresh(operation)
            await self.db.refresh(version)
            operation.state = "failed"
            operation.error = {"error_code": "UPLOAD_FAILED", "message": "Package staging did not complete", "retryable": False}
            if version.state == "staging":
                version.state = "install_failed"
            await self.db.commit()
            # Bytes remain durably addressed for explicit deletion/recovery; never report ready.
        return accepted(operation)

    async def register_action(self, tenant: str, space_id: str, kind: str, key: str | None, resource_id: str | None = None, request: ActivateVersion | None = None) -> dict[str, Any]:
        key = idempotency_key(key)
        body: dict[str, Any] = {"space_id": space_id, "resource_id": resource_id}
        if request is not None:
            body.update(request.model_dump())
        previous = await self.replay(tenant, kind, key, body)
        if previous:
            return accepted(previous)
        space = await self.space(tenant, space_id, write=True)
        if kind == "activate":
            assert request is not None and resource_id is not None
            skill = await self.skill(tenant, space_id, resource_id)
            if skill.revision != request.revision:
                raise SkillError(409, "REVISION_CONFLICT", "Skill has changed")
            if request.version_id is not None:
                version = await self.version(tenant, space_id, request.version_id)
                if version.skill_id != skill.id:
                    raise SkillError(404, "NOT_FOUND", "Version not found")
        elif kind == "delete_version":
            assert resource_id is not None
            version = await self.version(tenant, space_id, resource_id, hidden=True)
            skill = await self.skill(tenant, space_id, version.skill_id)
            if skill.active_version_id == version.id:
                raise SkillError(409, "ACTIVE_VERSION", "Deactivate or switch the active version first")
            if version.state != "deleted":
                version.state = "deleting"
        elif kind == "delete_skill":
            assert resource_id is not None
            skill = await self.skill(tenant, space_id, resource_id, hidden=True)
            if skill.state != "deleted":
                skill.state = "deleting"
        elif kind == "delete_space":
            space.state = "deleting"
        elif kind == "reindex":
            await self.search.validate_models(tenant, public(await self.config(tenant, space_id)))
        else:
            raise SkillError(400, "INVALID_OPERATION", "Unsupported operation")
        space.revision += 1
        operation = self.new_operation(tenant, kind, key, body, space_id, resource_id or space_id)
        await self.db.commit()
        return accepted(operation)

    async def register_batch(self, tenant: str, space_id: str | None, ids: list[str], key: str | None) -> dict[str, Any]:
        key = idempotency_key(key)
        kind = "delete_skills" if space_id else "delete_spaces"
        ids = sorted(set(ids))
        body = {"space_id": space_id, "ids": ids}
        previous = await self.replay(tenant, kind, key, body)
        if previous:
            return accepted(previous)
        items: list[dict[str, Any]] = []
        targets: list[str] = []
        if space_id:
            space = await self.space(tenant, space_id, write=True)
        for identity in ids:
            try:
                if space_id:
                    skill = await self.skill(tenant, space_id, identity, hidden=True)
                    if skill.state != "deleted":
                        skill.state = "deleting"
                else:
                    space = await self.space(tenant, identity, write=True, hidden=True)
                    if space.state != "deleted":
                        space.state = "deleting"
                space.revision += 1
                targets.append(identity)
            except SkillError as exc:
                items.append({"id": identity, "state": "failed", "error_code": exc.code, "retryable": False})
        operation = self.new_operation(tenant, kind, key, body, space_id, None)
        operation.payload = {**body, "targets": targets}
        operation.result = {"items": items}
        operation.progress = {"completed": 0, "total": len(ids)}
        await self.db.commit()
        return accepted(operation)

    async def update_config(self, tenant: str, space_id: str, request: UpdateConfig) -> dict[str, Any]:
        space = await self.space(tenant, space_id, write=True)
        config = await self.config(tenant, space_id)
        if config.revision != request.revision:
            raise SkillError(409, "REVISION_CONFLICT", "Configuration has changed")
        values = {**public(config), **request.model_dump(exclude_unset=True)}
        await self.search.validate_models(tenant, values)
        for key, value in request.model_dump(exclude_unset=True).items():
            if key != "revision":
                setattr(config, key, int(value) if key.endswith("model_id") and value is not None else value)
        config.revision += 1
        space.revision += 1
        await self.db.commit()
        return {"config": public(config), "requires_reindex": True}

    async def files(self, tenant: str, space_id: str, version_id: str) -> list[dict[str, Any]]:
        await self.space(tenant, space_id)
        await self.version(tenant, space_id, version_id)
        rows = await self.db.scalars(select(SkillVersionFile).where(SkillVersionFile.tenant_id == tenant, SkillVersionFile.version_id == version_id).order_by(SkillVersionFile.relative_path))
        return [{"path": row.relative_path, "size": row.size, "sha256": row.content_digest, "media_type": row.media_type} for row in rows]

    async def content(self, tenant: str, space_id: str, version_id: str, path: str | None = None) -> tuple[bytes, str]:
        await self.space(tenant, space_id)
        version = await self.version(tenant, space_id, version_id)
        query = (
            select(SkillVersionFile, File)
            .join(File, File.id == SkillVersionFile.file_id)
            .where(SkillVersionFile.tenant_id == tenant, SkillVersionFile.version_id == version_id, File.tenant_id == tenant)
        )
        if path is not None:
            query = query.where(SkillVersionFile.relative_path == path)
        rows = (await self.db.execute(query.order_by(SkillVersionFile.relative_path))).all()
        addresses = [(entry.relative_path, file.parent_id, file.location, entry.content_digest, entry.size) for entry, file in rows]
        if not addresses or (path is None and len(addresses) != version.file_count):
            raise SkillError(404, "NOT_FOUND", "Version content not found")
        await self.db.rollback()
        contents = [(name, await self.storage.get(bucket, key, digest, size)) for name, bucket, key, digest, size in addresses]
        result = (contents[0][1], "application/octet-stream") if path is not None else (zip_files(contents), "application/zip")
        # Linearize visibility after slow object IO and archive construction.
        await self.space(tenant, space_id)
        await self.version(tenant, space_id, version_id)
        return result

    async def retry(self, tenant: str, identity: str, key: str | None = None) -> dict[str, Any]:
        operation = await self.db.scalar(select(SkillOperation).where(SkillOperation.id == identity, SkillOperation.tenant_id == tenant).with_for_update())
        if operation is None:
            raise SkillError(404, "NOT_FOUND", "Operation not found")
        if key is not None and key in operation.payload.get("retry_keys", []):
            return accepted(operation)
        if operation.backend_owner != BACKEND:
            raise SkillError(409, "BACKEND_OWNER_MISMATCH", "Operation belongs to another backend")
        if operation.state not in {"failed", "partial"}:
            return accepted(operation)
        if operation.phase == "staging":
            raise SkillError(409, "INCOMPLETE_UPLOAD", "An incomplete upload must be removed and uploaded as a new version")
        operation.state = "pending"
        if key is not None:
            operation.payload = {**operation.payload, "retry_keys": [*operation.payload.get("retry_keys", []), key]}
        operation.error = None
        operation.next_attempt_at = datetime.now(UTC)
        operation.lease_owner = None
        operation.lease_expires_at = None
        operation.revision += 1
        await self.db.commit()
        return accepted(operation)

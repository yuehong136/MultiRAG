"""Database-leased operations; restart-safe metadata and external effect boundaries."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.db.db_models import File, Skill, SkillIndexGeneration, SkillOperation, SkillSpace, SkillVersion, SkillVersionFile
from api.skills.schemas import SkillError
from api.skills.search_types import SkillSearchError
from api.skills.service import BACKEND, SkillService, new_id, public
from api.skills.storage import SkillStorage

logger = logging.getLogger(__name__)
LEASE_SECONDS = 90


@dataclass(frozen=True)
class Lease:
    id: str
    owner: str
    revision: int


class SkillWorker:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], storage: SkillStorage, search: Any, cleanup: Any) -> None:
        self.sessions = sessions
        self.storage = storage
        self.search = search
        self.cleanup = cleanup
        self.owner = "python:" + uuid.uuid4().hex
        self.stop_event = asyncio.Event()

    async def claim(self) -> Lease | None:
        async with self.sessions() as db:
            operation = await db.scalar(
                select(SkillOperation)
                .where(
                    SkillOperation.backend_owner == BACKEND,
                    SkillOperation.phase != "staging",
                    or_(
                        and_(SkillOperation.state == "pending", or_(SkillOperation.next_attempt_at.is_(None), SkillOperation.next_attempt_at <= func.now())),
                        and_(SkillOperation.state == "running", SkillOperation.lease_expires_at <= func.now()),
                    ),
                )
                .order_by(SkillOperation.create_time, SkillOperation.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if operation is None:
                return None
            now = await db.scalar(select(func.now()))
            operation.state = "running"
            operation.attempts += 1
            operation.revision += 1
            operation.lease_owner = self.owner
            operation.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            lease = Lease(operation.id, self.owner, operation.revision)
            await db.commit()
            return lease

    async def locked(self, db: AsyncSession, lease: Lease) -> SkillOperation:
        row = await db.scalar(
            select(SkillOperation)
            .where(
                SkillOperation.id == lease.id,
                SkillOperation.state == "running",
                SkillOperation.lease_owner == lease.owner,
                SkillOperation.revision == lease.revision,
                SkillOperation.lease_expires_at > func.now(),
            )
            .with_for_update()
        )
        if row is None:
            raise SkillError(409, "LEASE_LOST", "Operation lease is no longer valid")
        return row

    async def heartbeat(self, lease: Lease) -> None:
        while True:
            await asyncio.sleep(LEASE_SECONDS / 3)
            async with self.sessions() as db:
                row = await self.locked(db, lease)
                now = await db.scalar(select(func.now()))
                row.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
                await db.commit()

    async def run_once(self) -> bool:
        lease = await self.claim()
        if lease is None:
            return False
        heartbeat = asyncio.create_task(self.heartbeat(lease))
        work = asyncio.create_task(self.process(lease))
        try:
            done, _ = await asyncio.wait({heartbeat, work}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                await heartbeat
            await work
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Errors from adapters carry a safe public message; unknown exceptions never do.
            code = exc.code if isinstance(exc, SkillError) else exc.error_code if isinstance(exc, SkillSearchError) else "OPERATION_FAILED"
            message = exc.message if isinstance(exc, SkillError | SkillSearchError) else "Skill operation failed"
            if code != "LEASE_LOST":
                await self.fail(lease, code, message)
            logger.warning("Skill operation %s failed (%s)", lease.id, code)
        finally:
            heartbeat.cancel()
            work.cancel()
            await asyncio.gather(heartbeat, work, return_exceptions=True)
        return True

    async def fail(self, lease: Lease, code: str, message: str) -> None:
        async with self.sessions() as db:
            try:
                operation = await self.locked(db, lease)
            except SkillError:
                return
            operation.state = "failed"
            operation.error = {"error_code": code, "message": message, "retryable": True}
            operation.lease_owner = None
            operation.lease_expires_at = None
            if operation.kind == "install" and not operation.payload.get("publication_done"):
                version = await db.get(SkillVersion, operation.resource_id)
                if version is not None and version.state in {"staging", "installed"}:
                    version.index_state = "failed"
            await db.commit()

    async def process(self, lease: Lease) -> None:
        async with self.sessions() as db:
            operation = await self.locked(db, lease)
            kind, tenant, payload, resource = operation.kind, operation.tenant_id, dict(operation.payload), operation.resource_id
        if kind == "install":
            await self.install(lease, tenant, payload)
        elif kind in {"activate", "reindex"}:
            await self.publish(
                lease,
                tenant,
                payload["space_id"],
                payload.get("resource_id") if kind == "activate" else None,
                payload.get("version_id"),
                activation=kind == "activate",
                expected_skill_revision=payload.get("revision"),
            )
            await self.finish(lease, {"items": [{"id": resource, "state": "succeeded", "retryable": False}]})
        elif kind.startswith("delete_"):
            await self.remove(lease, tenant, kind, payload, resource)
        else:
            raise SkillError(400, "INVALID_OPERATION", "Unsupported operation")

    async def finish(self, lease: Lease, result: dict[str, Any], state: str = "succeeded") -> None:
        async with self.sessions() as db:
            operation = await self.locked(db, lease)
            operation.state = state
            operation.phase = "done"
            operation.result = result
            operation.progress = {"completed": sum(item["state"] == "succeeded" for item in result.get("items", [])), "total": len(result.get("items", []))}
            operation.error = None
            operation.lease_owner = None
            operation.lease_expires_at = None
            await db.commit()

    async def install(self, lease: Lease, tenant: str, payload: dict[str, Any]) -> None:
        # Verify every persisted object before changing staging to installed.
        for obj in payload["objects"]:
            await self.storage.get(obj["bucket"], obj["key"], obj["sha256"], obj["size"])
        async with self.sessions() as db:
            await self.locked(db, lease)
            service = SkillService(db, self.storage, self.search)
            space = await service.space(tenant, payload["space_id"], write=True)
            version = await service.version(tenant, space.id, payload["version_id"], hidden=True)
            if version.state not in {"staging", "installed", "install_failed"}:
                raise SkillError(409, "RESOURCE_BUSY", "Version is no longer installable")
            version.state = "installed"
            await db.commit()
        if payload["activate"]:
            await self.publish(lease, tenant, payload["space_id"], payload["skill_id"], payload["version_id"], activation=True)
        async with self.sessions() as db:
            version = await db.get(SkillVersion, payload["version_id"])
            index_state = version.index_state
        await self.finish(
            lease,
            {
                "items": [{"id": payload["version_id"], "state": "succeeded", "retryable": False}],
                "skill_id": payload["skill_id"],
                "version_id": payload["version_id"],
                "index_state": index_state,
                "skipped_binary_count": payload["skipped_binary_count"],
            },
        )

    async def publish(self, lease: Lease, tenant: str, space_id: str, skill_id: str | None, version_id: str | None, *, activation: bool, expected_skill_revision: int | None = None) -> None:
        from api.skills.search_types import SkillIndexDocument

        async with self.sessions() as db:
            operation = await self.locked(db, lease)
            service = SkillService(db, self.storage, self.search)
            space = await service.space(tenant, space_id, write=True)
            if operation.payload.get("publication_done"):
                await db.rollback()
                await self.cleanup_generations(lease, tenant, space_id)
                return
            config = public(await service.config(tenant, space_id))
            skill = await service.skill(tenant, space_id, skill_id) if skill_id else None
            if skill and expected_skill_revision is not None and skill.revision != expected_skill_revision:
                raise SkillError(409, "REVISION_CONFLICT", "Skill has changed")
            if activation and version_id is not None:
                version = await service.version(tenant, space_id, version_id)
                if skill is None or version.skill_id != skill.id:
                    raise SkillError(404, "NOT_FOUND", "Version not found")
            if config["embedding_model_id"] is None:
                if not activation:
                    raise SkillError(503, "MODEL_NOT_CONFIGURED", "Configure an embedding model before reindexing")
                assert skill is not None
                skill.active_version_id = version_id
                skill.revision += 1
                space.revision += 1
                # Existing generations no longer describe this explicit publication.
                if space.active_generation_id:
                    old = await db.get(SkillIndexGeneration, space.active_generation_id)
                    if old:
                        old.state = "retired"
                    space.active_generation_id = None
                if version_id:
                    version = await db.get(SkillVersion, version_id)
                    version.index_state = "unindexed"
                    skill.description, skill.tags = await self.version_metadata(version, db)
                operation.payload = {**operation.payload, "publication_done": True}
                operation.phase = "cleaning"
                await db.commit()
                await self.cleanup_generations(lease, tenant, space_id)
                return
            source_revision = space.revision
            rows = list(await db.scalars(select(Skill).where(Skill.tenant_id == tenant, Skill.space_id == space_id, Skill.state == "active")))
            snapshot: list[dict[str, Any]] = []
            for entry in rows:
                selected = version_id if activation and entry.id == skill_id else entry.active_version_id
                if selected is None:
                    continue
                version = await service.version(tenant, space_id, selected)
                bindings = (
                    await db.execute(
                        select(SkillVersionFile, File)
                        .join(File, File.id == SkillVersionFile.file_id)
                        .where(SkillVersionFile.version_id == version.id, SkillVersionFile.tenant_id == tenant, File.tenant_id == tenant)
                        .order_by(SkillVersionFile.relative_path)
                    )
                ).all()
                actual_manifest = {binding.relative_path: {"path": binding.relative_path, "sha256": binding.content_digest, "size": binding.size} for binding, _ in bindings}
                expected_manifest = {item["path"]: item for item in version.manifest["files"]}
                if actual_manifest != expected_manifest:
                    raise SkillError(503, "CONTENT_INTEGRITY", "Version manifest bindings do not match")
                description, tags = await self.version_metadata(version, db)
                snapshot.append(
                    {
                        "skill_id": entry.id,
                        "version_id": version.id,
                        "name": entry.name,
                        "description": description,
                        "tags": tags,
                        "version": version.version,
                        "content_digest": version.content_digest,
                        "addresses": [(binding.relative_path, file.parent_id, file.location, binding.content_digest, binding.size) for binding, file in bindings],
                    }
                )
            generation_id = new_id()
            index_name = "skill_" + generation_id
            generation = SkillIndexGeneration(
                id=generation_id,
                tenant_id=tenant,
                space_id=space_id,
                config_revision=config["revision"],
                source_revision=source_revision,
                config=config,
                dimension=0,
                index_name=index_name,
                state="building",
            )
            db.add(generation)
            operation.phase = "indexing"
            operation.payload = {**operation.payload, "generations": [*operation.payload.get("generations", []), {"id": generation_id, "lease_revision": lease.revision}]}
            await db.commit()
        documents: list[Any] = []
        for entry in snapshot:
            parts: list[str] = []
            for path, bucket, key, digest, size in entry.pop("addresses"):
                content = await self.storage.get(bucket, key, digest, size)
                try:
                    decoded = content.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                parts.append(f"\n=== {path} ===\n{decoded}")
            documents.append(SkillIndexDocument(**entry, content="".join(parts)))
        try:
            dimension = await self.search.build(index_name, tenant, config, documents)
            async with self.sessions() as db:
                operation = await self.locked(db, lease)
                service = SkillService(db, self.storage, self.search)
                space = await service.space(tenant, space_id, write=True)
                if space.revision != source_revision:
                    raise SkillError(409, "SOURCE_CHANGED", "Assets changed while the index was building; retry the operation")
                generation = await db.get(SkillIndexGeneration, generation_id)
                generation.dimension = dimension
                generation.state = "active"
                previous = space.active_generation_id
                space.active_generation_id = generation_id
                if activation:
                    skill = await service.skill(tenant, space_id, skill_id)
                    skill.active_version_id = version_id
                    skill.revision += 1
                    if version_id:
                        active_version = await db.get(SkillVersion, version_id)
                        # Expose metadata belonging to the selected immutable version.
                        skill.description, skill.tags = await self.version_metadata(active_version, db)
                space.revision += 1
                for document in documents:
                    version = await db.get(SkillVersion, document.version_id)
                    version.index_state = "ready"
                if previous and previous != generation_id:
                    old = await db.get(SkillIndexGeneration, previous)
                    if old:
                        old.state = "retired"
                operation.payload = {**operation.payload, "publication_done": True}
                operation.phase = "cleaning"
                await db.commit()
        except Exception:
            async with self.sessions() as db:
                generation = await db.get(SkillIndexGeneration, generation_id)
                if generation is not None and generation.state == "building":
                    generation.state = "failed"
                    generation.error = {"error_code": "INDEX_BUILD_FAILED"}
                    await db.commit()
            raise
        await self.cleanup_generations(lease, tenant, space_id)

    async def version_metadata(self, version: SkillVersion, db: AsyncSession) -> tuple[str, list[str]]:
        # Metadata was validated before staging; retain it in the immutable install payload.
        operation = await db.scalar(select(SkillOperation).where(SkillOperation.resource_id == version.id, SkillOperation.kind == "install").order_by(SkillOperation.create_time))
        if operation is None or "description" not in operation.payload:
            raise SkillError(503, "CONTENT_INTEGRITY", "Version metadata is unavailable")
        return str(operation.payload["description"]), list(operation.payload.get("tags", []))

    async def cleanup_generations(self, lease: Lease, tenant: str, space_id: str) -> None:
        async with self.sessions() as db:
            operation = await self.locked(db, lease)
            fenced = [item["id"] for item in operation.payload.get("generations", []) if item["lease_revision"] < lease.revision]
            space = await db.get(SkillSpace, space_id)
            rows = await db.scalars(
                select(SkillIndexGeneration).where(
                    SkillIndexGeneration.tenant_id == tenant,
                    SkillIndexGeneration.space_id == space_id,
                    or_(SkillIndexGeneration.state.in_(["retired", "failed", "cleanup_failed"]), SkillIndexGeneration.id.in_(fenced)),
                )
            )
            targets = [(row.id, row.index_name) for row in rows if row.id != space.active_generation_id]
        for identity, index_name in targets:
            try:
                await self.search.delete(index_name)
                async with self.sessions() as db:
                    await self.locked(db, lease)
                    generation = await db.get(SkillIndexGeneration, identity)
                    generation.state = "deleted"
                    generation.error = None
                    await db.commit()
            except Exception as exc:
                async with self.sessions() as db:
                    await self.locked(db, lease)
                    generation = await db.get(SkillIndexGeneration, identity)
                    generation.state = "cleanup_failed"
                    generation.error = {"error_code": "INDEX_CLEANUP_FAILED"}
                    await db.commit()
                raise SkillError(503, "INDEX_CLEANUP_FAILED", "Old index cleanup failed; retry is required") from exc

    async def remove(self, lease: Lease, tenant: str, kind: str, payload: dict[str, Any], resource_id: str | None) -> None:
        is_batch = kind in {"delete_spaces", "delete_skills"}
        targets = payload.get("targets", []) if is_batch else [resource_id]
        async with self.sessions() as db:
            operation = await self.locked(db, lease)
            operation.phase = "cleaning"
            prior = list(operation.result.get("items", []))
            await db.commit()
        completed = {item["id"] for item in prior if item["state"] == "succeeded" or not item.get("retryable", False)}
        results = {item["id"]: item for item in prior}
        for identity in targets:
            if identity in completed:
                continue
            unit_kind = "delete_space" if kind == "delete_spaces" else "delete_skill" if kind == "delete_skills" else kind
            try:
                await self.remove_one(lease, tenant, unit_kind, payload.get("space_id"), identity)
                results[identity] = {"id": identity, "state": "succeeded", "retryable": False}
            except Exception as exc:
                code = exc.code if isinstance(exc, SkillError) else exc.error_code if isinstance(exc, SkillSearchError) else "DELETE_FAILED"
                if code == "LEASE_LOST":
                    raise
                results[identity] = {"id": identity, "state": "failed", "error_code": code, "retryable": True}
                async with self.sessions() as db:
                    await self.locked(db, lease)
                    model = {"delete_space": SkillSpace, "delete_skill": Skill, "delete_version": SkillVersion}[unit_kind]
                    asset = await db.get(model, identity)
                    if asset is not None and asset.state != "deleted":
                        asset.state = "delete_failed"
                    await db.commit()
            async with self.sessions() as db:
                operation = await self.locked(db, lease)
                operation.result = {"items": list(results.values())}
                operation.progress = {"completed": sum(item["state"] == "succeeded" for item in results.values()), "total": len(payload.get("ids", targets))}
                await db.commit()
        items = list(results.values())
        failed = sum(item["state"] != "succeeded" for item in items)
        state = "succeeded" if failed == 0 else "failed" if failed == len(items) else "partial"
        await self.finish(lease, {"items": items}, state)

    async def remove_one(self, lease: Lease, tenant: str, kind: str, space_id: str | None, identity: str) -> None:
        async with self.sessions() as db:
            await self.locked(db, lease)
            service = SkillService(db, self.storage, self.search)
            space = await service.space(tenant, identity if kind == "delete_space" else space_id, write=True, hidden=True)
            if kind == "delete_space":
                asset = space
                skills = list(await db.scalars(select(Skill).where(Skill.space_id == space.id, Skill.tenant_id == tenant, Skill.state != "deleted")))
                versions = list(
                    await db.scalars(select(SkillVersion).where(SkillVersion.skill_id.in_([skill.id for skill in skills]), SkillVersion.tenant_id == tenant, SkillVersion.state != "deleted"))
                )
                root = space.root_folder_id
            elif kind == "delete_skill":
                asset = await service.skill(tenant, space.id, identity, hidden=True)
                skills = [asset]
                versions = list(await db.scalars(select(SkillVersion).where(SkillVersion.skill_id == identity, SkillVersion.tenant_id == tenant, SkillVersion.state != "deleted")))
                root = asset.folder_id
            else:
                asset = await service.version(tenant, space.id, identity, hidden=True)
                skills = []
                versions = [asset]
                root = asset.folder_id
            if asset.state == "deleted":
                return
            version_ids = [version.id for version in versions]
            for row in [asset, *skills, *versions]:
                row.state = "deleting"
            for skill in skills:
                skill.active_version_id = None
                skill.revision += 1
            generations = list(
                await db.scalars(select(SkillIndexGeneration).where(SkillIndexGeneration.space_id == space.id, SkillIndexGeneration.tenant_id == tenant, SkillIndexGeneration.state != "deleted"))
            )
            index_targets = [(row.id, row.index_name) for row in generations]
            if kind == "delete_space":
                space.active_generation_id = None
            operation = await self.locked(db, lease)
            cleanup_records = dict(operation.payload.get("cleanup_records", {}))
            record = cleanup_records.get(identity)
            if record is None:
                tree = select(File.id).where(File.id == root, File.tenant_id == tenant).cte("skill_cleanup_tree", recursive=True)
                tree = tree.union(select(File.id).join(tree, File.parent_id == tree.c.id).where(File.tenant_id == tenant))
                file_ids = list(await db.scalars(select(tree.c.id)))
                installs = await db.scalars(select(SkillOperation).where(SkillOperation.kind == "install", SkillOperation.resource_id.in_(version_ids), SkillOperation.tenant_id == tenant))
                addresses = [obj for install in installs for obj in install.payload.get("objects", [])]
                record = {"root": root, "file_ids": file_ids, "objects": addresses, "version_ids": version_ids}
                cleanup_records[identity] = record
                operation.payload = {**operation.payload, "cleanup_records": cleanup_records}
            await db.commit()
        # Never delete files before every affected generation has been cleaned.
        for generation_id, name in index_targets:
            await self.search.delete(name, None if kind == "delete_space" else version_ids)
            if kind == "delete_space":
                async with self.sessions() as db:
                    await self.locked(db, lease)
                    generation = await db.get(SkillIndexGeneration, generation_id)
                    generation.state = "deleted"
                    await db.commit()
        async with self.sessions() as db:
            await self.locked(db, lease)
            root_exists = await db.scalar(select(File.id).where(File.id == root, File.tenant_id == tenant))
            # Capture immutable object addresses before legacy cleanup removes SQL rows.
            addresses = [(obj["bucket"], obj["key"]) for obj in record["objects"]]
        if root_exists:
            await self.cleanup(tenant, [root])
        else:
            async with self.sessions() as db:
                await self.locked(db, lease)
                remaining_ids = list(await db.scalars(select(File.id).where(File.id.in_(record["file_ids"]), File.tenant_id == tenant)))
            if remaining_ids:
                await self.cleanup(tenant, remaining_ids)
        # Independent SQL and object readback; do not parse legacy errors strings.
        async with self.sessions() as db:
            await self.locked(db, lease)
            bindings = list(await db.scalars(select(SkillVersionFile).where(SkillVersionFile.version_id.in_(version_ids), SkillVersionFile.tenant_id == tenant)))
            remaining = await db.scalar(select(func.count()).select_from(File).where(or_(File.id == root, File.id.in_(record["file_ids"]), File.id.in_([binding.file_id for binding in bindings]))))
            if remaining:
                raise SkillError(503, "FILE_CLEANUP_FAILED", "Skill file records still exist")
        for bucket, key in addresses:
            # rm is idempotent and its adapter verifies object absence.
            await self.storage.remove(bucket, key)
        async with self.sessions() as db:
            await self.locked(db, lease)
            now = await db.scalar(select(func.now()))
            await db.execute(delete(SkillVersionFile).where(SkillVersionFile.version_id.in_(version_ids), SkillVersionFile.tenant_id == tenant))
            await db.execute(update(SkillVersion).where(SkillVersion.id.in_(version_ids), SkillVersion.tenant_id == tenant, SkillVersion.state != "deleted").values(state="deleted", deleted_at=now))
            if kind == "delete_space":
                await db.execute(update(Skill).where(Skill.space_id == space.id, Skill.tenant_id == tenant, Skill.state != "deleted").values(state="deleted", deleted_at=now, active_version_id=None))
                await db.execute(update(SkillSpace).where(SkillSpace.id == identity, SkillSpace.tenant_id == tenant).values(state="deleted", deleted_at=now, active_generation_id=None))
            elif kind == "delete_skill":
                await db.execute(update(Skill).where(Skill.id == identity, Skill.tenant_id == tenant).values(state="deleted", deleted_at=now, active_version_id=None))
            await db.commit()

    async def recover_staging(self) -> None:
        # Staging is never executable. A bounded idle timeout records abandoned packages.
        async with self.sessions() as db:
            cutoff = (await db.scalar(select(func.now()))) - timedelta(minutes=30)
            rows = list(
                await db.scalars(
                    select(SkillOperation)
                    .where(
                        SkillOperation.backend_owner == BACKEND,
                        SkillOperation.phase == "staging",
                        or_(and_(SkillOperation.state == "pending", SkillOperation.update_date < cutoff.replace(tzinfo=None)), SkillOperation.state == "failed"),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for operation in rows:
                if operation.payload.get("cleanup_operation_id"):
                    continue
                operation.state = "failed"
                operation.error = {"error_code": "INCOMPLETE_UPLOAD", "message": "Upload stopped before sealing", "retryable": False}
                version = await db.get(SkillVersion, operation.resource_id)
                if version is not None and version.state == "staging":
                    version.state = "deleting"
                service = SkillService(db, self.storage, self.search)
                cleanup = service.new_operation(
                    operation.tenant_id,
                    "delete_version",
                    "abandoned:" + operation.id,
                    {"space_id": operation.space_id, "resource_id": operation.resource_id},
                    operation.space_id,
                    operation.resource_id,
                )
                operation.payload = {**operation.payload, "cleanup_operation_id": cleanup.id}
            await db.commit()

    async def sweep_orphan_generations(self) -> None:
        """Keep deleting fenced output, including collections recreated by late threads.

        A cancelled to_thread may finish after lease loss. Its immutable generation
        is permanently in the sweep set; it can never overwrite an active collection.
        """
        async with self.sessions() as db:
            now = await db.scalar(select(func.now()))
            operations = list(await db.scalars(select(SkillOperation).where(SkillOperation.backend_owner == BACKEND)))
            live: set[str] = set()
            known: set[str] = set()
            for operation in operations:
                for generation in operation.payload.get("generations", []):
                    known.add(generation["id"])
                    if operation.state == "running" and operation.lease_expires_at is not None and operation.lease_expires_at > now and generation["lease_revision"] == operation.revision:
                        live.add(generation["id"])
            rows = (
                await db.execute(
                    select(SkillIndexGeneration, SkillSpace)
                    .join(SkillSpace, SkillSpace.id == SkillIndexGeneration.space_id)
                    .where(SkillSpace.backend_owner == BACKEND, SkillIndexGeneration.id.in_(known - live))
                )
            ).all()
            targets = [(generation.id, generation.index_name) for generation, space in rows if space.active_generation_id != generation.id]
        for identity, index_name in targets:
            try:
                await self.search.delete(index_name)
                async with self.sessions() as db:
                    generation = await db.get(SkillIndexGeneration, identity)
                    if generation is not None and generation.state != "active":
                        generation.state = "deleted"
                        generation.error = None
                        await db.commit()
            except Exception:
                async with self.sessions() as db:
                    generation = await db.get(SkillIndexGeneration, identity)
                    if generation is not None and generation.state != "active":
                        generation.state = "cleanup_failed"
                        generation.error = {"error_code": "INDEX_CLEANUP_FAILED"}
                        await db.commit()

    async def run(self) -> None:
        sweep_after = 0.0
        while not self.stop_event.is_set():
            try:
                if asyncio.get_running_loop().time() >= sweep_after:
                    await self.sweep_orphan_generations()
                    sweep_after = asyncio.get_running_loop().time() + 30
                await self.recover_staging()
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Skill worker iteration failed; durable operations remain recoverable")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=2)
                except TimeoutError:
                    pass

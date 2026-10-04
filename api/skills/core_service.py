"""Python-native directory Skills core; no Go table or task contract dependency."""

import asyncio
import hashlib
import re
import time
from typing import Any

import yaml
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import File, File2Document, PythonSkillCoreConfig, PythonSkillCoreSpace, TenantLLM
from api.skills.core_schemas import CoreConfigRequest, CoreCreate, CoreIndexRequest, CoreSearchRequest, CoreUpdate, field_defaults
from api.skills.file_guard import SKILL_SOURCES
from api.skills.package import MAX_FILE_SIZE, MAX_FILES, MAX_TOTAL_SIZE, PackageFile, normalize_path
from api.skills.schemas import SkillError
from api.skills.search_types import SkillIndexDocument
from api.skills.service import folder, new_id
from api.skills.storage import SkillStorage

CORE_SOURCE = "python_skill_space_core"


def defaults() -> dict[str, Any]:
    return {"embd_id": "", "rerank_id": "", "top_k": 10, "vector_similarity_weight": 0.3, "similarity_threshold": 0.2, "field_config": field_defaults(), "index_version": "1.0.0"}


class SkillCoreService:
    def __init__(self, db: AsyncSession, storage: SkillStorage, search: Any) -> None:
        self.db, self.storage, self.search = db, storage, search

    async def ensure_isolated(self) -> None:
        table = "usr_ai.t_ai_go_skill_spaces"
        if await self.db.scalar(select(func.to_regclass(table))) is not None:
            if await self.db.scalar(text("SELECT EXISTS (SELECT 1 FROM usr_ai.t_ai_go_skill_spaces)")):
                raise SkillError(503, "BACKEND_DATABASE_NOT_ISOLATED", "Python core requires a database isolated from Go core data")

    async def space(self, tenant: str, identity: str, *, active: bool = True, lock: bool = False) -> PythonSkillCoreSpace:
        if not identity.strip():
            raise SkillError(400, "SPACE_REQUIRED", "An explicit space_id is required")
        query = select(PythonSkillCoreSpace).execution_options(populate_existing=True).where(PythonSkillCoreSpace.tenant_id == tenant, PythonSkillCoreSpace.id == identity)
        if lock:
            if active:
                await self.ensure_isolated()
            query = query.with_for_update()
        row = await self.db.scalar(query)
        if row is None or row.state == "deleted":
            raise SkillError(404, "NOT_FOUND", "Skill space not found")
        if active and row.state != "active":
            raise SkillError(409, "SPACE_DELETING", "Skill space is being deleted")
        return row

    async def config_row(self, tenant: str, identity: str) -> PythonSkillCoreConfig:
        row = await self.db.scalar(select(PythonSkillCoreConfig).execution_options(populate_existing=True).where(PythonSkillCoreConfig.tenant_id == tenant, PythonSkillCoreConfig.space_id == identity))
        if row is None:
            raise SkillError(404, "NOT_FOUND", "Skill configuration not found")
        return row

    async def model_reference(self, tenant: str, reference: str, kind: str) -> str:
        if not reference:
            return ""
        query = select(TenantLLM).where(TenantLLM.tenant_id == tenant, TenantLLM.mdl_type == kind, TenantLLM.status == "1")
        if reference.isascii() and reference.isdecimal():
            if not 0 < int(reference) < 2**63:
                raise SkillError(422, "MODEL_NOT_FOUND", "Model reference is invalid")
            query = query.where(TenantLLM.id == int(reference))
        elif "@" in reference:
            name, provider = reference.rsplit("@", 1)
            query = query.where(TenantLLM.llm_name == name, TenantLLM.llm_factory == provider)
        else:
            raise SkillError(422, "MODEL_REFERENCE_REQUIRED", "Use an exact model ID or full model@provider reference")
        rows = list(await self.db.scalars(query.limit(2)))
        if len(rows) != 1:
            raise SkillError(422, "MODEL_AMBIGUOUS" if rows else "MODEL_NOT_FOUND", "Model reference must identify one enabled tenant model")
        return str(rows[0].id)

    async def space_dto(self, row: PythonSkillCoreSpace) -> dict[str, Any]:
        config = await self.config_row(row.tenant_id, row.id)
        result = {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "name": row.name,
            "folder_id": row.folder_id,
            "description": row.description,
            "top_k": config.settings["top_k"],
            "status": "deleting" if row.state == "delete_failed" else row.state,
            "create_time": row.create_time,
            "update_time": row.update_date.strftime("%Y-%m-%d %H:%M:%S") if row.update_date else "",
        }
        result.update({key: config.settings[key] for key in ("embd_id", "rerank_id") if config.settings.get(key)})
        if row.state == "delete_failed":
            result["delete_error"] = {"error_code": "DELETE_FAILED", "retryable": True}
        return result

    async def create(self, tenant: str, request: CoreCreate) -> dict[str, Any]:
        await self.ensure_isolated()
        settings = defaults()
        settings["embd_id"] = await self.model_reference(tenant, request.embd_id, "embedding")
        settings["rerank_id"] = await self.model_reference(tenant, request.rerank_id, "rerank")
        await self.db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("python-skill-core-root:" + tenant, 0))))
        root = await self.db.scalar(select(File).where(File.tenant_id == tenant, File.parent_id == File.id, or_(File.source_type.is_(None), File.source_type.not_in((*SKILL_SOURCES, CORE_SOURCE)))))
        if root is None:
            root = folder(tenant, "/", None, "")
            self.db.add(root)
        tree = folder(tenant, request.name, root.id, CORE_SOURCE)
        row = PythonSkillCoreSpace(id=new_id(), tenant_id=tenant, name=request.name, folder_id=tree.id, description=request.description, state="active", revision=1, cleanup={})
        self.db.add_all([tree, row])
        await self.db.flush()
        self.db.add(PythonSkillCoreConfig(id=new_id(), tenant_id=tenant, space_id=row.id, settings=settings, index_data={}))
        await self.db.commit()
        return await self.space_dto(row)

    async def update(self, tenant: str, identity: str, request: CoreUpdate) -> dict[str, Any]:
        row = await self.space(tenant, identity, lock=True)
        config = await self.config_row(tenant, identity)
        values = dict(config.settings)
        for key, kind in (("embd_id", "embedding"), ("rerank_id", "rerank")):
            value = getattr(request, key)
            if value is not None:
                values[key] = await self.model_reference(tenant, value, kind)
        if request.name:
            row.name = request.name
            tree = await self.db.get(File, row.folder_id)
            if tree is not None:
                tree.name = request.name
        if request.description is not None:
            row.description = request.description
        if request.top_k is not None:
            values["top_k"] = request.top_k
        config.settings = values
        row.revision += 1
        await self.db.commit()
        return await self.space_dto(row)

    async def get_config(self, tenant: str, identity: str) -> dict[str, Any]:
        await self.space(tenant, identity)
        row = await self.config_row(tenant, identity)
        return {
            "id": row.id,
            "tenant_id": tenant,
            "space_id": identity,
            **row.settings,
            "status": "1",
            "create_time": row.create_time,
            "update_time": row.update_date.strftime("%Y-%m-%d %H:%M:%S") if row.update_date else "",
        }

    async def update_config(self, tenant: str, request: CoreConfigRequest) -> dict[str, Any]:
        row = await self.space(tenant, request.space_id, lock=True)
        config = await self.config_row(tenant, row.id)
        fields = request.field_config
        if set(fields) != {"name", "description", "tags", "content"}:
            raise SkillError(422, "INVALID_SEARCH_CONFIG", "Four field weights are required")
        for value in fields.values():
            if not isinstance(value, dict) or type(value.get("enabled")) is not bool or type(value.get("weight")) not in (int, float) or not 0 <= value["weight"] <= 10:
                raise SkillError(422, "INVALID_SEARCH_CONFIG", "Field weights are invalid")
        if not any(value["enabled"] and value["weight"] > 0 for value in fields.values()):
            raise SkillError(422, "INVALID_SEARCH_CONFIG", "At least one positive field weight is required")
        values = request.model_dump(exclude={"space_id"})
        values["embd_id"] = await self.model_reference(tenant, request.embd_id, "embedding")
        values["rerank_id"] = await self.model_reference(tenant, request.rerank_id, "rerank")
        config.settings = {**values, "index_version": config.settings.get("index_version", "1.0.0")}
        row.revision += 1
        await self.db.commit()
        return await self.get_config(tenant, row.id)

    async def read_skill(self, tenant: str, space_id: str, identity: str, version: str = "") -> dict[str, Any]:
        space = await self.space(tenant, space_id)
        skill = await self.db.scalar(select(File).where(File.id == identity, File.tenant_id == tenant, File.parent_id == space.folder_id, File.type == "folder"))
        if skill is None:
            raise SkillError(404, "NOT_FOUND", "Skill folder is not in this space")
        children = list(await self.db.scalars(select(File).where(File.tenant_id == tenant, File.parent_id == skill.id, File.id != skill.id)))
        if version:
            folders = [entry for entry in children if entry.type == "folder" and entry.name == version]
            if len(folders) != 1:
                raise SkillError(404, "NOT_FOUND", "Version folder not found or ambiguous")
            selected = folders[0]
        else:
            numeric = [entry for entry in children if entry.type == "folder" and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", entry.name)]
            if numeric:
                selected = max(numeric, key=lambda entry: (tuple(int(part) for part in entry.name.split(".")), entry.id))
                version = selected.name
            elif any(entry.name == "SKILL.md" and entry.type != "folder" for entry in children):
                selected = skill
                version = "1.0.0"
            else:
                raise SkillError(422, "SKILL_CONTENT_MISSING", "Skill requires a version directory or legacy SKILL.md")
        entries: list[tuple[str, str, str, int]] = []
        queue: list[tuple[str, str]] = [("", selected.id)]
        seen: set[str] = set()
        while queue:
            prefix, parent = queue.pop()
            if parent in seen:
                raise SkillError(422, "INVALID_PATH", "Directory cycle detected")
            seen.add(parent)
            for entry in await self.db.scalars(select(File).where(File.tenant_id == tenant, File.parent_id == parent, File.id != parent)):
                path = normalize_path(prefix + entry.name)
                if entry.type == "folder":
                    queue.append((path + "/", entry.id))
                else:
                    entries.append((path, entry.parent_id, entry.location or "", entry.size))
            if len(entries) > MAX_FILES or len(seen) > MAX_FILES:
                raise SkillError(413, "PACKAGE_TOO_LARGE", "Skill directory exceeds limits")
        result = {"skill_id": skill.name, "folder_id": skill.id, "version_id": selected.id, "version": version, "create_time": skill.create_time or 0}
        if len({entry[0] for entry in entries}) != len(entries):
            raise SkillError(422, "INVALID_PATH", "Duplicate file paths")
        await self.db.rollback()
        metadata: dict[str, Any] | None = None
        parts: list[str] = []
        digest = hashlib.sha256()
        total = 0
        for path, bucket, location, size in sorted(entries):
            if not location or size > MAX_FILE_SIZE:
                raise SkillError(422, "INVALID_FILE", "Skill file is unavailable or too large")
            self.storage.require_supported()
            data = await asyncio.to_thread(self.storage.storage.get_bytes, bucket, location)
            if data is None or len(data) != size:
                raise SkillError(503, "CONTENT_INTEGRITY", "Skill file readback failed")
            total += len(data)
            if total > MAX_TOTAL_SIZE:
                raise SkillError(413, "PACKAGE_TOO_LARGE", "Skill directory exceeds limits")
            digest.update(path.encode() + b"\0" + hashlib.sha256(data).digest())
            try:
                value = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            parts.append(f"\n=== {path} ===\n{value}")
            if path == "SKILL.md":
                lines = value.splitlines()
                try:
                    if lines[0] != "---":
                        raise ValueError
                    metadata = yaml.safe_load("\n".join(lines[1 : lines.index("---", 1)]))
                except (ValueError, IndexError, yaml.YAMLError) as exc:
                    raise SkillError(422, "INVALID_SKILL", "SKILL.md frontmatter is invalid") from exc
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not isinstance(metadata.get("description"), str):
            raise SkillError(422, "INVALID_SKILL", "SKILL.md requires name and description")
        tags = metadata.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise SkillError(422, "INVALID_SKILL", "Skill tags must be strings")
        return {**result, "name": metadata["name"], "description": metadata["description"], "tags": tags, "content": "".join(parts), "content_digest": digest.hexdigest()}

    @staticmethod
    def search_config(settings: dict[str, Any]) -> dict[str, Any]:
        return {
            "embedding_model_id": settings.get("embd_id") or None,
            "rerank_model_id": None,
            "fields": settings["field_config"],
            "vector_weight": settings["vector_similarity_weight"],
            "similarity_threshold": settings["similarity_threshold"],
            "top_k": settings["top_k"],
        }

    async def index(self, tenant: str, request: CoreIndexRequest, *, rebuild: bool = False) -> dict[str, Any]:
        space = await self.space(tenant, request.space_id, lock=True)
        config = await self.config_row(tenant, space.id)
        settings = dict(config.settings)
        if request.embd_id:
            settings["embd_id"] = await self.model_reference(tenant, request.embd_id, "embedding")
        if not settings.get("embd_id"):
            raise SkillError(422, "EMBEDDING_REQUIRED", "Configure an embedding model before indexing")
        state = dict(config.index_data)
        if space.cleanup.get("files") or space.cleanup.get("uploads"):
            raise SkillError(409, "CLEANUP_PENDING", "File cleanup must finish before indexing")
        revision, space_id, root = space.revision, space.id, space.folder_id
        selections = {entry["folder_id"]: entry.get("version", "") for entry in state.get("skills", [])}
        if rebuild:
            selections = dict.fromkeys(await self.db.scalars(select(File.id).where(File.tenant_id == tenant, File.parent_id == root, File.type == "folder", File.id != root)), "")
        else:
            for skill in request.skills:
                identity = skill.folder_id or skill.id
                if not identity:
                    raise SkillError(422, "INVALID_SKILL", "Supply one unambiguous skill folder ID")
                selections[identity] = skill.version
        for identity in selections:
            authorized = await self.db.scalar(select(File.id).where(File.id == identity, File.tenant_id == tenant, File.parent_id == root, File.type == "folder"))
            if authorized is None:
                raise SkillError(404, "NOT_FOUND", "Skill folder is not in this space")
        index_name = "skill_pycore_" + new_id()
        # Record the external effect before issuing it. Expired candidates remain
        # reclaimable after cancellation, including a late synchronous I/O return.
        state["retired"] = [*state.get("retired", []), {"name": index_name, "after": time.time() + 600}]
        config.index_data = state
        await self.db.commit()
        documents = [await self.read_skill(tenant, space_id, identity, version) for identity, version in selections.items()]
        await self.search.build(
            index_name,
            tenant,
            self.search_config(settings),
            [SkillIndexDocument(**{key: doc[key] for key in ("version_id", "name", "description", "tags", "version", "content", "content_digest")}, skill_id=doc["folder_id"]) for doc in documents],
        )
        space = await self.space(tenant, space_id, lock=True)
        config = await self.config_row(tenant, space_id)
        if space.revision != revision or time.time() >= next(item["after"] for item in config.index_data["retired"] if item["name"] == index_name):
            raise SkillError(409, "CONCURRENT_CHANGE", "Directory or configuration changed during indexing; retry")
        current = dict(config.index_data)
        retired = [item for item in current.get("retired", []) if item["name"] != index_name]
        if current.get("name"):
            retired.append({"name": current["name"], "after": 0})
        version_parts = settings.get("index_version", "1.0.0").split(".")
        if rebuild:
            settings["index_version"] = ".".join([*version_parts[:2], str(int(version_parts[2]) + 1)])
        config.settings = settings
        config.index_data = {
            "name": index_name,
            "settings": settings,
            "skills": [{key: value for key, value in doc.items() if key not in ("content", "content_digest")} for doc in documents],
            "retired": retired,
        }
        space.revision += 1
        await self.db.commit()
        result = {"indexed_count": len(documents)}
        if rebuild:
            result.update(total_skills=len(documents), failed_count=0, version=settings["index_version"])
        return result

    async def search_skills(self, tenant: str, request: CoreSearchRequest) -> dict[str, Any]:
        space = await self.space(tenant, request.space_id)
        config = await self.config_row(tenant, space.id)
        state = dict(config.index_data)
        revision = space.revision
        settings = dict(config.settings)
        if request.query.strip() and state.get("name") and settings.get("embd_id") and settings["vector_similarity_weight"] > 0 and state.get("settings", {}).get("embd_id") != settings.get("embd_id"):
            raise SkillError(409, "INDEX_REBUILD_REQUIRED", "Embedding model changed; rebuild the space index")
        docs = list(state.get("skills", []))
        await self.db.rollback()
        mode = (
            "keyword" if settings["vector_similarity_weight"] == 0 or not settings.get("embd_id") or not request.query.strip() else "vector" if settings["vector_similarity_weight"] == 1 else "hybrid"
        )
        scores: dict[str, float] | None = None
        if request.query.strip() and state.get("name"):
            hits = await self.search.query(state["name"], tenant, self.search_config(settings), request.query, mode, settings["top_k"])
            scores = {hit.version_id: hit.score for hit in hits}
        # Linearization after external I/O: a concurrent delete cannot return a
        # stale hit, even when the physical index removal is still retrying.
        space = await self.space(tenant, request.space_id)
        if space.revision != revision:
            raise SkillError(409, "CONCURRENT_CHANGE", "Directory changed during search; retry")
        visible = set(await self.db.scalars(select(File.id).where(File.tenant_id == tenant, File.id.in_([doc["version_id"] for doc in docs]))))
        result = [
            {**{key: value for key, value in doc.items() if key != "version_id"}, "score": scores[doc["version_id"]] if scores is not None else 0, "index_version": settings["index_version"]}
            for doc in docs
            if doc["version_id"] in visible and (scores is None or doc["version_id"] in scores)
        ]
        if request.query.strip() and not state.get("name"):
            result = []
        if scores is not None and request.sort_by in ("", "relevance", "score"):
            if request.sort_order not in ("", "asc", "desc"):
                raise SkillError(422, "INVALID_SORT", "Invalid sort direction")
            direction = 1 if request.sort_order == "asc" else -1
            result.sort(key=lambda entry: (direction * entry["score"], entry["skill_id"]))
        else:
            key = request.sort_by or "name"
            if key not in ("name", "create_time", "version") or request.sort_order not in ("", "asc", "desc"):
                raise SkillError(422, "INVALID_SORT", "Invalid sort field or direction")
            result.sort(key=lambda entry: (entry.get(key, ""), entry["skill_id"]), reverse=request.sort_order == "desc")
        start = (request.page - 1) * request.page_size
        return {"skills": result[start : start + request.page_size], "total": len(result), "query": request.query, "search_type": mode}

    async def remove_index(self, tenant: str, space_id: str, skill_id: str) -> None:
        space = await self.space(tenant, space_id, lock=True)
        config = await self.config_row(tenant, space_id)
        state = dict(config.index_data)
        matches = [doc for doc in state.get("skills", []) if (doc["skill_id"] == skill_id or doc["folder_id"] == skill_id)]
        state["skills"] = [doc for doc in state.get("skills", []) if (doc["skill_id"] != skill_id and doc["folder_id"] != skill_id)]
        cleanup = dict(space.cleanup)
        if cleanup.get("lease_until", 0) > time.time():
            raise SkillError(409, "CLEANUP_RUNNING", "Cleanup is running; retry")
        effects = list(cleanup.get("indices", []))
        if state.get("name") and matches:
            effects.append({"name": state["name"], "versions": [doc["version_id"] for doc in matches]})
        cleanup["indices"] = effects
        cleanup["not_before"] = time.time() + 2
        space.cleanup, config.index_data = cleanup, state
        space.revision += 1
        await self.db.commit()
        await self.clean(tenant, space_id)

    async def file_space(self, tenant: str, identity: str, *, active: bool = True) -> PythonSkillCoreSpace | None:
        tree = select(File.id, File.parent_id).where(File.id == identity, File.tenant_id == tenant).cte("core_ancestors", recursive=True)
        tree = tree.union(select(File.id, File.parent_id).join(tree, File.id == tree.c.parent_id).where(File.tenant_id == tenant))
        row = await self.db.scalar(
            select(PythonSkillCoreSpace).execution_options(populate_existing=True).where(PythonSkillCoreSpace.tenant_id == tenant, PythonSkillCoreSpace.folder_id.in_(select(tree.c.id)))
        )
        if row is not None and active and row.state != "active":
            raise SkillError(404, "NOT_FOUND", "File not found")
        if row is not None and active:
            if identity in row.cleanup.get("uploads", {}).get("ids", []):
                raise SkillError(404, "NOT_FOUND", "File not found")
            blocked = [item for item in row.cleanup.get("files", []) if identity in item["ids"]]
            if blocked:
                raise SkillError(404, "NOT_FOUND", "File not found")
        return row

    async def descendants(self, tenant: str, identity: str) -> list[File]:
        tree = select(File.id).where(File.id == identity, File.tenant_id == tenant).cte("core_descendants", recursive=True)
        tree = tree.union(select(File.id).join(tree, File.parent_id == tree.c.id).where(File.tenant_id == tenant))
        return list(await self.db.scalars(select(File).where(File.id.in_(select(tree.c.id)), File.tenant_id == tenant)))

    async def plan_delete(self, tenant: str, space_id: str, identity: str, *, entire: bool = False) -> None:
        space = await self.space(tenant, space_id, active=False, lock=True)
        config = await self.config_row(tenant, space_id)
        cleanup = dict(space.cleanup)
        if cleanup.get("lease_until", 0) > time.time() or cleanup.get("uploads"):
            raise SkillError(409, "CLEANUP_RUNNING", "Upload or cleanup is running; retry")
        plans = list(cleanup.get("files", []))
        if not any(plan["root"] == identity for plan in plans):
            rows = await self.descendants(tenant, identity)
            if not rows:
                raise SkillError(404, "NOT_FOUND", "File not found")
            if await self.db.scalar(select(File2Document.id).where(File2Document.file_id.in_([row.id for row in rows])).limit(1)):
                raise SkillError(409, "LINKED_DOCUMENT", "Skill files linked to datasets must be unlinked first")
            plans.append({"root": identity, "ids": [row.id for row in rows], "objects": [[row.parent_id, row.location] for row in rows if row.type != "folder" and row.location]})
        cleanup["files"] = plans
        state = dict(config.index_data)
        removed_ids = {value for plan in plans for value in plan["ids"]}
        # Any attachment change invalidates the containing indexed version.
        affected = []
        for doc in state.get("skills", []):
            descendants = await self.descendants(tenant, doc["version_id"])
            if entire or doc["folder_id"] in removed_ids or any(row.id in removed_ids for row in descendants):
                affected.append(doc["version_id"])
        indices = list(cleanup.get("indices", []))
        if state.get("name"):
            if entire:
                indices.append({"name": state["name"], "versions": None})
            elif affected:
                indices.append({"name": state["name"], "versions": affected})
        if entire:
            indices.extend({"name": item["name"], "versions": None} for item in state.get("retired", []))
            space.state = "deleting"
        cleanup["indices"] = indices
        cleanup["not_before"] = time.time() + 2
        space.cleanup = cleanup
        config.index_data = {**state, "skills": [doc for doc in state.get("skills", []) if doc["version_id"] not in affected]}
        space.revision += 1
        await self.db.commit()

    async def clean(self, tenant: str, space_id: str) -> None:
        space = await self.space(tenant, space_id, active=False, lock=True)
        cleanup = dict(space.cleanup)
        now = time.time()
        if cleanup.get("lease_until", 0) > now:
            raise SkillError(409, "CLEANUP_RUNNING", "Cleanup is running; retry")
        token = new_id()
        cleanup.update(lease_until=now + 300, lease_token=token)
        space.cleanup = cleanup
        entire = space.state in ("deleting", "delete_failed")
        await self.db.commit()
        try:
            for item in cleanup.get("indices", []):
                await self.search.delete(item["name"], item["versions"])
            for plan in cleanup.get("files", []):
                for bucket, location in plan["objects"]:
                    await self.storage.remove(bucket, location)
            space = await self.space(tenant, space_id, active=False, lock=True)
            if space.cleanup.get("lease_token") != token:
                raise SkillError(409, "LEASE_LOST", "Cleanup lease changed")
            for plan in cleanup.get("files", []):
                await self.db.execute(delete(File).where(File.tenant_id == tenant, File.id.in_(plan["ids"])))
                if await self.db.scalar(select(File.id).where(File.tenant_id == tenant, File.id.in_(plan["ids"])).limit(1)):
                    raise SkillError(503, "DELETE_FAILED", "File records remain")
            space.cleanup = {"retired_objects": cleanup["retired_objects"]} if cleanup.get("retired_objects") else {}
            if entire:
                space.state = "deleted"
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            space = await self.space(tenant, space_id, active=False, lock=True)
            if space.cleanup.get("lease_token") == token:
                space.cleanup = {**space.cleanup, "lease_until": 0, "error": "DELETE_FAILED"}
                if entire:
                    space.state = "delete_failed"
                await self.db.commit()
            raise

    async def create_folder(self, tenant: str, parent_id: str, name: str) -> dict[str, Any]:
        if normalize_path(name) != name or "/" in name:
            raise SkillError(422, "INVALID_PATH", "Folder name must be one path segment")
        space = await self.file_space(tenant, parent_id)
        if space is None:
            raise SkillError(404, "NOT_FOUND", "Skill folder not found")
        space = await self.space(tenant, space.id, lock=True)
        if space.cleanup.get("files") or space.cleanup.get("uploads"):
            raise SkillError(409, "CLEANUP_PENDING", "File cleanup is pending")
        parent = await self.db.get(File, parent_id)
        if parent is None or parent.type != "folder":
            raise SkillError(422, "INVALID_PARENT", "Parent must be a folder")
        if await self.db.scalar(select(File.id).where(File.tenant_id == tenant, File.parent_id == parent_id, File.name == name)):
            raise SkillError(409, "RESOURCE_CONFLICT", "Folder already exists")
        row = folder(tenant, name, parent_id, CORE_SOURCE)
        self.db.add(row)
        space.revision += 1
        await self.db.commit()
        return row.to_dict()

    async def upload(self, tenant: str, parent_id: str, contents: list[tuple[bytes, str]]) -> list[dict[str, Any]]:
        if not contents or len(contents) > MAX_FILES or sum(len(data) for data, _ in contents) > MAX_TOTAL_SIZE:
            raise SkillError(413, "PACKAGE_TOO_LARGE", "Upload exceeds limits")
        paths = [normalize_path(name) for _, name in contents]
        if len(set(paths)) != len(paths) or any(len(data) > MAX_FILE_SIZE for data, _ in contents):
            raise SkillError(422, "INVALID_UPLOAD", "Duplicate paths or oversized file")
        space = await self.file_space(tenant, parent_id)
        if space is None:
            raise SkillError(404, "NOT_FOUND", "Skill folder not found")
        space_id = space.id
        space = await self.space(tenant, space_id, lock=True)
        parent_row = await self.db.get(File, parent_id)
        if parent_row is None or parent_row.type != "folder":
            raise SkillError(422, "INVALID_PARENT", "Parent must be a folder")
        if space.cleanup.get("files") or space.cleanup.get("uploads"):
            raise SkillError(409, "CLEANUP_PENDING", "File cleanup is pending")
        created: list[File] = []
        staged_dirs: list[str] = []
        writes: list[tuple[str, str, bytes]] = []
        for (data, _), path in zip(contents, paths, strict=True):
            parent = parent_id
            parts = path.split("/")
            for name in parts[:-1]:
                existing = await self.db.scalar(select(File).where(File.tenant_id == tenant, File.parent_id == parent, File.name == name))
                if existing is None:
                    existing = folder(tenant, name, parent, CORE_SOURCE)
                    self.db.add(existing)
                    await self.db.flush()
                    staged_dirs.append(existing.id)
                if existing.type != "folder":
                    raise SkillError(409, "RESOURCE_CONFLICT", "Upload path conflicts with a file")
                parent = existing.id
            if await self.db.scalar(select(File.id).where(File.tenant_id == tenant, File.parent_id == parent, File.name == parts[-1])):
                raise SkillError(409, "RESOURCE_CONFLICT", "File already exists")
            identity = new_id()
            row = File(id=identity, tenant_id=tenant, parent_id=parent, name=parts[-1], location="python-skills/" + identity, type="other", size=len(data), source_type=CORE_SOURCE, created_by=tenant)
            self.db.add(row)
            created.append(row)
            writes.append((parent, row.location or "", data))
        space.cleanup = {**space.cleanup, "uploads": {"ids": [row.id for row in created] + staged_dirs, "objects": [[bucket, key] for bucket, key, _ in writes], "deadline": time.time() + 300}}
        space.revision += 1
        revision = space.revision
        result = [row.to_dict() for row in created]
        await self.db.commit()
        for bucket, key, data in writes:
            await self.storage.put(bucket, key, PackageFile(key, data, hashlib.sha256(data).hexdigest(), "application/octet-stream"))
        space = await self.space(tenant, space_id, lock=True)
        if space.revision != revision:
            raise SkillError(409, "CONCURRENT_CHANGE", "Directory changed during upload")
        space.cleanup = {key: value for key, value in space.cleanup.items() if key != "uploads"}
        await self.db.commit()
        return result

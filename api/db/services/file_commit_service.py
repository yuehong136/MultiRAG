"""Tenant-scoped immutable file snapshots with transactional working-tree edits."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from copy import deepcopy
from typing import Any, Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Document, File, File2Document, FileCommit, FileCommitItem, Knowledgebase, PythonSkillCoreSpace, UserTenant
from api.db.services.knowledgebase_service import KnowledgebaseService

MAX_FILES = 1000
MAX_BYTES = 32 * 1024 * 1024
Tree = dict[str, dict[str, Any]]
T = TypeVar("T")


class CommitError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


class FileChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    file_id: str = Field(min_length=1, max_length=32)
    operation: Literal["add", "modify", "delete", "rename"]
    file_name: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, max_length=MAX_BYTES)
    old_name: str | None = Field(default=None, min_length=1, max_length=255)
    new_name: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_operation(self) -> FileChange:
        if self.operation in {"add", "modify"} and self.content is None:
            raise ValueError("Content is required, including for an empty file")
        if self.operation == "add" and self.file_name is None:
            raise ValueError("file_name is required for add")
        if self.operation == "rename" and self.new_name is None:
            raise ValueError("new_name is required for rename")
        for name in (self.file_name, self.new_name):
            if name and (name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name):
                raise ValueError("Names must be a single path component")
        if self.operation in {"delete", "rename"} and self.content is not None:
            raise ValueError("Content is only accepted for add/modify")
        return self


class CreateCommit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(max_length=512)
    files: list[FileChange] = Field(max_length=MAX_FILES)
    parent_id: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_batch(self) -> CreateCommit:
        if len({change.file_id for change in self.files}) != len(self.files):
            raise ValueError("Duplicate file IDs")
        if sum(len((change.content or "").encode()) for change in self.files) > MAX_BYTES:
            raise ValueError("Commit content exceeds limit")
        return self


def diff_trees(old: Tree, new: Tree) -> list[dict[str, Any]]:
    """Compare complete states; storage relocation alone is not a file change."""
    result = []
    for identity in sorted(old.keys() | new.keys()):
        before, after = old.get(identity), new.get(identity)
        operation = ""
        if before is None:
            operation = "add"
        elif after is None:
            operation = "delete"
        elif any(before.get(key) != after.get(key) for key in ("hash", "size", "type")):
            operation = "modify"
        elif any(before.get(key) != after.get(key) for key in ("name", "parent_id")):
            operation = "rename"
        if operation:
            result.append(
                {
                    "file_id": identity,
                    "file_name": (after or before or {}).get("name"),
                    "operation": operation,
                    **{f"{prefix}_{field}": (entry or {}).get(field) for prefix, entry in (("old", before), ("new", after)) for field in ("hash", "location", "name")},
                }
            )
    return result


def build_tree(state: Tree, root: str) -> dict[str, Any]:
    """Render only persisted metadata, never today's File rows."""
    if root not in state:
        raise CommitError(503, "Snapshot root is missing")
    nodes = {key: {"id": key, **deepcopy(entry), **({"children": []} if entry["type"] == "folder" else {})} for key, entry in state.items()}
    for identity, entry in state.items():
        if identity == root:
            continue
        parent = nodes.get(entry["parent_id"])
        if parent is None or parent["type"] != "folder":
            raise CommitError(503, "Snapshot ancestry is invalid")
        parent["children"].append(nodes[identity])
    seen: set[str] = set()

    def visit(node: dict[str, Any]) -> None:
        if node["id"] in seen:
            raise CommitError(503, "Snapshot ancestry is cyclic")
        seen.add(node["id"])
        for child in sorted(node.get("children", []), key=lambda row: row["id"]):
            visit(child)
        if "children" in node:
            node["children"].sort(key=lambda row: row["id"])

    visit(nodes[root])
    if len(seen) != len(nodes):
        raise CommitError(503, "Snapshot contains unreachable entries")
    return nodes[root]


def public_commit(commit: FileCommit, *, tree: bool = False) -> dict[str, Any]:
    fields = ("id", "folder_id", "parent_id", "sequence", "message", "author_id", "file_count", "create_time")
    result = {field: getattr(commit, field) for field in fields}
    if tree:
        result["tree_state"] = commit.tree_state
    return result


class FileCommitService:
    def __init__(self, db: AsyncSession, storage: Any) -> None:
        self.db = db
        self.storage = storage

    async def io(self, function: Callable[..., T], *args: Any) -> T:
        # A cancelled to_thread can continue a put after compensation. Join the
        # actual operation before allowing rollback to remove its reserved key.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def membership(self, tenant: str, user: str) -> str:
        role = await self.db.scalar(
            select(UserTenant.role).where(UserTenant.tenant_id == tenant, UserTenant.user_id == user, UserTenant.status == "1", UserTenant.role.in_(["owner", "admin", "normal"]))
        )
        if role is None:
            raise CommitError(404, "Workspace not found")
        return role

    async def dataset(self, identity: str, tenant: str, user: str, write: bool) -> Knowledgebase:
        kb = await self.db.scalar(select(Knowledgebase).where(Knowledgebase.id == identity, Knowledgebase.tenant_id == tenant, KnowledgebaseService._visibility_and_status_filter(user)))
        if kb is None:
            raise CommitError(404, "Dataset not found")
        role = await self.membership(tenant, user)
        if write and kb.created_by != user and role not in {"owner", "admin"}:
            raise CommitError(403, "Dataset write permission required")
        return kb

    async def ancestry(self, identity: str, tenant: str) -> list[File]:
        rows = []
        seen: set[str] = set()
        while identity not in seen:
            seen.add(identity)
            row = await self.db.scalar(select(File).where(File.id == identity, File.tenant_id == tenant))
            if row is None:
                raise CommitError(404, "Workspace not found")
            rows.append(row)
            if row.parent_id == row.id:
                return rows
            identity = row.parent_id
            if len(rows) > 100:
                break
        raise CommitError(409, "Invalid workspace ancestry")

    async def resolve(self, prefix: str, identity: str, tenant: str, user: str, *, write: bool = False, lock: bool = False) -> File:
        if prefix == "datasets":
            target = await self.db.get(Knowledgebase, identity)
            if target is None:
                raise CommitError(404, "Dataset not found")
            tenant = target.tenant_id
            kb = await self.dataset(identity, tenant, user, write)
            folders = list(await self.db.scalars(select(File).where(File.tenant_id == tenant, File.name == kb.name, File.source_type == "knowledgebase", File.type == "folder")))
            if len(folders) != 1:
                raise CommitError(409, "Dataset folder mapping is missing or ambiguous")
            identity = folders[0].id
        elif prefix not in {"folders", "workspaces", "workspace"}:
            raise CommitError(404, "Workspace not found")
        target_folder = await self.db.get(File, identity)
        if target_folder is None:
            raise CommitError(404, "Workspace not found")
        tenant = target_folder.tenant_id
        await self.membership(tenant, user)
        ancestors = await self.ancestry(identity, tenant)
        if lock:
            # The common root serializes overlapping nested scopes in both stacks.
            await self.db.execute(select(File.id).where(File.id == ancestors[-1].id, File.tenant_id == tenant).with_for_update())
            ancestors = await self.ancestry(identity, tenant)
        if ancestors[0].type != "folder":
            raise CommitError(404, "Workspace not found")
        for row in ancestors:
            if await self.db.scalar(select(PythonSkillCoreSpace.id).where(PythonSkillCoreSpace.folder_id == row.id, PythonSkillCoreSpace.state != "deleted")):
                raise CommitError(409, "Use the Skills lifecycle for this folder")
            if row.source_type == "knowledgebase" and row.name != ".knowledgebase":
                kbs = list(await self.db.scalars(select(Knowledgebase).where(Knowledgebase.tenant_id == tenant, Knowledgebase.name == row.name, Knowledgebase.status == "1")))
                if len(kbs) != 1:
                    raise CommitError(409, "Dataset folder mapping is ambiguous")
                await self.dataset(kbs[0].id, tenant, user, write)
            elif row.source_type not in {"", "knowledgebase"}:
                raise CommitError(409, "Managed folders require their own lifecycle")
        return ancestors[0]

    async def rows(self, folder: File) -> dict[str, File]:
        result = {folder.id: folder}
        pending = [folder.id]
        while pending:
            children = list(await self.db.scalars(select(File).where(File.parent_id.in_(pending), File.id != File.parent_id).order_by(File.id)))
            pending = []
            for child in children:
                if child.tenant_id != folder.tenant_id or child.id in result:
                    raise CommitError(409, "Invalid workspace ancestry")
                if child.type == "folder" and child.source_type:
                    # A parent workspace must not bypass a private managed subtree.
                    raise CommitError(409, "Snapshot managed folders through their own scope")
                if await self.db.scalar(select(PythonSkillCoreSpace.id).where(PythonSkillCoreSpace.folder_id == child.id, PythonSkillCoreSpace.state != "deleted")):
                    raise CommitError(409, "Use the Skills lifecycle for this folder")
                result[child.id] = child
                if child.type == "folder":
                    pending.append(child.id)
            if len(result) > MAX_FILES:
                raise CommitError(413, "Workspace contains too many files")
        return result

    async def read(self, bucket: str, key: str) -> bytes:
        strict = getattr(self.storage, "get_bytes", None)
        if not callable(strict):
            raise CommitError(503, "Storage lacks strict readback")
        data = await self.io(strict, bucket, key)
        if not isinstance(data, bytes):
            raise CommitError(503, "File content is unavailable")
        return data

    async def live(self, folder: File, rows: dict[str, File]) -> tuple[Tree, dict[str, bytes]]:
        state: Tree = {}
        contents: dict[str, bytes] = {}
        total = 0
        for identity, row in rows.items():
            entry: dict[str, Any] = {"name": row.name, "parent_id": row.parent_id, "type": "folder" if row.type == "folder" else "file", "size": 0, "status": "1"}
            if row.type != "folder":
                bucket, key = row.parent_id, row.location
                links = list(await self.db.scalars(select(File2Document).where(File2Document.file_id == row.id)))
                if row.source_type:
                    if row.source_type != "knowledgebase" or len(links) != 1:
                        raise CommitError(409, "Managed file content cannot be resolved")
                    doc = await self.db.get(Document, links[0].document_id)
                    if doc is None:
                        raise CommitError(409, "Dataset document is missing")
                    kb = await self.db.get(Knowledgebase, doc.kb_id)
                    if kb is None or kb.tenant_id != folder.tenant_id:
                        raise CommitError(404, "File not found")
                    # The containing dataset was authorized by resolve; links may not cross it.
                    if kb.name != folder.name or folder.source_type != "knowledgebase":
                        raise CommitError(404, "File not found")
                    bucket, key = kb.id, doc.location
                if not key:
                    raise CommitError(503, "File content is unavailable")
                data = await self.read(bucket, key)
                total += len(data)
                if total > MAX_BYTES:
                    raise CommitError(413, "Workspace content exceeds limit")
                contents[identity] = data
                entry.update(hash=hashlib.sha256(data).hexdigest(), size=len(data), location=key, bucket=bucket)
            state[identity] = entry
        return state, contents

    async def latest(self, folder: File) -> FileCommit | None:
        return await self.db.scalar(select(FileCommit).where(FileCommit.tenant_id == folder.tenant_id, FileCommit.folder_id == folder.id).order_by(FileCommit.sequence.desc()).limit(1))

    async def commit(self, folder: File, identity: str) -> FileCommit:
        row = await self.db.scalar(select(FileCommit).where(FileCommit.id == identity, FileCommit.tenant_id == folder.tenant_id, FileCommit.folder_id == folder.id))
        if row is None:
            raise CommitError(404, "Commit not found")
        build_tree(row.tree_state, folder.id)
        return row

    async def create(self, prefix: str, identity: str, tenant: str, user: str, request: CreateCommit) -> dict[str, Any]:
        written: list[tuple[str, str]] = []
        commit_id = uuid4().hex
        committing = False
        try:
            folder = await self.resolve(prefix, identity, tenant, user, write=True, lock=True)
            tenant = folder.tenant_id
            latest = await self.latest(folder)
            if "parent_id" in request.model_fields_set and request.parent_id != (latest.id if latest else None):
                raise CommitError(409, "Commit head changed")
            rows = await self.rows(folder)
            # Validate every operation before reading or writing external storage.
            for change in request.files:
                row = rows.get(change.file_id)
                if change.operation == "add":
                    existing = await self.db.get(File, change.file_id)
                    if existing is not None or (latest and change.file_id in latest.tree_state):
                        raise CommitError(409, "File ID already exists")
                elif row is None or row.type == "folder":
                    raise CommitError(404, "File not found in workspace")
                if row is not None and await self.db.scalar(select(File2Document.id).where(File2Document.file_id == row.id).limit(1)):
                    raise CommitError(409, "Linked dataset files require the document lifecycle")
                if row is not None and row.source_type:
                    raise CommitError(409, "Managed files require their own lifecycle")
                if row is not None and change.old_name is not None and change.old_name != row.name:
                    raise CommitError(409, "File name changed")
            state, contents = await self.live(folder, rows)
            for change in request.files:
                row = rows.get(change.file_id)
                if change.operation == "add":
                    row = File(id=change.file_id, parent_id=folder.id, tenant_id=tenant, created_by=user, name=change.file_name, type="other", source_type="", size=0)
                    self.db.add(row)
                    rows[row.id] = row
                    state[row.id] = {"name": row.name, "parent_id": row.parent_id, "type": "file", "size": 0, "status": "1"}
                assert row is not None
                if change.operation == "delete":
                    await self.db.delete(row)
                    state.pop(row.id)
                    contents.pop(row.id, None)
                    continue
                if change.operation in {"add", "modify"}:
                    contents[row.id] = (change.content or "").encode()
                    state[row.id].update(hash=hashlib.sha256(contents[row.id]).hexdigest(), size=len(contents[row.id]))
                    row.size = len(contents[row.id])
                name = change.new_name if change.operation == "rename" else change.file_name
                if name:
                    row.name = name
                    state[row.id]["name"] = name
            if sum(map(len, contents.values())) > MAX_BYTES or len(state) > MAX_FILES:
                raise CommitError(413, "Snapshot exceeds limits")
            names: set[tuple[str, str]] = set()
            for entry in state.values():
                name_key = (entry["parent_id"], entry["name"])
                if name_key in names:
                    raise CommitError(409, "Duplicate file name")
                names.add(name_key)
            for file_id, data in contents.items():
                entry = state[file_id]
                inherited = latest.tree_state.get(file_id) if latest else None
                if inherited and inherited.get("hash") == entry["hash"]:
                    # Verify retained bytes too; missing inherited objects cannot be published.
                    if await self.read(inherited["bucket"], inherited["location"]) != data:
                        raise CommitError(503, "Snapshot content integrity failure")
                    entry.update(bucket=inherited["bucket"], location=inherited["location"])
                else:
                    bucket = rows[file_id].parent_id
                    key = f".file-commits/{tenant}/{folder.id}/{commit_id}/{entry['hash']}"
                    pair = (bucket, key)
                    if pair not in written:
                        written.append(pair)  # Reserve before put, including partial failures.
                        if await self.io(self.storage.put, bucket, key, data) is False:
                            raise CommitError(503, "Storage rejected content")
                        if await self.read(bucket, key) != data:
                            raise CommitError(503, "Snapshot content integrity failure")
                    entry.update(bucket=bucket, location=key)
                if any(change.file_id == file_id and change.operation in {"add", "modify"} for change in request.files):
                    # Ordinary Files may delete/move its live object. It must never
                    # own the immutable snapshot key, even for identical bytes.
                    bucket = rows[file_id].parent_id
                    key = f".file-working/{tenant}/{folder.id}/{commit_id}/{file_id}/{entry['hash']}"
                    written.append((bucket, key))
                    if await self.io(self.storage.put, bucket, key, data) is False:
                        raise CommitError(503, "Storage rejected working content")
                    if await self.read(bucket, key) != data:
                        raise CommitError(503, "Working content integrity failure")
                    rows[file_id].location = key
            build_tree(state, folder.id)
            changes = [change for change in diff_trees(latest.tree_state if latest else {}, state) if change["file_id"] != folder.id]
            snapshot = FileCommit(
                id=commit_id,
                tenant_id=tenant,
                folder_id=folder.id,
                parent_id=latest.id if latest else None,
                sequence=latest.sequence + 1 if latest else 1,
                message=request.message,
                author_id=user,
                file_count=len(changes),
                tree_state=state,
            )
            self.db.add(snapshot)
            await self.db.flush()
            for delta in changes:
                self.db.add(FileCommitItem(id=uuid4().hex, commit_id=commit_id, **{key: value for key, value in delta.items() if key != "file_name"}))
            await self.db.flush()
            result = public_commit(snapshot, tree=True)
            committing = True
            await self.db.commit()
            return result
        except BaseException as original:
            await self.db.rollback()
            if committing:
                # A lost COMMIT acknowledgement is ambiguous. Never remove an
                # object that a separately committed SQL snapshot may reference.
                async with AsyncSession(self.db.bind) as reader:
                    try:
                        durable = await reader.get(FileCommit, commit_id)
                    except Exception as exc:
                        raise CommitError(503, "Commit outcome requires recovery; objects retained") from exc
                    if durable is not None:
                        raise CommitError(503, "Commit persisted; reload history before retrying") from None
            failures = []
            for bucket, key in reversed(written):
                try:
                    if await self.io(self.storage.rm, bucket, key) is False or await self.io(self.storage.get_bytes, bucket, key) is not None:
                        failures.append(key)
                except Exception:
                    failures.append(key)
            if failures:
                raise CommitError(503, "Snapshot rollback cleanup requires recovery") from original
            raise

    async def list_commits(self, folder: File, page: int, size: int, desc: bool) -> dict[str, Any]:
        criteria = (FileCommit.tenant_id == folder.tenant_id, FileCommit.folder_id == folder.id)
        total = await self.db.scalar(select(func.count()).select_from(FileCommit).where(*criteria))
        order = FileCommit.sequence.desc() if desc else FileCommit.sequence.asc()
        rows = await self.db.scalars(select(FileCommit).where(*criteria).order_by(order).offset((page - 1) * size).limit(size))
        return {"total": total, "page": page, "page_size": size, "commits": [public_commit(row) for row in rows]}

    async def items(self, commit: FileCommit) -> list[dict[str, Any]]:
        rows = await self.db.scalars(select(FileCommitItem).where(FileCommitItem.commit_id == commit.id).order_by(FileCommitItem.file_id))
        fields = ("id", "file_id", "operation", "old_hash", "new_hash", "old_location", "new_location", "old_name", "new_name")
        return [{field: getattr(row, field) for field in fields} for row in rows]

    async def content(self, commit: FileCommit, file_id: str) -> bytes:
        entry = commit.tree_state.get(file_id)
        if not entry or entry["type"] != "file":
            raise CommitError(404, "File not found in commit")
        expected = f".file-commits/{commit.tenant_id}/{commit.folder_id}/"
        if not entry["location"].startswith(expected):
            raise CommitError(503, "Snapshot object identity is invalid")
        data = await self.read(entry["bucket"], entry["location"])
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["hash"]:
            raise CommitError(503, "Snapshot content integrity failure")
        return data

    async def versions(self, tenant: str, user: str, file_id: str) -> list[dict[str, Any]]:
        # Query history via tenant-scoped commits; works after File row deletion.
        records = (
            await self.db.execute(
                select(FileCommit, FileCommitItem)
                .join(FileCommitItem, FileCommitItem.commit_id == FileCommit.id)
                .where(
                    FileCommit.tenant_id.in_(select(UserTenant.tenant_id).where(UserTenant.user_id == user, UserTenant.status == "1", UserTenant.role.in_(["owner", "admin", "normal"]))),
                    FileCommitItem.file_id == file_id,
                )
                .order_by(FileCommit.create_time.desc(), FileCommit.id.desc())
            )
        ).all()
        result = []
        for commit, item in records:
            try:
                await self.resolve("folders", commit.folder_id, tenant, user)
            except CommitError as exc:
                if exc.status in {403, 404}:
                    continue
                raise
            result.append(
                {
                    "commit_id": commit.id,
                    "folder_id": commit.folder_id,
                    "operation": item.operation,
                    "hash": item.new_hash or item.old_hash or "",
                    "message": commit.message,
                    "create_time": commit.create_time,
                }
            )
        if not result:
            row = await self.db.get(File, file_id)
            if row is None:
                raise CommitError(404, "File not found")
            await self.resolve("folders", row.parent_id, tenant, user)
        return result

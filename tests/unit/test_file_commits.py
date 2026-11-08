"""Boundary and cancellation regressions for immutable file snapshots."""

import asyncio
import threading
from typing import Any

import pytest
from pydantic import ValidationError

from api.db.services.file_commit_service import CommitError, CreateCommit, FileCommitService, build_tree, diff_trees


@pytest.mark.parametrize(
    "files",
    [
        [{"file_id": "f", "operation": "modify"}],
        [{"file_id": "f", "operation": "delete", "content": "unexpected"}],
        [{"file_id": "f", "operation": "rename", "new_name": "../escape"}],
        [{"file_id": "f", "operation": "add", "file_name": "ok", "content": "", "tenant_id": "other"}],
        [{"file_id": "f", "operation": "delete"}, {"file_id": "f", "operation": "rename", "new_name": "duplicate"}],
    ],
)
def test_commit_rejects_ambiguous_or_unsafe_changes(files: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        CreateCommit(message="x", files=files)


def test_snapshot_diff_uses_full_states_and_historical_hierarchy() -> None:
    old = {"root": {"name": "old root", "parent_id": "root", "type": "folder"}, "a": {"name": "a", "parent_id": "root", "type": "file", "hash": "a", "size": 1}}
    after = {**old, "b": {"name": "b", "parent_id": "root", "type": "file", "hash": "b", "size": 1}}
    assert [row["operation"] for row in diff_trees(old, after)] == ["add"]
    assert build_tree(old, "root")["name"] == "old root"
    with pytest.raises(CommitError):
        build_tree({"root": old["root"], "bad": {"name": "bad", "parent_id": "missing", "type": "file"}}, "root")


async def test_cancellation_joins_storage_put_before_rollback(async_db: Any) -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def put() -> None:
        started.set()
        assert release.wait(5)
        finished.set()

    svc = FileCommitService(async_db, None)
    operation = asyncio.create_task(svc.io(put))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert finished.is_set()
    finally:
        release.set()

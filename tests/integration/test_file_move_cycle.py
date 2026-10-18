"""Folder move rejection before SQL/object mutations, through registered HTTP."""

import copy
from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import File
from common import resources, settings
from core.utils.encrypted_storage import EncryptedStorageWrapper
from tests.support.agent_update_release import release_api as release_api


@pytest.fixture
def move_tree(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = release_api
    ids = {name: uuid4().hex for name in ["root", "source", "child", "grandchild", "leaf", "sibling", "other"]}
    bucket = f"move-cycle-{uuid4().hex}"
    source = settings.STORAGE_IMPL
    storage = copy.copy(source)
    adapter = copy.copy(source.storage_impl) if isinstance(source, EncryptedStorageWrapper) else storage
    if isinstance(source, EncryptedStorageWrapper):
        storage.storage_impl = adapter
    adapter.bucket, adapter.prefix_path = bucket, "move-cycle"
    client = adapter.conn
    monkeypatch.setitem(resources._state, "storage", storage)
    with ExitStack() as cleanup:

        def remove_bucket() -> None:
            if client.bucket_exists(bucket):
                for item in client.list_objects(bucket, recursive=True):
                    client.remove_object(bucket, item.object_name)
                client.remove_bucket(bucket)
            assert not client.bucket_exists(bucket)

        def remove_rows() -> None:
            with Session(env["engine"]) as db:
                db.execute(sa.delete(File).where(File.tenant_id == env["owners"][0]))
                db.commit()
            with Session(env["engine"]) as db:
                assert db.scalar(sa.select(sa.func.count()).select_from(File).where(File.tenant_id == env["owners"][0])) == 0

        cleanup.callback(remove_bucket)
        cleanup.callback(remove_rows)
        parents = {"root": "root", "source": "root", "child": "source", "grandchild": "child", "leaf": "source", "sibling": "root", "other": "root"}
        with Session(env["engine"]) as db:
            for name, identity in ids.items():
                db.add(
                    File(
                        id=identity,
                        parent_id=ids[parents[name]],
                        tenant_id=env["owners"][0],
                        created_by=env["owners"][0],
                        name=name,
                        type="doc" if name in {"leaf", "sibling"} else "folder",
                        location=name if name in {"leaf", "sibling"} else "",
                    )
                )
            db.commit()
        client.make_bucket(bucket)
        storage.put(ids["source"], "leaf", b"nested-file")
        storage.put(ids["root"], "sibling", b"first-in-batch")
        yield dict(env, ids=ids, bucket=bucket, storage=storage, objects=client)


def snapshot(env: dict[str, Any]) -> Any:
    with Session(env["engine"]) as db:
        sql = [dict(row._mapping) for row in db.execute(sa.select(File.__table__).where(File.tenant_id == env["owners"][0]).order_by(File.id))]
    objects = {}
    for item in env["objects"].list_objects(env["bucket"], recursive=True):
        response = env["objects"].get_object(env["bucket"], item.object_name)
        try:
            objects[item.object_name] = response.read()
        finally:
            response.close()
            response.release_conn()
    return sql, objects


def test_folder_moves_reject_cycles_without_partial_batch_writes(move_tree: dict[str, Any]) -> None:
    env, ids = move_tree, move_tree["ids"]
    before = snapshot(env)
    for destination in ["source", "child", "grandchild", "root"]:
        response = env["client"].post(env["base"] + "/api/v1/files/move", json={"src_file_ids": [ids["sibling"], ids["source"]], "dest_file_id": ids[destination]}, timeout=30)
        assert response.status_code == 200 and response.json()["code"] != 0, response.text
        assert "Cannot move a folder" in response.json()["message"]
        assert snapshot(env) == before


def test_folder_move_to_sibling_preserves_contents(move_tree: dict[str, Any]) -> None:
    env, ids = move_tree, move_tree["ids"]
    response = env["client"].post(env["base"] + "/api/v1/files/move", json={"src_file_ids": [ids["source"]], "dest_file_id": ids["other"]}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    with Session(env["engine"]) as db:
        assert db.get(File, ids["source"]) is None
        moved = db.scalar(sa.select(File).where(File.parent_id == ids["other"], File.name == "source"))
        assert moved is not None
        leaf = db.get(File, ids["leaf"])
        assert leaf is not None and leaf.parent_id == moved.id
        assert env["storage"].get(leaf.parent_id, leaf.location) == b"nested-file"
    assert not env["storage"].obj_exist(ids["source"], "leaf")
    assert env["storage"].get(ids["root"], "sibling") == b"first-in-batch"


def test_folder_move_to_current_parent_allows_explicit_rename(move_tree: dict[str, Any]) -> None:
    env, ids = move_tree, move_tree["ids"]
    response = env["client"].post(env["base"] + "/api/v1/files/move", json={"src_file_ids": [ids["source"]], "dest_file_id": ids["root"], "new_name": "renamed"}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    with Session(env["engine"]) as db:
        assert db.get(File, ids["source"]) is None
        renamed = db.scalar(sa.select(File).where(File.parent_id == ids["root"], File.name == "renamed"))
        leaf = db.get(File, ids["leaf"])
        assert renamed is not None and leaf is not None and leaf.parent_id == renamed.id
        assert env["storage"].get(leaf.parent_id, leaf.location) == b"nested-file"
    assert env["storage"].get(ids["root"], "sibling") == b"first-in-batch"

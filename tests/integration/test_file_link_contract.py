"""File linking executes the real converter using only scratch database sessions."""

from __future__ import annotations

import copy
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import BackgroundTasks
from minio import Minio
from sqlalchemy.orm import Session

from api.apps.services import file_convert_service
from api.db.db_models import Document, File, File2Document, Knowledgebase, Task, UserTenant
from common import resources, settings
from common.config_utils import CONFIGS
from common.constants import RetCode
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_update_release import release_api as release_api
from tests.support.runtime_upload import read_object


def test_file_link_http_sql_preflight(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = release_api
    root, leaf, denied_folder, foreign_leaf, dataset, additional_dataset, foreign_dataset = [uuid4().hex for _ in range(7)]
    owner, other = env["owners"][:2]

    @contextmanager
    def scratch_connection() -> Any:
        with Session(env["engine"]) as db:
            yield db

    monkeypatch.setattr(file_convert_service, "db_connection", scratch_connection)
    with Session(env["engine"]) as db:
        for identifier, tenant in ((dataset, owner), (additional_dataset, owner), (foreign_dataset, other)):
            db.add(Knowledgebase(id=identifier, tenant_id=tenant, created_by=tenant, name=identifier, embd_id="unused"))
        for identifier, parent, tenant, kind in ((root, "", owner, "folder"), (leaf, root, owner, "doc"), (denied_folder, "", other, "folder"), (foreign_leaf, root, other, "doc")):
            db.add(File(id=identifier, parent_id=parent, tenant_id=tenant, created_by=tenant, name=identifier + ".txt", type=kind, location=identifier))
        db.commit()

    def snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with Session(env["engine"]) as db:
            docs = [doc.to_dict() for doc in db.scalars(sa.select(Document).where(Document.kb_id.in_([dataset, additional_dataset, foreign_dataset])).order_by(Document.id))]
            links = [link.to_dict() for link in db.scalars(sa.select(File2Document).where(File2Document.file_id.in_([leaf, foreign_leaf])))]
            return docs, links

    def link(files: list[str], datasets: list[str], token: str | None = None, mode: str = "replace") -> Any:
        return env["client"].post(
            f"{env['base']}/api/v1/files/link-to-datasets",
            params={"mode": mode},
            json={"file_ids": files, "kb_ids": datasets},
            headers={"Authorization": f"Bearer {token or env['keys'][0]}"},
            timeout=30,
        )

    try:
        before = snapshot()
        for files, datasets in (([denied_folder], [dataset]), ([root], [dataset]), ([leaf], [foreign_dataset]), ([leaf, uuid4().hex], [dataset])):
            result = link(files, datasets)
            assert result.status_code == 200 and result.json().get("retcode", result.json().get("code")) != 0, result.text
            assert snapshot() == before
        assert link([leaf], [dataset], "invalid").status_code == 401
        retired = env["client"].post(f"{env['base']}/api/v1/file/convert", json={"file_ids": [leaf], "kb_ids": [dataset]}, timeout=30)
        assert retired.status_code == 404 and snapshot() == before
        paths = env["client"].get(f"{env['base']}/openapi.json", timeout=30).json()["paths"]
        assert "/api/v1/file/convert" not in paths and "/api/v1/files/link-to-datasets" in paths
        # Remove only this fixture's foreign child; the same owned folder can now link.
        with Session(env["engine"]) as db:
            db.execute(sa.delete(File).where(File.id == foreign_leaf))
            db.commit()
        result = link([root, leaf, root], [dataset])
        assert result.status_code == 200 and result.json().get("retcode", result.json().get("code")) == 0 and result.json()["data"] is True, result.text
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            docs, links = snapshot()
            if len(docs) == len(links) == 1:
                break
            time.sleep(0.05)
        assert len(docs) == len(links) == 1
        assert docs[0]["kb_id"] == dataset and docs[0]["created_by"] == owner and docs[0]["location"] == leaf
        assert links[0]["file_id"] == leaf and links[0]["document_id"] == docs[0]["id"]
        first_doc = docs[0]
        assert link([leaf], [dataset, dataset]).json()["retcode"] == 0
        assert snapshot()[0] == [first_doc]
        assert link([leaf], [additional_dataset], mode="add").json()["retcode"] == 0
        deadline = time.monotonic() + 10
        while len(snapshot()[0]) != 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(snapshot()[0]) == 2 and first_doc in snapshot()[0]
        assert link([leaf], [dataset], mode="invalid").status_code == 422
        assert link([leaf], [dataset], mode="replace").json()["retcode"] == 0
        deadline = time.monotonic() + 10
        while len(snapshot()[0]) != 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert snapshot()[0] == [first_doc]
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(File2Document).where(File2Document.file_id.in_([leaf, foreign_leaf])))
            db.execute(sa.delete(Document).where(Document.kb_id.in_([dataset, additional_dataset, foreign_dataset])))
            db.execute(sa.delete(File).where(File.id.in_([root, leaf, denied_folder, foreign_leaf])))
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id.in_([dataset, additional_dataset, foreign_dataset])))
            db.commit()
        assert snapshot() == ([], [])


def test_file_link_authorization_before_background_side_effects(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, tmp_path: Path) -> None:
    """Check the complete batch before any destructive worker can be scheduled."""
    env = release_api
    owner, other = env["owners"][:2]
    ids = {
        name: uuid4().hex
        for name in (
            "root",
            "nested",
            "leaf",
            "foreign_leaf",
            "foreign_root",
            "owned_child",
            "shared_file",
            "target",
            "extra",
            "private",
            "team",
            "old_doc",
            "shared_doc",
            "shared_private_doc",
            "task",
            "sibling_task",
        )
    }
    files = [ids[name] for name in ("root", "nested", "leaf", "foreign_leaf", "foreign_root", "owned_child", "shared_file")]
    datasets = [ids[name] for name in ("target", "extra", "private", "team")]
    bucket = f"file-auth-{uuid4().hex}"
    queue = f"file-auth:{uuid4().hex}:queue"
    cancel_key = f"{ids['task']}-cancel"
    evidence: dict[str, Any] = {"ids": ids, "database": env["engine"].url.database, "bucket": bucket, "queue": queue, "cancel_key": cancel_key, "cases": []}
    report = request.config.getoption("--test-report-dir")
    report_path = (Path(report) if report else tmp_path) / "file-link-authorization.json"
    cfg = CONFIGS["minio"]
    storage_client = Minio(cfg["host"], access_key=cfg["user"], secret_key=cfg["password"], secure=str(cfg.get("secure", False)).lower() in {"true", "1", "yes"})
    redis = REDIS_CONN.REDIS
    assert redis is not None
    scheduled: list[tuple[Any, ...]] = []
    completed = threading.Event()
    worker_errors: list[str] = []
    original_convert = file_convert_service.convert_files
    original_add_task = BackgroundTasks.add_task

    @contextmanager
    def scratch_connection() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    def track_schedule(self: BackgroundTasks, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        if func is file_convert_service.convert_files_with_new_session:
            scheduled.append(args)
        original_add_task(self, func, *args, **kwargs)

    def track_worker(*args: Any, **kwargs: Any) -> None:
        try:
            original_convert(*args, **kwargs)
        except Exception as exc:
            worker_errors.append(type(exc).__name__)
            raise
        finally:
            completed.set()

    monkeypatch.setattr(file_convert_service, "db_connection", scratch_connection)
    monkeypatch.setattr(file_convert_service, "convert_files", track_worker)
    monkeypatch.setattr(BackgroundTasks, "add_task", track_schedule)
    monkeypatch.setattr(settings, "get_svr_queue_name", lambda priority: queue)
    storage = copy.copy(settings.STORAGE_IMPL)
    storage.conn, storage.bucket, storage.prefix_path = storage_client, bucket, ""
    monkeypatch.setitem(resources._state, "storage", storage)

    def sql_snapshot() -> dict[str, Any]:
        # Physical rows include xmin: even a same-value UPDATE would be visible.
        with env["engine"].connect() as db:
            return {
                model.__tablename__: [dict(row) for row in db.execute(sa.select(model.__table__, sa.literal_column("xmin::text").label("xmin")).where(condition).order_by(model.id)).mappings()]
                for model, condition in (
                    (File, File.id.in_(files)),
                    (Knowledgebase, Knowledgebase.id.in_(datasets)),
                    (Document, Document.kb_id.in_(datasets)),
                    (File2Document, File2Document.file_id.in_(files)),
                    (Task, sa.true()),
                )
            }

    def snapshot() -> dict[str, Any]:
        objects = {item.object_name: read_object(storage_client, bucket, item.object_name) for item in storage_client.list_objects(bucket, recursive=True)}
        return {"sql": sql_snapshot(), "objects": objects, "queue": redis.xrange(queue), "cancel": redis.get(cancel_key)}

    def link(file_ids: list[str], kb_ids: list[str], mode: str) -> Any:
        return env["client"].post(
            f"{env['base']}/api/v1/files/link-to-datasets",
            params={"mode": mode},
            json={"file_ids": file_ids, "kb_ids": kb_ids},
            headers={"Authorization": f"Bearer {env['keys'][0]}"},
            timeout=30,
        )

    def deny(label: str, file_ids: list[str], kb_ids: list[str], mode: str) -> None:
        before = snapshot()
        schedule_count = len(scheduled)
        response = link(file_ids, kb_ids, mode)
        assert response.status_code == 200 and response.json()["code"] == int(RetCode.DATA_ERROR), response.text
        assert response.json()["message"] == "No authorization.", response.text
        assert len(scheduled) == schedule_count
        assert snapshot() == before
        evidence["cases"].append({"case": label, "mode": mode, "code": response.json()["code"], "scheduled": 0, "sql_storage_redis_unchanged": True})

    def allow(file_ids: list[str], kb_ids: list[str], mode: str) -> None:
        completed.clear()
        schedule_count = len(scheduled)
        response = link(file_ids, kb_ids, mode)
        assert response.status_code == 200 and response.json()["retcode"] == 0 and response.json()["data"] is True, response.text
        assert completed.wait(15), "The real background converter did not finish"
        assert not worker_errors, worker_errors
        assert len(scheduled) == schedule_count + 1
        evidence["cases"].append({"case": "allowed", "mode": mode, "retcode": 0, "scheduled": 1, "worker_completed": True})

    def cleanup_sql() -> None:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(Task).where(Task.id.in_([ids["task"], ids["sibling_task"]])))
            db.execute(sa.delete(File2Document).where(File2Document.file_id.in_(files)))
            db.execute(sa.delete(Document).where(Document.kb_id.in_(datasets)))
            db.execute(sa.delete(File).where(File.id.in_(files)))
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id.in_(datasets)))
            db.execute(sa.delete(UserTenant).where(UserTenant.user_id == owner, UserTenant.tenant_id == other))
            db.commit()
        remaining = sql_snapshot()
        assert remaining[Task.__tablename__] == baseline_tasks
        assert all(not rows for table, rows in remaining.items() if table != Task.__tablename__)
        evidence.update(sql_rows_removed=True, baseline_task_rows_preserved=True, task_rows_after_cleanup=remaining[Task.__tablename__])

    def cleanup_storage() -> None:
        if storage_client.bucket_exists(bucket):
            for item in storage_client.list_objects(bucket, recursive=True):
                storage_client.remove_object(bucket, item.object_name)
            storage_client.remove_bucket(bucket)
        assert not storage_client.bucket_exists(bucket)
        evidence["bucket_removed"] = True

    def cleanup_redis() -> None:
        redis.delete(queue, cancel_key)
        assert not redis.exists(queue, cancel_key)
        evidence["redis_removed"] = True

    def save_evidence() -> None:
        report_path.write_text(json.dumps(evidence, indent=2, default=str) + "\n")
        report_path.chmod(0o600)

    # The suite intentionally reuses its scratch DB. Preserve every pre-existing
    # task, including full physical rows/xmin, while cleaning only our IDs.
    baseline_tasks = sql_snapshot()[Task.__tablename__]
    evidence["baseline_task_rows"] = baseline_tasks
    with ExitStack() as cleanup:
        cleanup.callback(save_evidence)
        cleanup.callback(cleanup_storage)
        cleanup.callback(cleanup_redis)
        cleanup.callback(cleanup_sql)
        storage_client.make_bucket(bucket)
        redis.xadd(queue, {"id": ids["task"], "doc_id": ids["old_doc"]})
        with Session(env["engine"]) as db:
            for name, tenant, permission in (("target", owner, "me"), ("extra", owner, "me"), ("private", other, "me"), ("team", other, "team")):
                db.add(
                    Knowledgebase(id=ids[name], tenant_id=tenant, created_by=tenant, name=f"auth_{ids[name]}", embd_id="unused", permission=permission, doc_num={"private": 2, "team": 1}.get(name, 0))
                )
            db.add(UserTenant(id=uuid4().hex, user_id=owner, tenant_id=other, role="normal", invited_by=other))
            for name, parent, tenant, kind in (
                ("root", "", owner, "folder"),
                ("nested", ids["root"], owner, "folder"),
                ("leaf", ids["nested"], owner, "doc"),
                ("foreign_leaf", ids["nested"], other, "doc"),
                ("foreign_root", "", other, "folder"),
                ("owned_child", ids["foreign_root"], owner, "doc"),
                ("shared_file", "", other, "doc"),
            ):
                db.add(File(id=ids[name], parent_id=parent, tenant_id=tenant, created_by=tenant, name=name + ".txt", location=ids[name], type=kind))
            for doc, file, kb in (("old_doc", "leaf", "private"), ("shared_doc", "shared_file", "team"), ("shared_private_doc", "shared_file", "private")):
                db.add(
                    Document(id=ids[doc], kb_id=ids[kb], parser_id="naive", created_by=other, type="doc", name=doc + ".txt", location=ids[file], thumbnail="old-thumbnail" if doc == "old_doc" else "")
                )
                db.add(File2Document(id=uuid4().hex, file_id=ids[file], document_id=ids[doc]))
            db.add(Task(id=ids["task"], doc_id=ids["old_doc"], progress=0.25))
            db.add(Task(id=ids["sibling_task"], doc_id=ids["shared_doc"], progress=0.5))
            db.commit()
        storage.put(ids["nested"], ids["leaf"], b"owned source bytes")
        storage.put(ids["private"], "old-thumbnail", b"protected thumbnail")
        initial = snapshot()
        assert len(initial["objects"]) == 2 and initial["cancel"] is None
        for mode in ("add", "replace"):
            deny("mixed_files", [ids["leaf"], ids["foreign_leaf"]], [ids["target"]], mode)
            deny("nested_folder_foreign_leaf", [ids["root"]], [ids["target"]], mode)
            deny("original_folder_with_owned_child", [ids["foreign_root"]], [ids["target"]], mode)
            deny("mixed_targets", [ids["leaf"]], [ids["target"], ids["private"]], mode)
        deny("owned_file_private_source", [ids["leaf"]], [ids["target"]], "replace")
        deny("team_file_private_source", [ids["shared_file"]], [ids["target"]], "replace")

        # Add only creates missing pairs, so inaccessible old datasets stay intact.
        allow([ids["leaf"], ids["shared_file"]], [ids["target"]], "add")
        added = snapshot()
        for table, rows in initial["sql"].items():
            if table != Knowledgebase.__tablename__:
                assert all(row in added["sql"][table] for row in rows), table
        initial_kbs = {row["id"]: row for row in initial["sql"][Knowledgebase.__tablename__]}
        for kb in added["sql"][Knowledgebase.__tablename__]:
            before_kb = initial_kbs[kb["id"]]
            if kb["id"] == ids["target"]:
                assert kb["doc_num"] == before_kb["doc_num"] + 2
                assert kb["xmin"] != before_kb["xmin"]
                assert {key: value for key, value in kb.items() if key not in {"doc_num", "update_time", "update_date", "xmin"}} == {
                    key: value for key, value in before_kb.items() if key not in {"doc_num", "update_time", "update_date", "xmin"}
                }
            else:
                assert kb == before_kb
        assert len(added["sql"][Document.__tablename__]) == len(added["sql"][File2Document.__tablename__]) == 5
        assert all(added[key] == initial[key] for key in ("objects", "queue", "cancel"))
        deny("replace_after_add_private_source", [ids["leaf"]], [ids["target"]], "replace")

        # Authorized nested expansion de-duplicates leaves and preserves old docs.
        with Session(env["engine"]) as db:
            db.execute(sa.delete(File).where(File.id == ids["foreign_leaf"]))
            db.commit()
        allow([ids["root"], ids["leaf"], ids["root"]], [ids["extra"], ids["extra"]], "add")
        assert scheduled[-1] == ([ids["leaf"]], [ids["extra"], ids["extra"]], owner, "add")
        assert len(snapshot()["sql"][Document.__tablename__]) == 6

        # Grant real team access to the removed dataset, then execute real cleanup.
        with Session(env["engine"]) as db:
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids["private"]).values(permission="team"))
            db.commit()
        before_replace = snapshot()
        allow([ids["leaf"]], [ids["target"], ids["extra"]], "replace")
        replaced = snapshot()
        assert len(replaced["sql"][Document.__tablename__]) == len(replaced["sql"][File2Document.__tablename__]) == 5
        expected_tasks = [row for row in before_replace["sql"][Task.__tablename__] if row["id"] != ids["task"]]
        assert replaced["sql"][Task.__tablename__] == expected_tasks
        assert any(row["id"] == ids["sibling_task"] for row in expected_tasks)
        evidence.update(task_rows_before_replace=before_replace["sql"][Task.__tablename__], task_rows_after_replace=replaced["sql"][Task.__tablename__], only_removed_document_task_deleted=True)
        assert all(row["id"] != ids["old_doc"] for row in replaced["sql"][Document.__tablename__])
        for table in (Document.__tablename__, File2Document.__tablename__):
            assert all(row in before_replace["sql"][table] for row in replaced["sql"][table])
        assert replaced["objects"] == {f"{ids['nested']}/{ids['leaf']}": b"owned source bytes"}
        assert replaced["queue"] == initial["queue"] and replaced["cancel"] == "x"
        evidence.update(source_bytes_preserved=True, authorized_thumbnail_removed=True, authorized_task_canceled=True, scheduled_batches=len(scheduled))

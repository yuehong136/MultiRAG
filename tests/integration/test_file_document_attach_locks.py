"""PostgreSQL attachment witnesses for the shared Doc -> File lock contract."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.apps.services.file_convert_service import convert_files
from api.db.db_models import Document, File, File2Document, Knowledgebase, Tenant, User, UserTenant
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from tests.support.resource_journal import ResourceJournal


@pytest.fixture
def attach_rows(bootstrapped_engine: sa.Engine, request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    engine = bootstrapped_engine
    journal = ResourceJournal(request.node.nodeid)
    ids = {
        name: uuid4().hex for name in ["owner", "member", "other", "team", "private", "foreign", "member_file", "shared_file", "foreign_file", "target", "source", "foreign_doc", "existing", "link"]
    }
    evidence = Path(os.environ["F_ATTACH_EVIDENCE"])
    evidence.mkdir(parents=True, exist_ok=True)
    path = evidence / (journal.run_id + ".json")
    report: dict[str, Any] = {"ids": ids, "database": engine.url.database, "host": engine.url.host, "port": engine.url.port, "events": [], "cleanup": {}}
    journal.event("reserve-sql-identities", ids=ids, database=engine.url.database, host=engine.url.host, port=engine.url.port, evidence=str(path))

    def reserve(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        if not getattr(context, "isinsert", False) or context.compiled is None:
            return
        table = context.compiled.statement.table.name
        for values in context.compiled_parameters:
            journal.event("reserve-sql-write", table=table, id=values.get("id"), file_id=values.get("file_id"), document_id=values.get("document_id"), tenant_id=values.get("tenant_id"))

    sa.event.listen(engine, "before_cursor_execute", reserve)
    try:
        with Session(engine) as db:
            for role in ["owner", "member", "other"]:
                user = ids[role]
                db.add(User(id=user, email=user + "@attach.test", nickname="Attach", password="unused", access_token="active", is_active=True))
                db.add(Tenant(id=user, name="Attach", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
                db.add(UserTenant(id=uuid4().hex, user_id=user, tenant_id=user, invited_by=user, role="owner"))
            db.add(UserTenant(id=uuid4().hex, user_id=ids["member"], tenant_id=ids["owner"], invited_by=ids["owner"], role="normal"))
            for key, owner, permission in [("team", "owner", "team"), ("private", "member", "me"), ("foreign", "other", "me")]:
                db.add(
                    Knowledgebase(
                        id=ids[key],
                        tenant_id=ids[owner],
                        created_by=ids[owner],
                        name="Attach " + key,
                        permission=permission,
                        embd_id="unused",
                        parser_id="naive",
                        parser_config={},
                        doc_num=2 if key == "team" else 1 if key == "foreign" else 0,
                    )
                )
            for key, owner, source in [("member_file", "member", "local"), ("shared_file", "owner", "knowledgebase"), ("foreign_file", "other", "local")]:
                db.add(File(id=ids[key], parent_id=ids[owner], tenant_id=ids[owner], created_by=ids[owner], name="source.txt", location="source.txt", type="doc", source_type=source, size=12))
            for key, dataset, owner in [("target", "team", "member"), ("source", "team", "owner"), ("foreign_doc", "foreign", "other")]:
                db.add(
                    Document(
                        id=ids[key],
                        kb_id=ids[dataset],
                        created_by=ids[owner],
                        name="source.txt",
                        location="source.txt",
                        type="doc",
                        source_type="local",
                        parser_id="naive",
                        parser_config={},
                        suffix="txt",
                        size=12,
                    )
                )
            db.add(File2Document(id=ids["existing"], file_id=ids["shared_file"], document_id=ids["source"]))
            db.commit()
        yield {"engine": engine, "ids": ids, "report": report, "journal": journal}
    finally:
        try:
            with Session(engine) as db:
                doc_ids = list(db.scalars(sa.select(Document.id).where(Document.kb_id.in_([ids[k] for k in ["team", "private", "foreign"]]))))
                file_ids = list(db.scalars(sa.select(File.id).where(File.tenant_id.in_([ids[k] for k in ["owner", "member", "other"]]))))
                conditions = [
                    (File2Document, sa.or_(File2Document.document_id.in_(doc_ids), File2Document.file_id.in_(file_ids))),
                    (Document, Document.id.in_(doc_ids)),
                    (File, File.id.in_(file_ids)),
                    (Knowledgebase, Knowledgebase.id.in_([ids[k] for k in ["team", "private", "foreign"]])),
                    (UserTenant, UserTenant.user_id.in_([ids[k] for k in ["owner", "member", "other"]])),
                    (Tenant, Tenant.id.in_([ids[k] for k in ["owner", "member", "other"]])),
                    (User, User.id.in_([ids[k] for k in ["owner", "member", "other"]])),
                ]
                for model, condition in conditions:
                    db.execute(sa.delete(model).where(condition))
                db.commit()
            with engine.connect() as reader:
                counts = {model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(condition)) for model, condition in conditions}
                assert not any(counts.values()), counts
            report["cleanup"] = counts
            journal.event("sql-cleanup-readback", counts=counts, absent=True)
        finally:
            sa.event.remove(engine, "before_cursor_execute", reserve)
            path.write_text(json.dumps(report, indent=2))
            path.chmod(0o600)


def _insert(env: dict[str, Any], *, file: str = "shared_file", actor: str = "member", session_type: type[Session] = Session) -> str:
    with session_type(env["engine"]) as db:
        pid = db.scalar(sa.text("SELECT pg_backend_pid()"))
        env["report"]["worker_pid"] = pid
        row = File2DocumentService.insert(db, {"id": env["ids"]["link"], "file_id": env["ids"][file], "document_id": env["ids"]["target"]}, actor_id=env["ids"][actor])
        return row.id


def _wait_blocked(env: dict[str, Any], owner_pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        pid = env["report"].get("worker_pid")
        with env["engine"].connect() as reader:
            blockers = reader.scalar(sa.text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}) if pid else []
        if owner_pid in blockers:
            env["report"]["events"].append({"worker_pid": pid, "blocked_by": owner_pid})
            return
        time.sleep(0.02)
    raise AssertionError("Attachment never waited on the independently held deletion row")


@pytest.mark.parametrize("commit_delete", [False, True])
def test_attach_waits_for_delete_and_rechecks_source(attach_rows: dict[str, Any], commit_delete: bool) -> None:
    env, ids = attach_rows, attach_rows["ids"]
    with Session(env["engine"]) as deletion, ThreadPoolExecutor(max_workers=1) as pool:
        owner_pid = deletion.scalar(sa.text("SELECT pg_backend_pid()"))
        deletion.scalar(sa.select(Document).where(Document.id == ids["source"]).with_for_update())
        deletion.scalar(sa.select(Knowledgebase).where(Knowledgebase.id == ids["team"]).with_for_update())
        deletion.scalar(sa.select(File).where(File.id == ids["shared_file"]).with_for_update())
        future = pool.submit(_insert, env)
        try:
            _wait_blocked(env, owner_pid)
            deletion.execute(sa.delete(File2Document).where(File2Document.file_id == ids["shared_file"]))
            deletion.execute(sa.delete(File).where(File.id == ids["shared_file"]))
            deletion.execute(sa.delete(Document).where(Document.id == ids["source"]))
            deletion.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids["team"]).values(doc_num=Knowledgebase.doc_num - 1))
            deletion.commit() if commit_delete else deletion.rollback()
            if commit_delete:
                with pytest.raises(LookupError, match="unavailable"):
                    future.result(10)
            else:
                assert future.result(10) == ids["link"]
        finally:
            deletion.rollback()
    with env["engine"].connect() as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(File2Document).where(File2Document.id == ids["link"])) == (0 if commit_delete else 1)
        assert reader.scalar(sa.select(File.id).where(File.id == ids["shared_file"])) == (None if commit_delete else ids["shared_file"])
        assert reader.scalar(sa.select(Knowledgebase.doc_num).where(Knowledgebase.id == ids["team"])) == (1 if commit_delete else 2)
        assert reader.scalar(sa.select(sa.func.count()).select_from(Document).where(Document.kb_id == ids["team"])) == (1 if commit_delete else 2)
    env["report"]["delete_committed"] = commit_delete


@pytest.mark.parametrize("change", ["tenant", "location", "membership", "actor", "actor_tenant", "document"])
def test_waiting_attach_rejects_changed_source_or_authorization(attach_rows: dict[str, Any], change: str) -> None:
    env, ids = attach_rows, attach_rows["ids"]
    with Session(env["engine"]) as mutation, ThreadPoolExecutor(max_workers=1) as pool:
        pid = mutation.scalar(sa.text("SELECT pg_backend_pid()"))
        if change == "document":
            mutation.scalar(sa.select(Document).where(Document.id == ids["target"]).with_for_update())
        else:
            mutation.scalar(sa.select(File).where(File.id == ids["shared_file"]).with_for_update())
        future = pool.submit(_insert, env)
        try:
            _wait_blocked(env, pid)
            if change in {"tenant", "location"}:
                mutation.execute(sa.update(File).where(File.id == ids["shared_file"]).values(**({"tenant_id": ids["other"]} if change == "tenant" else {"location": "replaced.txt"})))
            elif change == "membership":
                mutation.execute(sa.update(UserTenant).where(UserTenant.user_id == ids["member"], UserTenant.tenant_id == ids["owner"]).values(status="0"))
            elif change == "actor":
                mutation.execute(sa.update(User).where(User.id == ids["member"]).values(is_active=False))
            elif change == "actor_tenant":
                mutation.execute(sa.update(Tenant).where(Tenant.id == ids["member"]).values(status="0"))
            else:
                mutation.execute(sa.update(Document).where(Document.id == ids["target"]).values(kb_id=ids["foreign"]))
            mutation.commit()
            with pytest.raises(PermissionError):
                future.result(10)
        finally:
            mutation.rollback()
    with env["engine"].connect() as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(File2Document).where(File2Document.id == ids["link"])) == 0


def test_retry_reacquires_file_lock_before_insert(attach_rows: dict[str, Any]) -> None:
    env, ids = attach_rows, attach_rows["ids"]
    failed = threading.Event()

    class RetrySession(Session):
        def commit(self) -> None:
            if not failed.is_set():
                failed.set()
                raise OperationalError("controlled attachment commit", {}, OSError("controlled transport failure"))
            super().commit()

    def record_backend(session: Session, transaction: Any, connection: sa.Connection) -> None:
        env["report"]["worker_pid"] = connection.scalar(sa.text("SELECT pg_backend_pid()"))

    sa.event.listen(RetrySession, "after_begin", record_backend)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_insert, env, session_type=RetrySession)
        assert failed.wait(10)
        with Session(env["engine"]) as deletion:
            pid = deletion.scalar(sa.text("SELECT pg_backend_pid()"))
            deletion.scalar(sa.select(Knowledgebase).where(Knowledgebase.id == ids["team"]).with_for_update())
            deletion.scalar(sa.select(File).where(File.id == ids["shared_file"]).with_for_update())
            try:
                _wait_blocked(env, pid)
                deletion.execute(sa.delete(File2Document).where(File2Document.file_id == ids["shared_file"]))
                deletion.execute(sa.delete(File).where(File.id == ids["shared_file"]))
                deletion.commit()
                with pytest.raises(LookupError, match="unavailable"):
                    future.result(10)
            finally:
                deletion.rollback()
    with env["engine"].connect() as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(File2Document).where(File2Document.id == ids["link"])) == 0


def test_active_callers_keep_personal_file_to_team_dataset(attach_rows: dict[str, Any]) -> None:
    env, ids = attach_rows, attach_rows["ids"]
    with Session(env["engine"]) as db:
        convert_files(db, [ids["member_file"]], [ids["team"]], ids["member"], "add")
        doc = db.scalar(sa.select(Document).join(File2Document, File2Document.document_id == Document.id).where(File2Document.file_id == ids["member_file"]))
        assert doc is not None and doc.kb_id == ids["team"] and doc.created_by == ids["member"]
        FileService.add_file_from_kb(db, db.get(Document, ids["target"]).to_dict(), ids["owner"], ids["owner"])
    with env["engine"].connect() as reader:
        rows = reader.execute(
            sa.select(File.tenant_id, Knowledgebase.tenant_id)
            .select_from(File2Document)
            .join(File, File.id == File2Document.file_id)
            .join(Document, Document.id == File2Document.document_id)
            .join(Knowledgebase, Knowledgebase.id == Document.kb_id)
            .where(File.id == ids["member_file"])
        ).all()
        assert rows == [(ids["member"], ids["owner"])]
        assert reader.scalar(sa.select(sa.func.count()).select_from(File2Document).where(File2Document.document_id == ids["target"])) == 1


@pytest.mark.parametrize("target", ["foreign_file", "missing_file"])
def test_foreign_or_missing_file_denial_does_not_insert(attach_rows: dict[str, Any], target: str) -> None:
    env = attach_rows
    if target == "missing_file":
        env["ids"][target] = uuid4().hex
    with pytest.raises((PermissionError, LookupError)):
        _insert(env, file=target)
    with env["engine"].connect() as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(File2Document).where(File2Document.id == env["ids"]["link"])) == 0


@pytest.mark.parametrize("producer", ["convert", "file_from_kb"])
def test_active_producer_failure_rolls_back_only_new_rows_and_counter(attach_rows: dict[str, Any], producer: str) -> None:
    env, ids = attach_rows, attach_rows["ids"]

    def snapshot() -> dict[str, list[dict[str, Any]]]:
        with env["engine"].connect() as reader:
            return {model.__tablename__: [dict(row) for row in reader.execute(sa.select(model.__table__).order_by(model.id)).mappings()] for model in [Document, File, File2Document, Knowledgebase]}

    before = snapshot()
    fired = False

    def fail_mapping(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        nonlocal fired
        if getattr(context, "isinsert", False) and context.compiled.statement.table.name == File2Document.__tablename__:
            fired = True
            raise RuntimeError("Controlled mapping write failure")

    sa.event.listen(env["engine"], "before_cursor_execute", fail_mapping)
    try:
        with Session(env["engine"]) as db, pytest.raises(RuntimeError, match="Controlled mapping write failure"):
            if producer == "convert":
                convert_files(db, [ids["member_file"]], [ids["team"]], ids["member"], "add")
            else:
                FileService.add_file_from_kb(db, db.get(Document, ids["target"]).to_dict(), ids["owner"], ids["owner"])
    finally:
        sa.event.remove(env["engine"], "before_cursor_execute", fail_mapping)
    assert fired and snapshot() == before
    env["report"]["events"].append({"producer": producer, "unchanged_existing_and_no_new_rows": True})


def test_convert_waiting_for_shared_source_delete_has_no_orphan_or_count_drift(attach_rows: dict[str, Any]) -> None:
    env, ids = attach_rows, attach_rows["ids"]

    def convert() -> None:
        with Session(env["engine"]) as db:
            env["report"]["worker_pid"] = db.scalar(sa.text("SELECT pg_backend_pid()"))
            convert_files(db, [ids["shared_file"]], [ids["private"]], ids["member"], "add")

    with Session(env["engine"]) as deletion, ThreadPoolExecutor(max_workers=1) as pool:
        owner_pid = deletion.scalar(sa.text("SELECT pg_backend_pid()"))
        deletion.scalar(sa.select(Document).where(Document.id == ids["source"]).with_for_update())
        deletion.scalar(sa.select(Knowledgebase).where(Knowledgebase.id == ids["team"]).with_for_update())
        deletion.scalar(sa.select(File).where(File.id == ids["shared_file"]).with_for_update())
        future = pool.submit(convert)
        try:
            _wait_blocked(env, owner_pid)
            deletion.execute(sa.delete(File2Document).where(File2Document.file_id == ids["shared_file"]))
            deletion.execute(sa.delete(File).where(File.id == ids["shared_file"]))
            deletion.execute(sa.delete(Document).where(Document.id == ids["source"]))
            deletion.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids["team"]).values(doc_num=Knowledgebase.doc_num - 1))
            deletion.commit()
            with pytest.raises(LookupError):
                future.result(10)
        finally:
            deletion.rollback()
    with env["engine"].connect() as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(Document).where(Document.kb_id == ids["private"])) == 0
        assert reader.scalar(sa.select(Knowledgebase.doc_num).where(Knowledgebase.id == ids["private"])) == 0
        assert reader.scalar(sa.select(Knowledgebase.doc_num).where(Knowledgebase.id == ids["team"])) == 1
        assert reader.scalar(sa.select(sa.func.count()).select_from(File2Document)) == 0
    env["report"]["events"].append({"conversion_failed_without_new_document_or_count_drift": True})


def test_existing_mapping_noop_rechecks_actor_and_preserves_all_rows(attach_rows: dict[str, Any]) -> None:
    env, ids = attach_rows, attach_rows["ids"]

    def snapshot() -> dict[str, list[dict[str, Any]]]:
        with env["engine"].connect() as reader:
            return {model.__tablename__: [dict(row) for row in reader.execute(sa.select(model.__table__).order_by(model.id)).mappings()] for model in [Document, File, File2Document, Knowledgebase]}

    before = snapshot()
    with Session(env["engine"]) as db, pytest.raises(PermissionError):
        File2DocumentService.create_file_for_document(db, ids["source"], ids["owner"], actor_id=ids["other"])
    assert snapshot() == before
    with Session(env["engine"]) as db:
        assert File2DocumentService.create_file_for_document(db, ids["source"], ids["owner"], actor_id=ids["owner"]) is None
    assert snapshot() == before


@pytest.mark.parametrize("actor", ["owner", "member"])
def test_kb_file_creation_preserves_team_actor_and_owner(attach_rows: dict[str, Any], actor: str) -> None:
    env, ids = attach_rows, attach_rows["ids"]
    with Session(env["engine"]) as db:
        FileService.add_file_from_kb(db, db.get(Document, ids["target"]).to_dict(), ids["owner"], ids["owner"], actor_id=ids[actor])
    with env["engine"].connect() as reader:
        row = reader.execute(
            sa.select(File.tenant_id, File.created_by, File2Document.document_id)
            .select_from(File2Document)
            .join(File, File.id == File2Document.file_id)
            .where(File2Document.document_id == ids["target"])
        ).one()
        assert row == (ids["owner"], ids["owner"], ids["target"])


@pytest.mark.parametrize("revocation", ["membership", "user"])
def test_team_actor_revocation_does_not_borrow_owner_identity(attach_rows: dict[str, Any], revocation: str) -> None:
    env, ids = attach_rows, attach_rows["ids"]
    with Session(env["engine"]) as db:
        doc = db.get(Document, ids["target"]).to_dict()
        if revocation == "membership":
            db.execute(sa.update(UserTenant).where(UserTenant.user_id == ids["member"], UserTenant.tenant_id == ids["owner"]).values(status="0"))
        else:
            db.execute(sa.update(User).where(User.id == ids["member"]).values(is_active=False))
        db.commit()
        before = db.scalar(sa.select(sa.func.count()).select_from(File))
        with pytest.raises(PermissionError):
            FileService.add_file_from_kb(db, doc, ids["owner"], ids["owner"], actor_id=ids["member"])
    with env["engine"].connect() as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(File)) == before
        assert reader.scalar(sa.select(sa.func.count()).select_from(File2Document).where(File2Document.document_id == ids["target"])) == 0


def test_team_dataset_does_not_grant_an_unshared_existing_owner_file(attach_rows: dict[str, Any]) -> None:
    env, ids = attach_rows, attach_rows["ids"]
    with Session(env["engine"]) as db:
        db.execute(sa.delete(File2Document).where(File2Document.id == ids["existing"]))
        db.commit()
    with pytest.raises(PermissionError):
        _insert(env)
    with env["engine"].connect() as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(File2Document)) == 0


def test_concurrent_actual_converters_keep_one_pair_and_one_count(attach_rows: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, ids = attach_rows, attach_rows["ids"]
    barrier = threading.Barrier(2)
    original = File2DocumentService.get_by_file_id
    pids: dict[int, int] = {}

    def synchronized_lookup(cls: Any, db: Session, file_id: str) -> Any:
        rows = original(db, file_id)
        if file_id == ids["member_file"]:
            assert not rows
            barrier.wait(timeout=10)
        return rows

    monkeypatch.setattr(File2DocumentService, "get_by_file_id", classmethod(synchronized_lookup))

    def convert(index: int) -> None:
        with Session(env["engine"]) as db:
            pids[index] = db.scalar(sa.text("SELECT pg_backend_pid()"))
            convert_files(db, [ids["member_file"]], [ids["team"]], ids["member"], "add")

    with Session(env["engine"]) as holder, ThreadPoolExecutor(max_workers=2) as pool:
        owner_pid = holder.scalar(sa.text("SELECT pg_backend_pid()"))
        holder.scalar(sa.select(Knowledgebase).where(Knowledgebase.id == ids["team"]).with_for_update())
        jobs = [pool.submit(convert, index) for index in range(2)]
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with env["engine"].connect() as reader:
                    blockers = {pid: reader.scalar(sa.text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}) for pid in list(pids.values())}
                    states = {pid: reader.execute(sa.text("SELECT wait_event_type, query FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid}).one() for pid in blockers}

                def waits_for_holder(pid: int, visited: frozenset[int] = frozenset()) -> bool:
                    if pid == owner_pid:
                        return True
                    return pid not in visited and any(waits_for_holder(other, visited | {pid}) for other in blockers.get(pid, []))

                # PostgreSQL may report the earlier waiter as a soft blocker.
                # Prove that each complete wait chain reaches our held KB row.
                if len(blockers) == 2 and all(waits_for_holder(pid) and states[pid][0] == "Lock" and "t_ai_knowledgebases" in states[pid][1] for pid in blockers):
                    env["report"]["events"].append({"converters": pids, "both_wait_chains_reach_holder": owner_pid, "blockers": blockers})
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("Both actual converters did not reach the shared KB lock")
        finally:
            holder.rollback()
        for job in jobs:
            job.result(10)
    with env["engine"].connect() as reader:
        rows = reader.execute(
            sa.select(Document.id).join(File2Document, File2Document.document_id == Document.id).where(File2Document.file_id == ids["member_file"], Document.kb_id == ids["team"])
        ).all()
        assert len(rows) == 1
        assert reader.scalar(sa.select(sa.func.count()).select_from(Document).where(Document.kb_id == ids["team"])) == 3
        assert reader.scalar(sa.select(Knowledgebase.doc_num).where(Knowledgebase.id == ids["team"])) == 3
    env["report"]["events"].append({"one_new_document": True, "one_new_pair": True, "counter_increment": 1, "loser_placeholder_absent": True})

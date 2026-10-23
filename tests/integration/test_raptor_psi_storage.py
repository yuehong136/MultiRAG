"""Real SQL/Milvus migration readback with controlled model and failure seams."""

import copy
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, Task, UserTenant
from common.doc_store.document_history import document_history
from common.exceptions import TaskCanceledException
from core.raptor import RecursiveAbstractiveProcessing4TreeOrganizedRetrieval as Raptor
from core.svr import task_executor
from core.utils.raptor_migration import summary_rows
from tests.support.raptor_storage import raptor_storage as raptor_storage


@pytest.fixture
def migration(raptor_storage: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    env = raptor_storage
    dataset, other, tenant, doc = (uuid4().hex for _ in range(4))
    with Session(env["engine"]) as db:
        db.add(Knowledgebase(id=dataset, name="g_owned", tenant_id=tenant, created_by=tenant, embd_id="controlled"))
        db.add(Document(id=doc, kb_id=dataset, name="source.txt", type="txt", parser_id="naive", created_by=tenant))
        db.commit()
    monkeypatch.setattr(task_executor, "db_connection", lambda: Session(env["engine"]))
    monkeypatch.setattr(task_executor.search, "index_name_one", lambda *args: env["collection"])
    monkeypatch.setitem(vars(task_executor.settings), "docStoreConn", env["store"])
    monkeypatch.setattr(task_executor, "has_canceled", lambda task_id: False)
    env.update(dataset=dataset, other=other, tenant=tenant, doc=doc, calls={"llm": 0, "embedding": 0})

    async def chat(self: Raptor, *args: Any) -> str:
        env["calls"]["llm"] += 1
        return f"summary-{env['calls']['llm']}"

    async def embedding(self: Raptor, text: str) -> np.ndarray:
        env["calls"]["embedding"] += 1
        return np.array([0.1, 0.2])

    monkeypatch.setattr(Raptor, "_check_task_canceled", lambda *args: None)
    monkeypatch.setattr(Raptor, "_chat", chat)
    monkeypatch.setattr(Raptor, "_embedding_encode", embedding)
    for n in range(2):
        _insert(env, {"id": f"{doc}-{n}", "doc_id": doc, "kb_id": dataset, "content_with_weight": f"source {n}", "q_2_vec": [float(n), float(1 - n)], "chunk_order_int": n})
    _insert(env, {"id": f"{doc}-legacy", "doc_id": doc, "kb_id": dataset, "content_with_weight": "legacy", "raptor_kwd": "raptor", "q_2_vec": [1.0, 0.0]})
    _insert(env, {"id": f"{doc}-other", "doc_id": doc, "kb_id": other, "content_with_weight": "other KB", "raptor_kwd": "raptor", "q_2_vec": [0.0, 1.0]}, dataset=other)
    return env


def _insert(env: dict[str, Any], row: dict[str, Any], dataset: str | None = None) -> None:
    data = {"pk": row["id"], "raptor_kwd": "", "raptor_layer_int": 0, "extra": {}, **row}
    assert env["store"].insert([data], env["collection"], dataset or env["dataset"]) == []


def _read(env: dict[str, Any], dataset: str | None = None, doc: str | None = None) -> list[dict[str, Any]]:
    # Separate native client is independent of the writer connector.
    rows = env["reader"].query(
        collection_name=env["collection"], filter=f'kb_id == "{dataset or env["dataset"]}" && doc_id == "{doc or env["doc"]}"', output_fields=["*", "q_2_vec"], consistency_level="Strong"
    )
    return sorted(rows, key=lambda row: row["pk"])


async def _generate(env: dict[str, Any], builder: str, scope: str = "file") -> tuple[str, list[dict[str, Any]], dict[str, dict[str, dict[str, Any]]]]:
    task = uuid4().hex
    with Session(env["engine"]) as db:
        kb = db.get(Knowledgebase, env["dataset"])
        kb.raptor_task_id = task
        db.add(Task(id=task, doc_id="graph_raptor_x", task_type="raptor"))
        db.commit()
        chunks, _, previous = await task_executor.run_raptor_for_kb(
            {"id": task, "tenant_id": env["tenant"], "kb_id": env["dataset"], "name": "owned KB", "pagerank": 0},
            {"raptor": {"scope": scope, "prompt": "{cluster_content}", "max_token": 32, "threshold": 0.1, "random_seed": 0, "ext": {"tree_builder": builder}}},
            SimpleNamespace(max_length=4096),
            None,
            2,
            doc_ids=[env["doc"]],
            db=db,
        )
    for chunk in chunks:
        chunk["id"] = chunk["pk"]
        chunk["kb_id"] = env["dataset"]
    return task, chunks, previous


def _commit(env: dict[str, Any], task: str, chunks: list[dict[str, Any]], previous: dict[str, dict[str, dict[str, Any]]]) -> None:
    task_executor._commit_raptor_summaries(env["engine"], task, env["tenant"], env["dataset"], env["collection"], chunks, previous)


async def test_legacy_psi_raptor_and_scope_migration(migration: dict[str, Any]) -> None:
    env = migration
    normal = [row for row in _read(env) if not row["raptor_kwd"]]
    other = _read(env, env["other"])
    for builder, scope in [("psi", "file"), ("raptor", "file"), ("psi", "dataset"), ("raptor", "file")]:
        task, chunks, previous = await _generate(env, builder, scope)
        assert len(chunks) == 1
        _commit(env, task, chunks, previous)
        source = _read(env)
        assert [row for row in source if not row["raptor_kwd"]] == normal
        assert _read(env, env["other"]) == other
        location = env["doc"] if scope == "file" else "graph_raptor_x"
        target = _read(env, doc=location)
        summaries = [row for row in target if row["raptor_kwd"]]
        assert len(summaries) == 1 and summaries[0]["extra"]["raptor_method"] == builder
        assert len(summary_rows(document_history(env["store"], env["collection"], env["dataset"], "graph_raptor_x"))) == (scope == "dataset")
        env["record"]["readbacks"].append({"builder": builder, "scope": scope, "rows": target, "calls": dict(env["calls"])})
        env["save"]()
    assert env["calls"] == {"llm": 4, "embedding": 4}
    # Same method checkpoint makes no additional paid calls.
    _, chunks, previous = await _generate(env, "raptor")
    assert chunks == [] and previous == {} and env["calls"]["llm"] == 4


@pytest.mark.parametrize("failure", ["partial-insert", "cancel-after-insert", "delete-receipt"])
async def test_failed_replacement_preserves_history(migration: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    env = migration
    before = copy.deepcopy(_read(env))
    other = copy.deepcopy(_read(env, env["other"]))
    task, chunks, previous = await _generate(env, "psi")
    original_insert, original_delete = env["store"].insert, env["store"].delete
    injected = False

    def insert(*args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        receipt = original_insert(*args, **kwargs)
        if not injected:
            injected = True
            if failure == "partial-insert":
                return ["controlled partial write"]
        return receipt

    def delete(*args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        receipt = original_delete(*args, **kwargs)
        if failure == "delete-receipt" and not injected:
            injected = True
            return None
        return receipt

    if failure == "delete-receipt":
        monkeypatch.setattr(env["store"], "delete", delete)
    else:
        monkeypatch.setattr(env["store"], "insert", insert)
    if failure == "cancel-after-insert":
        monkeypatch.setattr(task_executor, "has_canceled", lambda task_id: injected)
    with pytest.raises((RuntimeError, TaskCanceledException)):
        _commit(env, task, chunks, previous)
    assert _read(env) == before and _read(env, env["other"]) == other
    env["record"]["readbacks"].append({"fault": failure, "preserved": True, "rows": before})
    env["save"]()


async def test_sql_commit_failure_restores_previous_tree(migration: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = migration
    before = copy.deepcopy(_read(env))
    task, chunks, previous = await _generate(env, "psi")
    commit = Session.commit
    injected = False

    def fail_once(db: Session) -> None:
        nonlocal injected
        if not injected:
            injected = True
            raise RuntimeError("controlled SQL commit failure")
        commit(db)

    monkeypatch.setattr(Session, "commit", fail_once)
    with pytest.raises(RuntimeError, match="controlled SQL"):
        _commit(env, task, chunks, previous)
    assert _read(env) == before


async def test_async_cancel_drains_native_replacement(migration: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import threading

    env = migration
    before = copy.deepcopy(_read(env))
    task, chunks, previous = await _generate(env, "psi")
    entered, release = threading.Event(), threading.Event()
    original = env["store"].insert

    def blocked(*args: Any, **kwargs: Any) -> Any:
        receipt = original(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return receipt

    monkeypatch.setattr(env["store"], "insert", blocked)
    pending = asyncio.create_task(task_executor._write_raptor_summaries(env["engine"], task, env["tenant"], env["dataset"], env["collection"], chunks, previous))
    assert await asyncio.to_thread(entered.wait, 10)
    pending.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert _read(env) == before


@pytest.mark.parametrize("commit_mode", ["failed", "lost-reply"])
async def test_sql_recovery_conflict_preserves_same_id_winner(migration: dict[str, Any], monkeypatch: pytest.MonkeyPatch, commit_mode: str) -> None:
    env = migration
    ordinary = [row for row in _read(env) if not row["raptor_kwd"]]
    foreign = _read(env, env["other"])
    task, chunks, previous = await _generate(env, "psi")
    commit = Session.commit
    injected = False
    winner = {**copy.deepcopy(chunks[0]), "content_with_weight": "later native winner", "q_2_vec": [0.3, 0.4], "extra": {"raptor_method": "psi", "winner": True}}

    def fail_after_native(db: Session) -> None:
        nonlocal injected
        if not injected:
            injected = True
            if commit_mode == "lost-reply":
                commit(db)
            assert env["store"].insert([winner], env["collection"], env["dataset"]) == []
            raise RuntimeError("controlled SQL failure with later native edit")
        commit(db)

    monkeypatch.setattr(Session, "commit", fail_after_native)
    with pytest.raises(RuntimeError, match="recovery conflict"):
        _commit(env, task, chunks, previous)
    after = _read(env)
    summaries = [row for row in after if row["raptor_kwd"]]
    assert len(summaries) == 1
    assert summaries[0]["pk"] == winner["id"]
    assert summaries[0]["content_with_weight"] == "later native winner"
    assert summaries[0]["extra"] == winner["extra"]
    np.testing.assert_array_equal(np.asarray(summaries[0]["q_2_vec"], dtype=np.float32), np.asarray(winner["q_2_vec"], dtype=np.float32))
    assert [row for row in after if not row["raptor_kwd"]] == ordinary
    assert _read(env, env["other"]) == foreign
    env["record"]["readbacks"].append({"fault": f"sql-{commit_mode}-same-id-winner", "preserved": True, "rows": after})
    env["save"]()


async def test_clear_waits_for_canceled_writer_without_resurrection(migration: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import threading

    from api.apps.services import dataset_api_service

    env = migration
    with Session(env["engine"]) as db:
        db.add(UserTenant(id=uuid4().hex, tenant_id=env["tenant"], user_id=env["tenant"], role="owner", invited_by=env["tenant"], status="1"))
        db.commit()
    ordinary = [row for row in _read(env) if not row["raptor_kwd"]]
    foreign = _read(env, env["other"])
    task, chunks, previous = await _generate(env, "psi")
    entered, release, cancel_signal = threading.Event(), threading.Event(), threading.Event()
    original = env["store"].insert

    def blocked(*args: Any, **kwargs: Any) -> Any:
        receipt = original(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return receipt

    def cancel(key: str, value: str) -> bool:
        assert key == f"{task}-cancel"
        cancel_signal.set()
        return True

    monkeypatch.setattr(env["store"], "insert", blocked)
    monkeypatch.setattr(task_executor, "has_canceled", lambda task_id: cancel_signal.is_set())
    monkeypatch.setattr(dataset_api_service, "REDIS_CONN", SimpleNamespace(set=cancel))

    def clear() -> tuple[bool, Any]:
        with Session(env["engine"]) as db:
            return dataset_api_service.delete_index(db, env["tenant"], env["dataset"], "raptor", expected_task_id=task)

    pending = asyncio.create_task(task_executor._write_raptor_summaries(env["engine"], task, env["tenant"], env["dataset"], env["collection"], chunks, previous))
    assert await asyncio.to_thread(entered.wait, 10)
    clearing = asyncio.create_task(asyncio.to_thread(clear))
    signaled = await asyncio.to_thread(cancel_signal.wait, 10)
    if not signaled:
        release.set()
        await asyncio.gather(pending, clearing, return_exceptions=True)
        pytest.fail("clear did not signal the owned task")
    await asyncio.sleep(0.05)
    blocked_by_writer = not clearing.done()
    release.set()
    assert blocked_by_writer
    with pytest.raises(TaskCanceledException):
        await pending
    assert (await clearing)[0] is True
    assert _read(env) == ordinary and _read(env, env["other"]) == foreign
    with Session(env["engine"]) as db:
        assert db.get(Task, task) is None
        assert not db.get(Knowledgebase, env["dataset"]).raptor_task_id
    env["record"]["readbacks"].append({"fault": "clear-during-native-write", "no_resurrection": True, "rows": _read(env)})
    env["save"]()


async def test_clear_invalid_receipt_keeps_task_binding(migration: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import dataset_api_service

    env = migration
    task, _, _ = await _generate(env, "psi")
    with Session(env["engine"]) as db:
        db.add(UserTenant(id=uuid4().hex, tenant_id=env["tenant"], user_id=env["tenant"], role="owner", invited_by=env["tenant"], status="1"))
        db.commit()
    before = _read(env)
    monkeypatch.setattr(dataset_api_service, "REDIS_CONN", SimpleNamespace(set=lambda *args: True))
    monkeypatch.setattr(env["store"], "delete", lambda *args, **kwargs: None)
    with Session(env["engine"]) as db:
        success, message = dataset_api_service.delete_index(db, env["tenant"], env["dataset"], "raptor", expected_task_id=task)
        assert not success and "acknowledged" in message
    with Session(env["engine"]) as db:
        assert db.get(Task, task) is not None
        assert db.get(Knowledgebase, env["dataset"]).raptor_task_id == task
    assert _read(env) == before

"""Owned Memory schema mutations on real Milvus 3.x, with independent Strong reads."""

import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from itertools import count
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
from pymilvus import Collection, MilvusClient, connections
from pymilvus.exceptions import MilvusException

from common.config_utils import CONFIGS
from common.doc_store.milvus_capabilities import supports_verified_milvus_3
from memory.utils import milvus_conn


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"Unexpected Milvus readback type: {type(value).__name__}")


@pytest.fixture(scope="module")
def memory_collection(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    config = CONFIGS["milvus"]
    options = {
        "uri": config["hosts"],
        "user": config.get("username", ""),
        "password": config.get("password", ""),
        "db_name": config.get("db_name") or "default",
        "token": config.get("token", ""),
        "timeout": 30,
    }
    identifier = uuid4().hex
    index, bank, alias = "memory_safe_" + identifier, "bank" + identifier, "memory_safe_writer_" + identifier
    name = f"{index}_{bank}"
    configured = request.config.getoption("--test-report-dir")
    report_dir = Path(configured) if configured else tmp_path_factory.mktemp("memory-milvus-mutations")
    record_path = report_dir / (name + ".json")
    record: dict[str, Any] = {"collection": name, "collection_absent_after_cleanup": False, "checks": [], "readbacks": []}

    def save() -> None:
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=_json_default) + "\n")
        record_path.chmod(0o600)

    save()
    with ExitStack() as cleanup:
        reader = MilvusClient(**options)
        cleanup.callback(reader.close)

        def remove() -> None:
            if reader.has_collection(name):
                reader.drop_collection(name)
            assert not reader.has_collection(name)
            record["collection_absent_after_cleanup"] = True
            save()

        cleanup.callback(remove)
        cleanup.callback(connections.disconnect, alias)
        connections.connect(alias=alias, **options)
        cls = next(cell.cell_contents for cell in milvus_conn.MilvusConnection.__closure__ or [] if isinstance(cell.cell_contents, type))
        store = object.__new__(cls)
        store._using = alias
        store.logger = logging.getLogger("test.memory.milvus_mutations")
        assert supports_verified_milvus_3(alias), "This suite requires a verified stable Milvus 3.x target"
        store.create_idx(index, bank, 2)
        record["server_version"] = reader.get_server_version()
        save()
        yield {"store": store, "reader": reader, "collection": name, "index": index, "bank": bank, "record": record, "save": save, "ids": count(1)}


def _read(env: dict[str, Any], identity: str) -> dict[str, Any] | None:
    rows = env["reader"].query(env["collection"], filter="id == " + json.dumps(identity), output_fields=["*", "extra_meta", "null_probe"], limit=1, consistency_level="Strong")
    return rows[0] if rows else None


@pytest.fixture
def memory_row(memory_collection: dict[str, Any], request: pytest.FixtureRequest) -> dict[str, Any]:
    env = memory_collection
    message_id = next(env["ids"])
    row = {
        "id": f"{env['bank']}_{message_id}",
        "memory_id": env["bank"],
        "message_id": message_id,
        "message_type": "raw",
        "source_id": 0,
        "user_id": "subject",
        "agent_id": "agent",
        "session_id": "session",
        "valid_at": "2026-11-15 10:00:00",
        "invalid_at": None,
        "forget_at": None,
        "status": True,
        "content": "original synthetic memory",
        "content_embed": [1.0, 0.0],
    }
    storage = env["store"].map_message_to_milvus_fields(row)
    storage.update({"extra_meta": {"keep": ["original", 42]}, "null_probe": "original"})
    ack = env["reader"].upsert(env["collection"], [storage], retry_times=0, retry_on_rate_limit=False)
    assert ack["upsert_count"] == 1
    assert _read(env, row["id"])["extra_meta"] == storage["extra_meta"]
    env["case"] = request.node.nodeid
    return row


def test_full_upsert_and_fallback_update_preserve_fields(memory_collection: dict[str, Any], memory_row: dict[str, Any]) -> None:
    env, row = memory_collection, memory_row
    replacement = {**row, "content": "complete replacement", "content_embed": [0.0, 1.0], "user_id": "updated subject"}
    assert env["store"].insert([replacement], env["index"], env["bank"]) == []
    legacy = env["store"].get(row["id"], env["index"], [env["bank"]])
    assert legacy is not None and legacy["content"] == "complete replacement" and legacy["content_embed"] == [0.0, 1.0]
    observed = _read(env, row["id"])
    assert observed["content_ltks"] == "complete replacement" and observed["q_2_vec"] == [0.0, 1.0]
    for field in ("memory_id", "message_id", "user_id", "agent_id", "session_id", "valid_at"):
        assert observed[field] == replacement[field]
    observed["extra_meta"] = {"preserve": True}
    assert env["reader"].upsert(env["collection"], [observed], retry_times=0, retry_on_rate_limit=False)["upsert_count"] == 1
    before = _read(env, row["id"])
    assert env["store"].update({"message_id": row["message_id"]}, {"content": "full-row correction"}, env["index"], env["bank"])
    assert _read(env, row["id"]) == {**before, "content_ltks": "full-row correction"}
    env["record"]["checks"].append("full upsert and non-partial correction preserve known fields, vector and source dynamic metadata")
    env["record"]["readbacks"].append(_read(env, row["id"]))
    env["save"]()


def test_status_forget_partial_update_and_clear_preserve_unmentioned_fields(memory_collection: dict[str, Any], memory_row: dict[str, Any]) -> None:
    env, row = memory_collection, memory_row
    before = _read(env, row["id"])
    assert env["store"].update({"message_id": row["message_id"]}, {"status": False, "forget_at": "2026-11-16 10:00:00"}, env["index"], env["bank"])
    assert _read(env, row["id"]) == {**before, "status_int": 0, "forget_at": "2026-11-16 10:00:00"}
    assert env["store"].update({"message_id": row["message_id"]}, {"forget_at": None}, env["index"], env["bank"])
    assert _read(env, row["id"]) == {**before, "status_int": 0, "forget_at": ""}
    env["record"]["checks"].append("status/forget native partial and explicit empty-string clear preserve vector/body/dynamic metadata")
    env["save"]()


def test_rejected_insert_and_full_update_leave_original_row(memory_collection: dict[str, Any], memory_row: dict[str, Any]) -> None:
    env, row = memory_collection, memory_row
    before = _read(env, row["id"])
    assert env["store"].insert([{**row, "content_embed": [1.0, 0.0, 0.5]}], env["index"], env["bank"])
    assert _read(env, row["id"]) == before
    assert not env["store"].update({"message_id": row["message_id"]}, {"content": None}, env["index"], env["bank"])
    assert _read(env, row["id"]) == before
    env["record"]["checks"].append("SDK vector/schema rejection and nonnullable full-row rejection preserve original message")
    env["save"]()


def test_partial_null_and_missing_row_behavior(memory_collection: dict[str, Any], memory_row: dict[str, Any]) -> None:
    env, row = memory_collection, memory_row
    before = _read(env, row["id"])
    assert env["reader"].upsert(env["collection"], [{"id": row["id"], "null_probe": None}], partial_update=True, retry_times=0, retry_on_rate_limit=False)["upsert_count"] == 1
    assert _read(env, row["id"]) == {**before, "null_probe": None}
    before = _read(env, row["id"])
    collection = Collection(env["collection"], using=env["store"]._using)
    with pytest.raises(MilvusException):
        collection.upsert([{"id": row["id"], "content_ltks": None}], partial_update=True, retry_times=0, retry_on_rate_limit=False)
    missing_id = f"{env['bank']}_999999999"
    with pytest.raises(MilvusException):
        collection.upsert([{"id": missing_id, "status_int": 0}], partial_update=True, retry_times=0, retry_on_rate_limit=False)
    assert _read(env, row["id"]) == before and _read(env, missing_id) is None
    assert not env["store"].update({"message_id": 999999999}, {"status": False}, env["index"], env["bank"])
    env["record"]["checks"].append("dynamic NULL works; nonnullable NULL and missing required-row partial reject without phantom rows")
    env["save"]()


def test_concurrent_status_and_forget_patches_both_survive(memory_collection: dict[str, Any], memory_row: dict[str, Any]) -> None:
    env, row = memory_collection, memory_row
    original = _read(env, row["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(5):
            assert env["reader"].upsert(env["collection"], [original], retry_times=0, retry_on_rate_limit=False)["upsert_count"] == 1
            barrier = Barrier(2)

            def patch(values: dict[str, Any]) -> bool:
                barrier.wait(timeout=10)
                return env["store"].update({"message_id": row["message_id"]}, values, env["index"], env["bank"])

            left = pool.submit(patch, {"status": False})
            right = pool.submit(patch, {"forget_at": "2026-11-16 10:00:00"})
            assert left.result(timeout=60) and right.result(timeout=60)
            assert _read(env, row["id"]) == {**original, "status_int": 0, "forget_at": "2026-11-16 10:00:00"}
    env["record"]["checks"].append("five synchronized adapter status/forget patch pairs preserve both fields and all unrelated data")
    env["save"]()


def test_unknown_or_mismatched_write_ack_is_not_replayed(memory_collection: dict[str, Any], memory_row: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, row = memory_collection, memory_row
    collection_factory = milvus_conn.Collection
    calls: list[str] = []
    mode = ["unknown"]

    class UnconfirmedAck:
        def __init__(self, collection: Any) -> None:
            self.collection = collection

        def __getattr__(self, name: str) -> Any:
            return getattr(self.collection, name)

        def upsert(self, rows: list[dict[str, Any]], **kwargs: Any) -> Any:
            calls.append(mode[0])
            self.collection.upsert(rows, **kwargs)
            if mode[0] == "unknown":
                raise TimeoutError("controlled response loss after real upsert")
            return SimpleNamespace(upsert_count=0)

    monkeypatch.setattr(milvus_conn, "Collection", lambda *args, **kwargs: UnconfirmedAck(collection_factory(*args, **kwargs)))
    before = _read(env, row["id"])
    assert not env["store"].update({"message_id": row["message_id"]}, {"status": False}, env["index"], env["bank"])
    assert calls == ["unknown"] and _read(env, row["id"]) == {**before, "status_int": 0}
    mode[0] = "mismatch"
    assert not env["store"].update({"message_id": row["message_id"]}, {"status": True}, env["index"], env["bank"])
    assert calls == ["unknown", "mismatch"] and _read(env, row["id"]) == before
    env["record"]["checks"].append("injected lost response and count mismatch after real upsert return failure with one submission each; committed data retained")
    env["save"]()


def test_forced_legacy_gate_full_upsert_and_failed_readback(memory_collection: dict[str, Any], memory_row: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the safe older-server branch on the isolated 3.x target."""
    env, row = memory_collection, memory_row
    monkeypatch.setattr(milvus_conn, "supports_verified_milvus_3", lambda using: False)
    before = _read(env, row["id"])
    assert env["store"].update({"message_id": row["message_id"]}, {"status": False}, env["index"], env["bank"])
    updated = _read(env, row["id"])
    assert updated == {**before, "status_int": 0}
    assert not env["store"].update({"message_id": row["message_id"]}, {"content": None}, env["index"], env["bank"])
    assert _read(env, row["id"]) == updated
    env["record"]["checks"].append("forced legacy version gate on 3.x uses complete-row status upsert and preserves row after SDK validation failure; not an actual 2.x run")
    env["save"]()

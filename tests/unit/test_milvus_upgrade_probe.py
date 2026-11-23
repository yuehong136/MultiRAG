"""The isolated probe cannot target normal business endpoints or arbitrary names."""

import copy
import subprocess
from typing import Any

import numpy as np
import pytest
from pymilvus.grpc_gen import schema_pb2

from scripts import milvus_clone_stopped_volumes as cold_copy
from scripts.milvus_clone_stopped_volumes import validate_projects
from scripts.milvus_upgrade_probe import _digest, semantic_schema, validate_target


@pytest.mark.parametrize("uri", ["http://127.0.0.1:19530", "http://remote.example:29530", "http://user:secret@localhost:29530", "http://localhost:29530/path", "http://localhost:29530?x=1"])
def test_rejects_nonisolated_endpoints(uri: str) -> None:
    with pytest.raises(ValueError):
        validate_target(uri, "upgrade_rehearsal_test")


@pytest.mark.parametrize("prefix", ["business", "upgrade_rehearsal_", "upgrade_rehearsal_test/other", "upgrade_rehearsal_test;drop"])
def test_rejects_unowned_collection_names(prefix: str) -> None:
    with pytest.raises(ValueError):
        validate_target("http://127.0.0.1:29530", prefix)


def test_allows_explicit_isolated_target_and_stable_float32_digest() -> None:
    validate_target("http://127.0.0.1:29530", "upgrade_rehearsal_test")
    assert _digest({"vector": [np.float32(0.5)], "id": np.int64(3), "status": True}) == _digest({"status": True, "id": 3, "vector": [0.5]})
    assert _digest({"vector": [np.float32(0.1)]}) != _digest({"vector": [0.1]})


@pytest.mark.parametrize(
    "source,target", [("docker", "multirag-milvus-upgrade-target"), ("multirag-milvus-upgrade-source", "docker"), ("multirag-milvus-upgrade-source", "multirag-milvus-upgrade-source")]
)
def test_clone_rejects_business_or_identical_projects(source: str, target: str) -> None:
    with pytest.raises(ValueError):
        validate_projects(source, target)


def test_clone_rejects_live_source_before_creating_a_target(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = "multirag-milvus-upgrade-source", "multirag-milvus-upgrade-target"
    commands: list[tuple[str, ...]] = []

    def docker(*args: str) -> str:
        commands.append(args)
        if args[:2] == ("volume", "ls"):
            return "\n".join(f"{source}_{suffix}" for suffix in ("etcd_data", "minio_data", "milvus_data"))
        if args[:2] == ("volume", "inspect"):
            return '[{"Labels":{"com.docker.compose.project":"multirag-milvus-upgrade-source","com.docker.compose.volume":"etcd_data"}}]'
        if args[0] == "ps":
            return "running-source-container"
        raise AssertionError("A live source must fail before any target creation or copy")

    monkeypatch.setattr(cold_copy, "_docker", docker)
    with pytest.raises(ValueError, match="Stop every container"):
        cold_copy.clone(source, target)
    assert not any(command[0] == "run" or command[:2] == ("volume", "create") for command in commands)


def test_clone_refuses_an_existing_target_before_any_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = "multirag-milvus-upgrade-source", "multirag-milvus-upgrade-target"

    def docker(*args: str) -> str:
        if args[:2] != ("volume", "ls"):
            raise AssertionError("Existing targets must fail before inspection or mutation")
        return "\n".join([*(f"{source}_{suffix}" for suffix in ("etcd_data", "minio_data", "milvus_data")), f"{target}_etcd_data"])

    monkeypatch.setattr(cold_copy, "_docker", docker)
    with pytest.raises(ValueError, match="every target volume must be new"):
        cold_copy.clone(source, target)


def test_cold_manifest_hash_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_hash(*args: str) -> str:
        assert args[-1] == "cd /volume && find . -type f -exec sha256sum {} +"
        raise subprocess.CalledProcessError(1, ["docker", *args], stderr="unreadable source file")

    monkeypatch.setattr(cold_copy, "_docker", fail_hash)
    with pytest.raises(subprocess.CalledProcessError):
        cold_copy._digest("multirag-milvus-upgrade-source_milvus_data")


def _schema_fixture() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return (
        {
            "auto_id": False,
            "enable_dynamic_field": True,
            "fields": [
                {"name": "id", "type": 21, "is_primary": True, "params": {"max_length": 256}},
                {"name": "content", "type": 21, "params": {"max_length": 2048, "enable_analyzer": True}},
                {"name": "sparse", "type": 104, "is_function_output": True},
                {"name": "counter", "type": 5},
            ],
            "functions": [{"name": "bm25", "type": 1, "input_field_names": ["content"], "output_field_names": ["sparse"], "params": {}}],
        },
        [{"field": "sparse", "index_name": "sparse", "index_param": {"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "BM25"}}],
    )


@pytest.mark.parametrize("change", ["primary", "auto_id", "nullable", "default_zero", "dynamic", "function", "index"])
def test_semantic_schema_digest_detects_metadata_changes(change: str) -> None:
    schema, indexes = _schema_fixture()
    original = _digest(semantic_schema(schema, indexes))
    changed, changed_indexes = copy.deepcopy(schema), copy.deepcopy(indexes)
    if change == "primary":
        changed["fields"][0]["is_primary"] = False
    elif change == "auto_id":
        changed["auto_id"] = True
    elif change == "nullable":
        changed["fields"][1]["nullable"] = True
    elif change == "default_zero":
        changed["fields"][3]["default_value"] = schema_pb2.ValueField(long_data=0)
    elif change == "dynamic":
        changed["enable_dynamic_field"] = False
    elif change == "function":
        changed["functions"][0]["params"] = {"changed": "true"}
    elif change == "index":
        changed_indexes[0]["index_param"]["metric_type"] = "IP"
    assert _digest(semantic_schema(changed, changed_indexes)) != original


def test_semantic_schema_keeps_typed_zero_default_and_ignores_only_physical_ids() -> None:
    schema, indexes = _schema_fixture()
    schema["fields"][3]["default_value"] = schema_pb2.ValueField(long_data=0)
    contract = semantic_schema(schema, indexes)
    assert contract["fields"][3]["default_value"] == {"long_data": "0"}
    schema["fields"][0]["field_id"] = 100
    schema["functions"][0]["id"] = 100
    schema["functions"][0]["input_field_ids"] = [101]
    indexes[0]["index_id"] = 999
    assert semantic_schema(schema, indexes) == contract


def test_semantic_schema_normalizes_old_sdk_protobuf_repeated_names() -> None:
    schema, indexes = _schema_fixture()
    original = semantic_schema(schema, indexes)
    schema["functions"][0]["input_field_names"] = schema_pb2.FunctionSchema(input_field_names=["content"]).input_field_names
    assert semantic_schema(schema, indexes) == original

"""Explicit consumers fail closed instead of regenerating or falling back."""

import json
from copy import deepcopy
from typing import Any

import httpx
import pytest

from examples.python.memory_compaction_consumer import consume
from memory.compaction.consumption import build_compaction_request, prepare_compaction_request
from memory.compaction.domain import input_fingerprint
from memory.revisions.domain import content_hash
from tests.support.memory_compaction_consumption import consumer_options, synthetic_artifact


def test_complete_request_is_immutable_and_every_preparation_fetches_once() -> None:
    ref, result = synthetic_artifact()
    original = deepcopy(result)
    calls = []

    def fetch(mid: str, opid: str) -> Any:
        calls.append((mid, opid))
        return deepcopy(result)

    for _ in range(2):
        value = prepare_compaction_request(ref, fetch, **consumer_options())
        assert set(value["request"]) == {"model", "messages", "max_completion_tokens"}
        assert json.loads(value["request"]["messages"][1]["content"]) == result["artifact"]["package"]
        assert value["budget"]["required_tokens"] == value["budget"]["estimated_request_tokens"] + 768
        assert value["snapshot_only"] is True
    assert calls == [("bank", "original-op")] * 2 and result == original


@pytest.mark.parametrize(
    "path,value",
    [
        (("operation", "status"), "persistence_unknown"),
        (("operation", "writer_busy"), True),
        (("operation", "artifact_id"), "other"),
        (("artifact", "valid_for_current"), False),
        (("artifact", "validity_checked"), False),
        (("artifact", "redacted"), True),
        (("artifact", "package"), None),
        (("artifact", "input_manifest_sha256"), "0" * 64),
    ],
)
def test_unusable_or_wrong_reference_never_builds(path: tuple[str, str], value: Any) -> None:
    ref, result = synthetic_artifact()
    result[path[0]][path[1]] = value
    with pytest.raises(ValueError):
        build_compaction_request(ref, result, **consumer_options())


@pytest.mark.parametrize("mutation", ["retained", "lineage", "order", "omitted_bool", "span_bool", "scope", "unsafe_int"])
def test_package_and_scope_tampering_is_rejected(mutation: str) -> None:
    ref, result = synthetic_artifact()
    package = result["artifact"]["package"]
    if mutation == "retained":
        package["retained"][0]["content"] += "forged"
    elif mutation == "lineage":
        package["lineage"][0]["scope"]["agent_id"] = "another"
    elif mutation == "order":
        package["covered"][0]["source_id"] = "3"
    elif mutation == "omitted_bool":
        package["omitted"]["source_count"] = False
    elif mutation == "span_bool":
        package["covered"][0]["end_offset"] = True
    elif mutation == "scope":
        result["artifact"]["sources"][0]["scope"]["user_id"] = "other"
    else:
        package["covered"][0]["source_id"] = 2**63 - 1
    with pytest.raises(ValueError):
        build_compaction_request(ref, result, **consumer_options())


@pytest.mark.parametrize(
    "options",
    [{"context_window_tokens": 1}, {"output_reserve_tokens": True}, {"safety_margin_tokens": -1}, {"count_tokens": lambda _: 0}, {"count_tokens": lambda _: False}, {"question": "x" * 16385}],
)
def test_budget_and_inputs_stop_without_clipping(options: dict[str, Any]) -> None:
    ref, result = synthetic_artifact()
    with pytest.raises(ValueError):
        build_compaction_request(ref, result, **{**consumer_options(), **options})


def test_fresh_refusal_does_not_use_a_previous_package() -> None:
    ref, result = synthetic_artifact()
    assert prepare_compaction_request(ref, lambda *_: result, **consumer_options())

    def denied(*args: str) -> Any:
        raise PermissionError("not current")

    with pytest.raises(PermissionError):
        prepare_compaction_request(ref, denied, **consumer_options())


@pytest.mark.parametrize("index", [0, 1])
def test_registered_empty_legacy_root_is_preserved_as_an_atomic_block(index: int) -> None:
    ref, result = synthetic_artifact()
    artifact, package = result["artifact"], result["artifact"]["package"]
    artifact["sources"][index]["content_sha256"] = content_hash("")
    package["lineage"][index]["content_sha256"] = content_hash("")
    if index == 0:
        package["covered"][0]["end_offset"] = 0
    else:
        package["retained"][0]["content"] = ""
    manifest = input_fingerprint([{"binding": root} for root in artifact["sources"]])
    ref["input_manifest_sha256"] = artifact["input_manifest_sha256"] = manifest
    prepared = build_compaction_request(ref, result, **consumer_options())
    assert json.loads(prepared["request"]["messages"][1]["content"]) == package


@pytest.mark.parametrize("fault", ["preview", "500", "timeout", "stale"])
def test_example_actual_http_sdk_boundary_never_retries_or_sends_in_preview(monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://must-not-receive.invalid/v1")
    ref, result = synthetic_artifact()
    requests = []
    if fault == "stale":
        result["artifact"].update(state="stale", valid_for_current=False, package=None)

    def send(client: httpx.Client, outgoing: httpx.Request, **kwargs: Any) -> httpx.Response:
        requests.append((outgoing.method, outgoing.url.host))
        if outgoing.url.host == "owned-api.invalid":
            assert outgoing.method == "GET" and outgoing.headers["Authorization"] == "Bearer owned-api-test"
            return httpx.Response(200, request=outgoing, json={"retcode": 0, "data": result})
        assert outgoing.url.host == "api.openai.com" and fault in {"500", "timeout"}
        assert set(json.loads(outgoing.content)) == {"model", "messages", "max_completion_tokens"}
        if fault == "timeout":
            raise httpx.ReadTimeout("controlled", request=outgoing)
        return httpx.Response(500, request=outgoing, json={"error": {"message": "controlled failure"}})

    monkeypatch.setattr(httpx.Client, "send", send)
    options = {key: value for key, value in consumer_options().items() if key != "tokenizer_label"}
    arguments = {"api_base": "https://owned-api.invalid/api/v1", "api_token": "owned-api-test", "model_key": "owned-model-test", "send": fault != "preview", **options}
    if fault == "preview":
        assert consume(ref, **arguments)["sent"] is False
    else:
        with pytest.raises(Exception):
            consume(ref, **arguments)
    assert requests == [("GET", "owned-api.invalid")] + ([] if fault in {"preview", "stale"} else [("POST", "api.openai.com")])

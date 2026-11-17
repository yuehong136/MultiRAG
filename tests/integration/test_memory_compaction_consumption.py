"""Real operation GET and storage gates before an explicitly fake SDK answer."""

import json
from copy import deepcopy
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.memory_compaction_models import MemoryCompactionArtifact
from api.db.memory_revision_models import MemoryMutationOperation, MemoryPublicationOutbox
from api.db.memory_writer_models import MemoryWriterAdmission
from examples.python.memory_compaction_consumer import consume
from memory.compaction.consumption import build_compaction_request
from tests.support.memory_compaction_http import compaction_api as compaction_api
from tests.support.memory_compaction_http import release_api as release_api
from tests.support.memory_compaction_http import request
from tests.support.memory_compaction_http import writes_api as writes_api
from tests.support.memory_writes_http import api_request, result


def _snapshot(env: dict[str, Any]) -> str:
    snapshot = {}
    with Session(env["engine"]) as reader:
        for model in (MemoryCompactionArtifact, MemoryMutationOperation, MemoryWriterAdmission, MemoryPublicationOutbox):
            rows = reader.scalars(sa.select(model).where(model.memory_id == env["memory_id"]))
            snapshot[model.__tablename__] = [{column.name: getattr(row, column.name) for column in model.__table__.columns} for row in rows]
    return json.dumps(snapshot, sort_keys=True, default=str)


def _artifact(env: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    mid = env["memory_id"]
    value = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=request(env)))
    artifact = value["artifact"]
    ref = {key: artifact[key] for key in ("memory_id", "operation_id", "artifact_id", "input_manifest_sha256")}
    scope = {**artifact["sources"][0]["scope"], "session_id": None}
    options = {"scope": scope, "model": "gpt-6.1-terra", "question": "哪些条件和未确认事项必须保留？", "context_window_tokens": 16384, "output_reserve_tokens": 512, "safety_margin_tokens": 256}
    return ref, value, options


def test_fresh_preview_and_explicit_official_sdk_consumption_are_read_only(compaction_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = compaction_api
    ref, value, options = _artifact(env)
    before = _snapshot(env)
    model_counts = (len(env["summary_calls"]), len(env["embedding_calls"]))
    original = httpx.Client.send
    observed: list[dict[str, Any]] = []

    def transport(client: httpx.Client, outgoing: httpx.Request, **kwargs: Any) -> httpx.Response:
        if outgoing.url.host == "owned-api.invalid":
            assert outgoing.method == "GET" and outgoing.url.path.endswith("/context-compaction-operations/" + ref["operation_id"])
            actual = api_request(env, "GET", f"/memories/{ref['memory_id']}/context-compaction-operations/{ref['operation_id']}")
            observed.append({"kind": "GET", "status": actual.status_code})
            return httpx.Response(actual.status_code, request=outgoing, json=actual.json())
        if outgoing.url.host != "api.openai.com":
            return original(client, outgoing, **kwargs)
        assert outgoing.method == "POST" and outgoing.url.path == "/v1/chat/completions"
        body = json.loads(outgoing.content)
        expected = build_compaction_request(ref, value, **options, count_tokens=lambda text: len(text), tokenizer_label="diagnostic_only")["request"]
        assert body == expected
        assert json.loads(body["messages"][1]["content"])["retained"] == value["artifact"]["package"]["retained"]
        observed.append({"kind": "SDK", "request": body})
        return httpx.Response(
            200,
            request=outgoing,
            json={
                "id": "owned-controlled-answer",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "协议测试响应，不是实际模型质量。"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    monkeypatch.setattr(httpx.Client, "send", transport)
    common = {"api_base": "https://owned-api.invalid/api/v1", "api_token": "owned-test-api", "model_key": "owned-test-model", **options}
    for send in (False, True, True):
        output = consume(ref, **common, send=send)
        assert output["sent"] is send
        assert "request" not in output and "package" not in output
    assert [item["kind"] for item in observed] == ["GET", "GET", "SDK", "GET", "SDK"]
    assert _snapshot(env) == before and (len(env["summary_calls"]), len(env["embedding_calls"])) == model_counts
    env["journal"].event("compaction-consumer-readback", read_only=True, fresh_gets=3, controlled_sdk_calls=2, actual_provider_calls=0, application_claimed=False)


@pytest.mark.parametrize("boundary", ["withdrawal", "denied", "wrong_manifest", "wrong_scope", "budget", "unknown_operation"])
def test_current_refusal_never_reaches_sdk_or_regenerates(compaction_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    env = compaction_api
    ref, value, options = _artifact(env)
    if boundary == "withdrawal":
        root = value["artifact"]["sources"][1]
        result(
            api_request(
                env,
                "POST",
                f"/memories/{ref['memory_id']}/sources/{root['source_id']}/withdrawals",
                json={"operation_id": "withdraw-consumer-suffix", "expected_source_revision": int(root["source_revision"]), "reason": "Owned consumer invalidation"},
            )
        )
    if boundary == "wrong_manifest":
        ref["input_manifest_sha256"] = "0" * 64
    if boundary == "wrong_scope":
        options["scope"] = {**options["scope"], "user_id": "not-the-subject"}
    if boundary == "budget":
        options["context_window_tokens"] = 1
    if boundary == "unknown_operation":
        ref["operation_id"] = "no-such-original-operation"
    before = _snapshot(env)
    counts = (len(env["summary_calls"]), len(env["embedding_calls"]))
    original = httpx.Client.send
    reads = []

    def transport(client: httpx.Client, outgoing: httpx.Request, **kwargs: Any) -> httpx.Response:
        if outgoing.url.host == "owned-api.invalid":
            actual = api_request(env, "GET", f"/memories/{ref['memory_id']}/context-compaction-operations/{ref['operation_id']}", actor=2 if boundary == "denied" else 1)
            reads.append(actual.status_code)
            return httpx.Response(actual.status_code, request=outgoing, json=actual.json())
        if outgoing.url.host == "api.openai.com":
            pytest.fail("Refused consumption must never reach SDK")
        return original(client, outgoing, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", transport)
    original_reference = deepcopy(ref)
    with pytest.raises(Exception):
        consume(ref, api_base="https://owned-api.invalid/api/v1", api_token="owned-test", model_key="owned-test", send=True, **options)
    assert len(reads) == 1 and ref == original_reference
    if boundary in {"denied", "unknown_operation"}:
        assert reads == [404]
    else:
        assert reads == [200]
    assert _snapshot(env) == before and counts == (len(env["summary_calls"]), len(env["embedding_calls"]))

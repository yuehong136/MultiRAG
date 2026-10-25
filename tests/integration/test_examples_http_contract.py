"""Execute published examples against real registered HTTP/SQL/native storage."""

import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.responses import JSONResponse

from tests.support.database import scratch_database
from tests.support.rest_chat_contract import RestChatContract
from tests.support.rest_chat_contract import rest_chat_contract as rest_chat_contract
from tests.support.rest_chunk_retrieval_contract import RestChunkContract
from tests.support.rest_chunk_retrieval_contract import rest_chunk_contract as rest_chunk_contract
from tests.support.rest_chunk_retrieval_contract import runtime_upload_api as runtime_upload_api
from tests.support.rest_chunk_retrieval_contract import search_api as search_api

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def bootstrapped_engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sa.Engine]:
    # The shared helper journals the exact DB before CREATE and verifies DROP.
    configured = os.environ.get("MULTIRAG_343BDA_EVIDENCE_DIR")
    evidence = Path(configured) if configured else tmp_path_factory.mktemp("example-resources")
    with scratch_database(evidence) as engine:
        yield engine


def run_example(language: str, name: str, env: dict[str, Any], *, write: bool = False, token: str = "api_key", timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    suffix, command = ("sh", ["bash"]) if language == "http" else ("py", [sys.executable])
    process_env = {**os.environ, "MULTIRAG_BASE_URL": env["base"], "MULTIRAG_API_KEY": env[token], "MULTIRAG_ALLOW_WRITE": "1" if write else "0"}
    if timeout is not None:
        process_env["MULTIRAG_TIMEOUT_SECONDS"] = str(timeout)
    for source, target in (("dataset", "MULTIRAG_DATASET_ID"), ("doc", "MULTIRAG_DOCUMENT_ID")):
        if source in env:
            process_env[target] = env[source]
    return subprocess.run([*command, str(ROOT / "examples" / language / f"{name}_example.{suffix}")], cwd=ROOT, env=process_env, capture_output=True, text=True, timeout=90)


@pytest.mark.parametrize("language", ["http", "python"])
def test_chat_examples_modes_cleanup_and_provider_failure(rest_chat_contract: RestChatContract, language: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.smoke import wait_for_ping

    ctx = rest_chat_contract
    assert wait_for_ping(ctx.runtime["base"], 2)
    sentinel = ctx.create_chat(credential="api_key")
    before = ctx.snapshot()
    result = run_example(language, "chat_assistant", ctx.runtime)
    assert result.returncode == 0, result.stderr
    assert ctx.snapshot() == before and ctx.provider["calls"] == []
    result = run_example(language, "chat_assistant", ctx.runtime, write=True)
    assert result.returncode == 0 and "Controlled answer" in result.stdout, result.stderr
    assert len(ctx.provider["calls"]) == 2
    snapshot = ctx.snapshot()
    owned = [row for row in snapshot["chats"] if row["name"].startswith("example-")]
    assert len(owned) == 1 and owned[0]["status"] == "0"
    assert not [row for row in snapshot["sessions"] if row["dialog_id"] == owned[0]["id"]]
    assert ctx.read_chat(sentinel["id"])["status"] == "1"
    import core.llm as providers

    async def fail_stream(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Controlled streaming failure")
        yield  # An async generator matching the provider boundary.

    with monkeypatch.context() as patch:
        patch.setattr(providers.ChatModel[ctx.factory], "async_chat_streamly", fail_stream)
        result = run_example(language, "chat_assistant", ctx.runtime, write=True)
        assert result.returncode != 0
    ctx.provider["failure"] = True
    result = run_example(language, "chat_assistant", ctx.runtime, write=True)
    assert result.returncode != 0
    snapshot = ctx.snapshot()
    assert all(row["status"] == "0" for row in snapshot["chats"] if row["name"].startswith("example-"))
    assert snapshot["sessions"] == []
    assert ctx.read_chat(sentinel["id"])["status"] == "1"


@pytest.mark.parametrize("language", ["http", "python"])
def test_chunk_and_retrieval_examples_real_store_and_denial(rest_chunk_contract: RestChunkContract, language: str, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = rest_chunk_contract
    sentinel = ctx.add("retrieval workflow sentinel")
    ctx.wait_for_count(1)
    before = ctx.snapshot()
    for write in (False, True):
        result = run_example(language, "chunk", ctx.env, write=write, token="sdk_token")
        assert result.returncode == 0, result.stderr
        ctx.assert_count(1)
        assert ctx.native()[0]["pk"] == sentinel["id"]
        assert ctx.native()[0]["content_with_weight"] == "retrieval workflow sentinel"
    result = run_example(language, "retrieval", ctx.env, token="sdk_token")
    assert result.returncode == 0 and sentinel["id"] in result.stdout, result.stderr
    snapshot = ctx.snapshot()
    assert snapshot["native"] == before["native"]
    assert snapshot["document"]["chunk_num"] == snapshot["dataset"]["chunk_num"] == 1
    ctx.env["invalid"] = "invalid-example-token"
    before = ctx.snapshot()
    for name in ("chunk", "retrieval"):
        result = run_example(language, name, ctx.env, write=True, token="invalid")
        assert result.returncode != 0
        assert ctx.snapshot() == before

    # A real route returns a business failure, then stalls beyond client timeout.
    # In both cases deletion must name only the chunk created by this invocation.
    module = sys.modules["api.apps.restful_apis.chunk"]
    for stall in (False, True):
        release, completed = threading.Event(), threading.Event()

        def fail_patch(*args: Any, **kwargs: Any) -> JSONResponse:
            if stall:
                release.wait(10)
            completed.set()
            return JSONResponse({"code": 102, "message": "Controlled PATCH failure"})

        with monkeypatch.context() as patch:
            patch.setattr(module, "_update_chunk", fail_patch)
            try:
                result = run_example(language, "chunk", ctx.env, write=True, token="sdk_token", timeout=2 if stall else None)
                assert result.returncode != 0
            finally:
                release.set()
                assert completed.wait(5)
        ctx.assert_count(1)
        assert ctx.native()[0]["pk"] == sentinel["id"]

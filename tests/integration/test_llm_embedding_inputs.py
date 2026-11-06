"""Real embedding factory, owned HTTP provider and independently read SQL usage."""

import json
import socket
import threading
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from api.db.db_models import TenantLLM
from api.db.joint_services.tenant_model_service import get_model_config_by_id
from api.db.services import tenant_llm_service
from api.db.services.llm_service import LLMBundle
from common.exceptions import ModelException
from core.llm.embedding import OpenAIEmbed
from tests.support.resource_journal import ResourceJournal


@dataclass
class _Provider:
    requests: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 7
    reject: bool = False
    reject_at: int = 0


@pytest.fixture
def embedding_env(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[tuple[Engine, str, int, int, _Provider]]:
    provider = _Provider()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            provider.requests.append({"path": self.path, **payload})
            inputs = payload.get("input", [])
            valid = self.path == "/v1/embeddings" and inputs and all(isinstance(text, str) and text.strip() for text in inputs)
            if provider.reject or provider.reject_at == len(provider.requests) or not valid:
                status = 400
                body = {"error": {"message": "controlled embedding input rejection", "type": "invalid_request_error"}}
            else:
                status = 200
                body = {
                    "object": "list",
                    "model": payload["model"],
                    "data": [{"object": "embedding", "index": index, "embedding": [len(text) + 0.25, sum(map(ord, text)) % 101 + 0.5]} for index, text in enumerate(inputs)],
                    "usage": {"prompt_tokens": provider.tokens, "total_tokens": provider.tokens},
                }
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    journal = ResourceJournal("llm-embedding-inputs")
    for attempt in range(32):
        address = ("127.0.0.1", 30000 + int(uuid.uuid4().hex[:4], 16) % 20000)
        journal.event("reserve-listener", address=list(address), attempt=attempt + 1)
        server = ThreadingHTTPServer(address, Handler, bind_and_activate=False)
        try:
            server.server_bind()
            server.server_activate()
            break
        except OSError:
            server.server_close()
            if attempt == 31:
                raise
    thread_name = "p-embedding-provider-" + journal.run_id
    journal.event("reserve-thread", name=thread_name, listener=list(address))
    thread = threading.Thread(target=server.serve_forever, name=thread_name, daemon=True)
    thread.start()
    owner = uuid.uuid4().hex
    engine = bootstrapped_engine
    journal.event("reserve-sql-rows", database=engine.url.database, owner=owner)
    report: dict[str, Any] = {"database": engine.url.database, "provider_host": "127.0.0.1", "provider_port": server.server_port, "tenant": owner, "cleanup": {}}

    @contextmanager
    def scratch_connection() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    monkeypatch.setattr(tenant_llm_service, "db_connection", scratch_connection)
    try:
        with Session(engine) as db:
            model = TenantLLM(
                tenant_id=owner,
                llm_factory="OpenAI",
                llm_name="line-d-embedding",
                mdl_type="embedding",
                api_key="owned-provider-key",
                api_base=f"http://127.0.0.1:{server.server_port}/v1",
                max_tokens=8192,
            )
            sibling = TenantLLM(tenant_id=owner, llm_factory="OtherProvider", llm_name="line-d-embedding", mdl_type="embedding", api_key="", used_tokens=41)
            db.add_all([model, sibling])
            db.commit()
            model_id, sibling_id = model.id, sibling.id
        assert model_id is not None and sibling_id is not None
        report["model_ids"] = [model_id, sibling_id]
        yield engine, owner, model_id, sibling_id, provider
    finally:
        try:
            with Session(engine) as db:
                db.execute(delete(TenantLLM).where(TenantLLM.tenant_id == owner))
                db.commit()
            with Session(engine) as db:
                remaining = db.scalar(select(func.count()).select_from(TenantLLM).where(TenantLLM.tenant_id == owner))
            report["cleanup"]["remaining_model_rows"] = remaining
            assert remaining == 0
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            report["cleanup"].update(provider_thread_alive=thread.is_alive(), provider_socket_fd=server.fileno())
            report["requests"] = provider.requests
            directory = request.config.getoption("--test-report-dir")
            if directory:
                Path(directory, f"resources-{owner}.json").write_text(json.dumps(report, indent=2) + "\n")
            assert not thread.is_alive()
            assert server.fileno() == -1
            with socket.socket() as reader:
                assert reader.connect_ex(address) != 0
            journal.event("cleanup-readback", remaining_model_rows=0, thread_alive=thread.is_alive(), socket_absent=True)


def _read_usage(engine: Engine, model_id: int, sibling_id: int) -> int:
    with Session(engine) as db:
        assert db.get(TenantLLM, sibling_id).used_tokens == 41
        return db.get(TenantLLM, model_id).used_tokens


def test_factory_normalizes_batches_and_queries_and_commits_only_the_model_usage(embedding_env: tuple[Engine, str, int, int, _Provider]) -> None:
    engine, owner, model_id, sibling_id, provider = embedding_env
    texts = ["valid", "", " \n\t", None, "\u2003", "  keep spaces  "] + [f"chunk {index}" for index in range(14)]
    original = texts.copy()
    expected = ["valid", "None", "None", "None", "None", "  keep spaces  "] + texts[6:]
    with Session(engine) as db, ExitStack() as clients:
        bundle = LLMBundle(db, owner, get_model_config_by_id(db, model_id, tenant_id=owner))
        assert type(bundle.mdl) is OpenAIEmbed
        clients.callback(bundle.mdl.client.close)
        bundle.langfuse = MagicMock()

        vectors, tokens = bundle.encode(texts)

        assert not db.in_transaction()
        assert texts == original
        assert vectors.shape == (20, 2)
        np.testing.assert_array_equal(vectors, [[len(text) + 0.25, sum(map(ord, text)) % 101 + 0.5] for text in expected])
        assert tokens == 14
        assert _read_usage(engine, model_id, sibling_id) == 14
        assert [text for call in provider.requests for text in call["input"]] == expected
        assert [len(call["input"]) for call in provider.requests] == [16, 4]

        for index, query in enumerate([None, "", " \n\t", "  valid query  "], start=1):
            vectors, tokens = bundle.encode_queries(query)
            expected_query = query if index == 4 else "None"
            assert vectors.shape == (2,)
            assert vectors.tolist() == [len(expected_query) + 0.25, sum(map(ord, expected_query)) % 101 + 0.5]
            assert tokens == 7
            assert provider.requests[-1]["input"] == [expected_query]
            assert _read_usage(engine, model_id, sibling_id) == 14 + index * 7

        assert all(call["model"] == "line-d-embedding" and call["encoding_format"] == "float" for call in provider.requests)
        generation = bundle.langfuse.start_observation.return_value
        assert [call.kwargs["usage_details"]["total_tokens"] for call in generation.update.call_args_list] == [14, 7, 7, 7, 7]
        assert generation.end.call_count == 5


def test_empty_batch_and_zero_usage_preserve_results_and_pending_caller_writes(embedding_env: tuple[Engine, str, int, int, _Provider]) -> None:
    engine, owner, model_id, sibling_id, provider = embedding_env
    provider.tokens = 0
    with Session(engine) as db, ExitStack() as clients:
        bundle = LLMBundle(db, owner, get_model_config_by_id(db, model_id, tenant_id=owner))
        clients.callback(bundle.mdl.client.close)
        assert bundle.langfuse is None
        vectors, tokens = bundle.encode([])
        assert vectors.shape == (0,)
        assert tokens == 0
        assert provider.requests == []
        assert _read_usage(engine, model_id, sibling_id) == 0

        row = db.get(TenantLLM, model_id)
        row.status = "0"
        vectors, tokens = bundle.encode([None])
        assert vectors.shape == (1, 2)
        assert tokens == 0
        assert row in db.dirty
        assert db.in_transaction()
        vectors, tokens = bundle.encode_queries(None)
        assert vectors.shape == (2,)
        assert tokens == 0
        assert row in db.dirty
        assert _read_usage(engine, model_id, sibling_id) == 0
        with Session(engine) as independent:
            assert independent.get(TenantLLM, model_id).status == "1"
        assert [call["input"] for call in provider.requests] == [["None"], ["None"]]


def test_provider_rejection_propagates_without_billing_or_success_trace(embedding_env: tuple[Engine, str, int, int, _Provider]) -> None:
    engine, owner, model_id, sibling_id, provider = embedding_env
    provider.reject = True
    with Session(engine) as db, ExitStack() as clients:
        bundle = LLMBundle(db, owner, get_model_config_by_id(db, model_id, tenant_id=owner))
        clients.callback(bundle.mdl.client.close)
        bundle.langfuse = MagicMock()
        with pytest.raises(ModelException) as caught:
            bundle.encode([None])
        assert caught.value.status_code == 400 and caught.value.retryable is False
        assert "controlled embedding input rejection" not in str(caught.value)
        assert _read_usage(engine, model_id, sibling_id) == 0
        with pytest.raises(ModelException) as caught:
            bundle.encode_queries(None)
        assert _read_usage(engine, model_id, sibling_id) == 0
        assert caught.value.status_code == 400 and caught.value.retryable is False
        assert [call["input"] for call in provider.requests] == [["None"], ["None"]]
        bundle.langfuse.start_observation.return_value.update.assert_not_called()
        assert bundle.langfuse.start_observation.return_value.end.call_count == 2


def test_localai_partial_batch_failure_never_bills_partial_vectors(embedding_env: tuple[Engine, str, int, int, _Provider]) -> None:
    from core.llm.embedding import LocalAIEmbed

    engine, owner, model_id, sibling_id, provider = embedding_env
    provider.reject_at = 2
    with Session(engine) as writer:
        writer.get(TenantLLM, model_id).llm_factory = "LocalAI"
        writer.commit()
    with Session(engine) as db, ExitStack() as clients:
        bundle = LLMBundle(db, owner, get_model_config_by_id(db, model_id, tenant_id=owner))
        assert type(bundle.mdl) is LocalAIEmbed
        clients.callback(bundle.mdl.client.close)
        bundle.langfuse = MagicMock()
        with pytest.raises(ModelException) as caught:
            bundle.encode([f"document {index}" for index in range(20)])
        assert caught.value.status_code == 400 and caught.value.retryable is False
        assert [len(call["input"]) for call in provider.requests] == [16, 4]
        assert _read_usage(engine, model_id, sibling_id) == 0
        bundle.langfuse.start_observation.return_value.update.assert_not_called()
        bundle.langfuse.start_observation.return_value.end.assert_called_once_with()

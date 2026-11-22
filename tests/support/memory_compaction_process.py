"""Owned request process that can lose its durable compaction attempt to SIGKILL."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import private_consumer_file


async def _run(context: dict[str, Any], directory: Path) -> None:
    from api.apps import app
    from api.db.db_models import engine
    from api.db.services.tenant_llm_service import TenantLLMService
    from tests.manual.memory_compaction_rollout import _owned_database
    from tests.support.memory_writes_http import WriteChat

    assert _owned_database(engine) == context["database"]
    journal = ResourceJournal("memory-compaction-request-process")
    started = context["mode"] == "generate"
    calls: list[int] = []
    model_factories = 0

    class PausedChat(WriteChat):
        async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> tuple[str, int]:
            assert started and not calls
            calls.append(gen_conf["max_tokens"])
            record, marker = directory / "attempt.json", directory / "durable-attempt.json"
            journal.event("reserve-durable-attempt-marker", path=str(marker))
            private_consumer_file(record, json.dumps({"pid": os.getpid(), "model_calls": len(calls)}), journal, [])
            record.replace(marker)
            await asyncio.sleep(180)
            raise AssertionError("Expected the parent process to terminate this request")

    def model(cls: type[TenantLLMService], config: dict[str, Any], lang: str = "Chinese", **kwargs: Any) -> PausedChat:
        nonlocal model_factories
        model_factories += 1
        assert started and config["tenant_id"] == context["tenant"] and config["mdl_type"] == "chat"
        return PausedChat([])

    patch = pytest.MonkeyPatch()
    patch.setattr(TenantLLMService, "model_instance_from_config", classmethod(model))
    root = f"/api/v1/memories/{context['memory']}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Authorization": "Bearer " + context["token"]}) as client:
        if started:
            await client.post(root + "/context-compactions", json=context["body"])
            raise AssertionError("The request exited without being interrupted")
        path = root + "/context-compaction-operations/" + context["body"]["operation_id"]
        original = await client.get(path)
        replay = await client.post(root + "/context-compactions", json=context["body"])
        assert original.status_code == replay.status_code == 200
        data = original.json()["data"]
        assert replay.json()["data"] == data and not calls and model_factories == 0
        assert data["operation"]["writer_busy"] and not data["operation"]["can_retry"] and data["artifact"]["package"] is None
        journal.event("compaction-process-restarted", original_operation_id=context["body"]["operation_id"], model_calls=0)


def main() -> None:
    assert os.environ["MULTIRAG_RUN_ID"] and len(sys.argv) == 2
    path = Path(sys.argv[1]).resolve(strict=True)
    assert path.is_relative_to(Path(os.environ["TMPDIR"]).resolve())
    context = json.loads(path.read_text())
    from common.config_utils import CONFIGS

    pg = CONFIGS["postgresql"]
    assert [pg["host"], int(pg["port"]), pg["dbname"]] == context["owned_postgres"]
    for store, endpoint in context["owned_hosts"].items():
        assert CONFIGS[store]["hosts" if store == "milvus" else "host"] == endpoint
    asyncio.run(_run(context, path.parent))


if __name__ == "__main__":
    main()

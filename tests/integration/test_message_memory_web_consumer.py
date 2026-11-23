"""Pinned Message editor configuration runs through the real authenticated Canvas."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
import uvicorn  # noqa: F401 -- initialize before the legacy Message asyncio bridge
from sqlalchemy.orm import Session

from agent.component import message as message_module
from api.db.db_models import Memory, Task, UserCanvas
from api.db.joint_services import memory_write_runtime
from api.db.memory_revision_models import ManagedMemoryState, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission
from common.constants import MemoryType
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.storage import ManagedMessageStore
from tests.support.agent_update_release import message_dsl
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_writes_http import api_request, result
from tests.support.memory_writes_http import writes_api as writes_api
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file

QUERY = "仅内部演练中先给检查步骤，保留原记录；没有生产或发布授权。"
RESPONSE = "助手声称已完成全部检查，但这只是自述，没有实际执行回执。"


def editor_save(env: dict[str, Any], tmp_path: Path, *, agent_id: str, structured: bool | None) -> dict[str, Any]:
    checkout, web_sha = pinned_web_checkout()
    journal = ResourceJournal("message-memory-editor-consumer")
    config, output = tmp_path / "editor.private.json", tmp_path / "editor.result.json"
    owned: list[Path] = []
    try:
        private_consumer_file(
            config,
            json.dumps(
                {
                    "baseURL": env["base"],
                    "token": env["jwts"][1],
                    "agentId": agent_id,
                    "messageNodeId": "Message:answer",
                    "memoryIds": [env["memory_id"]],
                    "ownedScratchConfirmed": True,
                    "resultFile": str(output),
                    **({} if structured is None else {"structuredMemory": structured}),
                }
            ),
            journal,
            owned,
        )
        journal.event("reserve-private-consumer-file", path=str(output), creation_owner="pinned Message editor consumer")
        assert not output.exists()
        owned.append(output)
        journal.event("reserve-message-editor-consumer", web_sha=web_sha, agent_id=agent_id, memory_id=env["memory_id"], structured=structured)
        completed = subprocess.run(
            ["node", "--import", "tsx", "scripts/consumers/memory-message.ts", str(config)],
            cwd=checkout,
            env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        receipt = json.loads(output.read_text())
        assert receipt["passed"] and receipt["reloaded"] and receipt["exportImportPreserved"]
        assert receipt["agentId"] == agent_id and receipt["messageNodeId"] == "Message:answer"
        assert receipt["structuredMemory"] is (structured is True)
        assert receipt["memoryIds"] == [env["memory_id"]]
        journal.event("message-editor-consumer-readback", web_sha=web_sha, receipt=receipt)
        return receipt
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("message-editor-private-files-cleaned", paths=[str(path) for path in owned], absent=True)


@pytest.mark.external_consumer
@pytest.mark.parametrize("structured", [None, True], ids=["legacy-default", "structured-opt-in"])
def test_pinned_message_editor_persists_and_runs_real_memory_save(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, structured: bool | None) -> None:
    env, mid, agent_id = writes_api, writes_api["memory_id"], uuid4().hex
    # Reuse the exact owned async session factory of the real writer fixture.
    monkeypatch.setattr(message_module, "async_db_connection", memory_write_runtime.async_db_connection)
    dsl = message_dsl(RESPONSE)
    dsl["components"]["Message:answer"]["obj"]["params"].update(stream=False, user_id="forged-dsl-subject")
    env["journal"].event("reserve-owned-agent", database=env["engine"].url.database, agent_id=agent_id, owner=env["owners"][1])
    with Session(env["engine"]) as writer:
        writer.execute(sa.update(Memory).where(Memory.id == mid).values(memory_type=MemoryType.RAW.value | MemoryType.SEMANTIC.value))
        writer.add(UserCanvas(id=agent_id, user_id=env["owners"][1], title="Owned Message memory consumer", dsl=dsl))
        writer.commit()
    receipt = editor_save(env, tmp_path, agent_id=agent_id, structured=structured)
    with Session(env["engine"]) as reader:
        persisted = reader.get(UserCanvas, agent_id)
        assert persisted is not None
        stored = json.loads(persisted.dsl) if isinstance(persisted.dsl, str) else persisted.dsl
        assert isinstance(stored, dict)
        digest = hashlib.sha256(json.dumps(stored, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert digest == receipt["persistedDslSha256"]
        params = stored["components"]["Message:answer"]["obj"]["params"]
        assert params["structured_memory"] is (structured is True) and params["memory_ids"] == [mid]
        assert params["user_id"] == "forged-dsl-subject"
    response = env["client"].post(
        env["base"] + "/api/v1/agents/chat/completions",
        headers={"Authorization": "Bearer " + env["jwts"][1]},
        json={"agent_id": agent_id, "release": False, "query": QUERY, "stream": False, "return_trace": True},
        timeout=90,
    )
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    assert RESPONSE in json.dumps(response.json(), ensure_ascii=False)
    with Session(env["engine"]) as reader:
        admissions = list(reader.scalars(sa.select(MemoryWriterAdmission).where(MemoryWriterAdmission.memory_id == mid, MemoryWriterAdmission.kind == "append_raw")))
        assert len(admissions) == 1, response.json()
        admission = admissions[0]
        sid, operation = admission.source_message_id, admission.operation_id
        assert sid is not None and sid > 2**53 and admission.publication_state == "ready"
        assert reader.get(ManagedMemoryState, mid) is None
        tasks = list(reader.scalars(sa.select(Task).where(Task.doc_id == mid)))
        source = reader.get(MemorySourceState, (mid, sid))
        assert source is not None and source.state == "active"
    raw = asyncio.run(ManagedMessageStore(env["tenants"]["personal"], mid).read(sid))
    assert raw is not None and raw["user_id"] == env["owners"][1] and raw["agent_id"] == agent_id
    assert raw["session_id"] in env["task_ids"] and raw["message_type"] == "raw"
    assert env["embedding_calls"] == [raw["content"]] and not env["chat_calls"]
    provenance = result(api_request(env, "GET", f"/memories/{mid}/sources/{sid}/provenance", params={"include_segments": True}))
    if structured:
        assert json.loads(raw["content"])["segments"] == [{"role": "user", "content": QUERY}, {"role": "assistant", "content": RESPONSE}]
        assert provenance["format"] == "structured" and provenance["event_time"]["status"] == "unknown"
        assert provenance["provenance"]["submitted_by"] == env["owners"][1]
        assert provenance["provenance"]["operation_id"] == operation
        assert provenance["provenance"]["speaker_identity_verified"] is False and provenance["provenance"]["content_verified"] is False
        assert tasks == [] and REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    else:
        assert raw["content"] == f"User Input: {QUERY}\nAgent Response: {RESPONSE}"
        assert provenance["format"] == "legacy_combined" and provenance["role_status"] == "role_unknown"
        assert len(tasks) == 1 and REDIS_CONN.REDIS.xlen(env["queue"]) == 1
        queued = REDIS_CONN.REDIS.xrange(env["queue"])[0][1]
        task = json.loads(queued.get("message", queued.get(b"message")))
        assert asyncio.run(memory_write_runtime.handle_coordinated_task(task))
        assert len(env["chat_calls"]) == 1

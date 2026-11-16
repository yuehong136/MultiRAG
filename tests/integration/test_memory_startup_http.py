"""Read-only startup context and bound original-source reading over real HTTP/store."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Base, Memory, Task, TenantLLM
from common.token_utils import encoder
from memory.revisions.storage import ManagedMessageStore
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_dreaming_http import dreaming_api as dreaming_api
from tests.support.memory_incremental_http import seed_source
from tests.support.memory_startup_http import APPLICABILITY, SCOPE, confirm, endpoint
from tests.support.memory_startup_http import startup_api as startup_api
from tests.support.memory_writes_http import api_request, result
from tests.support.memory_writes_http import writes_api as writes_api
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file


def request(env: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {**SCOPE, "budget_tokens": 8192, **overrides}


def preview(env: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    response = api_request(env, "POST", endpoint(env, "/startup-context"), json=request(env, **overrides))
    assert response.headers.get("cache-control") == "no-store"
    return result(response)


def sql_fingerprint(env: dict[str, Any]) -> str:
    """Independent exact-bank readback includes metadata, tasks and model counters."""
    state: dict[str, Any] = {}
    with Session(env["engine"]) as reader:
        for table in Base.metadata.sorted_tables:
            if table.schema == "usr_ai" and "memory_id" in table.c:
                rows = reader.execute(sa.select(table).where(table.c.memory_id == env["memory_id"])).mappings().all()
                state[table.fullname] = sorted(json.dumps(dict(row), sort_keys=True, default=str) for row in rows)
        for model, column, identity in ((Memory, Memory.id, env["memory_id"]), (Task, Task.doc_id, env["memory_id"]), (TenantLLM, TenantLLM.tenant_id, env["tenants"]["personal"])):
            rows = reader.execute(sa.select(model.__table__).where(column == identity)).mappings().all()
            state[model.__tablename__] = sorted(json.dumps(dict(row), sort_keys=True, default=str) for row in rows)
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def source_params(source: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {
        "expected_source_revision": source["source_revision"],
        "expected_source_generation": source["source_generation"],
        "content_sha256": source["content_sha256"],
        **source["scope"],
        "offset": 0,
        "limit": 7,
        **overrides,
    }


def read_source(env: dict[str, Any], source: dict[str, Any], **overrides: Any) -> Any:
    return api_request(env, "GET", endpoint(env, "/sources/" + source["source_id"]), params=source_params(source, **overrides))


def test_startup_keeps_complete_body_and_human_scope_without_writes_or_models(startup_api: dict[str, Any]) -> None:
    env = startup_api
    before, calls = sql_fingerprint(env), (len(env["embedding_calls"]), len(env["analysis_calls"]))
    capabilities = result(api_request(env, "GET", endpoint(env, "/capabilities")))["startup_context"]
    assert capabilities["supported"] is True and capabilities["persistent_cache"] is False and capabilities["model_calls"] is False
    first = preview(env)
    assert len(first["items"]) == 1 and first["data_only"] is True
    item = first["items"][0]
    assert item["entry_id"] == env["entry_id"] and item["content"] == "The project uses port 80. Keep the backup policy."
    assert item["verification"]["applicability"] == APPLICABILITY
    assert item["content"] in first["startup_text"] and APPLICABILITY in first["startup_text"]
    assert item["assessment"]["verification_status"] == "unverified"
    assert item["verification"]["result"] == "human_confirmed" and item["sources"]
    assert item["verification"]["checked_at_is_user_reported"] is False
    assert first["mutates_memory"] is False and first["model_calls"] is False and first["snapshot_only"] is True
    assert first["usage"]["estimated_tokens"] <= first["usage"]["budget_tokens"]
    assert "Synthetic human judgment" not in json.dumps(first)
    second = preview(env, entry_ids=[env["entry_id"]])
    assert second["items"] == first["items"] and second["startup_text"] == first["startup_text"]
    assert (len(env["embedding_calls"]), len(env["analysis_calls"])) == calls
    assert sql_fingerprint(env) == before


def test_exact_selectors_and_memory_acl_are_preserved(startup_api: dict[str, Any]) -> None:
    env = startup_api
    before = sql_fingerprint(env)
    for actor in (0, 2):
        assert api_request(env, "POST", endpoint(env, "/startup-context"), actor=actor, json=request(env)).status_code == 404
    for overrides in ({"user_id": ""}, {"user_id": SCOPE["user_id"] + " "}, {"agent_id": ""}, {"session_id": ""}):
        observed = preview(env, **overrides)
        assert observed["items"] == [] and env["entry_id"] not in json.dumps(observed)
    for invalid in ({"budget_tokens": True}, {"max_items": 0}, {"tenant_id": "forged"}, {"actor_id": "forged"}, {"mode": "query"}):
        assert api_request(env, "POST", endpoint(env, "/startup-context"), json=request(env, **invalid)).status_code == 422
    legacy = api_request(env, "POST", endpoint(env, "/context"), json={"mode": "startup", "query": "ignored", "budget_tokens": 2048})
    assert legacy.status_code == 422
    assert sql_fingerprint(env) == before and not env["analysis_calls"]


def test_budget_measures_entire_json_and_never_cuts_the_human_applicability(startup_api: dict[str, Any]) -> None:
    env = startup_api
    before = sql_fingerprint(env)
    for budget in (1024, 8192):
        observed = preview(env, budget_tokens=budget, entry_ids=[env["entry_id"]])
        serialized = json.dumps(observed, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        measured = len(encoder.encode(serialized, disallowed_special=()))
        assert measured <= observed["usage"]["estimated_tokens"] <= budget
        assert observed["usage"]["includes"] == "entire_json_with_startup_text"
        if observed["items"]:
            assert observed["items"][0]["verification"]["applicability"] == APPLICABILITY
            assert APPLICABILITY in observed["startup_text"] and "Keep the backup policy." in observed["startup_text"]
        else:
            assert observed["omitted"]["counts"].get("budget", 0) == 1
            assert APPLICABILITY not in observed["startup_text"]
    assert sql_fingerprint(env) == before


def test_dispute_replaces_startup_eligibility_without_hiding_original_assessment(startup_api: dict[str, Any]) -> None:
    env = startup_api
    assert preview(env)["items"]
    confirm(env, operation="human-disputes-startup-entry", decision="disputed")
    before = sql_fingerprint(env)
    observed = preview(env, entry_ids=[env["entry_id"]])
    assert observed["items"] == []
    assert observed["omitted"]["counts"].get("disputed", 0) == 1
    assert "port 80" not in json.dumps(observed) and APPLICABILITY not in json.dumps(observed)
    assert sql_fingerprint(env) == before
    confirm(env, operation="explicit-fresh-human-confirmation")
    assert preview(env)["items"][0]["assessment"]["verification_status"] == "unverified"


def test_source_aba_invalidates_startup_and_old_bound_excerpt(startup_api: dict[str, Any]) -> None:
    env = startup_api
    original = preview(env)
    source = original["items"][0]["sources"][0]
    assert result(read_source(env, source))["excerpt"]
    for status in (False, True):
        result(api_request(env, "PUT", f"/messages/{env['memory_id']}:900", json={"status": status}))
    observed = preview(env, entry_ids=[env["entry_id"]])
    assert observed["items"] == [] and observed["omitted"]["counts"].get("verification_stale", 0) == 1
    assert read_source(env, source).status_code == 409
    confirm(env, operation="human-recheck-after-source-aba")
    fresh = preview(env)
    assert fresh["package_sha256"] != original["package_sha256"]
    assert fresh["items"][0]["sources"][0]["source_generation"] != source["source_generation"]


def test_source_withdrawal_omits_old_text_and_refuses_bound_source_read(startup_api: dict[str, Any]) -> None:
    env = startup_api
    source = preview(env)["items"][0]["sources"][0]
    response = api_request(
        env,
        "POST",
        endpoint(env, "/sources/900/withdrawals"),
        json={"operation_id": "withdraw-startup-source", "expected_source_revision": int(source["source_revision"]), "reason": "Synthetic permanent source withdrawal."},
    )
    assert response.status_code in {200, 202}, response.text
    observed = preview(env, entry_ids=[env["entry_id"]])
    assert observed["items"] == [] and "port 80" not in json.dumps(observed) and APPLICABILITY not in json.dumps(observed)
    assert read_source(env, source).status_code in {404, 409}
    assert not env["analysis_calls"]


def test_actual_projection_drift_does_not_reuse_a_previous_startup_package(startup_api: dict[str, Any]) -> None:
    env = startup_api
    original = preview(env)
    item = original["items"][0]
    store = ManagedMessageStore(env["tenants"]["personal"], env["memory_id"])
    identity = int(item["message_id"])
    message = asyncio.run(store.read(identity))
    assert message is not None
    env["journal"].event("reserve-startup-projection-drift", memory_id=env["memory_id"], message_id=identity)
    asyncio.run(store.write({**message, "status": False, "content": message["content"] + " Unknown external change."}))
    asyncio.run(store.set_status(identity, True))
    before = sql_fingerprint(env)
    response = api_request(env, "POST", endpoint(env, "/startup-context"), json=request(env, entry_ids=[env["entry_id"]]))
    assert response.status_code == 409, response.text
    assert "port 80" not in response.text and "Unknown external change" not in response.text and APPLICABILITY not in response.text
    assert sql_fingerprint(env) == before


def test_bound_unicode_source_continuation_and_legacy_read_compatibility(startup_api: dict[str, Any]) -> None:
    env = startup_api
    text = "原文🧠é逐段读取，不把工具标签当角色。\n" * 4 + "结尾条件：仅离线批量。"
    identity = 9007199254741999
    seed_source(env, identity, text, session="unicode-source-session")
    state = result(api_request(env, "GET", endpoint(env, f"/sources/{identity}/state")))
    source = {
        "source_id": str(identity),
        "source_revision": str(state["source_revision"]),
        "source_generation": str(state["generation"]),
        "content_sha256": state["content_sha256"],
        "scope": {"user_id": SCOPE["user_id"], "agent_id": SCOPE["agent_id"], "session_id": "unicode-source-session"},
    }
    before, chunks, offset = sql_fingerprint(env), [], 0
    while True:
        response = read_source(env, source, offset=offset)
        assert response.headers.get("cache-control") == "no-store"
        page = result(response)
        assert page["offset_unit"] == "unicode_codepoint" and page["offset"] == offset
        assert page["source_revision"] == source["source_revision"] and page["source_generation"] == source["source_generation"]
        chunks.append(page["excerpt"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert "".join(chunks) == text
    denied = api_request(env, "GET", endpoint(env, f"/sources/{identity}"), actor=2, params=source_params(source))
    assert denied.status_code == 404
    partial = source_params(source)
    partial.pop("expected_source_generation")
    assert api_request(env, "GET", endpoint(env, f"/sources/{identity}"), params=partial).status_code == 422
    beyond = read_source(env, source, offset=len(text) + 1)
    assert beyond.status_code == 422, beyond.text
    assert text not in beyond.text
    legacy = result(api_request(env, "GET", endpoint(env, "/sources/900"), params={"offset": 0, "limit": 7}))
    assert "source_revision" not in legacy and "source_generation" not in legacy
    assert sql_fingerprint(env) == before


@pytest.mark.external_consumer
def test_pinned_web_startup_preview_and_bound_source_reader(startup_api: dict[str, Any], tmp_path: Path) -> None:
    env = startup_api
    checkout, web_sha = pinned_web_checkout()
    journal = ResourceJournal("memory-startup-web-consumer")
    configuration, output = tmp_path / "startup-consumer.private.json", tmp_path / "startup-consumer-result.json"
    owned: list[Path] = []
    before, calls = sql_fingerprint(env), (len(env["embedding_calls"]), len(env["analysis_calls"]))
    try:
        private_consumer_file(
            configuration,
            json.dumps(
                {
                    "baseURL": env["base"],
                    "token": env["jwts"][1],
                    "memoryId": env["memory_id"],
                    "entryId": env["entry_id"],
                    "userId": SCOPE["user_id"],
                    "agentId": SCOPE["agent_id"],
                    "ownedScratchConfirmed": True,
                    "resultFile": str(output),
                }
            ),
            journal,
            owned,
        )
        journal.event("reserve-private-consumer-file", path=str(output), creation_owner="exclusive pinned Web consumer")
        assert not output.exists()
        owned.append(output)
        journal.event("reserve-startup-web-consumer", web_sha=web_sha, memory_id=env["memory_id"], script="scripts/consumers/memory-startup.ts")
        completed = subprocess.run(
            ["node", "--import", "tsx", "scripts/consumers/memory-startup.ts", str(configuration)],
            cwd=checkout,
            env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"},
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        receipt = json.loads(output.read_text())
        assert receipt["passed"] is True
        assert sql_fingerprint(env) == before
        assert (len(env["embedding_calls"]), len(env["analysis_calls"])) == calls
        journal.event("startup-web-consumer-readback", web_sha=web_sha, receipt=receipt)
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("startup-web-private-files-cleaned", paths=[str(path) for path in owned], absent=True)

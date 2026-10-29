"""Actual pinned Agent history client, JWT authorization and independent SQL."""

import copy
from typing import Any
from uuid import uuid4

import pytest
import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db.db_models import API4Conversation, UserCanvas
from tests.support.after5e_web_consumers import after5e_consumer_http as after5e_consumer_http
from tests.support.after5e_web_consumers import run_consumer

pytestmark = pytest.mark.external_consumer


def test_agent_history_real_web_jwt_and_sql(after5e_consumer_http: dict[str, Any]) -> None:
    env = after5e_consumer_http
    owner, peer = env["owners"]
    agent, foreign_agent, sparse, deleted, foreign, wrong_source = [uuid4().hex for _ in range(6)]
    references = [{"chunks": [None, 0, None, "valid-ref"], "doc_aggs": []}]
    messages = [
        {"role": "user", "content": "History question", "prompt": "private-user-prompt"},
        {"role": "assistant", "content": "Zero [ID:1] and saved [ID:3]; absent [ID:0] [ID:2]", "prompt": "private-assistant-prompt"},
    ]
    env["journal"].event("reserve-agent-history-rows", database=env["engine"].url.database, agents=[agent, foreign_agent], sessions=[sparse, deleted, foreign, wrong_source])
    with Session(env["engine"]) as db:
        db.add_all([UserCanvas(id=agent, user_id=owner, title="Owned history", permission="me", dsl={}), UserCanvas(id=foreign_agent, user_id=peer, title="Foreign history", permission="me", dsl={})])
        for identifier, canvas_id, actor, source in [
            (sparse, agent, owner, "agent"),
            (deleted, agent, owner, "agent"),
            (foreign, foreign_agent, peer, "agent"),
            (wrong_source, foreign_agent, peer, "dialog"),
        ]:
            db.add(
                API4Conversation(id=identifier, dialog_id=canvas_id, user_id=actor, source=source, name="After5e session", message=copy.deepcopy(messages), reference=copy.deepcopy(references), dsl={})
            )
        db.commit()
    headers = {"Authorization": "Bearer " + env["tokens"][0]}
    invalid = requests.get(env["base"] + f"/api/v1/agents/{agent}/sessions", headers={"Authorization": "Bearer invalid-fixture-token"}, timeout=15)
    assert invalid.status_code == 401

    def readback() -> dict[str, Any] | None:
        with Session(env["engine"]) as reader:
            if reader.get(API4Conversation, deleted) is not None:
                return None
            preserved = {identifier: reader.get(API4Conversation, identifier) for identifier in (sparse, foreign, wrong_source)}
            assert all(row is not None for row in preserved.values())
            assert preserved[sparse].message == messages and preserved[sparse].reference == references
            remaining = list(reader.scalars(select(API4Conversation.id).where(API4Conversation.dialog_id == agent, API4Conversation.source == "agent").order_by(API4Conversation.id)))
            assert remaining == [sparse]
        return {
            "agentId": agent,
            "deletedSessionId": deleted,
            "deletedAbsent": True,
            "remainingSessionIds": remaining,
            "foreignSessionPreserved": True,
            "wrongSourcePreserved": True,
            "sparseSessionUnchanged": True,
            "privatePromptsUnchangedInSQL": True,
        }

    result = run_consumer(
        env,
        "verify-agent-history-routes.ts",
        {
            "agentId": agent,
            "sparseSessionId": sparse,
            "deleteSessionId": deleted,
            "expectedSessionIds": [sparse, deleted],
            "deletionOwned": {"authorized": True, "agentId": agent, "sessionId": deleted, "resourceOwner": "I", "purpose": "scratch fixture exact deletion"},
        },
        readback,
    )
    assert result["readback"]["deletedAbsent"]
    # Re-read over HTTP separately from the actual Web client's observations.
    response = requests.get(env["base"] + f"/api/v1/agents/{agent}/sessions/{sparse}", headers=headers, timeout=15)
    assert response.status_code == 200 and response.json()["retcode"] == 0
    normalized = response.json()["data"]["messages"]
    assert all("prompt" not in message for message in normalized)
    assert [item["citation_id"] for item in normalized[1]["reference"]] == ["1", "3"]
    assert normalized[1]["reference"][0]["id"] == 0 and normalized[1]["reference"][0]["content"] == "0"
    assert all(item.get("document_id") is None and "doc_type" not in item for item in normalized[1]["reference"])
    env["manifest"]("agent-consumer-readback", web_sha=result["web_sha"], agent=agent, deleted_session=deleted, sparse_preserved=True, foreign_preserved=True)

"""Actual independent Web and admin clients; scratch auth/SQL, controlled model output."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests
from sqlalchemy.orm import Session

from api.db.db_models import API4Conversation, Conversation, Search
from tests.support.agent_update_release import message_dsl, read_state, update
from tests.support.agent_update_release import release_api as release_api
from tests.support.reference_metadata_http import metadata_http as metadata_http
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


def web_run(env: dict[str, Any], identifier: str, token: str, mode: str) -> dict[str, Any]:
    root = Path(os.environ.get("WEB_COMPLETION_CHECKOUT", "/Users/xldu/project/web"))
    runner, script = root / "node_modules/.bin/tsx", root / "scripts/verify-completion-routes.ts"
    assert runner.exists() and script.exists(), "The actual Web consumer checkout is required."
    completed = subprocess.run(
        [str(runner), "--tsconfig", str(root / "tsconfig.app.json"), str(script)],
        cwd=root,
        env={**os.environ, "COMPLETION_ACCEPTANCE_BASE": env["base"], "COMPLETION_ACCEPTANCE_ID": identifier, "COMPLETION_ACCEPTANCE_TOKEN": token, "COMPLETION_ACCEPTANCE_MODE": mode},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.splitlines()[-1])


def test_actual_web_agent_plural(release_api: dict[str, Any]) -> None:
    env = release_api
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Web completions", "dsl": message_dsl("published web reply")}, timeout=30).json()
    assert created["retcode"] == 0
    agent_id = created["data"]["id"]
    update(env, agent_id, {"release": True})
    before = read_state(env, agent_id)
    result = web_run(env, agent_id, env["jwts"][0], "agent")
    with Session(env["engine"]) as db:
        conversation = db.get(API4Conversation, result["session_id"])
        assert conversation is not None and conversation.user_id == env["owners"][0] and conversation.dialog_id == agent_id
        assert [message["content"] for message in conversation.message if message["role"] == "user"] == ["web first", "web continued"]
    assert read_state(env, agent_id) == before
    smoke = subprocess.run(["make", "smoke"], cwd=Path(__file__).resolve().parents[2], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, capture_output=True, text=True, timeout=60)
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr


def test_actual_web_search_and_admin_chat(metadata_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    env = metadata_http
    with Session(env["engine"]) as db:
        before = db.get(Search, env["ids"]["search"]).to_dict()
    web_run(env, env["ids"]["search"], env["api_key"], "search")
    with Session(env["engine"]) as db:
        assert db.get(Search, env["ids"]["search"]).to_dict() == before
    paths = requests.get(f"{env['base']}/openapi.json", timeout=30).json()["paths"]
    assert "/api/v1/searches/{search_id}/completion" not in paths
    assert requests.post(f"{env['base']}/api/v1/searches/{env['ids']['search']}/completion", json={"question": "retired"}, timeout=30).status_code == 404

    chat_id = env["ids"]["chat"]
    response = requests.post(f"{env['base']}/api/v1/chats/{chat_id}/sessions", headers={"Authorization": f"Bearer {env['api_key']}"}, json={"name": "CLI session"}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    session_id = response.json()["data"]["id"]
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "admin/client"))
    admin = importlib.import_module("multirag_client")
    http = importlib.import_module("http_client")
    address = urlsplit(env["base"])
    transport = http.HttpClient(host=address.hostname, port=address.port)
    transport.login_token = env["api_key"]
    client = admin.MultiRAGClient(transport, "user")
    client.chat_on_session({"chat_name": "Metadata chat", "session_id": session_id, "message": "real CLI question"})
    assert "Answer" in capsys.readouterr().out
    with Session(env["engine"]) as db:
        conversation = db.get(Conversation, session_id)
        assert conversation is not None and conversation.dialog_id == chat_id
        assert conversation.message[-2]["content"] == "real CLI question" and "Answer" in conversation.message[-1]["content"]

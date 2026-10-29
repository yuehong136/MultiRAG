"""The CLI reads every session over authenticated HTTP before scoped deletion."""

import hashlib
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Conversation, Dialog
from tests.support.rest_chat_contract import RestChatContract
from tests.support.rest_chat_contract import rest_chat_contract as rest_chat_contract
from tests.support.rest_chat_contract import runtime_upload_api as runtime_upload_api


def test_cli_session_inventory_and_named_drop_read_back_real_sql(rest_chat_contract: RestChatContract, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    ctx = rest_chat_contract
    env = ctx.runtime
    source_root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(source_root / "admin/client"))
    transport_module = importlib.import_module("http_client")
    cli_module = importlib.import_module("multirag_client")
    for module, name in ((transport_module, "http_client.py"), (cli_module, "multirag_client.py")):
        assert Path(module.__file__).resolve() == source_root / "admin/client" / name
    endpoint = urlsplit(env["base"])
    transport = transport_module.HttpClient(host=endpoint.hostname, port=endpoint.port)
    transport.login_token = "Bearer " + env["jwt"]
    foreign_transport = transport_module.HttpClient(host=endpoint.hostname, port=endpoint.port, api_key=env["api_key"])
    cli = cli_module.MultiRAGClient(transport, "user")
    journal = env["journal"]
    record = env["record"]["cli_session_inventory"] = {
        "source_root": str(source_root),
        "source_sha256": {name: hashlib.sha256((source_root / "admin/client" / name).read_bytes()).hexdigest() for name in ("http_client.py", "multirag_client.py")},
        "target_count": 105,
        "authentication": "fixture-issued real JWT and API token; credentials excluded",
    }
    env["record_path"].write_text(json.dumps(env["record"]))

    def reserve_rows(db: Session, context: Any, instances: Any) -> None:
        for row in db.new:
            if isinstance(row, Conversation):
                journal.event("reserve-cli-session-row", database=env["engine"].url.database, id=row.id, dialog_id=row.dialog_id, user_id=row.user_id)
            elif isinstance(row, Dialog):
                journal.event("reserve-cli-chat-row", database=env["engine"].url.database, id=row.id, tenant_id=row.tenant_id)

    def read_sessions() -> dict[str, dict[str, Any]]:
        with env["engine"].connect() as reader:
            return {row["id"]: dict(row) for row in reader.execute(sa.select(Conversation.__table__)).mappings()}

    sa.event.listen(Session, "before_flush", reserve_rows)
    try:
        chat_name = "cli-pages-" + uuid4().hex
        target_chat = ctx.create_chat(name=chat_name)
        other_owned_chat = ctx.create_chat(name="cli-owned-" + uuid4().hex)
        foreign_chat = ctx.create_chat(credential="api_key", name="cli-foreign-" + uuid4().hex)
        record["chats"] = {"target": target_chat["id"], "other_owned": other_owned_chat["id"], "foreign": foreign_chat["id"]}
        env["record_path"].write_text(json.dumps(env["record"]))

        def create_session(index: int, chat_id: str = target_chat["id"], *, foreign: bool = False) -> str:
            journal.event("reserve-cli-session-request", chat_id=chat_id, name=f"session-{index:03d}", principal=env["owners"][int(foreign)])
            client = foreign_transport if foreign else transport
            response = client.request("POST", f"chats/{chat_id}/sessions", json_body={"name": f"session-{index:03d}"}, auth_kind="api" if foreign else "web")
            assert isinstance(response, requests.Response)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["code"] == 0, body
            return body["data"]["id"]

        with ThreadPoolExecutor(max_workers=6) as pool:
            target_ids = sorted(pool.map(create_session, range(105)))
        other_owned_id = create_session(0, other_owned_chat["id"])
        foreign_id = create_session(0, foreign_chat["id"], foreign=True)
        record["sessions"] = {"target": target_ids, "other_owned": other_owned_id, "foreign": foreign_id}
        env["record_path"].write_text(json.dumps(env["record"]))
        before = read_sessions()
        assert len(target_ids) == len(set(target_ids)) == 105
        assert set(before) == {*target_ids, other_owned_id, foreign_id}
        assert all(before[identifier]["dialog_id"] == target_chat["id"] and before[identifier]["user_id"] == env["owners"][0] for identifier in target_ids)
        assert before[foreign_id]["user_id"] == env["owners"][1]

        listed = cli._list_chat_sessions(target_chat["id"])
        assert listed is not None
        assert [row["id"] for row in listed] == target_ids
        assert read_sessions() == before
        # ID ordering places this target on the second 100-item page.
        target_id = target_ids[-1]
        cli.drop_chat_session({"chat_name": chat_name, "session_id": target_id})
        assert f"Success to drop chat session '{target_id}'" in capsys.readouterr().out
        after = read_sessions()
        assert target_id not in after
        assert after == {identifier: row for identifier, row in before.items() if identifier != target_id}
        assert ctx.read_session(other_owned_id) == before[other_owned_id]
        assert ctx.read_session(foreign_id) == before[foreign_id]
        assert ctx.provider["calls"] == []
        record.update(listed_ids=[row["id"] for row in listed], deleted_id=target_id, sql_before=json.loads(json.dumps(before, default=str)), sql_after=json.loads(json.dumps(after, default=str)))
        env["record_path"].write_text(json.dumps(env["record"], default=str))
        journal.event(
            "cli-session-consumer-readback", listed_ids=target_ids, deleted_id=target_id, before_count=len(before), after_count=len(after), other_owned_unchanged=True, foreign_unchanged=True
        )
    finally:
        sa.event.remove(Session, "before_flush", reserve_rows)

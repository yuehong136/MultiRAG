import asyncio
import json
from types import SimpleNamespace

from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.apps.restful_apis import config_api, system_api
from api.utils import health_utils


class Obj(SimpleNamespace):
    def to_dict(self):
        return dict(self.__dict__)


def _body(response):
    return json.loads(response.body)


def _db():
    return Session()


def _stub_owner(monkeypatch):
    monkeypatch.setattr(
        system_api.UserTenantService,
        "query",
        lambda *_args, **_kwargs: [SimpleNamespace(role="owner", tenant_id="tenant-1")],
    )


def test_system_restful_ping_returns_pong():
    response = asyncio.run(system_api.ping())

    assert response.status_code == 200
    assert response.body == b"pong"


def test_system_restful_healthz_sets_status(monkeypatch):
    async def _ok(_db):
        return {"status": "ok"}, True

    async def _degraded(_db):
        return {"status": "degraded"}, False

    monkeypatch.setattr(system_api, "run_health_checks_async", _ok)
    response = Response()

    assert asyncio.run(system_api.healthz(response, db=AsyncSession())) == {"status": "ok"}
    assert response.status_code == 200

    monkeypatch.setattr(system_api, "run_health_checks_async", _degraded)
    response = Response()

    assert asyncio.run(system_api.healthz(response, db=AsyncSession())) == {"status": "degraded"}
    assert response.status_code == 500


def test_system_restful_token_list_backfills_missing_beta(monkeypatch):
    _stub_owner(monkeypatch)
    monkeypatch.setattr(
        system_api.APITokenService,
        "query",
        lambda *_args, **_kwargs: [Obj(token="tok-1", beta="", name="old", description=None)],
    )
    monkeypatch.setattr(system_api, "generate_confirmation_token", lambda: "multirag-abcdefghijklmnopqrstuvwxyz0123456789")
    updates = []
    monkeypatch.setattr(
        system_api.APITokenService,
        "filter_update",
        lambda db, filters, payload: updates.append((db, filters, payload)) or True,
    )

    body = _body(system_api.token_list(db=_db(), user=SimpleNamespace(id="user-1")))

    assert body["retcode"] == 0
    assert body["data"][0]["beta"] == "abcdefghijklmnopqrstuvwxyz012345"
    assert updates[0][2]["token"] == "tok-1"


def test_system_restful_token_create_allows_upstream_empty_payload(monkeypatch):
    _stub_owner(monkeypatch)
    generated = iter(["multirag-token-value", "multirag-beta-value-abcdefghijklmnopqrstuvwxyz"])
    monkeypatch.setattr(system_api, "generate_confirmation_token", lambda: next(generated))
    saved = []
    monkeypatch.setattr(system_api.APITokenService, "save", lambda db, **kwargs: saved.append(kwargs) or Obj(**kwargs))

    body = _body(system_api.new_token(request=None, name=None, db=_db(), user=SimpleNamespace(id="user-1")))

    assert body["retcode"] == 0
    assert body["data"]["name"] == "API Token"
    assert saved[0]["name"] == "API Token"
    assert saved[0]["description"] is None


def test_system_restful_token_delete_uses_path_token(monkeypatch):
    _stub_owner(monkeypatch)
    deleted = []
    monkeypatch.setattr(system_api.APITokenService, "filter_delete", lambda db, filters: deleted.append((db, filters)) or 1)

    body = _body(system_api.rm("tok-1", db=_db(), user=SimpleNamespace(id="user-1")))

    assert body["retcode"] == 0
    assert body["data"] is True
    assert deleted


def test_config_restful_log_level_validation(monkeypatch):
    monkeypatch.setattr(config_api, "set_log_level", lambda pkg_name, level: level == "INFO")

    ok = _body(config_api.set_logger_level(config_api.LogLevelRequest(pkg_name="core", level="INFO"), user=object()))
    bad = _body(config_api.set_logger_level(config_api.LogLevelRequest(pkg_name="core", level="NOPE"), user=object()))

    assert ok["data"] == {"pkg_name": "core", "level": "INFO"}
    assert bad["retmsg"] == "Invalid log level: NOPE"


def test_multirag_server_alive_uses_restful_ping(monkeypatch):
    seen = {}

    class DummyResponse:
        status_code = 200

    def fake_get(url):
        seen["url"] = url
        return DummyResponse()

    monkeypatch.setattr(health_utils.settings, "HOST_IP", "0.0.0.0")
    monkeypatch.setattr(health_utils.settings, "HOST_PORT", "9380")
    monkeypatch.setattr(health_utils.requests, "get", fake_get)

    assert health_utils.check_multirag_server_alive()["status"] == "alive"
    assert seen["url"] == "http://127.0.0.1:9380/api/v1/system/ping"

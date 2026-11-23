"""Management route contract of the model gateway on the real application."""

import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from api.model_gateway import management

PREFIX = "/api/v1/model-gateway"


MODEL_ID = "a" * 32


def _routes():
    # register_page loads route files under the name without the "_api" suffix.
    return sys.modules["api.apps.restful_apis.model_gateway"]


@pytest.fixture
def enabled(client):
    client.app.dependency_overrides[_routes().require_enabled] = lambda: None
    return client


def _sdk_token_principal():
    from api.identity.principal import AuthenticatedActor, AuthenticationContext, AuthenticationSource, IdentityAssurance, TenantMembershipEvidence, build_principal_from_authenticated_actor

    return build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(platform_user_id="user-unit", display_name="Unit Tester"),
        membership=TenantMembershipEvidence(platform_user_id="user-unit", tenant_id="tenant-unit"),
        authentication=AuthenticationContext(source=AuthenticationSource.SDK_API_TOKEN, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime.now(UTC)),
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/catalog"), ("PATCH", f"/catalog/models/{MODEL_ID}"), ("PATCH", "/settings"), ("GET", "/overview"), ("POST", "/keys"), ("GET", "/logs"), ("GET", "/usage")],
)
def test_routes_do_not_exist_while_the_gateway_is_disabled(client, monkeypatch, method, path):
    monkeypatch.setattr(management, "catalog_view", pytest.fail)

    response = client.request(method, PREFIX + path, json={"name": "runner"} if method != "GET" else None)

    assert response.status_code == 404


def test_sdk_api_token_cannot_manage_the_gateway(enabled, monkeypatch):
    from api.utils import api_utils

    enabled.app.dependency_overrides[api_utils.async_current_user] = _sdk_token_principal
    monkeypatch.setattr(management, "create_key", pytest.fail)

    assert enabled.get(f"{PREFIX}/keys").status_code == 403
    assert enabled.post(f"{PREFIX}/keys", json={"name": "runner"}).status_code == 403


@pytest.mark.parametrize(("method", "path"), [("GET", "/candidates"), ("GET", "/models"), ("POST", "/models"), ("PATCH", "/models/m1"), ("DELETE", "/models/m1")])
def test_publishing_routes_are_gone(enabled, method, path):
    assert enabled.request(method, PREFIX + path, json={} if method in {"POST", "PATCH"} else None).status_code in {404, 405}


def test_catalog_is_listed_for_the_owner(enabled, monkeypatch):
    async def catalog_view(db, tenant_id):
        return {"new_models_enabled": True, "connections": [], "models": [{"model_id": MODEL_ID, "call_name": "qwen", "tenant": tenant_id}]}

    monkeypatch.setattr(management, "catalog_view", catalog_view)

    response = enabled.get(f"{PREFIX}/catalog")

    assert response.status_code == 200
    assert response.json()["data"]["models"] == [{"model_id": MODEL_ID, "call_name": "qwen", "tenant": "user-unit"}]


def test_model_switch_alias_and_limits_reach_the_operation(enabled, monkeypatch):
    seen = {}

    async def update_catalog_model(db, tenant_id, actor_id, model_id, body):
        seen.update(tenant_id=tenant_id, model_id=model_id, changes=body.model_dump(exclude_unset=True))
        return {"model_id": model_id}

    monkeypatch.setattr(management, "update_catalog_model", update_catalog_model)

    response = enabled.patch(f"{PREFIX}/catalog/models/{MODEL_ID}", json={"enabled": False, "alias": "chat-default", "rpm_limit": 600, "tpm_limit": None})

    assert response.status_code == 200
    # Explicit nulls reach the operation, so an alias or a limit can be removed again.
    assert seen == {"tenant_id": "user-unit", "model_id": MODEL_ID, "changes": {"enabled": False, "alias": "chat-default", "rpm_limit": 600, "tpm_limit": None}}


def test_connection_switch_and_default_reach_their_operations(enabled, monkeypatch):
    seen = {}

    async def update_catalog_instance(db, tenant_id, actor_id, instance_id, body):
        seen["instance"] = (instance_id, body.enabled)
        return {"models": []}

    async def update_settings(db, tenant_id, actor_id, body):
        seen["settings"] = body.new_models_enabled
        return {"models": []}

    monkeypatch.setattr(management, "update_catalog_instance", update_catalog_instance)
    monkeypatch.setattr(management, "update_settings", update_settings)

    assert enabled.patch(f"{PREFIX}/catalog/instances/inst-1", json={"enabled": True}).status_code == 200
    assert enabled.patch(f"{PREFIX}/settings", json={"new_models_enabled": False}).status_code == 200
    assert seen == {"instance": ("inst-1", True), "settings": False}


def test_overview_reports_the_configured_address_and_data_plane(enabled, monkeypatch):
    seen = {}

    async def overview(db, tenant_id, *, days, config, data_plane):
        seen.update(days=days, data_plane=data_plane, port=config.http_port)
        return {"days": days}

    async def data_plane_status(config):
        return "down"

    monkeypatch.setattr(management, "overview", overview)
    monkeypatch.setattr(management, "data_plane_status", data_plane_status)

    assert enabled.get(f"{PREFIX}/overview").json()["data"] == {"days": 7}
    assert enabled.get(f"{PREFIX}/overview", params={"days": 30}).status_code == 200
    assert enabled.get(f"{PREFIX}/overview", params={"days": 14}).status_code == 422
    assert seen == {"days": 30, "data_plane": "down", "port": 9390}


def test_key_creation_is_scoped_to_the_owner_and_returns_the_plaintext_once(enabled, monkeypatch):
    seen = {}

    async def create_key(db, tenant_id, actor_id, body):
        seen.update(tenant_id=tenant_id, actor_id=actor_id, body=body)
        return {"id": "k" * 32, "name": body.name, "key": "sk-mrag-plaintext"}

    monkeypatch.setattr(management, "create_key", create_key)

    response = enabled.post(f"{PREFIX}/keys", json={"name": "  eval-runner  "})

    assert response.status_code == 200
    assert response.json() == {"code": 0, "message": "success", "data": {"id": "k" * 32, "name": "eval-runner", "key": "sk-mrag-plaintext"}}
    assert (seen["tenant_id"], seen["actor_id"]) == ("user-unit", "user-unit")
    # Only a name is required: everything else is optional metadata or scope.
    body = seen["body"]
    assert (body.description, body.tags, body.expires_at, body.allow_all_models, body.allowed_model_ids, body.allowed_instance_ids) == (None, None, None, False, [], [])
    assert (body.rpm_limit, body.tpm_limit, body.max_parallel_requests, body.token_quota, body.quota_period) == (None, None, None, None, None)


def test_limits_and_quota_are_passed_to_the_operation(enabled, monkeypatch):
    seen = {}

    async def create_key(db, tenant_id, actor_id, body):
        seen["body"] = body
        return {"id": "k" * 32}

    async def update_key(db, tenant_id, actor_id, key_id, body):
        seen["patch"] = body.model_dump(exclude_unset=True)
        return {"id": key_id}

    monkeypatch.setattr(management, "create_key", create_key)
    monkeypatch.setattr(management, "update_key", update_key)

    created = enabled.post(f"{PREFIX}/keys", json={"name": "runner", "rpm_limit": 60, "tpm_limit": 100000, "max_parallel_requests": 4, "token_quota": 5000000, "quota_period": "month"})
    cleared = enabled.patch(f"{PREFIX}/keys/{'k' * 32}", json={"rpm_limit": None, "token_quota": None, "quota_period": None})

    assert created.status_code == 200 and cleared.status_code == 200
    body = seen["body"]
    assert (body.rpm_limit, body.tpm_limit, body.max_parallel_requests, body.token_quota, body.quota_period) == (60, 100000, 4, 5000000, "month")
    # Explicit nulls reach the operation, so a limit can be removed again.
    assert seen["patch"] == {"rpm_limit": None, "token_quota": None, "quota_period": None}


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/keys", {}),
        ("/keys", {"name": "   "}),
        ("/keys", {"name": "runner", "department": "research"}),
        ("/keys", {"name": "runner", "expires_at": "2020-01-01T00:00:00+00:00"}),
        ("/keys", {"name": "runner", "expires_at": "2999-01-01T00:00:00"}),
        ("/keys", {"name": "runner", "tags": {str(index): "v" for index in range(17)}}),
        ("/keys", {"name": "runner", "tags": {"purpose": "x" * 257}}),
        ("/keys", {"name": "runner", "allowed_model_ids": ["gm-chat"]}),
        ("/keys", {"name": "runner", "allowed_model_ids": [0]}),
        ("/keys", {"name": "runner", "allowed_instance_ids": [""]}),
        ("/keys", {"name": "runner", "rpm_limit": 0}),
        ("/keys", {"name": "runner", "max_parallel_requests": 1.5}),
        ("/keys", {"name": "runner", "token_quota": 100}),
        ("/keys", {"name": "runner", "quota_period": "month"}),
        ("/keys", {"name": "runner", "token_quota": 100, "quota_period": "week"}),
    ],
)
def test_invalid_bodies_are_rejected_before_any_operation(enabled, monkeypatch, path, body):
    monkeypatch.setattr(management, "create_key", pytest.fail)

    assert enabled.post(PREFIX + path, json=body).status_code == 422


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (f"/catalog/models/{MODEL_ID}", {"alias": "chat default"}),
        (f"/catalog/models/{MODEL_ID}", {"tpm_limit": -5}),
        (f"/catalog/models/{MODEL_ID}", {"token_quota": 100, "quota_period": "day"}),
        (f"/catalog/models/{MODEL_ID}", {"base_url": "http://elsewhere"}),
        ("/catalog/models/seven", {"enabled": True}),
        ("/catalog/instances/inst-1", {}),
        ("/settings", {"new_models_enabled": "sometimes"}),
    ],
)
def test_invalid_catalog_changes_are_rejected_before_any_operation(enabled, monkeypatch, path, body):
    for operation in ("update_catalog_model", "update_catalog_instance", "update_settings"):
        monkeypatch.setattr(management, operation, pytest.fail)

    assert enabled.patch(PREFIX + path, json=body).status_code == 422


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (management.GatewayNotFound("missing"), 404),
        (management.GatewayConflict("A revoked key cannot be changed"), 409),
        (ValueError("allowed_model_ids contains a model this tenant has not registered"), 400),
        (IntegrityError("insert", {}, Exception("duplicate")), 409),
    ],
)
def test_operation_failures_map_to_statuses_without_leaking_sql(enabled, monkeypatch, error, status):
    async def update_key(db, tenant_id, actor_id, key_id, body):
        raise error

    monkeypatch.setattr(management, "update_key", update_key)

    response = enabled.patch(f"{PREFIX}/keys/{'k' * 32}", json={"name": "renamed"})

    assert response.status_code == status
    assert response.json()["code"] == status
    assert "insert" not in response.text


def test_log_query_passes_validated_filters_and_utc_bounds(enabled, monkeypatch):
    seen = {}

    async def query_logs(db, tenant_id, **filters):
        seen.update(tenant_id=tenant_id, **filters)
        return {"items": [], "total": 0, "page": filters["page"], "page_size": filters["page_size"]}

    monkeypatch.setattr(management, "query_logs", query_logs)

    response = enabled.get(f"{PREFIX}/logs", params={"page": 2, "page_size": 20, "key_id": "k" * 32, "outcome": "error", "start": "2026-11-01T00:00:00", "end": "2026-11-02T08:00:00+08:00"})

    assert response.status_code == 200
    assert (seen["tenant_id"], seen["page"], seen["page_size"], seen["key_id"], seen["outcome"]) == ("user-unit", 2, 20, "k" * 32, "error")
    assert seen["start"] == datetime(2026, 11, 1, tzinfo=UTC)
    assert seen["end"] == datetime(2026, 11, 2, tzinfo=UTC)


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 101}, {"outcome": "slow"}, {"start": "yesterday"}])
def test_log_query_rejects_invalid_parameters(enabled, monkeypatch, params):
    monkeypatch.setattr(management, "query_logs", pytest.fail)

    assert enabled.get(f"{PREFIX}/logs", params=params).status_code == 422


def test_usage_defaults_to_the_last_seven_days_grouped_by_key(enabled, monkeypatch):
    seen = {}

    async def usage_summary(db, tenant_id, **arguments):
        seen.update(arguments)
        return [{"key_id": "k" * 32, "requests": 1}]

    monkeypatch.setattr(management, "usage_summary", usage_summary)

    response = enabled.get(f"{PREFIX}/usage")

    data = response.json()["data"]
    assert response.status_code == 200 and data["group_by"] == "key" and data["items"] == [{"key_id": "k" * 32, "requests": 1}]
    assert seen["group_by"] == "key" and (seen["end"] - seen["start"]).days == 7
    assert enabled.get(f"{PREFIX}/usage", params={"group_by": "department"}).status_code == 422

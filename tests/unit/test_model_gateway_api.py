"""Management route contract of the model gateway on the real application."""

import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from api.model_gateway import management

PREFIX = "/api/v1/model-gateway"


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


@pytest.mark.parametrize(("method", "path"), [("GET", "/models"), ("POST", "/keys"), ("GET", "/logs"), ("GET", "/usage")])
def test_routes_do_not_exist_while_the_gateway_is_disabled(client, monkeypatch, method, path):
    monkeypatch.setattr(management, "list_models", pytest.fail)

    response = client.request(method, PREFIX + path, json={"name": "runner"} if method == "POST" else None)

    assert response.status_code == 404


def test_sdk_api_token_cannot_manage_the_gateway(enabled, monkeypatch):
    from api.utils import api_utils

    enabled.app.dependency_overrides[api_utils.async_current_user] = _sdk_token_principal
    monkeypatch.setattr(management, "create_key", pytest.fail)

    assert enabled.get(f"{PREFIX}/keys").status_code == 403
    assert enabled.post(f"{PREFIX}/keys", json={"name": "runner"}).status_code == 403


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
    assert (body.description, body.tags, body.expires_at, body.allow_all_models, body.allowed_model_ids) == (None, None, None, False, [])
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
        ("/models", {"name": "chat default", "model_id": 1, "model_type": "chat"}),
        ("/models", {"name": "chat-default", "model_id": 0, "model_type": "chat"}),
        ("/models", {"name": "rerank-default", "model_id": 1, "model_type": "rerank"}),
        ("/models", {"name": "chat-default", "model_id": 1, "model_type": "chat", "base_url": "http://elsewhere"}),
        ("/models", {"name": "chat-default", "model_id": 1, "model_type": "chat", "tpm_limit": -5}),
        ("/models", {"name": "chat-default", "model_id": 1, "model_type": "chat", "token_quota": 100, "quota_period": "day"}),
        ("/keys", {"name": "runner", "rpm_limit": 0}),
        ("/keys", {"name": "runner", "max_parallel_requests": 1.5}),
        ("/keys", {"name": "runner", "token_quota": 100}),
        ("/keys", {"name": "runner", "quota_period": "month"}),
        ("/keys", {"name": "runner", "token_quota": 100, "quota_period": "week"}),
    ],
)
def test_invalid_bodies_are_rejected_before_any_operation(enabled, monkeypatch, path, body):
    monkeypatch.setattr(management, "create_key", pytest.fail)
    monkeypatch.setattr(management, "create_model", pytest.fail)

    assert enabled.post(PREFIX + path, json=body).status_code == 422


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (management.GatewayNotFound("missing"), 404),
        (management.GatewayConflict("A revoked key cannot be changed"), 409),
        (ValueError("allowed_model_ids contains an unknown published model"), 400),
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

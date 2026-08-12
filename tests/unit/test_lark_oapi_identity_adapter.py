"""Official SDK contract tests for the EIM-I4 lazy Feishu adapter."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest

from api.identity.providers.contracts import (
    FeishuClientFailure,
    FeishuDirectoryClientError,
    FeishuDomain,
    FeishuProviderCredential,
)
from api.identity.providers.lark_oapi import LarkOapiFeishuDirectoryClient


def _credential(*, domain: FeishuDomain = FeishuDomain.FEISHU) -> FeishuProviderCredential:
    return FeishuProviderCredential(
        provider_account_id="provider-account-test",
        app_id="cli_test",
        app_secret="app-secret-test",
        credential_generation=3,
        domain=domain,
    )


def test_importing_identity_adapter_keeps_the_sdk_lazy_and_does_not_install_loop() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio
import sys
asyncio.set_event_loop(None)
import api.identity.providers.lark_oapi
assert not any(name == 'lark_oapi' or name.startswith('lark_oapi.') for name in sys.modules)
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = None
assert loop is None
print('lazy')
""",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "lazy"


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_adapter_rejects_non_finite_or_non_positive_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="provider timeout must be positive"):
        LarkOapiFeishuDirectoryClient(timeout_seconds=timeout)


async def test_sdk_token_tenant_and_contact_calls_use_typed_async_seams_and_explicit_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lark_oapi.api.auth.v3 import InternalTenantAccessTokenRequest
    from lark_oapi.api.contact.v3 import GetUserRequest
    from lark_oapi.api.tenant.v2 import QueryTenantRequest
    from lark_oapi.core.const import AUTHORIZATION
    from lark_oapi.core.http import Transport
    from lark_oapi.core.model import RawResponse
    from lark_oapi.core.token import TokenManager

    token = "tenant-token-test"
    requests: list[tuple[str, str | None, str | None, object]] = []

    async def aexecute(config: object, request: object, option: object) -> RawResponse:
        del config
        tenant_token = getattr(option, "tenant_access_token", None)
        authorization = getattr(request, "headers", {}).get(AUTHORIZATION)
        requests.append((request.uri, tenant_token, authorization, request))
        response = RawResponse()
        response.status_code = 200
        response.headers = {}
        payload: dict[str, Any]
        if isinstance(request, InternalTenantAccessTokenRequest):
            payload = {
                "code": 0,
                "msg": "success",
                "data": {"tenant_access_token": token, "expire": 7200},
            }
        elif isinstance(request, QueryTenantRequest):
            payload = {"code": 0, "msg": "success", "data": {"tenant": {"tenant_key": "tenant-key-test"}}}
        elif isinstance(request, GetUserRequest):
            payload = {
                "code": 0,
                "msg": "success",
                "data": {
                    "user": {
                        "open_id": "ou_test",
                        "union_id": "on_test",
                        "user_id": "user_test",
                        "employee_no": "",
                        "name": "",
                        "status": {
                            "is_frozen": False,
                            "is_resigned": False,
                            "is_activated": True,
                            "is_exited": False,
                            "is_unjoin": False,
                        },
                    }
                },
            }
        else:
            raise AssertionError("unexpected SDK request")
        response.content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(Transport, "aexecute", staticmethod(aexecute))
    monkeypatch.setattr(TokenManager, "get_self_tenant_token", lambda *_args: (_ for _ in ()).throw(AssertionError("sync TokenManager path used")))
    adapter = LarkOapiFeishuDirectoryClient(timeout_seconds=2.0)
    credential = _credential()

    token_result = await adapter.fetch_tenant_token(credential)
    tenant_result = await adapter.get_tenant(credential, tenant_access_token=token)
    user_result = await adapter.get_user(
        credential,
        tenant_access_token=token,
        identifier_type="open_id",
        identifier_value="ou_test",
    )

    assert token_result.tenant_access_token == token
    assert token_result.expires_in_seconds == 7200
    assert tenant_result.tenant_key == "tenant-key-test"
    assert user_result.user is not None
    assert user_result.user.user_id == "user_test"
    assert user_result.user.employee_no is None
    assert user_result.user.display_name is None
    assert [item[0] for item in requests] == [
        "/open-apis/auth/v3/tenant_access_token/internal",
        "/open-apis/tenant/v2/tenant/query",
        "/open-apis/contact/v3/users/:user_id",
    ]
    assert requests[0][1] is None
    assert requests[1][1] == token
    assert requests[2][1] == token
    assert all(item[2] is None for item in requests)
    assert isinstance(requests[2][3], GetUserRequest)
    assert requests[2][3].queries == [("user_id_type", "open_id")]
    assert requests[2][3].paths == {"user_id": "ou_test"}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["data"]["user"].__setitem__("user_id", 7),
        lambda payload: payload["data"]["user"].__setitem__("status", "active"),
        lambda payload: payload["data"]["user"]["status"].__setitem__("is_frozen", 0),
        lambda payload: payload["data"]["user"]["status"].pop("is_unjoin"),
    ],
)
async def test_contact_response_rejects_non_string_or_incomplete_status_primitives(
    monkeypatch: pytest.MonkeyPatch,
    mutation: Any,
) -> None:
    from lark_oapi.core.http import Transport
    from lark_oapi.core.model import RawResponse

    payload = {
        "code": 0,
        "msg": "success",
        "data": {
            "user": {
                "open_id": "ou_test",
                "user_id": "user_test",
                "employee_no": "EMP-1",
                "status": {
                    "is_frozen": False,
                    "is_resigned": False,
                    "is_activated": True,
                    "is_exited": False,
                    "is_unjoin": False,
                },
            }
        },
    }
    mutation(payload)

    async def aexecute(*_args: object, **_kwargs: object) -> RawResponse:
        response = RawResponse()
        response.status_code = 200
        response.headers = {}
        response.content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(Transport, "aexecute", staticmethod(aexecute))
    adapter = LarkOapiFeishuDirectoryClient()

    with pytest.raises(FeishuDirectoryClientError) as caught:
        await adapter.get_user(
            _credential(),
            tenant_access_token="token-test",
            identifier_type="open_id",
            identifier_value="ou_test",
        )

    assert caught.value.failure is FeishuClientFailure.RESPONSE_INVALID
    assert "ou_test" not in str(caught.value)


async def test_non_success_http_with_zero_business_code_preserves_all_three_envelopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lark_oapi.core.http import Transport
    from lark_oapi.core.model import RawResponse

    async def aexecute(*_args: object, **_kwargs: object) -> RawResponse:
        response = RawResponse()
        response.status_code = 429
        response.headers = {}
        response.content = json.dumps({"code": 0, "msg": "rate limited"}).encode()
        return response

    monkeypatch.setattr(Transport, "aexecute", staticmethod(aexecute))
    adapter = LarkOapiFeishuDirectoryClient()

    token = await adapter.fetch_tenant_token(_credential())
    tenant = await adapter.get_tenant(_credential(), tenant_access_token="token-test")
    user = await adapter.get_user(
        _credential(),
        tenant_access_token="token-test",
        identifier_type="open_id",
        identifier_value="ou_test",
    )

    assert (token.http_status, token.code, token.tenant_access_token) == (429, 0, None)
    assert (tenant.http_status, tenant.code, tenant.tenant_key) == (429, 0, None)
    assert (user.http_status, user.code, user.user) == (429, 0, None)


def test_lark_domain_is_selected_without_exposing_credential() -> None:
    import api.identity.providers.lark_oapi as adapter_module

    sdk = adapter_module._load_sdk()
    client = LarkOapiFeishuDirectoryClient()._build_client(_credential(domain=FeishuDomain.LARK), sdk)

    assert client.config is not None
    assert client.config.domain == sdk["LARK_DOMAIN"]
    assert "app-secret-test" not in repr(_credential(domain=FeishuDomain.LARK))

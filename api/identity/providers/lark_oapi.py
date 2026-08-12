"""Lazy official ``lark-oapi`` 1.7.2 adapter for Feishu identity calls."""

from __future__ import annotations

import json
import math
from typing import Any

from api.identity.providers.contracts import (
    FeishuClientFailure,
    FeishuDirectoryClientError,
    FeishuDirectoryUser,
    FeishuDomain,
    FeishuGetUserResponse,
    FeishuProviderCredential,
    FeishuTenantResponse,
    FeishuTenantTokenResponse,
)

_MAX_TOKEN_LENGTH = 16_384
_MAX_PROVIDER_VALUE_LENGTH = 512


class LarkOapiFeishuDirectoryClient:
    """Official async SDK seam that exposes only approved provider fields.

    All SDK imports are local so importing the platform identity package never
    installs the SDK's module-global event loop.  Auth V3 keeps the generated
    request/transport but parses the official top-level wire response strictly
    because ``lark-oapi==1.7.2`` models that response under a nonexistent
    ``data`` field.
    """

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        if not isinstance(timeout_seconds, int | float) or isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("provider timeout must be positive")
        self._timeout_seconds = float(timeout_seconds)

    async def fetch_tenant_token(
        self,
        credential: FeishuProviderCredential,
    ) -> FeishuTenantTokenResponse:
        sdk = _load_sdk()
        client = self._build_client(credential, sdk)
        request = (
            sdk["InternalTenantAccessTokenRequest"]
            .builder()
            .request_body(sdk["InternalTenantAccessTokenRequestBody"].builder().app_id(credential.app_id).app_secret(credential.app_secret).build())
            .build()
        )
        try:
            response = await client.auth.v3.tenant_access_token.ainternal(request)
        except sdk["UnmarshalException"]:
            raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID) from None
        except Exception:
            raise FeishuDirectoryClientError(FeishuClientFailure.TRANSPORT) from None
        _require_response_type(response, sdk["InternalTenantAccessTokenResponse"])
        http_status, code = _response_envelope(response)
        if code != 0 or not 200 <= http_status < 300:
            return FeishuTenantTokenResponse(http_status=http_status, code=code)
        token, expires_in = _parse_auth_success_wire(response, expected_code=code)
        return FeishuTenantTokenResponse(
            http_status=http_status,
            code=code,
            tenant_access_token=token,
            expires_in_seconds=expires_in,
        )

    async def get_tenant(
        self,
        credential: FeishuProviderCredential,
        *,
        tenant_access_token: str,
    ) -> FeishuTenantResponse:
        sdk = _load_sdk()
        client = self._build_client(credential, sdk)
        request = sdk["QueryTenantRequest"].builder().build()
        option = sdk["RequestOption"].builder().tenant_access_token(tenant_access_token).build()
        try:
            response = await client.tenant.v2.tenant.aquery(request, option)
        except sdk["UnmarshalException"]:
            raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID) from None
        except Exception:
            raise FeishuDirectoryClientError(FeishuClientFailure.TRANSPORT) from None
        _require_response_type(response, sdk["QueryTenantResponse"])
        http_status, code = _response_envelope(response)
        if code != 0 or not 200 <= http_status < 300:
            return FeishuTenantResponse(http_status=http_status, code=code)
        data = response.data
        if not isinstance(data, sdk["QueryTenantResponseBody"]) or not isinstance(data.tenant, sdk["Tenant"]):
            raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID)
        tenant_key = _optional_text(data.tenant.tenant_key, max_length=255, required=True)
        return FeishuTenantResponse(http_status=http_status, code=code, tenant_key=tenant_key)

    async def get_user(
        self,
        credential: FeishuProviderCredential,
        *,
        tenant_access_token: str,
        identifier_type: str,
        identifier_value: str,
    ) -> FeishuGetUserResponse:
        sdk = _load_sdk()
        client = self._build_client(credential, sdk)
        request = sdk["GetUserRequest"].builder().user_id_type(identifier_type).user_id(identifier_value).build()
        option = sdk["RequestOption"].builder().tenant_access_token(tenant_access_token).build()
        try:
            response = await client.contact.v3.user.aget(request, option)
        except sdk["UnmarshalException"]:
            raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID) from None
        except Exception:
            raise FeishuDirectoryClientError(FeishuClientFailure.TRANSPORT) from None
        _require_response_type(response, sdk["GetUserResponse"])
        http_status, code = _response_envelope(response)
        if code != 0 or not 200 <= http_status < 300:
            return FeishuGetUserResponse(http_status=http_status, code=code)
        data = response.data
        if not isinstance(data, sdk["GetUserResponseBody"]) or not isinstance(data.user, sdk["User"]):
            raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID)
        user = data.user
        status = user.status
        if not isinstance(status, sdk["UserStatus"]):
            raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID)
        status_values = (
            status.is_frozen,
            status.is_resigned,
            status.is_activated,
            status.is_exited,
            status.is_unjoin,
        )
        if any(type(value) is not bool for value in status_values):
            raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID)
        user_id = _optional_text(user.user_id, max_length=255, required=True)
        if user_id is None:
            raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID)
        return FeishuGetUserResponse(
            http_status=http_status,
            code=code,
            user=FeishuDirectoryUser(
                user_id=user_id,
                open_id=_optional_text(user.open_id, max_length=255),
                union_id=_optional_text(user.union_id, max_length=255),
                employee_no=_optional_text(user.employee_no, max_length=255),
                display_name=_optional_text(user.name, max_length=_MAX_PROVIDER_VALUE_LENGTH),
                is_frozen=status_values[0],
                is_resigned=status_values[1],
                is_activated=status_values[2],
                is_exited=status_values[3],
                is_unjoin=status_values[4],
            ),
        )

    def _build_client(self, credential: FeishuProviderCredential, sdk: dict[str, Any]) -> Any:
        builder = sdk["Client"].builder().app_id(credential.app_id).app_secret(credential.app_secret).timeout(self._timeout_seconds).enable_set_token(True).source("multirag-eim-i4")
        if credential.domain is FeishuDomain.LARK:
            builder = builder.domain(sdk["LARK_DOMAIN"])
        else:
            builder = builder.domain(sdk["FEISHU_DOMAIN"])
        return builder.build()


def _load_sdk() -> dict[str, Any]:
    try:
        from lark_oapi import Client
        from lark_oapi.api.auth.v3 import (
            InternalTenantAccessTokenRequest,
            InternalTenantAccessTokenRequestBody,
            InternalTenantAccessTokenResponse,
        )
        from lark_oapi.api.contact.v3 import GetUserRequest, GetUserResponse, GetUserResponseBody, User, UserStatus
        from lark_oapi.api.tenant.v2 import QueryTenantRequest, QueryTenantResponse, QueryTenantResponseBody, Tenant
        from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN
        from lark_oapi.core.exception import UnmarshalException
        from lark_oapi.core.model import RequestOption
    except Exception:
        raise FeishuDirectoryClientError(FeishuClientFailure.TRANSPORT) from None
    return {
        "Client": Client,
        "FEISHU_DOMAIN": FEISHU_DOMAIN,
        "GetUserRequest": GetUserRequest,
        "GetUserResponse": GetUserResponse,
        "GetUserResponseBody": GetUserResponseBody,
        "InternalTenantAccessTokenRequest": InternalTenantAccessTokenRequest,
        "InternalTenantAccessTokenRequestBody": InternalTenantAccessTokenRequestBody,
        "InternalTenantAccessTokenResponse": InternalTenantAccessTokenResponse,
        "LARK_DOMAIN": LARK_DOMAIN,
        "QueryTenantRequest": QueryTenantRequest,
        "QueryTenantResponse": QueryTenantResponse,
        "QueryTenantResponseBody": QueryTenantResponseBody,
        "RequestOption": RequestOption,
        "Tenant": Tenant,
        "UnmarshalException": UnmarshalException,
        "User": User,
        "UserStatus": UserStatus,
    }


def _require_response_type(response: object, expected: type[Any]) -> None:
    if not isinstance(response, expected):
        raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID)


def _response_envelope(response: Any) -> tuple[int, int]:
    raw = response.raw
    if raw is None or type(raw.status_code) is not int or not 100 <= raw.status_code <= 599:
        raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID)
    if type(response.code) is not int:
        raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID)
    return raw.status_code, response.code


def _parse_auth_success_wire(
    response: Any,
    *,
    expected_code: int,
) -> tuple[str, int]:
    raw = response.raw
    content = raw.content if raw is not None else None
    if type(content) is not bytes:
        raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID)
    try:
        payload: object = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID) from None
    if type(payload) is not dict:
        raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID)
    wire_code = payload.get("code")
    if type(wire_code) is not int or wire_code != expected_code:
        raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID)
    token = _optional_text(
        payload.get("tenant_access_token"),
        max_length=_MAX_TOKEN_LENGTH,
        required=True,
    )
    expires_in = payload.get("expire")
    if token is None or type(expires_in) is not int or expires_in <= 0 or expires_in > 86_400:
        raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID)
    return token, expires_in


def _optional_text(
    value: object,
    *,
    max_length: int,
    required: bool = False,
) -> str | None:
    if not required and (value is None or value == ""):
        return None
    if type(value) is not str or not value.strip() or len(value) > max_length:
        raise FeishuDirectoryClientError(FeishuClientFailure.RESPONSE_INVALID)
    return value

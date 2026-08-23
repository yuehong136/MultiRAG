"""Feishu Contact V3 enterprise identity provider for EIM-I4."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from api.identity.contracts import ProviderContext
from api.identity.providers.contracts import (
    ExternalIdentityAssertion,
    ExternalIdentityIdentifier,
    FeishuClientFailure,
    FeishuDirectoryClient,
    FeishuDirectoryClientError,
    FeishuDirectoryUser,
    FeishuDomain,
    FeishuProviderCredential,
    ProviderCredentialError,
    ProviderCredentialResolver,
    ProviderDirectoryStatus,
    ProviderErrorCode,
    ProviderIdentifierKind,
    ProviderIdentity,
    ProviderIdentityResult,
    ProviderIdentityStatus,
)
from api.identity.providers.lark_oapi import LarkOapiFeishuDirectoryClient
from api.identity.providers.runtime import (
    AsyncExpiringSingleFlightCache,
    PerKeyRateLimiter,
    ProducedValue,
    ProviderRuntimeCapacityError,
)
from api.identity.validation import valid_provider_context, valid_text, valid_timestamp

_POSITIVE_CACHE_TTL_SECONDS = 300.0
_NEGATIVE_CACHE_TTL_SECONDS = 30.0
_TOKEN_EXPIRY_SAFETY_SECONDS = 600
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
_DEFAULT_CONTACT_CALLS_PER_SECOND = 15.0
_DEFAULT_RECONCILIATION_CALLS_PER_SECOND = 1.0
_MAX_TOKEN_CACHE_ENTRIES = 512
_MAX_IDENTITY_CACHE_ENTRIES = 2_048
_MAX_IN_FLIGHT = 512

_SCOPE_ERROR_CODES = frozenset({41050})
_NOT_FOUND_ERROR_CODES = frozenset({41012})
_CONTACT_INVALID_ERROR_CODES = frozenset({10003, 40001})
_CONTACT_PROVIDER_ERROR_CODES = frozenset({10005, 10015, 20002})
_AUTH_CREDENTIAL_ERROR_CODES = frozenset({10015, 20002})
_CONTROL_PLANE_KNOWN_ERROR_CODES = frozenset({10003, 10005, 10015, 20002})
_TRANSIENT_ERROR_CODES = frozenset({40003})


@dataclass(frozen=True, slots=True)
class _AccountGeneration:
    tenant_id: str
    provider_tenant_key: str = field(repr=False)
    provider_account_id: str = field(repr=False)
    account_revision: int
    last_scope_change_at: datetime | None
    credential_generation: int
    domain: FeishuDomain


@dataclass(frozen=True, slots=True)
class _DirectoryLookupKey:
    account: _AccountGeneration
    identifier_type: ProviderIdentifierKind
    identifier_value: str = field(repr=False)


class _ProviderCallFailed(RuntimeError):
    def __init__(self, result: ProviderIdentityResult) -> None:
        self.result = result
        super().__init__(result.error_code.value if result.error_code is not None else ProviderErrorCode.PROVIDER_UNAVAILABLE.value)


class FeishuEnterpriseIdentityProvider:
    """Provider-neutral orchestration over the lazy official SDK adapter."""

    def __init__(
        self,
        credential_resolver: ProviderCredentialResolver,
        *,
        reconciliation_credential_resolver: ProviderCredentialResolver,
        directory_client: FeishuDirectoryClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        request_timeout_seconds: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
        contact_calls_per_second: float = _DEFAULT_CONTACT_CALLS_PER_SECOND,
        reconciliation_calls_per_second: float = _DEFAULT_RECONCILIATION_CALLS_PER_SECOND,
    ) -> None:
        if not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0 or not math.isfinite(reconciliation_calls_per_second) or reconciliation_calls_per_second <= 0:
            raise ValueError("provider runtime limits must be finite and positive")
        self._credential_resolver = credential_resolver
        self._reconciliation_credential_resolver = reconciliation_credential_resolver
        self._directory_client = directory_client or LarkOapiFeishuDirectoryClient(timeout_seconds=request_timeout_seconds)
        self._clock = clock
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._request_timeout_seconds = request_timeout_seconds
        self._token_cache = AsyncExpiringSingleFlightCache[_AccountGeneration, str](
            max_entries=_MAX_TOKEN_CACHE_ENTRIES,
            max_in_flight=_MAX_IN_FLIGHT,
            clock=clock,
        )
        self._identity_cache = AsyncExpiringSingleFlightCache[_DirectoryLookupKey, ProviderIdentityResult](
            max_entries=_MAX_IDENTITY_CACHE_ENTRIES,
            max_in_flight=_MAX_IN_FLIGHT,
            clock=clock,
        )
        self._rate_limiter = PerKeyRateLimiter[_AccountGeneration](
            calls_per_second=contact_calls_per_second,
            max_keys=_MAX_TOKEN_CACHE_ENTRIES,
            clock=clock,
            sleep=sleep,
        )
        self._reconciliation_rate_limiter = PerKeyRateLimiter[_AccountGeneration](
            calls_per_second=reconciliation_calls_per_second,
            max_keys=_MAX_TOKEN_CACHE_ENTRIES,
            clock=clock,
            sleep=sleep,
        )

    async def resolve(
        self,
        context: ProviderContext,
        assertion: ExternalIdentityAssertion,
    ) -> ProviderIdentityResult:
        validation = _validate_resolve_input(context, assertion)
        if validation is not None:
            return validation
        credential = await self._resolve_credential(context)
        if isinstance(credential, ProviderIdentityResult):
            return credential
        identifiers = {identifier.kind: identifier.value for identifier in assertion.identifiers}
        result = await self._lookup(
            context,
            credential,
            ProviderIdentifierKind.OPEN_ID,
            identifiers[ProviderIdentifierKind.OPEN_ID],
        )
        if result.status is not ProviderIdentityStatus.RESOLVED or result.identity is None:
            return result
        identity = result.identity
        if identity.open_id is None:
            return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
        for kind, expected in identifiers.items():
            actual = {
                ProviderIdentifierKind.OPEN_ID: identity.open_id,
                ProviderIdentifierKind.USER_ID: identity.provider_user_id,
                ProviderIdentifierKind.UNION_ID: identity.union_id,
            }[kind]
            if actual != expected:
                return _result(ProviderIdentityStatus.CONFLICT, ProviderErrorCode.LINK_CONFLICT)
        return result

    async def refresh(
        self,
        context: ProviderContext,
        provider_user_id: str,
    ) -> ProviderIdentityResult:
        if not valid_provider_context(context):
            return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
        if context.provider != "feishu":
            return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.PROVIDER_MISMATCH)
        if not valid_text(provider_user_id, max_length=255):
            return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
        credential = await self._resolve_credential(context)
        if isinstance(credential, ProviderIdentityResult):
            return credential
        result = await self._lookup(
            context,
            credential,
            ProviderIdentifierKind.USER_ID,
            provider_user_id,
        )
        if result.status is ProviderIdentityStatus.RESOLVED and result.identity is not None and result.identity.provider_user_id != provider_user_id:
            return _result(ProviderIdentityStatus.CONFLICT, ProviderErrorCode.LINK_CONFLICT)
        return result

    async def reconcile(
        self,
        context: ProviderContext,
        provider_user_id: str,
    ) -> ProviderIdentityResult:
        """Probe one canonical user without consulting the identity cache.

        Token verification remains shared and cached, while Contact calls use
        a lower-priority per-account limiter so reconciliation cannot consume
        the foreground identity lookup budget.
        """

        invalid = _validate_refresh_input(context, provider_user_id)
        if invalid is not None:
            return invalid
        credential = await self._resolve_reconciliation_credential(context)
        if isinstance(credential, ProviderIdentityResult):
            return credential
        account = _account_generation(context, credential)
        try:
            result = await self._fetch_identity(
                context,
                credential,
                account,
                ProviderIdentifierKind.USER_ID,
                provider_user_id,
                rate_limiter=self._reconciliation_rate_limiter,
            )
        except _ProviderCallFailed as exc:
            return exc.result
        except ProviderRuntimeCapacityError:
            return _result(
                ProviderIdentityStatus.UNAVAILABLE,
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                retryable=True,
            )
        except FeishuDirectoryClientError as exc:
            return _client_failure_result(exc.failure)
        except Exception:
            return _result(
                ProviderIdentityStatus.UNAVAILABLE,
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                retryable=True,
            )
        if result.status is ProviderIdentityStatus.RESOLVED and result.identity is not None and result.identity.provider_user_id != provider_user_id:
            return _result(
                ProviderIdentityStatus.CONFLICT,
                ProviderErrorCode.LINK_CONFLICT,
            )
        return replace(result, from_cache=False)

    async def invalidate(self, context: ProviderContext) -> None:
        """Invalidate completed cache entries for a trusted account event."""

        if not valid_provider_context(context):
            return
        await self._token_cache.invalidate(lambda key: key.provider_account_id == context.provider_account_id)
        await self._identity_cache.invalidate(lambda key: key.account.provider_account_id == context.provider_account_id)

    async def _resolve_credential(
        self,
        context: ProviderContext,
    ) -> FeishuProviderCredential | ProviderIdentityResult:
        return await self._resolve_credential_with(
            self._credential_resolver,
            context,
        )

    async def _resolve_reconciliation_credential(
        self,
        context: ProviderContext,
    ) -> FeishuProviderCredential | ProviderIdentityResult:
        return await self._resolve_credential_with(
            self._reconciliation_credential_resolver,
            context,
        )

    async def _resolve_credential_with(
        self,
        resolver: ProviderCredentialResolver,
        context: ProviderContext,
    ) -> FeishuProviderCredential | ProviderIdentityResult:
        try:
            credential = await resolver.resolve(context)
        except ProviderCredentialError as exc:
            return _result(ProviderIdentityStatus.UNAVAILABLE, exc.code)
        except Exception:
            return _result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.CREDENTIAL_UNAVAILABLE)
        if not _valid_credential(context, credential):
            return _result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.CREDENTIAL_UNAVAILABLE)
        return credential

    async def _lookup(
        self,
        context: ProviderContext,
        credential: FeishuProviderCredential,
        identifier_type: ProviderIdentifierKind,
        identifier_value: str,
    ) -> ProviderIdentityResult:
        account = _account_generation(context, credential)
        key = _DirectoryLookupKey(account, identifier_type, identifier_value)

        async def produce() -> ProducedValue[ProviderIdentityResult]:
            result = await self._fetch_identity(
                context,
                credential,
                account,
                identifier_type,
                identifier_value,
            )
            return ProducedValue(result, _identity_result_ttl(result))

        try:
            result, from_cache = await self._identity_cache.get_or_create(key, produce)
        except _ProviderCallFailed as exc:
            return exc.result
        except ProviderRuntimeCapacityError:
            return _result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, retryable=True)
        except FeishuDirectoryClientError as exc:
            return _client_failure_result(exc.failure)
        except Exception:
            return _result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, retryable=True)
        return replace(result, from_cache=from_cache)

    async def _fetch_identity(
        self,
        context: ProviderContext,
        credential: FeishuProviderCredential,
        account: _AccountGeneration,
        identifier_type: ProviderIdentifierKind,
        identifier_value: str,
        *,
        rate_limiter: PerKeyRateLimiter[_AccountGeneration] | None = None,
    ) -> ProviderIdentityResult:
        token = await self._verified_tenant_token(context, credential, account)
        try:
            await (rate_limiter or self._rate_limiter).wait(account)
            async with asyncio.timeout(self._request_timeout_seconds):
                response = await self._directory_client.get_user(
                    credential,
                    tenant_access_token=token,
                    identifier_type=identifier_type.value,
                    identifier_value=identifier_value,
                )
        except TimeoutError:
            return _result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, retryable=True)
        except ProviderRuntimeCapacityError:
            return _result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, retryable=True)
        except FeishuDirectoryClientError as exc:
            return _client_failure_result(exc.failure)
        if not _valid_envelope(response.http_status, response.code):
            return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
        if response.http_status == 401:
            await self._token_cache.invalidate(lambda key: key == account)
        if response.code != 0 or not 200 <= response.http_status < 300:
            return _classify_contact_error(response.http_status, response.code)
        if response.user is None:
            return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
        return _identity_from_user(context, response.user, self._now())

    async def _verified_tenant_token(
        self,
        context: ProviderContext,
        credential: FeishuProviderCredential,
        account: _AccountGeneration,
    ) -> str:
        async def produce() -> ProducedValue[str]:
            try:
                async with asyncio.timeout(self._request_timeout_seconds):
                    response = await self._directory_client.fetch_tenant_token(credential)
            except TimeoutError:
                raise _ProviderCallFailed(_result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, retryable=True)) from None
            except FeishuDirectoryClientError as exc:
                raise _ProviderCallFailed(_client_failure_result(exc.failure)) from None
            if not _valid_envelope(response.http_status, response.code):
                raise _ProviderCallFailed(_result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID))
            if response.code != 0 or not 200 <= response.http_status < 300:
                raise _ProviderCallFailed(_classify_auth_error(response.http_status, response.code))
            token = response.tenant_access_token
            expires_in = response.expires_in_seconds
            if not (type(token) is str and valid_text(token, max_length=16_384) and type(expires_in) is int and 0 < expires_in <= 86_400):
                raise _ProviderCallFailed(_result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID))
            try:
                async with asyncio.timeout(self._request_timeout_seconds):
                    tenant = await self._directory_client.get_tenant(
                        credential,
                        tenant_access_token=token,
                    )
            except TimeoutError:
                raise _ProviderCallFailed(_result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, retryable=True)) from None
            except FeishuDirectoryClientError as exc:
                raise _ProviderCallFailed(_client_failure_result(exc.failure)) from None
            if not _valid_envelope(tenant.http_status, tenant.code):
                raise _ProviderCallFailed(_result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID))
            if tenant.code != 0 or not 200 <= tenant.http_status < 300:
                raise _ProviderCallFailed(_classify_tenant_error(tenant.http_status, tenant.code))
            if not valid_text(tenant.tenant_key, max_length=255):
                raise _ProviderCallFailed(_result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID))
            if tenant.tenant_key != context.provider_tenant_key:
                raise _ProviderCallFailed(_result(ProviderIdentityStatus.CONFLICT, ProviderErrorCode.TENANT_MISMATCH))
            return ProducedValue(
                token,
                max(0.0, float(expires_in - _TOKEN_EXPIRY_SAFETY_SECONDS)),
            )

        try:
            token, _from_cache = await self._token_cache.get_or_create(account, produce)
            return token
        except ProviderRuntimeCapacityError:
            raise _ProviderCallFailed(_result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, retryable=True)) from None


def _validate_resolve_input(
    context: ProviderContext,
    assertion: ExternalIdentityAssertion,
) -> ProviderIdentityResult | None:
    if not valid_provider_context(context) or not isinstance(assertion, ExternalIdentityAssertion):
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
    if context.provider != "feishu" or assertion.provider != context.provider:
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.PROVIDER_MISMATCH)
    if assertion.provider_tenant_key is not None and assertion.provider_tenant_key != context.provider_tenant_key:
        return _result(ProviderIdentityStatus.CONFLICT, ProviderErrorCode.TENANT_MISMATCH)
    if type(assertion.identifiers) is not tuple or not 1 <= len(assertion.identifiers) <= 3:
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
    seen: set[ProviderIdentifierKind] = set()
    for identifier in assertion.identifiers:
        if (
            not isinstance(identifier, ExternalIdentityIdentifier)
            or not isinstance(identifier.kind, ProviderIdentifierKind)
            or identifier.kind in seen
            or not valid_text(identifier.value, max_length=255)
        ):
            return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
        seen.add(identifier.kind)
    if ProviderIdentifierKind.OPEN_ID not in seen:
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
    return None


def _validate_refresh_input(
    context: ProviderContext,
    provider_user_id: str,
) -> ProviderIdentityResult | None:
    if not valid_provider_context(context):
        return _result(
            ProviderIdentityStatus.INVALID,
            ProviderErrorCode.ASSERTION_INVALID,
        )
    if context.provider != "feishu":
        return _result(
            ProviderIdentityStatus.INVALID,
            ProviderErrorCode.PROVIDER_MISMATCH,
        )
    if not valid_text(provider_user_id, max_length=255):
        return _result(
            ProviderIdentityStatus.INVALID,
            ProviderErrorCode.ASSERTION_INVALID,
        )
    return None


def _valid_credential(
    context: ProviderContext,
    credential: object,
) -> bool:
    return isinstance(credential, FeishuProviderCredential) and bool(
        credential.provider_account_id == context.provider_account_id
        and credential.app_id == context.provider_account_key
        and valid_text(credential.app_secret, max_length=4_096)
        and type(credential.credential_generation) is int
        and 1 <= credential.credential_generation <= (1 << 63) - 1
        and isinstance(credential.domain, FeishuDomain)
    )


def _account_generation(
    context: ProviderContext,
    credential: FeishuProviderCredential,
) -> _AccountGeneration:
    return _AccountGeneration(
        tenant_id=context.tenant_id,
        provider_tenant_key=context.provider_tenant_key,
        provider_account_id=context.provider_account_id,
        account_revision=context.provider_account_revision,
        last_scope_change_at=context.provider_account_last_scope_change_at,
        credential_generation=credential.credential_generation,
        domain=credential.domain,
    )


def _identity_from_user(
    context: ProviderContext,
    user: FeishuDirectoryUser,
    verified_at: datetime,
) -> ProviderIdentityResult:
    if not valid_timestamp(verified_at):
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
    if not valid_text(user.user_id, max_length=255):
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
    optional_values = (user.open_id, user.union_id, user.employee_no)
    if any(value is not None and not valid_text(value, max_length=255) for value in optional_values):
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
    if user.display_name is not None and not valid_text(user.display_name, max_length=512):
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
    statuses = (
        user.is_frozen,
        user.is_resigned,
        user.is_activated,
        user.is_exited,
        user.is_unjoin,
    )
    if any(type(value) is not bool for value in statuses):
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
    if not user.is_activated or user.is_frozen or user.is_resigned or user.is_exited or user.is_unjoin:
        return _result(ProviderIdentityStatus.INACTIVE, ProviderErrorCode.INACTIVE)
    return ProviderIdentityResult(
        status=ProviderIdentityStatus.RESOLVED,
        identity=ProviderIdentity(
            provider=context.provider,
            provider_tenant_key=context.provider_tenant_key,
            provider_account_id=context.provider_account_id,
            provider_user_id=user.user_id,
            verified_at=verified_at,
            open_id=user.open_id,
            union_id=user.union_id,
            employee_no=user.employee_no,
            display_name=user.display_name,
            provider_status=ProviderDirectoryStatus.ACTIVE,
        ),
    )


def _classify_contact_error(http_status: int, code: int) -> ProviderIdentityResult:
    if not _valid_envelope(http_status, code):
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
    # Known business codes take precedence over an edge-rewritten HTTP status.
    # Otherwise an app/control-plane failure could become a user miss and enter
    # linking/JIT, while an invalid identifier could be misreported as a bad
    # provider credential.
    if code in _CONTACT_INVALID_ERROR_CODES:
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
    if code in _CONTACT_PROVIDER_ERROR_CODES:
        return _result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE)
    if code in _SCOPE_ERROR_CODES:
        return _result(ProviderIdentityStatus.NOT_IN_SCOPE, ProviderErrorCode.NOT_IN_SCOPE)
    if code in _NOT_FOUND_ERROR_CODES:
        return _result(ProviderIdentityStatus.NOT_FOUND, ProviderErrorCode.NOT_FOUND)
    if code in _TRANSIENT_ERROR_CODES:
        return _result(
            ProviderIdentityStatus.UNAVAILABLE,
            ProviderErrorCode.PROVIDER_UNAVAILABLE,
            retryable=True,
        )
    # An unknown business failure is never evidence that an identity is absent
    # or out of scope.  Only a code-zero transport response may use 403/404 as
    # the Contact outcome; otherwise linking/JIT could run on an upstream error.
    if code != 0:
        return _result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE)
    if http_status == 403:
        return _result(ProviderIdentityStatus.NOT_IN_SCOPE, ProviderErrorCode.NOT_IN_SCOPE)
    if http_status == 404:
        return _result(ProviderIdentityStatus.NOT_FOUND, ProviderErrorCode.NOT_FOUND)
    retryable = http_status in {401, 429} or http_status >= 500
    return _result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, retryable=retryable)


def _classify_auth_error(http_status: int, code: int) -> ProviderIdentityResult:
    """Classify Auth failures without creating an identity-miss signal."""

    if not _valid_envelope(http_status, code):
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
    if code in _AUTH_CREDENTIAL_ERROR_CODES:
        return _result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.CREDENTIAL_UNAVAILABLE)
    return _classify_control_plane_error(http_status, code)


def _classify_tenant_error(http_status: int, code: int) -> ProviderIdentityResult:
    """Classify tenant-ownership failures without an identity-miss signal."""

    if not _valid_envelope(http_status, code):
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
    return _classify_control_plane_error(http_status, code)


def _classify_control_plane_error(http_status: int, code: int) -> ProviderIdentityResult:
    """Map shared Auth/Tenant transport and service failures.

    ``NOT_FOUND`` and ``NOT_IN_SCOPE`` are actionable Contact outcomes.  An
    Auth or tenant-ownership failure must never trigger linking/JIT logic, even
    when the upstream transport happens to return the same HTTP status.
    """

    if not _valid_envelope(http_status, code):
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
    if code in _CONTROL_PLANE_KNOWN_ERROR_CODES:
        return _result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE)
    retryable = http_status in {401, 429} or http_status >= 500 or code in _TRANSIENT_ERROR_CODES
    return _result(
        ProviderIdentityStatus.UNAVAILABLE,
        ProviderErrorCode.PROVIDER_UNAVAILABLE,
        retryable=retryable,
    )


def _valid_envelope(http_status: object, code: object) -> bool:
    return type(http_status) is int and 100 <= http_status <= 599 and type(code) is int


def _client_failure_result(failure: FeishuClientFailure) -> ProviderIdentityResult:
    if failure is FeishuClientFailure.RESPONSE_INVALID:
        return _result(ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID)
    return _result(ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, retryable=True)


def _identity_result_ttl(result: ProviderIdentityResult) -> float:
    if result.status is ProviderIdentityStatus.RESOLVED:
        return _POSITIVE_CACHE_TTL_SECONDS
    if result.status in {
        ProviderIdentityStatus.NOT_FOUND,
        ProviderIdentityStatus.NOT_IN_SCOPE,
        ProviderIdentityStatus.INACTIVE,
    }:
        return _NEGATIVE_CACHE_TTL_SECONDS
    return 0.0


def _result(
    status: ProviderIdentityStatus,
    error_code: ProviderErrorCode,
    *,
    retryable: bool = False,
) -> ProviderIdentityResult:
    return ProviderIdentityResult(
        status=status,
        error_code=error_code,
        retryable=retryable,
    )

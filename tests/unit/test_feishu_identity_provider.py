"""Fail-closed orchestration tests for the EIM-I4 Feishu provider."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from api.identity.contracts import ProviderContext
from api.identity.providers import (
    ExternalIdentityAssertion,
    ExternalIdentityIdentifier,
    FeishuDirectoryUser,
    FeishuDomain,
    FeishuEnterpriseIdentityProvider,
    FeishuGetUserResponse,
    FeishuProviderCredential,
    FeishuTenantResponse,
    FeishuTenantTokenResponse,
    ProviderErrorCode,
    ProviderIdentifierKind,
    ProviderIdentityStatus,
)

_NOW = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
_SENSITIVE = "must-never-appear"


def _context(**changes: object) -> ProviderContext:
    return replace(
        ProviderContext(
            tenant_id="tenant-test",
            provider="feishu",
            provider_tenant_key="provider-tenant-test",
            provider_account_id="account-test",
            provider_account_key="cli_test",
            provider_account_revision=3,
            provider_account_last_scope_change_at=_NOW,
        ),
        **changes,
    )


def _credential(**changes: object) -> FeishuProviderCredential:
    return replace(
        FeishuProviderCredential(
            provider_account_id="account-test",
            app_id="cli_test",
            app_secret=_SENSITIVE,
            credential_generation=7,
            domain=FeishuDomain.FEISHU,
        ),
        **changes,
    )


def _assertion(*identifiers: ExternalIdentityIdentifier) -> ExternalIdentityAssertion:
    return ExternalIdentityAssertion(
        provider="feishu",
        provider_tenant_key="provider-tenant-test",
        identifiers=identifiers
        or (
            ExternalIdentityIdentifier(
                ProviderIdentifierKind.OPEN_ID,
                "open-test",
            ),
        ),
    )


class _Credentials:
    def __init__(self, credential: FeishuProviderCredential | None = None) -> None:
        self.credential = credential or _credential()
        self.calls = 0

    async def resolve(self, context: ProviderContext) -> FeishuProviderCredential:
        del context
        self.calls += 1
        return self.credential


class _Directory:
    def __init__(self) -> None:
        self.token_calls = 0
        self.tenant_calls = 0
        self.user_calls = 0
        self.tenant_key = "provider-tenant-test"
        self.user = FeishuDirectoryUser(
            user_id="user-test",
            open_id="open-test",
            union_id="union-test",
            employee_no=None,
            display_name=None,
            is_activated=True,
        )
        self.token_gate: asyncio.Event | None = None
        self.token_entered = asyncio.Event()

    async def fetch_tenant_token(
        self,
        credential: FeishuProviderCredential,
    ) -> FeishuTenantTokenResponse:
        del credential
        self.token_calls += 1
        self.token_entered.set()
        if self.token_gate is not None:
            await self.token_gate.wait()
        return FeishuTenantTokenResponse(
            http_status=200,
            code=0,
            tenant_access_token="token-test",
            expires_in_seconds=7200,
        )

    async def get_tenant(
        self,
        credential: FeishuProviderCredential,
        *,
        tenant_access_token: str,
    ) -> FeishuTenantResponse:
        del credential, tenant_access_token
        self.tenant_calls += 1
        return FeishuTenantResponse(
            http_status=200,
            code=0,
            tenant_key=self.tenant_key,
        )

    async def get_user(
        self,
        credential: FeishuProviderCredential,
        *,
        tenant_access_token: str,
        identifier_type: str,
        identifier_value: str,
    ) -> FeishuGetUserResponse:
        del credential, tenant_access_token, identifier_type, identifier_value
        self.user_calls += 1
        return FeishuGetUserResponse(http_status=200, code=0, user=self.user)


def _provider(
    directory: _Directory,
    credentials: _Credentials | None = None,
    *,
    clock: Callable[[], float] | None = None,
) -> FeishuEnterpriseIdentityProvider:
    return FeishuEnterpriseIdentityProvider(
        credentials or _Credentials(),
        directory_client=directory,
        clock=clock or time.monotonic,
        now=lambda: _NOW,
        contact_calls_per_second=1.0,
        sleep=lambda _delay: asyncio.sleep(0),
    )


@pytest.mark.parametrize("limit", [0.0, -1.0, float("inf"), float("nan")])
def test_reconciliation_limiter_must_be_finite_and_positive(limit: float) -> None:
    with pytest.raises(ValueError, match="provider runtime limits"):
        FeishuEnterpriseIdentityProvider(
            _Credentials(),
            directory_client=_Directory(),
            reconciliation_calls_per_second=limit,
        )


async def test_resolve_verifies_tenant_projects_only_whitelisted_identity_and_caches() -> None:
    directory = _Directory()
    provider = _provider(directory)

    first = await provider.resolve(_context(), _assertion())
    second = await provider.resolve(_context(), _assertion())

    assert first.status is ProviderIdentityStatus.RESOLVED
    assert first.identity is not None
    assert first.identity.provider_user_id == "user-test"
    assert first.identity.open_id == "open-test"
    assert first.identity.employee_no is None
    assert first.identity.verified_at == _NOW
    assert first.from_cache is False
    assert second.from_cache is True
    assert (directory.token_calls, directory.tenant_calls, directory.user_calls) == (1, 1, 1)
    assert _SENSITIVE not in repr(first)
    assert "provider-tenant-test" not in repr(first.identity)
    assert "open-test" not in repr(first.identity)


async def test_reconciliation_bypasses_identity_cache_but_reuses_verified_token() -> None:
    directory = _Directory()
    provider = _provider(directory)

    cached = await provider.refresh(_context(), "user-test")
    first = await provider.reconcile(_context(), "user-test")
    second = await provider.reconcile(_context(), "user-test")

    assert cached.status is ProviderIdentityStatus.RESOLVED
    assert first.status is second.status is ProviderIdentityStatus.RESOLVED
    assert first.from_cache is second.from_cache is False
    assert directory.token_calls == 1
    assert directory.tenant_calls == 1
    assert directory.user_calls == 3


async def test_reconciliation_rejects_mismatched_subject_and_never_caches_result() -> None:
    directory = _Directory()
    directory.user = replace(directory.user, user_id="different-user")
    provider = _provider(directory)

    first = await provider.reconcile(_context(), "user-test")
    second = await provider.reconcile(_context(), "user-test")

    assert first.status is second.status is ProviderIdentityStatus.CONFLICT
    assert first.error_code is second.error_code is ProviderErrorCode.LINK_CONFLICT
    assert directory.user_calls == 2


async def test_tenant_mismatch_fails_closed_before_contact_and_is_not_cached() -> None:
    directory = _Directory()
    directory.tenant_key = "wrong-provider-tenant"
    provider = _provider(directory)

    first = await provider.resolve(_context(), _assertion())
    second = await provider.resolve(_context(), _assertion())

    assert first.status is ProviderIdentityStatus.CONFLICT
    assert first.error_code is ProviderErrorCode.TENANT_MISMATCH
    assert second.status is ProviderIdentityStatus.CONFLICT
    assert directory.user_calls == 0
    assert directory.token_calls == 2
    assert directory.tenant_calls == 2


@pytest.mark.parametrize(
    "user",
    [
        FeishuDirectoryUser(user_id="user-test", open_id="open-test", is_activated=False),
        FeishuDirectoryUser(user_id="user-test", open_id="open-test", is_activated=True, is_frozen=True),
        FeishuDirectoryUser(user_id="user-test", open_id="open-test", is_activated=True, is_resigned=True),
        FeishuDirectoryUser(user_id="user-test", open_id="open-test", is_activated=True, is_exited=True),
        FeishuDirectoryUser(user_id="user-test", open_id="open-test", is_activated=True, is_unjoin=True),
    ],
)
async def test_every_non_active_directory_state_is_inactive_and_negative_cached(
    user: FeishuDirectoryUser,
) -> None:
    directory = _Directory()
    directory.user = user
    provider = _provider(directory)

    first = await provider.resolve(_context(), _assertion())
    second = await provider.resolve(_context(), _assertion())

    assert first.status is ProviderIdentityStatus.INACTIVE
    assert first.error_code is ProviderErrorCode.INACTIVE
    assert second.from_cache is True
    assert directory.user_calls == 1


@pytest.mark.parametrize(
    ("http_status", "code", "status", "error", "retryable"),
    [
        (403, 41050, ProviderIdentityStatus.NOT_IN_SCOPE, ProviderErrorCode.NOT_IN_SCOPE, False),
        (404, 41012, ProviderIdentityStatus.NOT_FOUND, ProviderErrorCode.NOT_FOUND, False),
        (404, 10003, ProviderIdentityStatus.INVALID, ProviderErrorCode.ASSERTION_INVALID, False),
        (403, 10005, ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, False),
        (404, 10015, ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, False),
        (404, 20002, ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, False),
        (403, 99999, ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, False),
        (404, 99999, ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, False),
        (403, 40003, ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, True),
        (404, 40003, ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, True),
        (429, 0, ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, True),
        (503, 40003, ProviderIdentityStatus.UNAVAILABLE, ProviderErrorCode.PROVIDER_UNAVAILABLE, True),
    ],
)
async def test_contact_errors_have_stable_non_overlapping_classification(
    http_status: int,
    code: int,
    status: ProviderIdentityStatus,
    error: ProviderErrorCode,
    retryable: bool,
) -> None:
    class ErrorDirectory(_Directory):
        async def get_user(self, *args: object, **kwargs: object) -> FeishuGetUserResponse:
            del args, kwargs
            self.user_calls += 1
            return FeishuGetUserResponse(http_status=http_status, code=code)

    result = await _provider(ErrorDirectory()).resolve(_context(), _assertion())

    assert result.status is status
    assert result.error_code is error
    assert result.retryable is retryable


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (10003, ProviderErrorCode.PROVIDER_UNAVAILABLE),
        (10005, ProviderErrorCode.PROVIDER_UNAVAILABLE),
        (10015, ProviderErrorCode.CREDENTIAL_UNAVAILABLE),
        (20002, ProviderErrorCode.CREDENTIAL_UNAVAILABLE),
    ],
)
async def test_auth_business_codes_are_stage_aware_and_contact_is_not_called(
    code: int,
    error: ProviderErrorCode,
) -> None:
    class AuthErrorDirectory(_Directory):
        async def fetch_tenant_token(
            self,
            credential: FeishuProviderCredential,
        ) -> FeishuTenantTokenResponse:
            del credential
            self.token_calls += 1
            return FeishuTenantTokenResponse(http_status=200, code=code)

    directory = AuthErrorDirectory()
    result = await _provider(directory).resolve(_context(), _assertion())

    assert result.status is ProviderIdentityStatus.UNAVAILABLE
    assert result.error_code is error
    assert result.retryable is False
    assert directory.tenant_calls == 0
    assert directory.user_calls == 0


@pytest.mark.parametrize("stage", ["token", "tenant"])
async def test_control_plane_404_never_becomes_an_identity_not_found_result(stage: str) -> None:
    class MissingControlPlaneDirectory(_Directory):
        async def fetch_tenant_token(
            self,
            credential: FeishuProviderCredential,
        ) -> FeishuTenantTokenResponse:
            if stage == "token":
                del credential
                self.token_calls += 1
                return FeishuTenantTokenResponse(http_status=404, code=0)
            return await super().fetch_tenant_token(credential)

        async def get_tenant(
            self,
            credential: FeishuProviderCredential,
            *,
            tenant_access_token: str,
        ) -> FeishuTenantResponse:
            if stage == "tenant":
                del credential, tenant_access_token
                self.tenant_calls += 1
                return FeishuTenantResponse(http_status=404, code=0)
            return await super().get_tenant(
                credential,
                tenant_access_token=tenant_access_token,
            )

    directory = MissingControlPlaneDirectory()
    result = await _provider(directory).resolve(_context(), _assertion())

    assert result.status is ProviderIdentityStatus.UNAVAILABLE
    assert result.error_code is ProviderErrorCode.PROVIDER_UNAVAILABLE
    assert result.retryable is False
    assert directory.user_calls == 0


@pytest.mark.parametrize("code", [10003, 10005, 10015, 20002])
async def test_tenant_control_plane_business_codes_never_become_identity_miss(
    code: int,
) -> None:
    class TenantErrorDirectory(_Directory):
        async def get_tenant(
            self,
            credential: FeishuProviderCredential,
            *,
            tenant_access_token: str,
        ) -> FeishuTenantResponse:
            del credential, tenant_access_token
            self.tenant_calls += 1
            return FeishuTenantResponse(http_status=404, code=code)

    directory = TenantErrorDirectory()
    result = await _provider(directory).resolve(_context(), _assertion())

    assert result.status is ProviderIdentityStatus.UNAVAILABLE
    assert result.error_code is ProviderErrorCode.PROVIDER_UNAVAILABLE
    assert result.retryable is False
    assert directory.user_calls == 0


@pytest.mark.parametrize(
    "assertion",
    [
        ExternalIdentityAssertion(
            "dingtalk",
            identifiers=(ExternalIdentityIdentifier(ProviderIdentifierKind.OPEN_ID, "open-test"),),
        ),
        ExternalIdentityAssertion(
            "feishu",
            identifiers=(ExternalIdentityIdentifier(ProviderIdentifierKind.USER_ID, "user-test"),),
        ),
        ExternalIdentityAssertion(
            "feishu",
            identifiers=(
                ExternalIdentityIdentifier(ProviderIdentifierKind.OPEN_ID, "open-test"),
                ExternalIdentityIdentifier(ProviderIdentifierKind.OPEN_ID, "open-test"),
            ),
        ),
        ExternalIdentityAssertion(
            "feishu",
            provider_tenant_key="wrong-provider-tenant",
            identifiers=(ExternalIdentityIdentifier(ProviderIdentifierKind.OPEN_ID, "open-test"),),
        ),
    ],
)
async def test_malformed_or_mismatched_assertions_do_not_resolve_credentials(
    assertion: ExternalIdentityAssertion,
) -> None:
    credentials = _Credentials()
    result = await _provider(_Directory(), credentials).resolve(_context(), assertion)

    assert result.status in {ProviderIdentityStatus.INVALID, ProviderIdentityStatus.CONFLICT}
    assert credentials.calls == 0


async def test_same_generation_cold_requests_share_one_token_and_contact_lookup() -> None:
    directory = _Directory()
    directory.token_gate = asyncio.Event()
    provider = _provider(directory)

    first = asyncio.create_task(provider.resolve(_context(), _assertion()))
    second = asyncio.create_task(provider.resolve(_context(), _assertion()))
    await directory.token_entered.wait()
    assert directory.token_calls == 1
    directory.token_gate.set()

    results = await asyncio.gather(first, second)
    assert [result.status for result in results] == [
        ProviderIdentityStatus.RESOLVED,
        ProviderIdentityStatus.RESOLVED,
    ]
    assert directory.token_calls == 1
    assert directory.tenant_calls == 1
    assert directory.user_calls == 1


async def test_secret_generation_change_never_reuses_token_or_identity_cache() -> None:
    directory = _Directory()
    credentials = _Credentials(_credential(credential_generation=1))
    provider = _provider(directory, credentials)

    first = await provider.resolve(_context(), _assertion())
    credentials.credential = _credential(credential_generation=2)
    second = await provider.resolve(_context(), _assertion())

    assert first.status is second.status is ProviderIdentityStatus.RESOLVED
    assert directory.token_calls == 2
    assert directory.tenant_calls == 2
    assert directory.user_calls == 2


async def test_account_revision_fences_two_provider_caches_and_late_old_refill() -> None:
    class Clock:
        value = 100.0

        def __call__(self) -> float:
            return self.value

    class RevisionDirectory(_Directory):
        def __init__(self) -> None:
            super().__init__()
            self.old_refill_entered = asyncio.Event()
            self.release_old_refill = asyncio.Event()

        async def get_user(
            self,
            credential: FeishuProviderCredential,
            *,
            tenant_access_token: str,
            identifier_type: str,
            identifier_value: str,
        ) -> FeishuGetUserResponse:
            del credential, tenant_access_token, identifier_type, identifier_value
            self.user_calls += 1
            if self.user_calls == 2:
                self.old_refill_entered.set()
                await self.release_old_refill.wait()
                user_id = "old-revision-late"
            elif self.user_calls >= 3:
                user_id = "new-revision"
            else:
                user_id = "old-revision"
            return FeishuGetUserResponse(
                http_status=200,
                code=0,
                user=replace(
                    self.user,
                    user_id=user_id,
                ),
            )

    clock = Clock()
    directory_a = _Directory()
    directory_b = RevisionDirectory()
    provider_a = _provider(directory_a, clock=clock)
    provider_b = _provider(directory_b, clock=clock)
    old_context = _context(provider_account_revision=3)
    new_context = _context(provider_account_revision=4)

    old_a = await provider_a.resolve(old_context, _assertion())
    old_b = await provider_b.resolve(old_context, _assertion())
    assert old_a.from_cache is False
    assert old_b.from_cache is False
    assert (await provider_a.resolve(old_context, _assertion())).from_cache is True
    assert (await provider_b.resolve(old_context, _assertion())).from_cache is True

    # Only this process gets the best-effort post-commit acceleration.
    await provider_a.invalidate(new_context)
    clock.value = 1_000.0

    new_a = await provider_a.resolve(new_context, _assertion())
    assert new_a.from_cache is False
    assert directory_a.user_calls == 2

    old_refill = asyncio.create_task(provider_b.resolve(old_context, _assertion()))
    await directory_b.old_refill_entered.wait()
    new_b = await provider_b.resolve(new_context, _assertion())
    assert new_b.from_cache is False
    assert new_b.identity is not None
    assert new_b.identity.provider_user_id == "new-revision"

    directory_b.release_old_refill.set()
    late = await old_refill
    assert late.identity is not None
    assert late.identity.provider_user_id == "old-revision-late"

    new_b_again = await provider_b.resolve(new_context, _assertion())
    assert new_b_again.from_cache is True
    assert new_b_again.identity is not None
    assert new_b_again.identity.provider_user_id == "new-revision"
    assert directory_b.user_calls == 3


async def test_account_revision_bump_fences_cached_not_found() -> None:
    class MutableDirectory(_Directory):
        def __init__(self) -> None:
            super().__init__()
            self.exists = False

        async def get_user(
            self,
            credential: FeishuProviderCredential,
            *,
            tenant_access_token: str,
            identifier_type: str,
            identifier_value: str,
        ) -> FeishuGetUserResponse:
            del credential, tenant_access_token, identifier_type, identifier_value
            self.user_calls += 1
            if not self.exists:
                return FeishuGetUserResponse(http_status=404, code=41012)
            return FeishuGetUserResponse(
                http_status=200,
                code=0,
                user=self.user,
            )

    directory = MutableDirectory()
    provider = _provider(directory)
    old_context = _context(provider_account_revision=3)

    missing = await provider.resolve(old_context, _assertion())
    cached_missing = await provider.resolve(old_context, _assertion())
    assert missing.status is ProviderIdentityStatus.NOT_FOUND
    assert cached_missing.from_cache is True
    assert directory.user_calls == 1

    # EIM-I7 created/updated bumps this durable revision even for an unknown
    # identity, so another API process cannot keep hitting the old miss key.
    directory.exists = True
    refreshed = await provider.resolve(
        _context(provider_account_revision=4),
        _assertion(),
    )

    assert refreshed.status is ProviderIdentityStatus.RESOLVED
    assert refreshed.from_cache is False
    assert directory.user_calls == 2

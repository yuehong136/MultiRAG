"""Security contracts for verified, dry-run-first identity onboarding."""

from __future__ import annotations

import asyncio
import gc
import weakref
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from api.identity.contracts import IdentityErrorCode, ProviderContext, ProvisioningMode
from api.identity.onboarding import ChannelOnboardingService
from api.identity.onboarding_contracts import (
    ChannelOnboardingAction,
    ChannelOnboardingError,
    ChannelOnboardingPlan,
    ChannelOnboardingRequest,
    ChannelOnboardingResult,
    ChannelOnboardingSourceSnapshot,
    ChannelOnboardingStatus,
    FeishuChannelOnboardingSource,
    VerifiedChannelOnboardingIntent,
    VerifiedFeishuInstallation,
)
from api.identity.providers.contracts import (
    FeishuDomain,
    FeishuGetUserResponse,
    FeishuTenantResponse,
    FeishuTenantTokenResponse,
    ProviderErrorCode,
)
from api.identity.provisioning import HmacLinkCodeCodec
from api.identity_adapters.channel_onboarding import LarkFeishuInstallationVerifier
from scripts import onboard_feishu_identity as onboarding_cli

_CHANNEL_ID = "channel-sensitive-unit"
_TENANT_ID = "tenant-sensitive-unit"
_APP_ID = "app-sensitive-unit"
_APP_SECRET = "app-secret-sensitive-unit"
_PROVIDER_TENANT_KEY = "provider-tenant-sensitive-unit"
_ACCOUNT_ID = "account-sensitive-unit"
_PUBLIC_DIGEST = "1" * 64
_ENVELOPE_DIGEST = "2" * 64
_UNDERLYING_SECRET = "underlying-exception-sensitive-unit"


def _codec() -> HmacLinkCodeCodec:
    return HmacLinkCodeCodec(
        keys={"unit-key": b"k" * 32},
        active_key_id="unit-key",
    )


def _snapshot() -> ChannelOnboardingSourceSnapshot:
    return ChannelOnboardingSourceSnapshot(
        channel_id=_CHANNEL_ID,
        tenant_id=_TENANT_ID,
        provider="feishu",
        channel_generation=7,
        public_config_digest=_PUBLIC_DIGEST,
        secret_version=11,
        secret_envelope_digest=_ENVELOPE_DIGEST,
        provider_account_key=_APP_ID,
        domain=FeishuDomain.FEISHU,
    )


def _source_value() -> FeishuChannelOnboardingSource:
    return FeishuChannelOnboardingSource(
        snapshot=_snapshot(),
        app_secret=_APP_SECRET,
    )


def _proof(*, provider_account_key: str = _APP_ID) -> VerifiedFeishuInstallation:
    return VerifiedFeishuInstallation(
        provider="feishu",
        provider_tenant_key=_PROVIDER_TENANT_KEY,
        provider_account_key=provider_account_key,
        verified_at=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
    )


def _request() -> ChannelOnboardingRequest:
    return ChannelOnboardingRequest(
        channel_id=_CHANNEL_ID,
        mode=ProvisioningMode.LINK_ONLY,
        link_code_ttl_seconds=600,
    )


def _result() -> ChannelOnboardingResult:
    return ChannelOnboardingResult(
        status=ChannelOnboardingStatus.APPLIED,
        actions=(ChannelOnboardingAction.CREATE_CHANNEL_LINK,),
        mode=ProvisioningMode.LINK_ONLY,
        policy_revision=1,
        provider_account_revision=2,
        context=ProviderContext(
            tenant_id=_TENANT_ID,
            provider="feishu",
            provider_tenant_key=_PROVIDER_TENANT_KEY,
            provider_account_id=_ACCOUNT_ID,
            provider_account_key=_APP_ID,
            provider_account_revision=2,
        ),
    )


class _CodecFactory:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.ready = True

    def __call__(self) -> HmacLinkCodeCodec:
        self.events.append("readiness")
        if not self.ready:
            raise RuntimeError(_UNDERLYING_SECRET)
        return _codec()


class _Source:
    def __init__(
        self,
        events: list[str],
        *,
        value: FeishuChannelOnboardingSource | None = None,
        error: Exception | None = None,
    ) -> None:
        self.events = events
        self.value = value or _source_value()
        self.error = error

    async def load(self, channel_id: str) -> FeishuChannelOnboardingSource:
        assert channel_id == _CHANNEL_ID
        self.events.append("source")
        if self.error is not None:
            raise self.error
        return self.value


class _Verifier:
    def __init__(
        self,
        events: list[str],
        *,
        value: VerifiedFeishuInstallation | None = None,
        error: Exception | None = None,
    ) -> None:
        self.events = events
        self.value = value or _proof()
        self.error = error

    async def verify(
        self,
        source: FeishuChannelOnboardingSource,
    ) -> VerifiedFeishuInstallation:
        assert source.app_secret == _APP_SECRET
        self.events.append("verify")
        if self.error is not None:
            raise self.error
        return self.value


class _Repository:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.preview_error: Exception | None = None
        self.apply_error: Exception | None = None
        self.preview_intents: list[VerifiedChannelOnboardingIntent] = []
        self.apply_intents: list[VerifiedChannelOnboardingIntent] = []

    async def preview(
        self,
        intent: VerifiedChannelOnboardingIntent,
    ) -> tuple[ChannelOnboardingAction, ...]:
        self.events.append("preview")
        self.preview_intents.append(intent)
        if self.preview_error is not None:
            raise self.preview_error
        return (
            ChannelOnboardingAction.CREATE_PROVIDER_TENANT,
            ChannelOnboardingAction.CREATE_PROVIDER_ACCOUNT,
            ChannelOnboardingAction.MARK_PROVIDER_ACCOUNT_HEALTHY,
            ChannelOnboardingAction.CREATE_TENANT_POLICY,
            ChannelOnboardingAction.CREATE_CHANNEL_LINK,
        )

    async def apply(
        self,
        intent: VerifiedChannelOnboardingIntent,
    ) -> ChannelOnboardingResult:
        self.events.append("apply")
        self.apply_intents.append(intent)
        if self.apply_error is not None:
            raise self.apply_error
        return _result()


def _service(
    events: list[str],
    *,
    source: _Source | None = None,
    verifier: _Verifier | None = None,
    repository: _Repository | None = None,
    codec_factory: object | None = None,
) -> tuple[ChannelOnboardingService, _Repository, _CodecFactory]:
    repo = repository or _Repository(events)
    factory = codec_factory or _CodecFactory(events)
    assert callable(factory)
    return (
        ChannelOnboardingService(
            source or _Source(events),
            verifier or _Verifier(events),
            repo,
            factory,
        ),
        repo,
        factory,
    )


@pytest.mark.parametrize(
    "codec_factory",
    [
        lambda: object(),
        lambda: (_ for _ in ()).throw(RuntimeError(_UNDERLYING_SECRET)),
    ],
)
async def test_hmac_readiness_fails_before_source_verifier_or_repository(
    codec_factory: object,
) -> None:
    events: list[str] = []
    service, repository, _ = _service(
        events,
        codec_factory=codec_factory,
    )

    with pytest.raises(ChannelOnboardingError) as caught:
        await service.plan(_request())

    assert caught.value.code is IdentityErrorCode.POLICY_UNAVAILABLE
    assert events == []
    assert repository.preview_intents == []
    assert repository.apply_intents == []
    assert _UNDERLYING_SECRET not in str(caught.value)
    assert _UNDERLYING_SECRET not in repr(caught.value)


async def test_verified_dry_run_orders_external_verification_before_repository() -> None:
    events: list[str] = []
    service, repository, _ = _service(events)

    plan = await service.plan(_request())

    assert events == ["readiness", "source", "verify", "preview"]
    assert repository.apply_intents == []
    assert plan.actions == (
        ChannelOnboardingAction.CREATE_PROVIDER_TENANT,
        ChannelOnboardingAction.CREATE_PROVIDER_ACCOUNT,
        ChannelOnboardingAction.MARK_PROVIDER_ACCOUNT_HEALTHY,
        ChannelOnboardingAction.CREATE_TENANT_POLICY,
        ChannelOnboardingAction.CREATE_CHANNEL_LINK,
    )
    assert repository.preview_intents[0].source.secret_version == 11
    assert repository.preview_intents[0].proof.provider_tenant_key == _PROVIDER_TENANT_KEY


async def test_repository_is_not_entered_while_provider_verification_is_pending() -> None:
    events: list[str] = []
    verification_started = asyncio.Event()
    allow_verification = asyncio.Event()

    class _BlockingVerifier:
        async def verify(
            self,
            source: FeishuChannelOnboardingSource,
        ) -> VerifiedFeishuInstallation:
            assert source.app_secret == _APP_SECRET
            events.append("verify-start")
            verification_started.set()
            await allow_verification.wait()
            events.append("verify-finish")
            return _proof()

    repository = _Repository(events)
    service = ChannelOnboardingService(
        _Source(events),
        _BlockingVerifier(),
        repository,
        _CodecFactory(events),
    )

    task = asyncio.create_task(service.plan(_request()))
    await asyncio.wait_for(verification_started.wait(), timeout=1)
    assert events == ["readiness", "source", "verify-start"]
    assert repository.preview_intents == []
    assert repository.apply_intents == []

    allow_verification.set()
    await asyncio.wait_for(task, timeout=1)
    assert events == [
        "readiness",
        "source",
        "verify-start",
        "verify-finish",
        "preview",
    ]


async def test_apply_rechecks_hmac_readiness_before_any_repository_write() -> None:
    events: list[str] = []
    service, repository, codec_factory = _service(events)
    plan = await service.plan(_request())
    events.clear()
    codec_factory.ready = False

    with pytest.raises(ChannelOnboardingError) as caught:
        await service.apply(plan)

    assert caught.value.code is IdentityErrorCode.POLICY_UNAVAILABLE
    assert events == ["readiness"]
    assert repository.apply_intents == []


async def test_apply_accepts_only_an_untampered_plan_from_the_same_service() -> None:
    first_events: list[str] = []
    first, first_repository, _ = _service(first_events)
    plan = await first.plan(_request())

    second_events: list[str] = []
    second, second_repository, _ = _service(second_events)
    with pytest.raises(ChannelOnboardingError) as cross_service:
        await second.apply(plan)
    assert cross_service.value.code is IdentityErrorCode.ASSERTION_INVALID
    assert second_events == []
    assert second_repository.apply_intents == []

    tampered = replace(plan, link_code_ttl_seconds=601)
    with pytest.raises(ChannelOnboardingError) as changed:
        await first.apply(tampered)
    assert changed.value.code is IdentityErrorCode.ASSERTION_INVALID
    assert first_repository.apply_intents == []


@pytest.mark.parametrize("mutation", ["intent", "actions"])
async def test_apply_rejects_replaced_private_or_public_plan_state(
    mutation: str,
) -> None:
    events: list[str] = []
    service, repository, _ = _service(events)
    plan = await service.plan(_request())
    events.clear()
    if mutation == "intent":
        other_source = replace(
            plan._intent.source,
            channel_id="other-channel-sensitive-unit",
            tenant_id="other-tenant-sensitive-unit",
        )
        changed_plan = replace(
            plan,
            _intent=replace(plan._intent, source=other_source),
        )
    else:
        changed_plan = replace(plan, actions=())

    with pytest.raises(ChannelOnboardingError) as caught:
        await service.apply(changed_plan)

    assert caught.value.code is IdentityErrorCode.ASSERTION_INVALID
    assert events == []
    assert repository.apply_intents == []


async def test_same_plan_is_one_shot_even_under_concurrent_apply() -> None:
    events: list[str] = []
    apply_started = asyncio.Event()
    release_apply = asyncio.Event()

    class _BlockingRepository(_Repository):
        async def apply(
            self,
            intent: VerifiedChannelOnboardingIntent,
        ) -> ChannelOnboardingResult:
            self.events.append("apply")
            self.apply_intents.append(intent)
            apply_started.set()
            await release_apply.wait()
            return _result()

    repository = _BlockingRepository(events)
    service, _, _ = _service(events, repository=repository)
    plan = await service.plan(_request())
    events.clear()

    first = asyncio.create_task(service.apply(plan))
    await asyncio.wait_for(apply_started.wait(), timeout=1)
    with pytest.raises(ChannelOnboardingError) as concurrent:
        await service.apply(plan)
    assert concurrent.value.code is IdentityErrorCode.ASSERTION_INVALID
    assert len(repository.apply_intents) == 1

    release_apply.set()
    result = await asyncio.wait_for(first, timeout=1)
    assert result.status is ChannelOnboardingStatus.APPLIED
    with pytest.raises(ChannelOnboardingError) as replay:
        await service.apply(plan)
    assert replay.value.code is IdentityErrorCode.ASSERTION_INVALID
    assert len(repository.apply_intents) == 1


async def test_unapplied_plan_registry_does_not_retain_abandoned_authority() -> None:
    events: list[str] = []
    service, _, _ = _service(events)
    plan = await service.plan(_request())
    reference = weakref.ref(plan)
    assert len(service._issued_plans) == 1

    del plan
    gc.collect()

    assert reference() is None
    assert len(service._issued_plans) == 0


async def test_apply_uses_the_verified_intent_without_reloading_credentials() -> None:
    events: list[str] = []
    service, repository, _ = _service(events)
    plan = await service.plan(_request())
    events.clear()

    result = await service.apply(plan)

    assert events == ["readiness", "apply"]
    assert repository.apply_intents == repository.preview_intents
    assert result.status is ChannelOnboardingStatus.APPLIED
    assert result.context.provider_account_key == _APP_ID


async def test_account_mismatch_in_provider_proof_never_reaches_repository() -> None:
    events: list[str] = []
    verifier = _Verifier(
        events,
        value=_proof(provider_account_key="different-app-sensitive-unit"),
    )
    service, repository, _ = _service(events, verifier=verifier)

    with pytest.raises(ChannelOnboardingError) as caught:
        await service.plan(_request())

    assert caught.value.code is IdentityErrorCode.ASSERTION_INVALID
    assert events == ["readiness", "source", "verify"]
    assert repository.preview_intents == []
    assert repository.apply_intents == []


@pytest.mark.parametrize(
    ("stage", "expected_code"),
    [
        ("source", ProviderErrorCode.CREDENTIAL_UNAVAILABLE),
        ("verifier", ProviderErrorCode.PROVIDER_UNAVAILABLE),
        ("preview", IdentityErrorCode.REPOSITORY_UNAVAILABLE),
        ("apply", IdentityErrorCode.REPOSITORY_UNAVAILABLE),
    ],
)
async def test_unexpected_failures_are_stable_and_do_not_leak(
    stage: str,
    expected_code: IdentityErrorCode | ProviderErrorCode,
) -> None:
    events: list[str] = []
    source = _Source(events)
    verifier = _Verifier(events)
    repository = _Repository(events)
    if stage == "source":
        source.error = RuntimeError(_UNDERLYING_SECRET)
    elif stage == "verifier":
        verifier.error = RuntimeError(_UNDERLYING_SECRET)
    elif stage == "preview":
        repository.preview_error = RuntimeError(_UNDERLYING_SECRET)
    elif stage == "apply":
        repository.apply_error = RuntimeError(_UNDERLYING_SECRET)
    else:
        raise AssertionError(stage)
    service, _, _ = _service(
        events,
        source=source,
        verifier=verifier,
        repository=repository,
    )

    with pytest.raises(ChannelOnboardingError) as caught:
        plan = await service.plan(_request())
        await service.apply(plan)

    assert caught.value.code is expected_code
    assert str(caught.value) == expected_code.value
    assert _UNDERLYING_SECRET not in str(caught.value)
    assert _UNDERLYING_SECRET not in repr(caught.value)
    if stage != "apply":
        assert repository.apply_intents == []


def test_public_projections_and_errors_do_not_render_sensitive_scope() -> None:
    source = _source_value()
    proof = _proof()
    intent = VerifiedChannelOnboardingIntent(
        source=source.snapshot,
        proof=proof,
        mode=ProvisioningMode.LINK_ONLY,
        link_code_ttl_seconds=600,
    )
    plan = ChannelOnboardingPlan(
        actions=(ChannelOnboardingAction.CREATE_CHANNEL_LINK,),
        mode=ProvisioningMode.LINK_ONLY,
        link_code_ttl_seconds=600,
        _intent=intent,
        _seal=object(),
    )
    values = (
        _request(),
        source.snapshot,
        source,
        proof,
        intent,
        plan,
        _result(),
        ChannelOnboardingError(IdentityErrorCode.OWNERSHIP_CONFLICT),
    )
    sensitive_values = (
        _CHANNEL_ID,
        _TENANT_ID,
        _APP_ID,
        _APP_SECRET,
        _PROVIDER_TENANT_KEY,
        _ACCOUNT_ID,
        _PUBLIC_DIGEST,
        _ENVELOPE_DIGEST,
    )

    for value in values:
        rendered = repr(value)
        for sensitive in sensitive_values:
            assert sensitive not in rendered


class _DirectoryClient:
    def __init__(
        self,
        *,
        token_response: FeishuTenantTokenResponse,
        tenant_response: FeishuTenantResponse | None = None,
        token_error: Exception | None = None,
        tenant_error: Exception | None = None,
    ) -> None:
        self.token_response = token_response
        self.tenant_response = tenant_response or FeishuTenantResponse(
            http_status=200,
            code=0,
            tenant_key=_PROVIDER_TENANT_KEY,
        )
        self.token_error = token_error
        self.tenant_error = tenant_error
        self.fetch_calls = 0
        self.tenant_calls = 0

    async def fetch_tenant_token(self, credential: object) -> FeishuTenantTokenResponse:
        del credential
        self.fetch_calls += 1
        if self.token_error is not None:
            raise self.token_error
        return self.token_response

    async def get_tenant(
        self,
        credential: object,
        *,
        tenant_access_token: str,
    ) -> FeishuTenantResponse:
        del credential
        assert tenant_access_token == "tenant-token-sensitive-unit"
        self.tenant_calls += 1
        if self.tenant_error is not None:
            raise self.tenant_error
        return self.tenant_response

    async def get_user(
        self,
        credential: object,
        *,
        tenant_access_token: str,
        identifier_type: str,
        identifier_value: str,
    ) -> FeishuGetUserResponse:
        del (
            credential,
            tenant_access_token,
            identifier_type,
            identifier_value,
        )
        raise AssertionError("onboarding verifier must not call the user API")


@pytest.mark.parametrize(
    ("auth_code", "expected_code"),
    [
        (10015, ProviderErrorCode.CREDENTIAL_UNAVAILABLE),
        (20002, ProviderErrorCode.CREDENTIAL_UNAVAILABLE),
        (10003, ProviderErrorCode.PROVIDER_UNAVAILABLE),
        (10005, ProviderErrorCode.PROVIDER_UNAVAILABLE),
    ],
)
async def test_auth_v3_business_failure_never_reaches_repository(
    auth_code: int,
    expected_code: ProviderErrorCode,
) -> None:
    events: list[str] = []
    client = _DirectoryClient(
        token_response=FeishuTenantTokenResponse(
            http_status=200,
            code=auth_code,
            tenant_access_token="tenant-token-sensitive-unit",
        )
    )
    repository = _Repository(events)
    service = ChannelOnboardingService(
        _Source(events),
        LarkFeishuInstallationVerifier(client),
        repository,
        _CodecFactory(events),
    )

    with pytest.raises(ChannelOnboardingError) as caught:
        await service.plan(_request())

    assert caught.value.code is expected_code
    assert client.fetch_calls == 1
    assert client.tenant_calls == 0
    assert repository.preview_intents == []
    assert repository.apply_intents == []
    assert "tenant-token-sensitive-unit" not in repr(caught.value)


@pytest.mark.parametrize(
    "tenant_response",
    [
        FeishuTenantResponse(
            http_status=200,
            code=10003,
            tenant_key=_PROVIDER_TENANT_KEY,
        ),
        FeishuTenantResponse(
            http_status=503,
            code=0,
            tenant_key=_PROVIDER_TENANT_KEY,
        ),
        FeishuTenantResponse(http_status=200, code=0, tenant_key=None),
    ],
)
async def test_tenant_v2_failure_never_reaches_repository(
    tenant_response: FeishuTenantResponse,
) -> None:
    events: list[str] = []
    client = _DirectoryClient(
        token_response=FeishuTenantTokenResponse(
            http_status=200,
            code=0,
            tenant_access_token="tenant-token-sensitive-unit",
        ),
        tenant_response=tenant_response,
    )
    repository = _Repository(events)
    service = ChannelOnboardingService(
        _Source(events),
        LarkFeishuInstallationVerifier(client),
        repository,
        _CodecFactory(events),
    )

    with pytest.raises(ChannelOnboardingError) as caught:
        await service.plan(_request())

    assert caught.value.code is ProviderErrorCode.PROVIDER_UNAVAILABLE
    assert client.fetch_calls == 1
    assert client.tenant_calls == 1
    assert repository.preview_intents == []
    assert repository.apply_intents == []
    assert _PROVIDER_TENANT_KEY not in repr(caught.value)


def test_cli_has_no_plaintext_secret_argument_and_never_echoes_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = onboarding_cli._build_parser()
    destinations = {action.dest for action in parser._actions}
    assert destinations == {
        "apply",
        "channel_id",
        "help",
        "link_code_ttl_seconds",
        "mode",
    }
    bootstrapped = False

    def bootstrap(*, initialize_resources: bool = True) -> None:
        del initialize_resources
        nonlocal bootstrapped
        bootstrapped = True

    monkeypatch.setattr(onboarding_cli, "ensure_initialized", bootstrap)
    secret = "cli-plaintext-secret-must-not-echo"

    exit_code = onboarding_cli.main(
        [
            "--channel-id",
            _CHANNEL_ID,
            "--mode",
            "link_only",
            "--link-code-ttl-seconds",
            "600",
            "--app-secret",
            secret,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert bootstrapped is False
    assert secret not in captured.out
    assert secret not in captured.err
    assert _CHANNEL_ID not in captured.out
    assert _CHANNEL_ID not in captured.err

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from api.identity.contracts import IdentityErrorCode, ProviderContext, ProvisioningMode
from api.identity.onboarding_contracts import (
    ChannelOnboardingAction,
    ChannelOnboardingError,
    ChannelOnboardingPlan,
    ChannelOnboardingRequest,
    ChannelOnboardingResult,
    ChannelOnboardingSourceSnapshot,
    ChannelOnboardingStatus,
    VerifiedChannelOnboardingIntent,
    VerifiedFeishuInstallation,
)
from api.identity.providers.contracts import FeishuDomain, ProviderErrorCode
from api.identity.provisioning import HmacLinkCodeCodec
from common.app_config import IdentityProvisioningConfig
from scripts import onboard_feishu_identity as cli

_CHANNEL_ID = "channel-sensitive-opaque-id"
_TENANT_ID = "tenant-sensitive-opaque-id"
_PROVIDER_TENANT_KEY = "provider-tenant-sensitive"
_PROVIDER_ACCOUNT_KEY = "app-sensitive"


def _plan(
    actions: tuple[ChannelOnboardingAction, ...],
) -> ChannelOnboardingPlan:
    source = ChannelOnboardingSourceSnapshot(
        channel_id=_CHANNEL_ID,
        tenant_id=_TENANT_ID,
        provider="feishu",
        channel_generation=1,
        public_config_digest="1" * 64,
        secret_version=1,
        secret_envelope_digest="2" * 64,
        provider_account_key=_PROVIDER_ACCOUNT_KEY,
        domain=FeishuDomain.FEISHU,
    )
    proof = VerifiedFeishuInstallation(
        provider="feishu",
        provider_tenant_key=_PROVIDER_TENANT_KEY,
        provider_account_key=_PROVIDER_ACCOUNT_KEY,
        verified_at=datetime(2026, 8, 13, tzinfo=UTC),
    )
    intent = VerifiedChannelOnboardingIntent(
        source=source,
        proof=proof,
        mode=ProvisioningMode.JIT,
        link_code_ttl_seconds=300,
    )
    return ChannelOnboardingPlan(
        actions=actions,
        mode=intent.mode,
        link_code_ttl_seconds=intent.link_code_ttl_seconds,
        _intent=intent,
        _seal=object(),
    )


class _FakeService:
    def __init__(
        self,
        *,
        actions: tuple[ChannelOnboardingAction, ...],
        failure: BaseException | None = None,
    ) -> None:
        self._plan = _plan(actions)
        self._failure = failure
        self.plan_calls = 0
        self.apply_calls = 0
        self.request: ChannelOnboardingRequest | None = None

    async def plan(self, request: ChannelOnboardingRequest) -> ChannelOnboardingPlan:
        self.plan_calls += 1
        self.request = request
        if self._failure is not None:
            raise self._failure
        return self._plan

    async def apply(self, plan: ChannelOnboardingPlan) -> ChannelOnboardingResult:
        self.apply_calls += 1
        assert plan is self._plan
        return ChannelOnboardingResult(
            status=ChannelOnboardingStatus.APPLIED,
            actions=plan.actions,
            mode=plan.mode,
            policy_revision=1,
            provider_account_revision=2,
            context=ProviderContext(
                tenant_id=_TENANT_ID,
                provider="feishu",
                provider_tenant_key=_PROVIDER_TENANT_KEY,
                provider_account_id="provider-account-sensitive-id",
                provider_account_key=_PROVIDER_ACCOUNT_KEY,
                provider_account_revision=2,
            ),
        )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    service: _FakeService,
    *,
    apply: bool,
) -> tuple[int, dict[str, object], list[str]]:
    events: list[str] = []

    def bootstrap(*, initialize_resources: bool = True) -> None:
        assert initialize_resources is False
        events.append("bootstrap")

    def build_service() -> _FakeService:
        events.append("build_service")
        return service

    monkeypatch.setattr(cli, "ensure_initialized", bootstrap)
    monkeypatch.setattr(cli, "_build_service", build_service)
    argv = [
        "--channel-id",
        _CHANNEL_ID,
        "--mode",
        "jit",
        "--link-code-ttl-seconds",
        "300",
    ]
    if apply:
        argv.append("--apply")
    exit_code = cli.main(argv)
    output = capsys.readouterr().out
    return exit_code, json.loads(output), events


def test_default_is_verified_dry_run_and_never_calls_apply(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actions = (
        ChannelOnboardingAction.CREATE_PROVIDER_TENANT,
        ChannelOnboardingAction.CREATE_PROVIDER_ACCOUNT,
    )
    service = _FakeService(actions=actions)

    exit_code, payload, events = _run(
        monkeypatch,
        capsys,
        service,
        apply=False,
    )

    assert exit_code == 0
    assert events == ["bootstrap", "build_service"]
    assert service.plan_calls == 1
    assert service.apply_calls == 0
    assert service.request == ChannelOnboardingRequest(
        channel_id=_CHANNEL_ID,
        mode=ProvisioningMode.JIT,
        link_code_ttl_seconds=300,
    )
    assert payload == {
        "action_count": 2,
        "actions": ["create_provider_tenant", "create_provider_account"],
        "code": "OK",
        "status": "dry_run_ready",
    }
    assert _CHANNEL_ID not in json.dumps(payload)


def test_explicit_apply_uses_the_exact_verified_plan(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actions = (ChannelOnboardingAction.CREATE_CHANNEL_LINK,)
    service = _FakeService(actions=actions)

    exit_code, payload, _events = _run(
        monkeypatch,
        capsys,
        service,
        apply=True,
    )

    assert exit_code == 0
    assert service.plan_calls == 1
    assert service.apply_calls == 1
    assert payload == {
        "action_count": 1,
        "actions": ["create_channel_link"],
        "code": "OK",
        "status": "applied",
    }


def test_stable_rejection_prints_only_the_error_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = _FakeService(
        actions=(),
        failure=ChannelOnboardingError(ProviderErrorCode.CREDENTIAL_UNAVAILABLE),
    )

    exit_code, payload, _events = _run(
        monkeypatch,
        capsys,
        service,
        apply=False,
    )

    assert exit_code == 2
    assert payload == {
        "action_count": 0,
        "actions": [],
        "code": "IDENTITY_PROVIDER_CREDENTIAL_UNAVAILABLE",
        "status": "rejected",
    }
    assert _CHANNEL_ID not in json.dumps(payload)


def test_unexpected_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_marker = "secret-and-upstream-response-must-not-leak"
    service = _FakeService(actions=(), failure=RuntimeError(secret_marker))

    exit_code, payload, _events = _run(
        monkeypatch,
        capsys,
        service,
        apply=False,
    )

    assert exit_code == 1
    assert payload == {
        "action_count": 0,
        "actions": [],
        "code": "INTERNAL_ERROR",
        "status": "failed",
    }
    assert secret_marker not in json.dumps(payload)


@pytest.mark.parametrize(
    "argv",
    [
        ["--channel-id", _CHANNEL_ID, "--mode", "invalid-mode", "--link-code-ttl-seconds", "300"],
        ["--channel-id", _CHANNEL_ID, "--mode", "jit", "--link-code-ttl-seconds", "59"],
        ["--channel-id", "x" * 33, "--mode", "jit", "--link-code-ttl-seconds", "300"],
    ],
)
def test_invalid_arguments_are_rejected_before_bootstrap_without_echoing_input(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    bootstrapped = False

    def bootstrap(*, initialize_resources: bool = True) -> None:
        del initialize_resources
        nonlocal bootstrapped
        bootstrapped = True

    monkeypatch.setattr(cli, "ensure_initialized", bootstrap)

    exit_code = cli.main(argv)
    rendered = capsys.readouterr().out

    assert exit_code == 2
    assert bootstrapped is False
    assert json.loads(rendered) == {
        "action_count": 0,
        "actions": [],
        "code": IdentityErrorCode.ASSERTION_INVALID.value,
        "status": "rejected",
    }
    assert not any(value in rendered for value in argv if value not in {"--channel-id", "--mode", "--link-code-ttl-seconds"})


def test_codec_factory_uses_only_the_independent_identity_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded_key = base64.urlsafe_b64encode(b"identity-only-test-key-material!"[:32]).decode()
    provisioning = IdentityProvisioningConfig(
        active_key_id="active_test",
        hmac_keyring={"active_test": encoded_key},
    )
    config = SimpleNamespace(
        identity=SimpleNamespace(provisioning=provisioning),
    )
    monkeypatch.setattr(cli, "get_app_config", lambda: config)

    codec = cli._build_link_code_codec()

    assert isinstance(codec, HmacLinkCodeCodec)
    assert encoded_key not in repr(codec)

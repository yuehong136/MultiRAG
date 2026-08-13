"""Verify and optionally apply enterprise identity onboarding for one Feishu Channel.

The default invocation is a verified dry-run.  It performs Feishu Auth V3 and
Tenant V2 reads, then previews database actions without writing.  Only the
explicit ``--apply`` flag enters the authoritative write transaction.

Examples::

    uv run --no-sync python scripts/onboard_feishu_identity.py \
        --channel-id CHANNEL_ID --mode jit --link-code-ttl-seconds 300
    uv run --no-sync python scripts/onboard_feishu_identity.py \
        --channel-id CHANNEL_ID --mode jit --link-code-ttl-seconds 300 --apply

Output is deliberately de-identified: it contains only stable status, code,
action names, and an action count.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from typing import NoReturn, Protocol

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.identity.contracts import IdentityErrorCode, ProvisioningMode
from api.identity.onboarding_contracts import (
    ChannelOnboardingAction,
    ChannelOnboardingError,
    ChannelOnboardingPlan,
    ChannelOnboardingRequest,
    ChannelOnboardingResult,
)
from common.app_config import AppConfigError, get_app_config
from common.bootstrap import ensure_initialized

_OK_CODE = "OK"
_CONFIGURATION_INVALID_CODE = "CONFIGURATION_INVALID"
_INTERNAL_ERROR_CODE = "INTERNAL_ERROR"


class _SafeArgumentError(RuntimeError):
    """Argument rejection whose message never contains user input."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _SafeArgumentError


class _OnboardingService(Protocol):
    async def plan(self, request: ChannelOnboardingRequest) -> ChannelOnboardingPlan: ...

    async def apply(self, plan: ChannelOnboardingPlan) -> ChannelOnboardingResult: ...


@dataclass(frozen=True, slots=True)
class _Options:
    request: ChannelOnboardingRequest = field(repr=False)
    apply: bool


@dataclass(frozen=True, slots=True)
class _Output:
    status: str
    code: str
    actions: tuple[ChannelOnboardingAction, ...] = ()


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description=("Verify a stored Feishu Channel installation and preview identity onboarding. Writes require --apply."),
    )
    parser.add_argument(
        "--channel-id",
        required=True,
        help="opaque ID of an existing Feishu Channel",
    )
    parser.add_argument(
        "--mode",
        required=True,
        help="tenant provisioning mode: preprovisioned, link_only, or jit",
    )
    parser.add_argument(
        "--link-code-ttl-seconds",
        required=True,
        type=int,
        help="tenant link-code TTL in seconds (60..900)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the verified plan atomically; omitted means dry-run",
    )
    return parser


def _parse_args(argv: list[str] | None) -> _Options:
    namespace = _build_parser().parse_args(argv)
    try:
        mode = ProvisioningMode(namespace.mode)
        return _Options(
            request=ChannelOnboardingRequest(
                channel_id=namespace.channel_id,
                mode=mode,
                link_code_ttl_seconds=namespace.link_code_ttl_seconds,
            ),
            apply=namespace.apply,
        )
    except (ChannelOnboardingError, TypeError, ValueError):
        raise _SafeArgumentError from None


def _build_link_code_codec() -> object:
    """Build the real I6 codec from the independent identity keyring."""

    from api.identity.provisioning import HmacLinkCodeCodec

    provisioning = get_app_config().identity.provisioning
    keys = provisioning.require_hmac_keyring()
    return HmacLinkCodeCodec(
        keys=keys,
        active_key_id=provisioning.active_key_id,
    )


def _build_service() -> _OnboardingService:
    """Assemble production adapters after bootstrap has initialized config."""

    from api.channel_control.secret_store import get_channel_secret_store
    from api.db.db_models import async_session_factory
    from api.identity.onboarding import ChannelOnboardingService
    from api.identity.providers.lark_oapi import LarkOapiFeishuDirectoryClient
    from api.identity_adapters.channel_onboarding import (
        ChannelFeishuCredentialSource,
        LarkFeishuInstallationVerifier,
        SqlAlchemyVerifiedChannelOnboardingRepository,
    )

    if async_session_factory is None:
        raise ChannelOnboardingError(IdentityErrorCode.REPOSITORY_UNAVAILABLE)
    source = ChannelFeishuCredentialSource(
        async_session_factory,
        get_channel_secret_store(),
    )
    verifier = LarkFeishuInstallationVerifier(
        LarkOapiFeishuDirectoryClient(),
    )
    repository = SqlAlchemyVerifiedChannelOnboardingRepository(async_session_factory)
    return ChannelOnboardingService(
        source,
        verifier,
        repository,
        _build_link_code_codec,
    )


async def _execute(options: _Options, service: _OnboardingService) -> _Output:
    plan = await service.plan(options.request)
    if not options.apply:
        return _Output(
            status="dry_run_ready",
            code=_OK_CODE,
            actions=plan.actions,
        )
    result = await service.apply(plan)
    return _Output(
        status=result.status.value,
        code=_OK_CODE,
        actions=result.actions,
    )


def _render(output: _Output) -> str:
    payload = {
        "action_count": len(output.actions),
        "actions": [action.value for action in output.actions],
        "code": output.code,
        "status": output.status,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _rejection(code: str, *, status: str = "rejected") -> _Output:
    return _Output(status=status, code=code)


def main(argv: list[str] | None = None) -> int:
    try:
        options = _parse_args(argv)
    except _SafeArgumentError:
        print(_render(_rejection(IdentityErrorCode.ASSERTION_INVALID.value)))
        return 2

    try:
        # Configuration and instrumentation must initialize before db_models
        # creates the async engine inside _build_service().  This operational
        # command does not need document stores or retrieval resources.
        ensure_initialized(initialize_resources=False)
        output = asyncio.run(_execute(options, _build_service()))
    except ChannelOnboardingError as exc:
        print(_render(_rejection(exc.code.value)))
        return 2
    except AppConfigError:
        print(_render(_rejection(_CONFIGURATION_INVALID_CODE)))
        return 2
    except KeyboardInterrupt:
        print(_render(_rejection(_INTERNAL_ERROR_CODE, status="failed")))
        return 130
    except Exception:
        print(_render(_rejection(_INTERNAL_ERROR_CODE, status="failed")))
        return 1

    print(_render(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

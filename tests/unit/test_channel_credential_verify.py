"""Credential self-check probes and their verdict boundaries (CHN-O6).

The interesting property is not "does it call the right URL" -- it is that the
two failure kinds never blur into each other. A rejection sends an admin to
re-enter a credential; reporting a timeout that way sends them to re-enter a
credential that was already correct.
"""

from __future__ import annotations

import subprocess
import sys

import httpx
import pytest

from api.channel_providers import UnknownChannelProvider, verify_module
from api.channels.dingtalk.verify import _DingTalkCredentialVerifier
from api.channels.feishu.verify import _FeishuCredentialVerifier
from api.channels.verification import (
    ChannelCredentialRejected,
    ChannelVerificationUnavailable,
    credential_verifier,
)

# STATUS_DLL_INIT_FAILED, same as tests/unit/test_channel_provider_spec.py.
_WINDOWS_DLL_INIT_FAILED = 0xC0000142

_FEISHU_CREDENTIAL = {"app_id": "cli_aaaaaaaaaaaaaaaa", "app_secret": "secret_aaaaaaaaaaaaaaaa"}
_DINGTALK_CREDENTIAL = {"client_id": "ding_aaaaaaaaaaaaaaaa", "client_secret": "secret_aaaaaaaaaaaaaaaa"}


def _feishu(handler) -> _FeishuCredentialVerifier:
    return _FeishuCredentialVerifier(transport=httpx.MockTransport(handler))


def _dingtalk(handler) -> _DingTalkCredentialVerifier:
    return _DingTalkCredentialVerifier(transport=httpx.MockTransport(handler))


# --- Feishu -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_feishu_zero_code_is_a_pass() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"code": 0, "msg": "ok", "tenant_access_token": "t-aaaa", "expire": 7200})

    await _feishu(handler).verify_credential(credential=_FEISHU_CREDENTIAL, public_config={"domain": "feishu"})

    assert seen["url"] == "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"


@pytest.mark.asyncio
async def test_feishu_lark_domain_targets_the_international_host() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"code": 0})

    await _feishu(handler).verify_credential(credential=_FEISHU_CREDENTIAL, public_config={"domain": "lark"})

    assert seen["url"].startswith("https://open.larksuite.com/")


@pytest.mark.asyncio
async def test_feishu_rejects_on_a_non_zero_code_even_though_status_is_400() -> None:
    """A wrong App Secret comes back as HTTP 400, so status alone cannot decide."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 10003, "msg": "invalid app_secret"})

    with pytest.raises(ChannelCredentialRejected):
        await _feishu(handler).verify_credential(credential=_FEISHU_CREDENTIAL, public_config={"domain": "feishu"})


@pytest.mark.asyncio
async def test_feishu_server_error_is_inconclusive_not_a_rejection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": 99999, "msg": "service unavailable"})

    with pytest.raises(ChannelVerificationUnavailable):
        await _feishu(handler).verify_credential(credential=_FEISHU_CREDENTIAL, public_config={"domain": "feishu"})


@pytest.mark.asyncio
async def test_feishu_unparseable_answer_is_inconclusive() -> None:
    """A proxy's HTML error page is not evidence about anyone's credential."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>captive portal</html>")

    with pytest.raises(ChannelVerificationUnavailable):
        await _feishu(handler).verify_credential(credential=_FEISHU_CREDENTIAL, public_config={"domain": "feishu"})


@pytest.mark.asyncio
async def test_feishu_transport_failure_is_inconclusive_and_leaks_no_secret(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(ChannelVerificationUnavailable):
        await _feishu(handler).verify_credential(credential=_FEISHU_CREDENTIAL, public_config={"domain": "feishu"})

    assert "secret_aaaaaaaaaaaaaaaa" not in caplog.text


@pytest.mark.asyncio
async def test_feishu_missing_secret_is_rejected_without_a_round_trip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("an incomplete credential must not reach the provider")

    with pytest.raises(ChannelCredentialRejected) as captured:
        await _feishu(handler).verify_credential(credential={"app_id": "cli_aaaaaaaaaaaaaaaa"}, public_config={})

    assert captured.value.error_code == "CHANNEL_CREDENTIAL_INCOMPLETE"


# --- DingTalk ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dingtalk_ticket_and_endpoint_is_a_pass() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"endpoint": "wss://example.invalid/ws", "ticket": "tk-aaaa"})

    await _dingtalk(handler).verify_credential(credential=_DINGTALK_CREDENTIAL, public_config={})


@pytest.mark.asyncio
async def test_dingtalk_client_error_is_a_rejection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "Forbidden.AccessDenied"})

    with pytest.raises(ChannelCredentialRejected):
        await _dingtalk(handler).verify_credential(credential=_DINGTALK_CREDENTIAL, public_config={})


@pytest.mark.asyncio
async def test_dingtalk_server_error_is_inconclusive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(ChannelVerificationUnavailable):
        await _dingtalk(handler).verify_credential(credential=_DINGTALK_CREDENTIAL, public_config={})


@pytest.mark.asyncio
async def test_dingtalk_success_without_a_ticket_is_not_a_pass() -> None:
    """A 2xx that is missing what we asked for means the contract moved."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"endpoint": "wss://example.invalid/ws"})

    with pytest.raises(ChannelVerificationUnavailable):
        await _dingtalk(handler).verify_credential(credential=_DINGTALK_CREDENTIAL, public_config={})


# --- registry seam ----------------------------------------------------------


def test_every_registered_provider_resolves_to_a_verifier() -> None:
    assert credential_verifier("feishu") is not None
    assert credential_verifier("dingtalk") is not None


def test_unknown_provider_has_no_verifier_and_does_not_raise() -> None:
    """Fail closed as "cannot check", not as a 500 on the management page."""

    assert credential_verifier("not-a-provider") is None
    with pytest.raises(UnknownChannelProvider):
        verify_module("not-a-provider")


def test_resolving_a_verifier_does_not_import_a_transport_sdk() -> None:
    """The reason verify.py exists instead of a WorkerProvider method.

    lark-oapi installs a process-global event loop on import. The API process
    must be able to check a Feishu credential without acquiring one.

    Runs in a subprocess for the same reason as the spec purity test: in-process
    this would pass or fail on whether some earlier test in the session happened
    to import the SDK, which is a property of the test order, not of this code.
    """

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from api.channels.verification import credential_verifier; assert credential_verifier('feishu') is not None; assert credential_verifier('dingtalk') is not None; banned = {'lark_oapi', 'aiohttp', 'sqlalchemy', 'fastapi'} & set(sys.modules); print(sorted(banned))"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode == _WINDOWS_DLL_INIT_FAILED and not result.stdout.strip():
        # Same Windows late-in-a-long-run process-creation failure the spec
        # purity test documents: no signal at all, so neither pass nor fail is
        # a true statement about the code.
        pytest.skip(f"subprocess could not start (rc={result.returncode}); purity unverified")

    assert result.returncode == 0, f"subprocess exited {result.returncode}:\n{result.stderr}"
    assert result.stdout.strip() == "[]", f"resolving a verifier pulled in {result.stdout.strip()}\nstderr:\n{result.stderr}"

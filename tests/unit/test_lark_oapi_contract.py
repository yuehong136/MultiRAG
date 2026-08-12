"""EIM-F1 contracts for the official ``lark-oapi`` worker boundary."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_CONTACT_V3_FIXTURE = Path(__file__).parents[1] / "fixtures/eim_f1/contact_v3/get_user_success.json"
_WINDOWS_DLL_INIT_FAILED = 0xC0000142


def _run_isolated(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-c", script, *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode == _WINDOWS_DLL_INIT_FAILED and not result.stdout.strip():
        pytest.skip(f"subprocess could not start (rc={result.returncode}); contract unverified")
    return result


@pytest.mark.parametrize(
    "module_name",
    (
        "api.channel_control",
        "api.channel_providers",
        "api.channels.verification",
        "api.identity",
    ),
)
def test_platform_imports_do_not_load_lark_or_install_an_event_loop(module_name: str) -> None:
    """Control-plane and identity imports must stay outside the SDK boundary."""

    result = _run_isolated(
        """
import asyncio
import importlib
import sys

asyncio.set_event_loop(None)
importlib.import_module(sys.argv[1])

loaded = sorted(name for name in sys.modules if name == "lark_oapi" or name.startswith("lark_oapi."))
assert not loaded, f"platform import loaded worker SDK modules: {loaded}"
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = None
assert loop is None, "platform import installed a process event loop"
print("clean")
""",
        module_name,
    )

    assert result.returncode == 0, f"subprocess exited {result.returncode}:\n{result.stderr}"
    assert result.stdout.strip() == "clean"


def test_lark_sdk_import_and_client_build_have_only_the_known_idle_loop_side_effect() -> None:
    """The worker-only SDK may install one loop, but must not start work at import."""

    result = _run_isolated(
        r"""
import asyncio
import json
import threading
import warnings

asyncio.set_event_loop(None)
threads_before = {(thread.ident, thread.name) for thread in threading.enumerate()}

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"pkg_resources is deprecated as an API.*",
        category=UserWarning,
        module=r"lark_oapi\.ws\.pb\.google",
    )
    import lark_oapi as lark
    from lark_oapi.core.token import TokenManager
    from lark_oapi.ws import client as ws_client

del TokenManager
loop = asyncio.get_event_loop()
assert loop is ws_client.loop
assert not loop.is_running()
assert not loop.is_closed()
assert not asyncio.all_tasks(loop)
assert {(thread.ident, thread.name) for thread in threading.enumerate()} == threads_before

client = lark.Client.builder().app_id("cli_eim_f1_example").app_secret("secret-eim-f1-example").build()
assert callable(client.contact.v3.user.get)
assert callable(client.contact.v3.user.aget)
assert asyncio.get_event_loop() is loop
assert not loop.is_running()
assert not asyncio.all_tasks(loop)
assert {(thread.ident, thread.name) for thread in threading.enumerate()} == threads_before

print(json.dumps({"loop": "idle", "tasks": 0, "threads_started": 0}, sort_keys=True))
loop.close()
asyncio.set_event_loop(None)
""",
    )

    assert result.returncode == 0, f"subprocess exited {result.returncode}:\n{result.stderr}"
    assert json.loads(result.stdout) == {"loop": "idle", "tasks": 0, "threads_started": 0}


def test_tenant_token_cache_hits_sequentially_but_has_no_cold_miss_single_flight() -> None:
    """Characterize 1.7.2 token caching so EIM-I4 does not assume coalescing."""

    result = _run_isolated(
        r"""
import asyncio
import json
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"pkg_resources is deprecated as an API.*",
        category=UserWarning,
        module=r"lark_oapi\.ws\.pb\.google",
    )
    import lark_oapi.core.token.manager as token_manager_module
    from lark_oapi.core.cache import LocalCache
    from lark_oapi.core.model import Config, RawResponse
    from lark_oapi.core.token import TokenManager

TOKEN = "tenant-token-eim-f1-example"


def token_response():
    response = RawResponse()
    response.status_code = 200
    response.content = json.dumps(
        {
            "code": 0,
            "msg": "success",
            "tenant_access_token": TOKEN,
            "expire": 7200,
        }
    ).encode("utf-8")
    return response


config = Config()
config.app_id = "cli_eim_f1_cache_example"
config.app_secret = "secret-eim-f1-example"

sequential_calls = []


def sequential_execute(request_config, request):
    assert request_config is config
    sequential_calls.append(request.uri)
    return token_response()


TokenManager.cache = LocalCache()
token_manager_module.Transport.execute = staticmethod(sequential_execute)
assert TokenManager.get_self_tenant_token(config) == TOKEN
assert TokenManager.get_self_tenant_token(config) == TOKEN
assert sequential_calls == ["/open-apis/auth/v3/tenant_access_token/internal"]

cold_calls = []
cold_calls_lock = threading.Lock()
cold_barrier = threading.Barrier(2, timeout=5)


def cold_execute(request_config, request):
    assert request_config is config
    with cold_calls_lock:
        cold_calls.append(request.uri)
    cold_barrier.wait()
    return token_response()


TokenManager.cache = LocalCache()
token_manager_module.Transport.execute = staticmethod(cold_execute)
threads_before = {(thread.ident, thread.name) for thread in threading.enumerate()}
with ThreadPoolExecutor(max_workers=2, thread_name_prefix="eim-f1-token") as executor:
    futures = [executor.submit(TokenManager.get_self_tenant_token, config) for _ in range(2)]
    tokens = [future.result(timeout=10) for future in futures]

assert tokens == [TOKEN, TOKEN]
assert cold_calls == [
    "/open-apis/auth/v3/tenant_access_token/internal",
    "/open-apis/auth/v3/tenant_access_token/internal",
]
assert {(thread.ident, thread.name) for thread in threading.enumerate()} == threads_before

loop = asyncio.get_event_loop()
assert not loop.is_running()
assert not asyncio.all_tasks(loop)
loop.close()
asyncio.set_event_loop(None)
print(json.dumps({"cold_upstream_calls": len(cold_calls), "sequential_upstream_calls": len(sequential_calls)}))
""",
    )

    assert result.returncode == 0, f"subprocess exited {result.returncode}:\n{result.stderr}"
    assert json.loads(result.stdout) == {"cold_upstream_calls": 2, "sequential_upstream_calls": 1}


def test_contact_v3_get_user_fixture_uses_typed_models_and_open_id_request() -> None:
    """Pin the official request/response seam that EIM-I4 will consume."""

    result = _run_isolated(
        r"""
import asyncio
import json
import sys
import warnings
from pathlib import Path

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"pkg_resources is deprecated as an API.*",
        category=UserWarning,
        module=r"lark_oapi\.ws\.pb\.google",
    )
    from lark_oapi.api.contact.v3 import (
        GetUserRequest,
        GetUserResponse,
        GetUserResponseBody,
        User,
        UserStatus,
    )
    from lark_oapi.core import AccessTokenType, HttpMethod, JSON
    from lark_oapi.core.exception import UnmarshalException

fixture_text = Path(sys.argv[1]).read_text(encoding="utf-8")
fixture = json.loads(fixture_text)
assert set(fixture) == {"code", "msg", "data"}
assert set(fixture["data"]) == {"user"}
assert set(fixture["data"]["user"]) == {"open_id", "user_id", "employee_no", "status"}
assert set(fixture["data"]["user"]["status"]) == {
    "is_frozen",
    "is_resigned",
    "is_activated",
    "is_exited",
    "is_unjoin",
}

response = JSON.unmarshal(fixture_text, GetUserResponse)
assert isinstance(response, GetUserResponse)
assert response.success()
assert isinstance(response.data, GetUserResponseBody)
assert isinstance(response.data.user, User)
assert isinstance(response.data.user.status, UserStatus)
assert response.data.user.open_id == "ou_eim_f1_example"
assert response.data.user.user_id == "u_eim_f1_example"
assert response.data.user.employee_no == "EMP-EIM-F1-0001"
assert response.data.user.status.is_activated is True
assert response.data.user.status.is_frozen is False
assert response.data.user.status.is_resigned is False
assert response.data.user.status.is_exited is False
assert response.data.user.status.is_unjoin is False

invalid = json.loads(fixture_text)
invalid["data"]["user"]["status"] = "active"
try:
    JSON.unmarshal(json.dumps(invalid), GetUserResponse)
except UnmarshalException:
    pass
else:
    raise AssertionError("Contact V3 status stopped being a typed object")

request = GetUserRequest.builder().user_id_type("open_id").user_id("ou_eim_f1_example").build()
assert request.http_method is HttpMethod.GET
assert request.uri == "/open-apis/contact/v3/users/:user_id"
assert request.paths == {"user_id": "ou_eim_f1_example"}
assert request.queries == [("user_id_type", "open_id")]
assert request.token_types == {AccessTokenType.TENANT, AccessTokenType.USER}

loop = asyncio.get_event_loop()
loop.close()
asyncio.set_event_loop(None)
print("typed")
""",
        str(_CONTACT_V3_FIXTURE),
    )

    assert result.returncode == 0, f"subprocess exited {result.returncode}:\n{result.stderr}"
    assert result.stdout.strip() == "typed"

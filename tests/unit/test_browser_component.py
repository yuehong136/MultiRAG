"""Browser contracts without a browser, network, or database service."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent.component import browser as module
from agent.component.browser import Browser, BrowserParam


def node() -> Browser:
    param = BrowserParam()
    param.llm_id = "registered@OpenAI"
    obj = object.__new__(Browser)
    obj._param = param
    obj._id = "Browser:test"
    obj._canvas = SimpleNamespace(artifact_owner_id="actor", get_tenant_id=lambda: "tenant", _id="graph", is_canceled=lambda: False, get_variable_value=lambda value: [{"id": "a"}], dsl={})
    return obj


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_steps", True),
        ("max_steps", 1.2),
        ("max_steps", 0),
        ("headless", 0),
        ("persist_session", "false"),
        ("temperature", float("nan")),
        ("temperature", True),
        ("max_retries", -1),
        ("upload_sources", "x"),
        ("prompts", " "),
    ],
)
def test_invalid_params(field: str, value: Any) -> None:
    obj = node()
    setattr(obj._param, field, value)
    with pytest.raises(ValueError):
        obj._param.check()


def test_legal_false_zero_and_defaults() -> None:
    obj = node()
    obj._param.headless = False
    obj._param.persist_session = False
    assert obj._param.temperature == 0 and obj._param.check()
    assert obj._param.outputs["downloaded_files"]["type"] == "Array<Object>"


@pytest.mark.parametrize("candidate,expected", [("%2e%2e%2fsecret.txt", "secret.txt"), ("..\\secret.txt", "secret.txt"), ("..", ""), ("%00.txt", ""), ("/a/b.csv", "b.csv")])
def test_filename(candidate: str, expected: str) -> None:
    assert Browser._safe_url_filename(candidate) == expected


def test_history_only_explicit_final() -> None:
    assert Browser._extract_history_text(SimpleNamespace(final_result=lambda: "")) == ""
    assert Browser._extract_history_text(SimpleNamespace(extracted_content="secret")) == ""
    for result in (False, None):
        with pytest.raises(RuntimeError):
            Browser._extract_history_text(SimpleNamespace(is_successful=lambda: result, final_result=lambda: "failed"))


async def test_profile_isolates_actor_and_serializes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MULTIRAG_BROWSER_PROFILE_ROOT", str(tmp_path))
    first, second = node(), node()
    path = first._resolve_persistent_profile_dir()
    second._canvas.artifact_owner_id = "other"
    assert path != second._resolve_persistent_profile_dir()
    second._canvas.artifact_owner_id = "actor"
    acquired = asyncio.Event()

    async def competing() -> None:
        async with second._profile():
            acquired.set()

    async with first._profile() as profile:
        assert profile == path
        waiter = asyncio.create_task(competing())
        await asyncio.sleep(0.15)
        assert not acquired.is_set()
    await waiter
    assert acquired.is_set()
    await first.clear_persistent_profile()
    assert not path.exists()


async def test_ephemeral_profile_cleanup_on_cancel() -> None:
    obj = node()
    obj._param.persist_session = False
    with pytest.raises(asyncio.CancelledError):
        async with obj._profile() as profile:
            assert profile.exists()
            raise asyncio.CancelledError
    assert not profile.exists()


def test_download_symlink_rejected(tmp_path: Path) -> None:
    (tmp_path / "escape").symlink_to("/etc/hosts")
    with pytest.raises(ValueError, match="escapes"):
        Browser._download_payloads(str(tmp_path))


def test_url_refusal_has_zero_request_and_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    get = lambda *a, **kw: pytest.fail("denied URL made a request")
    monkeypatch.setattr(module.requests.Session, "get", get)
    with pytest.raises(ValueError, match="non-public"):
        node()._prepare_upload_url_file("http://127.0.0.1/secret", str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_redirect_guard_and_response_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, closes = [], []
    response = SimpleNamespace(status_code=302, headers={"Location": "http://private.test/a"}, close=lambda: closes.append(True))

    def guard(url: str) -> tuple[str, str]:
        calls.append(url)
        if "private.test" in url:
            raise ValueError("blocked redirect")
        return "public.test", "8.8.8.8"

    def get(session: Any, url: str, **kwargs: Any) -> Any:
        assert session.trust_env is False and kwargs["allow_redirects"] is False
        return response

    monkeypatch.setattr(module, "assert_url_is_safe", guard)
    monkeypatch.setattr(module.requests.Session, "get", get)
    with pytest.raises(ValueError, match="blocked redirect"):
        node()._prepare_upload_url_file("https://public.test/a", str(tmp_path))
    assert len(calls) == 2 and len(closes) == 1 and not list(tmp_path.iterdir())


async def test_foreign_file_denied_before_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    obj = node()
    obj._param.upload_sources = ["foreign"]

    @asynccontextmanager
    async def db() -> Any:
        yield SimpleNamespace(scalar=AsyncMock(return_value=None))

    monkeypatch.setattr(module, "async_db_connection", db)
    with pytest.raises(PermissionError):
        await obj._prepare_upload_files(str(tmp_path))
    assert not list(tmp_path.iterdir())


async def test_agent_constructor_failure_closes_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import browser_use

    _ = browser_use.Agent  # Resolve lazy annotations before replacing Browser.

    obj = node()
    monkeypatch.setattr(obj, "_build_browser_llm", AsyncMock(return_value=SimpleNamespace()))
    kill = AsyncMock()
    monkeypatch.setattr(browser_use, "Browser", lambda **kw: SimpleNamespace(kill=kill))

    def fail(**kwargs: Any) -> None:
        raise ValueError("constructor")

    monkeypatch.setattr(browser_use, "Agent", fail)
    with pytest.raises(ValueError, match="constructor"):
        await obj._run_browser_use_async("task", str(tmp_path), [], tmp_path)
    kill.assert_awaited_once()


async def test_run_cancel_closes_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import browser_use

    _ = browser_use.Agent  # Resolve lazy annotations before replacing Browser.

    obj = node()
    monkeypatch.setattr(obj, "_build_browser_llm", AsyncMock(return_value=SimpleNamespace()))
    started, stopped = asyncio.Event(), asyncio.Event()
    kill = AsyncMock()

    async def run(**kwargs: Any) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(browser_use, "Browser", lambda **kw: SimpleNamespace(kill=kill))
    monkeypatch.setattr(browser_use, "Agent", lambda **kw: SimpleNamespace(run=run))
    task = asyncio.create_task(obj._run_browser_use_async("task", str(tmp_path), [], tmp_path))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    kill.assert_awaited_once()


async def test_resolved_debug_inputs_and_output_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    obj = node()
    obj._param.persist_session = False
    obj._param.prompts = "Task: {sys.query}"
    obj._canvas.get_variable_value = lambda value: "saved value"
    prepare = AsyncMock(return_value=[])
    run = AsyncMock(return_value=SimpleNamespace(final_result=lambda: "reply", is_successful=lambda: True))
    monkeypatch.setattr(obj, "_prepare_upload_files", prepare)
    monkeypatch.setattr(obj, "_run_browser_use_async", run)
    monkeypatch.setattr(obj, "_save_downloads", AsyncMock(return_value=[]))
    obj.set_output("_ERROR", "stale error")
    obj.set_output("downloaded_files", [{"file_id": "stale"}])
    result = await obj._invoke_async(**{"sys.query": "debug override"})
    assert result["content"] == "reply" and result["downloaded_files"] == [] and "_ERROR" not in result
    assert obj.get_input_value("sys.query") == "debug override"
    assert obj.get_input_value("prompts") == "Task: debug override"
    assert run.call_args.args[0] == "Task: debug override"
    assert prepare.call_args.kwargs["inputs"]["sys.query"] == "debug override"
    assert obj._upload_values(["{Begin:x@files}"], inputs={"Begin:x@files": [{"id": "upload"}]}) == [{"id": "upload"}]

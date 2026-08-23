"""Lifecycle composition tests for the default-disabled EIM-I8 worker."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from api.apps.restful_apis import identity_reconciliation_api as reconciliation_api
from common.app_config import IdentityReconciliationConfig


def _app_config(
    reconciliation: IdentityReconciliationConfig,
) -> Any:
    return SimpleNamespace(
        identity=SimpleNamespace(reconciliation=reconciliation),
    )


class _BlockingWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.exited = asyncio.Event()

    async def run(self, stop_event: asyncio.Event) -> None:
        self.started.set()
        try:
            await stop_event.wait()
        finally:
            self.exited.set()


async def test_disabled_lifespan_has_no_database_or_registry_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = IdentityReconciliationConfig()
    monkeypatch.setattr(
        reconciliation_api,
        "get_app_config",
        lambda: _app_config(config),
    )

    def forbidden_build(
        _config: IdentityReconciliationConfig,
    ) -> None:
        raise AssertionError("disabled reconciliation must not compose resources")

    monkeypatch.setattr(
        reconciliation_api,
        "_build_identity_reconciliation_worker",
        forbidden_build,
    )
    app = FastAPI()
    app.include_router(reconciliation_api.router)

    async with app.router.lifespan_context(app):
        assert not hasattr(app.state, reconciliation_api._STATE_KEY)

    assert reconciliation_api.router.routes == []


async def test_enabled_lifespan_starts_and_stops_one_process_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = IdentityReconciliationConfig(enabled=True)
    worker = _BlockingWorker()
    builds: list[IdentityReconciliationConfig] = []
    monkeypatch.setattr(
        reconciliation_api,
        "get_app_config",
        lambda: _app_config(config),
    )

    def build(
        selected: IdentityReconciliationConfig,
    ) -> _BlockingWorker:
        builds.append(selected)
        return worker

    monkeypatch.setattr(
        reconciliation_api,
        "_build_identity_reconciliation_worker",
        build,
    )
    app = FastAPI()
    app.include_router(reconciliation_api.router)

    async with app.router.lifespan_context(app):
        await worker.started.wait()
        handle = getattr(app.state, reconciliation_api._STATE_KEY)
        assert handle.task.get_name() == "identity-reconciliation-worker"
        assert builds == [config]

    assert worker.exited.is_set()
    assert not hasattr(app.state, reconciliation_api._STATE_KEY)


async def test_unexpected_worker_exit_is_logged_without_exception_payload(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = IdentityReconciliationConfig(enabled=True)
    entered = asyncio.Event()

    class _CrashingWorker:
        async def run(self, stop_event: asyncio.Event) -> None:
            del stop_event
            entered.set()
            raise RuntimeError("provider-user-sensitive")

    monkeypatch.setattr(
        reconciliation_api,
        "get_app_config",
        lambda: _app_config(config),
    )
    monkeypatch.setattr(
        reconciliation_api,
        "_build_identity_reconciliation_worker",
        lambda _config: _CrashingWorker(),
    )
    app = FastAPI()
    app.include_router(reconciliation_api.router)
    caplog.set_level(logging.INFO, logger=reconciliation_api.__name__)

    async with app.router.lifespan_context(app):
        await entered.wait()
        await asyncio.sleep(0)

    rendered = caplog.text
    assert "IDENTITY_RECONCILIATION_WORKER_CRASHED" in rendered
    assert "error_type=RuntimeError" in rendered
    assert "provider-user-sensitive" not in rendered


def test_enabled_composition_fails_closed_without_async_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.db import db_models
    from api.identity_adapters import provider_runtime

    monkeypatch.setattr(db_models, "async_session_factory", None)

    def forbidden_registry(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("missing database must fail before registry access")

    monkeypatch.setattr(
        provider_runtime,
        "get_identity_provider_registry",
        forbidden_registry,
    )

    with pytest.raises(
        reconciliation_api.IdentityReconciliationRuntimeUnavailableError,
        match="requires the async database lifecycle",
    ):
        reconciliation_api._build_identity_reconciliation_worker(
            IdentityReconciliationConfig(enabled=True),
        )


def test_route_module_import_does_not_read_config_or_compose_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import common.app_config as app_config_module

    def forbidden_config() -> None:
        raise AssertionError("route import must not read runtime configuration")

    monkeypatch.setattr(app_config_module, "get_app_config", forbidden_config)
    source = Path(reconciliation_api.__file__)
    spec = importlib.util.spec_from_file_location(
        "identity_reconciliation_import_probe",
        source,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    assert module.router.routes == []


def test_fresh_route_import_does_not_import_database_runtime() -> None:
    # A dotted import would execute the existing eager ``api.apps`` package
    # initializer before this route module and therefore measure an unrelated
    # canonical API dependency on db_models.  Load this file in a fresh
    # interpreter so the assertion covers this route's own dependency chain.
    source = Path(reconciliation_api.__file__).resolve()
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util, pathlib, sys; "
                "source = pathlib.Path(sys.argv[1]); "
                "spec = importlib.util.spec_from_file_location("
                "'_identity_reconciliation_route_probe', source); "
                "assert spec is not None and spec.loader is not None; "
                "module = importlib.util.module_from_spec(spec); "
                "spec.loader.exec_module(module); "
                "assert 'api.db.db_models' not in sys.modules"
            ),
            str(source),
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert probe.returncode == 0, probe.stderr

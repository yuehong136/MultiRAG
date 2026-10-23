"""The process entry point must configure LiteLLM before application imports."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("configured", [None, "True", "False", "", "custom"])
def test_cost_map_setting_precedes_application_imports(configured: str | None) -> None:
    env = os.environ.copy()
    env.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    if configured is not None:
        env["LITELLM_LOCAL_MODEL_COST_MAP"] = configured
    # Stop at the first application import, before bootstrap creates resources.
    # In the default case, also exercise the installed LiteLLM import with its
    # HTTP boundary instrumented: falling back after a failed fetch is a failure.
    program = """
import builtins
import json
import os
import runpy
import sys

original_import = builtins.__import__
class StartupBoundary(Exception):
    pass

def observe_import(name, *args, **kwargs):
    if name.split(".")[0] in {"agent", "common", "api", "core"}:
        builtins.__import__ = original_import
        result = {"setting": os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP")}
        if sys.argv[1] == "default":
            import httpx
            calls = []
            def deny_get(*args, **kwargs):
                calls.append(str(args[0]))
                raise RuntimeError("cost map network access forbidden")
            httpx.get = deny_get
            import litellm
            result.update(http_calls=calls, models=len(litellm.model_cost))
        print(json.dumps(result))
        raise StartupBoundary
    return original_import(name, *args, **kwargs)

builtins.__import__ = observe_import
try:
    runpy.run_path("api/multirag_server.py", run_name="__main__")
except StartupBoundary:
    pass
"""
    result = subprocess.run(
        [sys.executable, "-c", program, "default" if configured is None else "explicit"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed["setting"] == ("True" if configured is None else configured)
    if configured is None:
        assert observed["http_calls"] == []
        assert observed["models"] > 0

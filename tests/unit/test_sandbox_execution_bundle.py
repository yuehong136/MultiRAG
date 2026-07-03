#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import base64
import importlib.util
import json
import sys
import types
from enum import Enum
from pathlib import Path
from unittest.mock import patch

EXECUTOR_MANAGER_PATH = next(
    parent / "agent" / "sandbox" / "executor_manager" for parent in Path(__file__).resolve().parents if (parent / "agent" / "sandbox" / "executor_manager" / "services" / "execution.py").exists()
)
EXECUTION_MODULE_PATH = EXECUTOR_MANAGER_PATH / "services" / "execution.py"


def _load_execution_module() -> types.ModuleType:
    core_module = types.ModuleType("core")
    core_config_module = types.ModuleType("core.config")
    core_container_module = types.ModuleType("core.container")
    core_logger_module = types.ModuleType("core.logger")
    models_module = types.ModuleType("models")
    models_enums_module = types.ModuleType("models.enums")
    models_schemas_module = types.ModuleType("models.schemas")
    utils_module = types.ModuleType("utils")
    utils_common_module = types.ModuleType("utils.common")

    core_config_module.TIMEOUT = 10
    core_container_module.allocate_container_blocking = None
    core_container_module.release_container = None
    core_logger_module.logger = types.SimpleNamespace(info=lambda *_args, **_kwargs: None, warning=lambda *_args, **_kwargs: None, error=lambda *_args, **_kwargs: None)
    utils_common_module.async_run_command = None

    class SupportLanguage(str, Enum):
        PYTHON = "python"
        NODEJS = "nodejs"

    class ResultStatus(str, Enum):
        SUCCESS = "success"
        PROGRAM_ERROR = "program_error"
        RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"
        UNAUTHORIZED_ACCESS = "unauthorized_access"
        PROGRAM_RUNNER_ERROR = "program_runner_error"

    class ResourceLimitType(str, Enum):
        TIME = "time"
        MEMORY = "memory"

    class UnauthorizedAccessType(str, Enum):
        FILE_ACCESS = "file_access"
        DISALLOWED_SYSCALL = "disallowed_syscall"

    class RuntimeErrorType(str, Enum):
        NONZERO_EXIT = "nonzero_exit"

    class ArtifactItem:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    class ExecutionStructuredResult:
        @classmethod
        def model_validate_json(cls, _payload: str) -> "ExecutionStructuredResult":
            return cls()

    class CodeExecutionResult:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    class CodeExecutionRequest:
        pass

    models_enums_module.ResourceLimitType = ResourceLimitType
    models_enums_module.ResultStatus = ResultStatus
    models_enums_module.RuntimeErrorType = RuntimeErrorType
    models_enums_module.SupportLanguage = SupportLanguage
    models_enums_module.UnauthorizedAccessType = UnauthorizedAccessType
    models_schemas_module.ArtifactItem = ArtifactItem
    models_schemas_module.CodeExecutionRequest = CodeExecutionRequest
    models_schemas_module.CodeExecutionResult = CodeExecutionResult
    models_schemas_module.ExecutionStructuredResult = ExecutionStructuredResult

    stub_modules = {
        "core": core_module,
        "core.config": core_config_module,
        "core.container": core_container_module,
        "core.logger": core_logger_module,
        "models": models_module,
        "models.enums": models_enums_module,
        "models.schemas": models_schemas_module,
        "utils": utils_module,
        "utils.common": utils_common_module,
    }

    module_name = "sandbox_execution_under_test"
    spec = importlib.util.spec_from_file_location(module_name, EXECUTION_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    with patch.dict(sys.modules, stub_modules):
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return module


def _build_request(module: types.ModuleType, code: str, language: str, arguments: dict[str, object]) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        code_b64=base64.b64encode(code.encode("utf-8")).decode("ascii"),
        language=language,
        arguments=arguments,
    )


def test_python_bundle_writes_args_json_and_reads_it_from_runner(tmp_path: Path) -> None:
    module = _load_execution_module()
    arguments = {"name": "multirag", "values": list(range(3))}
    req = _build_request(module, "def main(**kwargs):\n    return kwargs\n", module.SupportLanguage.PYTHON, arguments)

    bundle = module._build_execution_bundle(req, str(tmp_path))

    assert bundle.code_name == "main.py"
    assert bundle.runner_name == "runner.py"
    assert bundle.args_name == "args.json"
    assert json.loads((tmp_path / "args.json").read_text(encoding="utf-8")) == arguments
    assert bundle.args_size_bytes == len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))

    runner_source = (tmp_path / "runner.py").read_text(encoding="utf-8")
    assert "args.json" in runner_source
    assert "json.load(f)" in runner_source
    assert "sys.argv" not in runner_source
    assert "main(**args)" in runner_source
    assert module.RESULT_MARKER_PREFIX in runner_source


def test_node_bundle_writes_args_json_and_reads_it_from_runner(tmp_path: Path) -> None:
    module = _load_execution_module()
    arguments = {"large": "x" * 256}
    req = _build_request(module, "module.exports = function main(args) { return args; };\n", module.SupportLanguage.NODEJS, arguments)

    bundle = module._build_execution_bundle(req, str(tmp_path))

    assert bundle.code_name == "main.js"
    assert bundle.runner_name == "runner.js"
    assert bundle.args_name == "args.json"
    assert json.loads((tmp_path / "args.json").read_text(encoding="utf-8")) == arguments

    runner_source = (tmp_path / "runner.js").read_text(encoding="utf-8")
    assert "args.json" in runner_source
    assert "fs.readFileSync(path.join(__dirname, 'args.json'), 'utf8')" in runner_source
    assert "process.argv" not in runner_source
    assert "main(args)" in runner_source
    assert module.RESULT_MARKER_PREFIX in runner_source


def test_container_run_args_do_not_include_serialized_arguments() -> None:
    module = _load_execution_module()
    serialized_arguments = json.dumps({"large": "x" * 256}, ensure_ascii=False)

    python_args = module._build_container_run_args(
        language=module.SupportLanguage.PYTHON,
        task_id="task-1",
        container="sandbox-python",
        runner_name="runner.py",
    )
    node_args = module._build_container_run_args(
        language=module.SupportLanguage.NODEJS,
        task_id="task-2",
        container="sandbox-node",
        runner_name="runner.js",
    )

    assert python_args[-1] == "runner.py"
    assert "-I" in python_args
    assert "-B" in python_args
    assert serialized_arguments not in python_args

    assert node_args[-1] == "runner.js"
    assert "-I" not in node_args
    assert "-B" not in node_args
    assert serialized_arguments not in node_args

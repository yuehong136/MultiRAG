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

"""Opt-in host execution using the shared POSIX process implementation."""

from typing import Any
import os

from .base import SandboxProviderConfigError
from .process import ProcessProvider


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


class LocalProvider(ProcessProvider):
    """Trusted development only; enabling another provider never enables this one."""

    def initialize(self, config: dict[str, Any]) -> bool:
        if not _env_enabled("SANDBOX_LOCAL_ENABLED"):
            raise SandboxProviderConfigError("Local code execution is disabled. Set SANDBOX_LOCAL_ENABLED=true to enable it.")
        effective = dict(config)
        for key in self.get_config_schema():
            env_name = f"SANDBOX_LOCAL_{key.upper()}"
            if env_name in os.environ:
                effective[key] = os.environ[env_name]
        return super().initialize(effective)

    @staticmethod
    def get_config_schema() -> dict[str, dict[str, Any]]:
        schema = ProcessProvider.get_config_schema()
        labels = {
            "python_bin": "Python binary",
            "node_bin": "Node.js binary",
            "work_dir": "Working directory",
            "timeout": "Timeout (seconds)",
            "max_memory_mb": "Max memory (MB)",
            "max_output_bytes": "Max output (bytes)",
            "max_artifacts": "Max artifacts",
            "max_artifact_bytes": "Max artifact size (bytes)",
        }
        limits = {
            "timeout": (1, 600),
            "max_memory_mb": (1, 65536),
            "max_output_bytes": (1024, 10485760),
            "max_artifacts": (0, 100),
            "max_artifact_bytes": (1024, 104857600),
        }
        for key, field in schema.items():
            field.update(label=labels[key], scope="runtime")
            if key in limits:
                field.update(min=limits[key][0], max=limits[key][1])
        schema["local_enabled"] = {
            "type": "boolean",
            "default": _env_enabled("SANDBOX_LOCAL_ENABLED"),
            "label": "Host execution enabled",
            "readonly": True,
            "scope": "deployment",
            "description": "Requires SANDBOX_LOCAL_ENABLED=true on every executing process. Host execution has no isolation boundary.",
        }
        return schema

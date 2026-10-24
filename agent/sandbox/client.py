#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
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

"""
Sandbox client for agent components.

This module provides a unified interface for agent components to interact
with the configured sandbox provider.
"""

import hashlib
import json
import logging
import os
import sys
import threading
from typing import Any

from api.db.db_models import SessionLocal
from api.db.services.system_settings_service import SystemSettingsService
from agent.sandbox.providers import ProviderManager
from agent.sandbox.providers.base import ExecutionResult, SandboxProviderConfigError, SandboxProviderNotConfigured

logger = logging.getLogger(__name__)


# Publish a manager only after initialization succeeds; a failed explicit
# selection must not turn into HTTP fallback on a subsequent request.
_provider_manager: ProviderManager | None = None
_provider_lock = threading.Lock()
_provider_signature: str | None = None


def get_provider_manager() -> ProviderManager:
    global _provider_manager, _provider_signature
    with _provider_lock:
        provider_type, from_env = _resolve_provider_type()
        config = _load_provider_config(provider_type, from_env)
        signature = hashlib.sha256(json.dumps([provider_type, config, os.environ.get("SANDBOX_LOCAL_ENABLED", "")], sort_keys=True, allow_nan=False).encode()).hexdigest()
        if _provider_manager is None or signature != _provider_signature:
            manager = ProviderManager()
            _load_provider_from_settings(manager, provider_type, config)
            _provider_manager = manager
            _provider_signature = signature
        return _provider_manager


def _load_provider_from_settings(manager: ProviderManager, provider_type: str, config: dict[str, Any]) -> None:
    from agent.sandbox.providers import SelfManagedProvider, AliyunCodeInterpreterProvider, E2BProvider, LocalProvider, SSHProvider

    provider_classes = {
        "self_managed": SelfManagedProvider,
        "aliyun_codeinterpreter": AliyunCodeInterpreterProvider,
        "e2b": E2BProvider,
        "local": LocalProvider,
        "ssh": SSHProvider,
    }
    provider_class = provider_classes.get(provider_type)
    if provider_class is None:
        raise SandboxProviderConfigError(f"Unknown sandbox provider type: {provider_type}")
    try:
        provider = provider_class()
        if not provider.initialize(config):
            raise SandboxProviderConfigError(f"Failed to initialize sandbox provider: {provider_type}")
    except SandboxProviderConfigError:
        raise
    except Exception as exc:
        # Do not include provider secrets or silently execute through another backend.
        raise SandboxProviderConfigError(f"Failed to configure sandbox provider: {provider_type}") from exc
    manager.set_provider(provider_type, provider)
    logger.info("Sandbox provider '%s' initialized successfully", provider_type)


def _resolve_provider_type() -> tuple[str, bool]:
    provider_type = os.environ.get("SANDBOX_PROVIDER_TYPE", "").strip()
    if provider_type:
        return provider_type, True
    with SessionLocal() as db:
        settings = SystemSettingsService.get_by_name(db, "sandbox.provider_type")
        if not settings or not str(settings[0].value).strip():
            raise SandboxProviderNotConfigured("Sandbox provider type not configured.")
        return str(settings[0].value).strip(), False


def _load_provider_config_from_settings(provider_type: str) -> dict[str, Any]:
    with SessionLocal() as db:
        settings = SystemSettingsService.get_by_name(db, f"sandbox.{provider_type}")
        if not settings:
            return {}
        try:
            config = json.loads(settings[0].value)
        except (TypeError, ValueError) as exc:
            raise SandboxProviderConfigError(f"Invalid JSON configuration for sandbox provider: {provider_type}") from exc
    if not isinstance(config, dict):
        raise SandboxProviderConfigError(f"Sandbox provider configuration must be an object: {provider_type}")
    return config


def _load_provider_config(provider_type: str, from_env: bool = False) -> dict[str, Any]:
    try:
        config = _load_provider_config_from_settings(provider_type)
    except SandboxProviderConfigError:
        raise
    except Exception:
        if not from_env:
            raise
        # An explicit environment selection can run without an admin DB.
        logger.warning("Sandbox system settings unavailable; using environment configuration for '%s'", provider_type)
        config = {}
    config.update(_load_provider_config_from_env(provider_type))
    return config


def _load_provider_config_from_env(provider_type: str) -> dict[str, Any]:
    if provider_type == "local":
        mapping = {
            "SANDBOX_LOCAL_PYTHON_BIN": "python_bin",
            "SANDBOX_LOCAL_NODE_BIN": "node_bin",
            "SANDBOX_LOCAL_WORK_DIR": "work_dir",
            "SANDBOX_LOCAL_TIMEOUT": "timeout",
            "SANDBOX_LOCAL_MAX_MEMORY_MB": "max_memory_mb",
            "SANDBOX_LOCAL_MAX_OUTPUT_BYTES": "max_output_bytes",
            "SANDBOX_LOCAL_MAX_ARTIFACTS": "max_artifacts",
            "SANDBOX_LOCAL_MAX_ARTIFACT_BYTES": "max_artifact_bytes",
        }
        return {key: os.environ[name] for name, key in mapping.items() if name in os.environ}
    if provider_type == "self_managed":
        config: dict[str, Any] = {}
        host = os.environ.get("SANDBOX_HOST", "").strip()
        port = os.environ.get("SANDBOX_EXECUTOR_MANAGER_PORT", "9385").strip()
        if host:
            config["endpoint"] = f"http://{host}:{port or '9385'}"
        if os.environ.get("SANDBOX_EXECUTOR_MANAGER_POOL_SIZE", "").strip():
            config["pool_size"] = os.environ["SANDBOX_EXECUTOR_MANAGER_POOL_SIZE"]
        return config
    return {}


def reload_provider() -> None:
    """Forget cached settings; the next use initializes a fresh provider."""
    global _provider_manager, _provider_signature
    with _provider_lock:
        _provider_manager = None
        _provider_signature = None


def execute_code(code: str, language: str = "python", timeout: int = 30, arguments: dict[str, Any] | None = None) -> ExecutionResult:
    """
    Execute code in the configured sandbox.

    This is the main entry point for agent components to execute code.

    Args:
        code: Source code to execute
        language: Programming language (python, nodejs, javascript)
        timeout: Maximum execution time in seconds
        arguments: Optional arguments dict to pass to main() function

    Returns:
        ExecutionResult containing stdout, stderr, exit_code, and metadata

    Raises:
        RuntimeError: If no provider is configured or execution fails
    """
    provider_manager = get_provider_manager()

    if not provider_manager.is_configured():
        raise SandboxProviderNotConfigured("No sandbox provider configured.")

    provider = provider_manager.get_provider()
    assert provider is not None

    logger.info("CodeExec using sandbox provider %s (language=%s, timeout=%s)", provider_manager.get_provider_name(), language, timeout)

    # Create a sandbox instance
    instance = provider.create_instance(template=language)

    try:
        # Execute the code
        result = provider.execute_code(instance_id=instance.instance_id, code=code, language=language, timeout=timeout, arguments=arguments)

        return result

    finally:
        # Keep the execution error as the cause if execution and cleanup both fail.
        execution_error = sys.exception()
        try:
            if provider.destroy_instance(instance.instance_id) is not True:
                raise RuntimeError("Provider did not confirm instance destruction.")
        except Exception as exc:
            raise RuntimeError("Sandbox execution cleanup failed.") from execution_error or exc


def health_check() -> bool:
    """
    Check if the sandbox provider is healthy.

    Returns:
        True if provider is configured and healthy, False otherwise
    """
    try:
        provider_manager = get_provider_manager()

        if not provider_manager.is_configured():
            return False

        provider = provider_manager.get_provider()
        return provider.health_check()

    except Exception as e:
        logger.error(f"Sandbox health check failed: {e}")
        return False


def get_provider_info() -> dict[str, Any]:
    """
    Get information about the current sandbox provider.

    Returns:
        Dictionary with provider information:
        - provider_type: Type of the active provider
        - configured: Whether provider is configured
        - healthy: Whether provider is healthy
    """
    try:
        provider_manager = get_provider_manager()

        return {
            "provider_type": provider_manager.get_provider_name(),
            "configured": provider_manager.is_configured(),
            "healthy": health_check(),
        }

    except Exception as e:
        logger.error(f"Failed to get provider info: {e}")
        return {
            "provider_type": None,
            "configured": False,
            "healthy": False,
        }

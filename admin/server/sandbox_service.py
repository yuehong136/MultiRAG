"""Admin sandbox configuration on the existing request-scoped sync transaction."""

import json
import os
from typing import Any

from sqlalchemy.orm import Session

from agent.sandbox.providers import AliyunCodeInterpreterProvider, E2BProvider, LocalProvider, SandboxProvider, SelfManagedProvider, SSHProvider
from agent.sandbox.providers.local import _env_enabled
from api.common.exceptions import AdminException
from api.db.db_models import SystemSettings
from api.db.services.system_settings_service import SystemSettingsService


class SandboxMgr:
    PROVIDER_CLASSES: dict[str, type[SandboxProvider]] = {
        "self_managed": SelfManagedProvider,
        "local": LocalProvider,
        "ssh": SSHProvider,
        "aliyun_codeinterpreter": AliyunCodeInterpreterProvider,
        "e2b": E2BProvider,
    }
    PROVIDER_REGISTRY = {
        "self_managed": {"name": "Self-Managed", "description": "Docker executor manager", "tags": ["self-hosted"]},
        "local": {"name": "Local", "description": "Trusted host process execution; no isolation boundary", "tags": ["host"]},
        "ssh": {"name": "SSH", "description": "Execute as a trusted remote POSIX account", "tags": ["remote", "ssh"]},
        "aliyun_codeinterpreter": {"name": "Aliyun Code Interpreter", "description": "Aliyun cloud execution", "tags": ["cloud"]},
        "e2b": {"name": "E2B", "description": "E2B cloud execution", "tags": ["cloud"]},
    }

    @staticmethod
    def list_providers() -> list[dict[str, Any]]:
        return [
            {
                "id": key,
                **metadata,
                "available": key != "local" or _env_enabled("SANDBOX_LOCAL_ENABLED"),
                "unavailable_reason": "SANDBOX_LOCAL_ENABLED=true is required" if key == "local" and not _env_enabled("SANDBOX_LOCAL_ENABLED") else None,
            }
            for key, metadata in SandboxMgr.PROVIDER_REGISTRY.items()
        ]

    @staticmethod
    def get_provider_config_schema(provider_id: str) -> dict[str, dict[str, Any]]:
        provider = SandboxMgr.PROVIDER_CLASSES.get(provider_id)
        if provider is None:
            raise AdminException(f"Unknown provider: {provider_id}")
        return provider.get_config_schema()

    @staticmethod
    def _stored_config(db: Session, provider_type: str) -> dict[str, Any]:
        rows = SystemSettingsService.get_by_name(db, f"sandbox.{provider_type}")
        if not rows:
            return {}
        try:
            value = json.loads(rows[0].value)
        except (ValueError, TypeError):
            raise AdminException("Stored sandbox configuration is invalid.") from None
        if not isinstance(value, dict):
            raise AdminException("Stored sandbox configuration must be an object.")
        return value

    @staticmethod
    def _active_provider(db: Session) -> str:
        rows = SystemSettingsService.get_by_name(db, "sandbox.provider_type")
        return rows[0].value if rows else "self_managed"

    @staticmethod
    def get_config(db: Session, provider_type: str | None = None) -> dict[str, Any]:
        from agent.sandbox.client import _load_provider_config_from_env

        active = SandboxMgr._active_provider(db)
        selected = provider_type if provider_type is not None else active
        schema = SandboxMgr.get_provider_config_schema(selected)
        stored = SandboxMgr._stored_config(db, selected)
        config = {key: stored.get(key, field.get("default")) for key, field in schema.items() if not field.get("readonly") and not field.get("secret") and (key in stored or "default" in field)}
        return {
            "provider_type": selected,
            "config": config,
            "configured_secrets": [key for key, field in schema.items() if field.get("secret") and stored.get(key)],
            "active_provider_type": active,
            "effective_provider_type": os.environ.get("SANDBOX_PROVIDER_TYPE", "").strip() or active,
            "environment_overrides": sorted(_load_provider_config_from_env(selected)),
        }

    @staticmethod
    def _validated_config(db: Session, provider_type: str, config: dict[str, Any]) -> dict[str, Any]:
        schema = SandboxMgr.get_provider_config_schema(provider_type)
        if provider_type == "local" and not _env_enabled("SANDBOX_LOCAL_ENABLED"):
            raise AdminException("Local execution is disabled. Set SANDBOX_LOCAL_ENABLED=true on the server.")
        for key in config:
            if key not in schema:
                raise AdminException(f"Unknown configuration field '{key}'.")
            if schema[key].get("readonly"):
                raise AdminException(f"Configuration field '{key}' is read-only.")
        stored = SandboxMgr._stored_config(db, provider_type)
        merged = {}
        for key, field in schema.items():
            if field.get("readonly"):
                continue
            if key in config:
                merged[key] = config[key]
            elif field.get("secret") and key in stored:
                merged[key] = stored[key]
            elif "default" in field:
                merged[key] = field["default"]
            elif field.get("required"):
                raise AdminException(f"Required field '{key}' is missing.")
        for key, value in merged.items():
            field = schema[key]
            expected = {"string": str, "integer": int, "boolean": bool}.get(field["type"])
            if expected is None or type(value) is not expected:
                raise AdminException(f"Field '{key}' must be {field['type']}.")
            if field.get("required") and isinstance(value, str) and not value.strip():
                raise AdminException(f"Required field '{key}' is empty.")
            if expected is int and (("min" in field and value < field["min"]) or ("max" in field and value > field["max"])):
                raise AdminException(f"Field '{key}' is out of range.")
        valid, error = SandboxMgr.PROVIDER_CLASSES[provider_type]().validate_config(merged)
        if not valid:
            raise AdminException(error or "Invalid provider configuration.")
        return merged

    @staticmethod
    def set_config(db: Session, provider_type: str, config: dict[str, Any], set_active: bool = True) -> dict[str, Any]:
        from agent.sandbox.client import reload_provider

        merged = SandboxMgr._validated_config(db, provider_type, config)
        values = [(f"sandbox.{provider_type}", json.dumps(merged, allow_nan=False), "json")]
        if set_active:
            values.append(("sandbox.provider_type", provider_type, "string"))
        try:
            # Do not call SettingsMgr.update_by_name: it commits each row separately.
            for name, value, data_type in values:
                row = db.get(SystemSettings, name)
                if row is None:
                    db.add(SystemSettings(name=name, value=value, data_type=data_type, source="admin"))
                else:
                    row.value = value
                    row.data_type = data_type
                    row.source = "admin"
            db.commit()
        except Exception:
            db.rollback()
            raise AdminException("Failed to save sandbox configuration.") from None
        reload_provider()
        return SandboxMgr.get_config(db, provider_type)

    @staticmethod
    def test_connection(db: Session, provider_type: str, config: dict[str, Any]) -> dict[str, Any]:
        merged = SandboxMgr._validated_config(db, provider_type, config)
        provider = SandboxMgr.PROVIDER_CLASSES[provider_type]()
        instance = None
        try:
            if not provider.initialize(merged):
                raise AdminException("Provider initialization failed.")
            instance = provider.create_instance("python")
            if instance is None:
                raise AdminException("Provider did not create an instance.")
            result = provider.execute_code(instance.instance_id, 'def main():\n    print("TEST_PASSED")\n    return {"ok": True}\n', "python", timeout=10)
        except AdminException:
            raise
        except Exception:
            raise AdminException("Sandbox connection test failed. Check configuration, trust and runtime availability.") from None
        finally:
            if instance is not None:
                try:
                    if not provider.destroy_instance(instance.instance_id):
                        raise RuntimeError("Cleanup returned false.")
                except Exception:
                    raise AdminException("Sandbox test cleanup failed; inspect the provider before retrying.") from None
        success = result.exit_code == 0 and "TEST_PASSED" in result.stdout
        return {
            "success": success,
            "message": "Test passed" if success else "Test failed",
            "details": {"exit_code": result.exit_code, "execution_time": result.execution_time, "stdout": result.stdout, "stderr": result.stderr},
        }

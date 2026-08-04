"""Channel control-plane orchestration with tenant and secret boundaries."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from api.channel_control.repository import ChannelRepository
from api.channel_control.schemas import (
    ChannelBindingResponse,
    ChannelBindingUpsertRequest,
    ChannelCreateRequest,
    ChannelRuntimeResponse,
    ChannelUpdateRequest,
    ChatChannelResponse,
    FeishuConfigInput,
    FeishuConfigPatch,
    SecretStatus,
)
from api.channel_control.secret_store import EncryptedSecret, SecretStore, SecretStoreUnavailable
from api.db.db_models import ChannelBinding, ChannelRuntimeStatus, ChannelSecret, ChatChannel
from common.misc_utils import get_uuid

_SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "app_secret",
        "secret",
        "token",
        "api_token",
        "api_key",
        "password",
        "authorization",
        "credential",
    }
)


class ChannelControlError(RuntimeError):
    """Base error with a message safe to return to management clients."""

    def __init__(self, message: str, *, error_code: str) -> None:
        self.safe_message = message
        self.error_code = error_code
        super().__init__(message)


class ChannelAccessDenied(ChannelControlError):
    def __init__(self) -> None:
        super().__init__("No authorization.", error_code="CHANNEL_NOT_ACCESSIBLE")


class InvalidChannelConfiguration(ChannelControlError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code="INVALID_CHANNEL_CONFIGURATION")


class ChannelCredentialUnavailable(ChannelControlError):
    def __init__(self) -> None:
        super().__init__("Channel credential encryption is unavailable.", error_code="CHANNEL_SECRET_STORE_UNAVAILABLE")


@dataclass(frozen=True, slots=True)
class RuntimeBindingSpec:
    """Internal runner configuration; never expose it from a public route."""

    binding_id: str
    channel_id: str
    tenant_id: str
    provider: str
    public_config: dict[str, Any]
    target_type: str
    target_id: str
    target_revision_id: str | None
    policy: dict[str, Any]
    generation: int
    encrypted_secret: EncryptedSecret


@dataclass(frozen=True, slots=True)
class ResolvedRuntimeBindingSpec:
    """Decrypted provider connection data for an authenticated runner only."""

    binding_id: str
    provider: str
    public_config: dict[str, Any]
    credentials: dict[str, str]
    generation: int


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if normalized in _SENSITIVE_CONFIG_KEYS or "secret" in normalized or "token" in normalized or "password" in normalized:
                return True
            if _contains_sensitive_key(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _sanitize_public_config(value: Any) -> dict[str, Any]:
    """Defensively strip credentials if an old row contains legacy plaintext."""

    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key, nested in value.items():
        normalized = str(key).strip().lower()
        if normalized in {"app_secret", "secret", "token", "api_token", "api_key", "password", "authorization"}:
            continue
        if isinstance(nested, dict):
            sanitized[str(key)] = _sanitize_public_config(nested)
        elif isinstance(nested, list):
            sanitized[str(key)] = [_sanitize_public_config(item) if isinstance(item, dict) else item for item in nested]
        else:
            sanitized[str(key)] = nested
    return sanitized


def _create_public_config(config: FeishuConfigInput) -> tuple[dict[str, Any], dict[str, str] | None]:
    credential: dict[str, str] = {}
    if config.credential.app_id:
        credential["app_id"] = config.credential.app_id
    public = {
        "credential": credential,
        "domain": config.domain,
        "allowed_open_ids": list(config.allowed_open_ids),
    }
    secret = config.credential.app_secret
    plaintext = {"app_secret": secret.get_secret_value()} if secret is not None else None
    return public, plaintext


def _patch_public_config(current: dict[str, Any], patch: FeishuConfigPatch) -> tuple[dict[str, Any], dict[str, str] | None, bool]:
    public = _sanitize_public_config(deepcopy(current))
    public.setdefault("credential", {})
    changed = False

    if "domain" in patch.model_fields_set and patch.domain is not None and public.get("domain") != patch.domain:
        public["domain"] = patch.domain
        changed = True
    if "allowed_open_ids" in patch.model_fields_set and patch.allowed_open_ids is not None:
        allowed_open_ids = list(patch.allowed_open_ids)
        if public.get("allowed_open_ids") != allowed_open_ids:
            public["allowed_open_ids"] = allowed_open_ids
            changed = True

    plaintext: dict[str, str] | None = None
    credential_patch = patch.credential
    if credential_patch is not None:
        credential = public["credential"]
        if not isinstance(credential, dict):
            credential = {}
            public["credential"] = credential
            changed = True
        if "app_id" in credential_patch.model_fields_set and credential_patch.app_id is not None and credential.get("app_id") != credential_patch.app_id:
            credential["app_id"] = credential_patch.app_id
            changed = True
        if credential_patch.app_secret is not None:
            plaintext = {"app_secret": credential_patch.app_secret.get_secret_value()}

    return public, plaintext, changed


class ChannelControlService:
    def __init__(self, repository: ChannelRepository, secret_store: SecretStore) -> None:
        self._repository = repository
        self._secret_store = secret_store

    async def list_channels(self, tenant_id: str) -> dict[str, Any]:
        channels, total = await self._repository.list_channels(tenant_id)
        items = [await self._serialize_channel(channel) for channel in channels]
        return {"items": items, "total": total}

    async def get_channel(self, tenant_id: str, channel_id: str) -> dict[str, Any]:
        channel = await self._require_channel(tenant_id, channel_id)
        return await self._serialize_channel(channel)

    async def create_channel(self, tenant_id: str, request: ChannelCreateRequest) -> dict[str, Any]:
        channel_id = get_uuid()
        public_config, plaintext = _create_public_config(request.config)
        binding_request = request.binding
        if binding_request is None and request.chat_id is not None:
            binding_request = ChannelBindingUpsertRequest(
                target_type="multirag.dialog",
                target_id=request.chat_id,
                enabled=request.status == 1,
            )
        elif binding_request is not None and request.status == 1 and not binding_request.enabled:
            binding_request = binding_request.model_copy(update={"enabled": True})

        encrypted: EncryptedSecret | None = None
        try:
            if plaintext is not None:
                encrypted = await self._encrypt_secret(
                    tenant_id=tenant_id,
                    channel_id=channel_id,
                    plaintext=plaintext,
                    version=1,
                )
            if binding_request is not None:
                await self._validate_target(tenant_id, binding_request)

            channel = ChatChannel(
                id=channel_id,
                tenant_id=tenant_id,
                name=request.name,
                channel=request.channel,
                config=public_config,
                chat_id=binding_request.target_id if binding_request and binding_request.target_type == "multirag.dialog" else None,
                status=int(bool(binding_request and binding_request.enabled)),
                generation=1,
            )
            secret = self._new_secret(channel_id, encrypted) if encrypted is not None else None
            binding = self._new_binding(channel_id, binding_request) if binding_request is not None else None
            if binding is not None and binding.enabled:
                self._ensure_ready(channel, secret, binding)

            self._repository.add(channel)
            # No ORM relationships are declared between the control-plane
            # models, so SQLAlchemy cannot infer parent/child flush ordering
            # from the foreign keys alone.  Persist the channel first while
            # keeping the whole operation in the same transaction.
            await self._repository.flush()
            if secret is not None:
                self._repository.add(secret)
            if binding is not None:
                self._repository.add(binding)
            await self._repository.flush()
            await self._repository.commit()
            return self._serialize(channel, secret, binding)
        except Exception:
            await self._repository.rollback()
            raise

    async def update_channel(self, tenant_id: str, channel_id: str, request: ChannelUpdateRequest) -> dict[str, Any]:
        try:
            channel = await self._require_channel(tenant_id, channel_id, for_update=True)
            secret = await self._repository.get_secret(channel.id, for_update=True)
            binding = await self._repository.get_binding(channel.id, for_update=True)
            runtime_changed = False
            binding_generation_advanced = False

            if request.name is not None:
                channel.name = request.name
            if request.config is not None:
                public, plaintext, config_changed = _patch_public_config(channel.config, request.config)
                if config_changed:
                    channel.config = public
                    runtime_changed = True
                if plaintext is not None:
                    next_version = (secret.version + 1) if secret is not None else 1
                    encrypted = await self._encrypt_secret(
                        tenant_id=tenant_id,
                        channel_id=channel.id,
                        plaintext=plaintext,
                        version=next_version,
                    )
                    if secret is None:
                        secret = self._new_secret(channel.id, encrypted)
                        self._repository.add(secret)
                    else:
                        secret.ciphertext = encrypted.ciphertext
                        secret.key_id = encrypted.key_id
                        secret.version = encrypted.version
                    runtime_changed = True

            if "chat_id" in request.model_fields_set:
                binding, compatibility_changed = await self._apply_compatibility_chat_id(
                    tenant_id,
                    channel,
                    binding,
                    request.chat_id,
                )
                runtime_changed = runtime_changed or compatibility_changed
                binding_generation_advanced = binding_generation_advanced or compatibility_changed

            if request.binding is not None:
                binding_request = request.binding
                await self._validate_target(tenant_id, binding_request)
                if binding is None:
                    binding = self._new_binding(channel.id, binding_request)
                    self._repository.add(binding)
                    binding_changed = True
                else:
                    desired_binding = (
                        binding_request.target_type,
                        binding_request.target_id,
                        binding_request.target_revision_id,
                        binding_request.policy,
                        binding_request.enabled,
                    )
                    current_binding = (
                        binding.target_type,
                        binding.target_id,
                        binding.target_revision_id,
                        binding.policy,
                        binding.enabled,
                    )
                    binding_changed = desired_binding != current_binding
                    if binding_changed:
                        binding.target_type = binding_request.target_type
                        binding.target_id = binding_request.target_id
                        binding.target_revision_id = binding_request.target_revision_id
                        binding.policy = deepcopy(binding_request.policy)
                        binding.enabled = binding_request.enabled
                        binding.generation += 1
                if binding_changed:
                    channel.chat_id = binding.target_id if binding.target_type == "multirag.dialog" else None
                    channel.status = int(binding.enabled)
                    runtime_changed = True
                    binding_generation_advanced = True

            desired_enabled = request.status == 1 if request.status is not None else None
            if desired_enabled is not None:
                if binding is None and desired_enabled:
                    raise InvalidChannelConfiguration("A channel binding is required before enabling the channel.")
                if binding is not None and binding.enabled != desired_enabled:
                    binding.enabled = desired_enabled
                    binding.generation += 1
                    binding_generation_advanced = True
                    runtime_changed = True
                channel.status = int(desired_enabled)

            if runtime_changed:
                channel.generation += 1
                if binding is not None and not binding_generation_advanced:
                    binding.generation += 1
            if binding is not None and binding.enabled:
                await self._validate_existing_binding(tenant_id, binding)
                self._ensure_ready(channel, secret, binding)

            await self._repository.flush()
            await self._repository.commit()
            return self._serialize(channel, secret, binding)
        except Exception:
            await self._repository.rollback()
            raise

    async def delete_channel(self, tenant_id: str, channel_id: str) -> bool:
        try:
            channel = await self._require_channel(tenant_id, channel_id, for_update=True)
            await self._repository.delete(channel)
            await self._repository.commit()
            return True
        except Exception:
            await self._repository.rollback()
            raise

    async def upsert_binding(
        self,
        tenant_id: str,
        channel_id: str,
        request: ChannelBindingUpsertRequest,
    ) -> dict[str, Any]:
        try:
            channel = await self._require_channel(tenant_id, channel_id, for_update=True)
            await self._validate_target(tenant_id, request)
            secret = await self._repository.get_secret(channel.id, for_update=True)
            binding = await self._repository.get_binding(channel.id, for_update=True)
            if _contains_sensitive_key(request.policy):
                raise InvalidChannelConfiguration("Channel policy must not contain credentials.")

            if binding is None:
                binding = self._new_binding(channel.id, request)
                self._repository.add(binding)
                changed = True
            else:
                desired = (
                    request.target_type,
                    request.target_id,
                    request.target_revision_id,
                    request.policy,
                    request.enabled,
                )
                current = (
                    binding.target_type,
                    binding.target_id,
                    binding.target_revision_id,
                    binding.policy,
                    binding.enabled,
                )
                changed = desired != current
                if changed:
                    binding.target_type = request.target_type
                    binding.target_id = request.target_id
                    binding.target_revision_id = request.target_revision_id
                    binding.policy = deepcopy(request.policy)
                    binding.enabled = request.enabled
                    binding.generation += 1

            if binding.enabled:
                self._ensure_ready(channel, secret, binding)
            if changed:
                channel.chat_id = binding.target_id if binding.target_type == "multirag.dialog" else None
                channel.status = int(binding.enabled)
                channel.generation += 1
            await self._repository.flush()
            await self._repository.commit()
            return self._serialize(channel, secret, binding)
        except Exception:
            await self._repository.rollback()
            raise

    async def set_enabled(self, tenant_id: str, channel_id: str, *, enabled: bool) -> dict[str, Any]:
        try:
            channel = await self._require_channel(tenant_id, channel_id, for_update=True)
            secret = await self._repository.get_secret(channel.id, for_update=True)
            binding = await self._repository.get_binding(channel.id, for_update=True)
            if binding is None:
                raise InvalidChannelConfiguration("A channel binding is required before enabling the channel.")
            if enabled:
                await self._validate_existing_binding(tenant_id, binding)
                self._ensure_ready(channel, secret, binding)
            if binding.enabled != enabled or channel.status != int(enabled):
                binding.enabled = enabled
                binding.generation += 1
                channel.status = int(enabled)
                channel.generation += 1
            await self._repository.flush()
            await self._repository.commit()
            return self._serialize(channel, secret, binding)
        except Exception:
            await self._repository.rollback()
            raise

    async def get_runtime(self, tenant_id: str, channel_id: str) -> dict[str, Any]:
        channel = await self._require_channel(tenant_id, channel_id)
        binding = await self._repository.get_binding(channel.id)
        if binding is None:
            return ChannelRuntimeResponse(
                binding_id=None,
                desired_generation=None,
                observed_generation=0,
                state="waiting",
                runner_id=None,
                heartbeat_at=None,
                connected_at=None,
                last_error_code=None,
            ).model_dump(mode="json")
        runtime = await self._repository.get_runtime(binding.id)
        return self._serialize_runtime(binding, runtime)

    async def load_runtime_binding(self, binding_id: str) -> RuntimeBindingSpec:
        """Load one active desired-state bundle for an authenticated runner."""

        bundle = await self._repository.get_runtime_binding(binding_id)
        if bundle is None:
            raise ChannelAccessDenied
        channel, binding, secret = bundle
        if channel.status != 1 or not binding.enabled:
            raise InvalidChannelConfiguration("The channel binding is not enabled.")
        if secret is None:
            raise InvalidChannelConfiguration("The channel credential is not configured.")
        return RuntimeBindingSpec(
            binding_id=binding.id,
            channel_id=channel.id,
            tenant_id=channel.tenant_id,
            provider=channel.channel,
            public_config=_sanitize_public_config(channel.config),
            target_type=binding.target_type,
            target_id=binding.target_id,
            target_revision_id=binding.target_revision_id,
            policy=deepcopy(binding.policy),
            generation=binding.generation,
            encrypted_secret=EncryptedSecret(
                ciphertext=secret.ciphertext,
                key_id=secret.key_id,
                version=secret.version,
            ),
        )

    async def list_desired_runtimes(self) -> list[dict[str, Any]]:
        """Return credential-free desired state for the external supervisor."""

        bundles = await self._repository.list_runtime_bindings()
        return [
            {
                "binding_id": binding.id,
                "provider": channel.channel,
                "generation": binding.generation,
            }
            for channel, binding, secret in bundles
            if secret is not None
        ]

    async def resolve_runtime_binding(
        self,
        binding_id: str,
        *,
        expected_generation: int | None = None,
    ) -> ResolvedRuntimeBindingSpec:
        """Decrypt one active provider credential for a trusted runner."""

        runtime = await self.load_runtime_binding(binding_id)
        if expected_generation is not None and runtime.generation != expected_generation:
            raise InvalidChannelConfiguration("The channel binding generation is stale.")
        try:
            plaintext = await self._secret_store.decrypt(
                tenant_id=runtime.tenant_id,
                channel_id=runtime.channel_id,
                encrypted=runtime.encrypted_secret,
            )
        except SecretStoreUnavailable as error:
            raise ChannelCredentialUnavailable from error
        credentials = {key: value for key, value in plaintext.items() if isinstance(key, str) and isinstance(value, str)}
        credential_config = runtime.public_config.get("credential")
        if isinstance(credential_config, dict):
            app_id = credential_config.get("app_id")
            if isinstance(app_id, str) and app_id:
                credentials["app_id"] = app_id
        if not credentials.get("app_id") or not credentials.get("app_secret"):
            raise InvalidChannelConfiguration("The channel credential is incomplete.")
        return ResolvedRuntimeBindingSpec(
            binding_id=runtime.binding_id,
            provider=runtime.provider,
            public_config=runtime.public_config,
            credentials=credentials,
            generation=runtime.generation,
        )

    async def report_runtime(
        self,
        *,
        binding_id: str,
        observed_generation: int,
        state: Literal["waiting", "starting", "connected", "stopping", "stopped", "error"],
        runner_id: str | None,
        heartbeat_at: datetime,
        connected_at: datetime | None = None,
        last_error_code: str | None = None,
    ) -> None:
        """Upsert a sanitized runner heartbeat using the binding generation fence."""

        try:
            bundle = await self._repository.get_runtime_binding(binding_id, for_update=True)
            if bundle is None:
                raise ChannelAccessDenied
            _channel, binding, _secret = bundle
            if observed_generation != binding.generation:
                raise InvalidChannelConfiguration("The observed channel generation is invalid.")
            runtime = await self._repository.get_runtime(binding_id, for_update=True)
            if runtime is None:
                runtime = ChannelRuntimeStatus(
                    id=get_uuid(),
                    binding_id=binding_id,
                    observed_generation=observed_generation,
                    state=state,
                    runner_id=runner_id,
                    heartbeat_at=heartbeat_at,
                    connected_at=connected_at,
                    last_error_code=last_error_code,
                )
                self._repository.add(runtime)
            else:
                runtime.observed_generation = observed_generation
                runtime.state = state
                runtime.runner_id = runner_id
                runtime.heartbeat_at = heartbeat_at
                runtime.connected_at = connected_at
                runtime.last_error_code = last_error_code
            await self._repository.flush()
            await self._repository.commit()
        except Exception:
            await self._repository.rollback()
            raise

    async def _serialize_channel(self, channel: ChatChannel) -> dict[str, Any]:
        secret = await self._repository.get_secret(channel.id)
        binding = await self._repository.get_binding(channel.id)
        runtime = await self._repository.get_runtime(binding.id) if binding is not None else None
        revision_stale = await self._binding_revision_stale(channel.tenant_id, binding)
        return self._serialize(
            channel,
            secret,
            binding,
            runtime=runtime,
            include_runtime=True,
            revision_stale=revision_stale,
        )

    async def _binding_revision_stale(self, tenant_id: str, binding: ChannelBinding | None) -> bool | None:
        """Report whether the bound Canvas release stopped being the latest one.

        The executor re-checks the same guard for every message, so a stale
        binding keeps failing closed while its runtime row legitimately stays
        ``connected`` at the current generation. Without this hint the management
        page shows a healthy runner for a channel that answers nothing.
        """

        if binding is None or binding.target_type != "multirag.canvas_agent" or binding.target_revision_id is None:
            return None
        return not await self._repository.canvas_revision_is_latest_published(
            tenant_id,
            binding.target_id,
            binding.target_revision_id,
        )

    def _serialize(
        self,
        channel: ChatChannel,
        secret: ChannelSecret | None,
        binding: ChannelBinding | None,
        *,
        runtime: ChannelRuntimeStatus | None = None,
        include_runtime: bool = False,
        revision_stale: bool | None = None,
    ) -> dict[str, Any]:
        binding_response = ChannelBindingResponse.model_validate(binding).model_copy(update={"revision_stale": revision_stale}) if binding is not None else None
        response = ChatChannelResponse(
            id=channel.id,
            tenant_id=channel.tenant_id,
            name=channel.name,
            channel=channel.channel,
            config=_sanitize_public_config(channel.config),
            chat_id=channel.chat_id,
            status=channel.status,
            generation=channel.generation,
            create_time=channel.create_time,
            update_time=channel.update_time,
            secret=SecretStatus(
                configured=secret is not None,
                version=secret.version if secret is not None else None,
            ),
            binding=binding_response,
            runtime=(ChannelRuntimeResponse.model_validate(self._serialize_runtime(binding, runtime)) if include_runtime and binding is not None else None),
        )
        return response.model_dump(mode="json")

    @staticmethod
    def _serialize_runtime(binding: ChannelBinding, runtime: ChannelRuntimeStatus | None) -> dict[str, Any]:
        if runtime is None or runtime.observed_generation != binding.generation:
            response = ChannelRuntimeResponse(
                binding_id=binding.id,
                desired_generation=binding.generation,
                observed_generation=runtime.observed_generation if runtime is not None else 0,
                state="waiting" if binding.enabled else "stopped",
                runner_id=None,
                heartbeat_at=None,
                connected_at=None,
                last_error_code=None,
            )
        else:
            response = ChannelRuntimeResponse(
                binding_id=binding.id,
                desired_generation=binding.generation,
                observed_generation=runtime.observed_generation,
                state=runtime.state,
                runner_id=runtime.runner_id,
                heartbeat_at=runtime.heartbeat_at,
                connected_at=runtime.connected_at,
                last_error_code=runtime.last_error_code,
            )
        return response.model_dump(mode="json")

    async def _require_channel(
        self,
        tenant_id: str,
        channel_id: str,
        *,
        for_update: bool = False,
    ) -> ChatChannel:
        channel = await self._repository.get_channel(tenant_id, channel_id, for_update=for_update)
        if channel is None:
            raise ChannelAccessDenied
        return channel

    async def _validate_target(self, tenant_id: str, request: ChannelBindingUpsertRequest) -> None:
        if _contains_sensitive_key(request.policy):
            raise InvalidChannelConfiguration("Channel policy must not contain credentials.")
        if request.target_type == "multirag.dialog":
            if not await self._repository.dialog_belongs_to_tenant(tenant_id, request.target_id):
                raise InvalidChannelConfiguration("The selected dialog is unavailable.")
            return
        revision_id = request.target_revision_id
        if revision_id is None or not await self._repository.canvas_revision_is_latest_published(
            tenant_id,
            request.target_id,
            revision_id,
        ):
            raise InvalidChannelConfiguration("The selected agent revision is not the latest published version.")

    async def _validate_existing_binding(self, tenant_id: str, binding: ChannelBinding) -> None:
        await self._validate_target(
            tenant_id,
            ChannelBindingUpsertRequest(
                target_type=binding.target_type,
                target_id=binding.target_id,
                target_revision_id=binding.target_revision_id,
                policy=binding.policy,
                enabled=binding.enabled,
            ),
        )

    async def _encrypt_secret(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        plaintext: dict[str, str],
        version: int,
    ) -> EncryptedSecret:
        try:
            encrypted = await self._secret_store.encrypt(
                tenant_id=tenant_id,
                channel_id=channel_id,
                plaintext=plaintext,
                version=version,
            )
        except SecretStoreUnavailable as error:
            raise ChannelCredentialUnavailable from error
        if encrypted.version != version or not encrypted.ciphertext or not encrypted.key_id:
            raise ChannelCredentialUnavailable
        return encrypted

    @staticmethod
    def _new_secret(channel_id: str, encrypted: EncryptedSecret) -> ChannelSecret:
        return ChannelSecret(
            id=get_uuid(),
            channel_id=channel_id,
            ciphertext=encrypted.ciphertext,
            key_id=encrypted.key_id,
            version=encrypted.version,
        )

    @staticmethod
    def _new_binding(channel_id: str, request: ChannelBindingUpsertRequest) -> ChannelBinding:
        if _contains_sensitive_key(request.policy):
            raise InvalidChannelConfiguration("Channel policy must not contain credentials.")
        return ChannelBinding(
            id=get_uuid(),
            channel_id=channel_id,
            target_type=request.target_type,
            target_id=request.target_id,
            target_revision_id=request.target_revision_id,
            policy=deepcopy(request.policy),
            enabled=request.enabled,
            generation=1,
        )

    @staticmethod
    def _ensure_ready(
        channel: ChatChannel,
        secret: ChannelSecret | None,
        binding: ChannelBinding,
    ) -> None:
        del binding
        public_config = _sanitize_public_config(channel.config)
        credential = public_config.get("credential")
        app_id = credential.get("app_id") if isinstance(credential, dict) else None
        if not app_id or secret is None:
            raise InvalidChannelConfiguration("Feishu App ID and App Secret are required before enabling the channel.")

    async def _apply_compatibility_chat_id(
        self,
        tenant_id: str,
        channel: ChatChannel,
        binding: ChannelBinding | None,
        chat_id: str | None,
    ) -> tuple[ChannelBinding | None, bool]:
        if chat_id is None:
            channel.chat_id = None
            if binding is not None and binding.target_type == "multirag.dialog":
                await self._repository.delete(binding)
                channel.status = 0
                return None, True
            return binding, False

        request = ChannelBindingUpsertRequest(
            target_type="multirag.dialog",
            target_id=chat_id,
            policy=deepcopy(binding.policy) if binding is not None else {},
            enabled=binding.enabled if binding is not None else False,
        )
        await self._validate_target(tenant_id, request)
        channel.chat_id = chat_id
        if binding is None:
            binding = self._new_binding(channel.id, request)
            self._repository.add(binding)
            return binding, True
        if binding.target_type == request.target_type and binding.target_id == request.target_id and binding.target_revision_id is None:
            return binding, False
        binding.target_type = request.target_type
        binding.target_id = request.target_id
        binding.target_revision_id = None
        binding.generation += 1
        return binding, True

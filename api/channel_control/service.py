"""Channel control-plane orchestration with tenant and secret boundaries."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from api.channel_control.repository import ChannelRepository
from api.channel_control.schemas import (
    ChannelBindingResponse,
    ChannelBindingUpsertRequest,
    ChannelCreateRequest,
    ChannelRuntimeResponse,
    ChannelUpdateRequest,
    ChatChannelResponse,
    SecretStatus,
)
from api.channel_control.secret_store import EncryptedSecret, SecretStore, SecretStoreUnavailable
from api.channel_providers import ProviderSpec, provider_spec, resolve_path
from api.channel_providers.functions import ProviderConfigInvalid, merge_config_patch, missing_required_fields, split_config, validate_config
from api.db.db_models import ChannelBinding, ChannelRuntimeStatus, ChannelSecret, ChatChannel
from common.app_config import get_app_config
from common.constants import TenantPermission
from common.misc_utils import get_uuid

LOGGER = logging.getLogger(__name__)


def _short_hash(value: str) -> str:
    """Log-safe identifier, matching how the supervisor redacts binding ids."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


# States that only a live runner can hold, so heartbeat silence disproves them.
# ``waiting``/``stopped``/``error`` legitimately stop reporting a heartbeat.
_LIVE_RUNTIME_STATES = frozenset({"starting", "connected", "stopping"})
# Tolerate a few missed reports before calling a runner dead.
_HEARTBEAT_STALE_INTERVALS = 3
# Reported when the bound Canvas release stopped being the latest published one.
#
# Deliberately the same code the executor already raises per message
# (``TargetRevisionUnavailableError``) rather than a second name for one fact:
# an operator tracing a silent channel greps one string, not two. It is
# duplicated instead of imported because ``api.channel_execution`` imports this
# package, and a constant is not worth an import cycle;
# ``test_stale_revision_error_code_matches_the_executor`` binds the two.
_REVISION_STALE_ERROR_CODE = "TARGET_REVISION_UNAVAILABLE"

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

# Substrings that mark a config key as credential-bearing. Providers spell their
# credentials as ``client_secret`` / ``bot_token`` / ``channel_access_token`` /
# ``signing_secret`` / ``zalo cookies``, so matching has to be by substring --
# an exact-match list lets every one of those through. ``*_key`` is handled
# separately in :func:`_is_secret_leaf_key`.
_SECRET_KEY_SUBSTRINGS = ("secret", "token", "password", "passwd", "authorization", "cookie")


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


class ChannelTargetNotAccessible(ChannelControlError):
    """The caller may see this target but may not publish it to a channel.

    Distinct from :class:`ChannelAccessDenied`, which is about the channel row
    itself. Binding a target re-publishes someone's Agent or Dialog to an
    entire external workspace, so it takes an updater role in the tenant that
    owns the target -- not merely the ability to read it.
    """

    def __init__(self) -> None:
        super().__init__(
            "You do not have permission to bind this target to an external channel.",
            error_code="CHANNEL_TARGET_NOT_ACCESSIBLE",
        )


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
    # The admin's binding policy, free-form by design. It has always been
    # validated to carry no credentials, which is what makes it safe to hand a
    # runner verbatim rather than transcribing known keys one at a time.
    policy: dict[str, Any]


def _is_secret_leaf_key(name: str) -> bool:
    """Whether one config key holds a credential rather than a public setting.

    Substring matching, not exact membership: provider credential fields are
    named ``client_secret`` / ``bot_token`` / ``channel_access_token``, never
    the bare words, so an exact-match blocklist passes every one of them
    through to a read path that echoes ``config`` back to the browser.

    ``*_key`` is tested separately from the substring list because a bare
    ``key`` substring would also strike ``key_id`` -- the non-secret label
    identifying which master key encrypted a row -- and ``keywords``.
    """

    return any(marker in name for marker in _SECRET_KEY_SUBSTRINGS) or name == "key" or name.endswith("_key")


def _contains_sensitive_key(value: Any) -> bool:
    """Whether a binding policy carries anything credential-shaped.

    Stricter than :func:`_is_secret_leaf_key` by exactly one key: ``credential``
    is rejected outright here while :func:`_sanitize_public_config` recurses
    into it. A policy has no business carrying a credential block at all, but a
    public config legitimately holds ``credential.app_id``.
    """

    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if normalized in _SENSITIVE_CONFIG_KEYS or _is_secret_leaf_key(normalized):
                return True
            if _contains_sensitive_key(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _sanitize_public_config(value: Any) -> dict[str, Any]:
    """Defensively strip credentials if an old row contains legacy plaintext.

    A blocklist backstop, not the primary defence: credentials are supposed to
    live in the encrypted secret store and never reach ``config`` at all. It
    guards the two paths that hand ``config`` to someone -- the tenant read
    response and the public config handed to a worker.
    """

    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key, nested in value.items():
        normalized = str(key).strip().lower()
        if _is_secret_leaf_key(normalized):
            continue
        if isinstance(nested, dict):
            sanitized[str(key)] = _sanitize_public_config(nested)
        elif isinstance(nested, list):
            sanitized[str(key)] = [_sanitize_public_config(item) if isinstance(item, dict) else item for item in nested]
        else:
            sanitized[str(key)] = nested
    return sanitized


def _spec_for(channel: ChatChannel) -> ProviderSpec:
    """The registered spec for a stored channel row."""

    return provider_spec(channel.channel)


def _account_identity(channel: ChatChannel) -> str | None:
    """Which provider account a channel connects as, or None if unset."""

    return _spec_for(channel).account_identity(_sanitize_public_config(channel.config))


def _parse_config(spec: ProviderSpec, payload: Mapping[str, Any], *, partial: bool = False) -> BaseModel:
    """Validate a raw config body against the model its provider declares.

    Surfaces as ``INVALID_CHANNEL_CONFIGURATION`` rather than a bare 422: the
    request models can no longer type this field (a PATCH body does not say
    which provider it is for), and the client already renders that code as a
    real message. The wrapped error names field locations only, never the
    submitted values.
    """

    try:
        return validate_config(spec, payload, partial=partial)
    except ProviderConfigInvalid as error:
        raise InvalidChannelConfiguration(str(error)) from None


def _create_public_config(spec: ProviderSpec, config: BaseModel) -> tuple[dict[str, Any], dict[str, str] | None]:
    return split_config(spec, config)


def _patch_public_config(spec: ProviderSpec, current: dict[str, Any], patch: BaseModel) -> tuple[dict[str, Any], dict[str, str] | None, bool]:
    return merge_config_patch(spec, current, patch, sanitize=_sanitize_public_config)


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
        spec = provider_spec(request.channel)
        public_config, plaintext = _create_public_config(spec, _parse_config(spec, request.config))
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
                await self._ensure_ready(channel, secret, binding)

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
                spec = _spec_for(channel)
                public, plaintext, config_changed = _patch_public_config(spec, channel.config, _parse_config(spec, request.config, partial=True))
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
                await self._ensure_ready(channel, secret, binding)

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
                await self._ensure_ready(channel, secret, binding)
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
                await self._ensure_ready(channel, secret, binding)
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
        # The dedicated runtime route used to be the *least* accurate view of a
        # channel: the list response resolved staleness and this one did not, so
        # the panel an operator opens to diagnose a silent channel was the one
        # place that never mentioned the reason.
        revision_stale = await self._binding_revision_stale(channel.tenant_id, binding)
        return self._serialize_runtime(binding, runtime, revision_stale=revision_stale)

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
        desired: list[dict[str, Any]] = []
        for channel, binding, secret in bundles:
            if secret is None:
                # Skipping is right -- a worker cannot start without a
                # credential -- but skipping *silently* left the admin staring
                # at `waiting` forever with no error code anywhere in the
                # system. The runtime row is untouched on purpose: this is an
                # observation, not a state transition.
                LOGGER.warning(
                    "channel_control_event=desired_binding_skipped error_code=CHANNEL_SECRET_MISSING binding_id_hash=%s provider=%s",
                    _short_hash(binding.id),
                    channel.channel,
                )
                continue
            desired.append(
                {
                    "binding_id": binding.id,
                    "provider": channel.channel,
                    "generation": binding.generation,
                }
            )
        return desired

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

        # A provider's credential is split across two stores: the secret half is
        # encrypted, the non-secret half (an app id, a corp id) legitimately
        # lives in the public config. Reassemble it here so the worker receives
        # one credential and never has to know about the split.
        spec = provider_spec(runtime.provider)
        for path in sorted(spec.credential_paths - spec.secret_paths):
            value = resolve_path(runtime.public_config, path)
            if isinstance(value, str) and value:
                credentials[path.rsplit(".", 1)[-1]] = value

        missing = missing_required_fields(spec, runtime.public_config, configured_secrets=bool(plaintext))
        if missing:
            raise InvalidChannelConfiguration("The channel credential is incomplete.")
        return ResolvedRuntimeBindingSpec(
            binding_id=runtime.binding_id,
            provider=runtime.provider,
            public_config=runtime.public_config,
            credentials=credentials,
            generation=runtime.generation,
            policy=runtime.policy,
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
        del tenant_id
        return not await self._repository.canvas_revision_is_latest_published(
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
            runtime=(ChannelRuntimeResponse.model_validate(self._serialize_runtime(binding, runtime, revision_stale=revision_stale)) if include_runtime and binding is not None else None),
        )
        return response.model_dump(mode="json")

    @staticmethod
    def _heartbeat_is_stale(runtime: ChannelRuntimeStatus) -> bool:
        """Decide whether a live-looking runtime row is only a dead runner's leftover.

        A runner rewrites this row on a fixed interval, so silence disproves the
        states only a live process can hold. Without this check a killed worker
        keeps the management page on ``connected`` forever, because its final row
        still matches the desired generation.
        """

        if runtime.state not in _LIVE_RUNTIME_STATES:
            return False
        heartbeat_at = runtime.heartbeat_at
        if heartbeat_at is None:
            # A live state that never reported is already self-contradictory.
            return True
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=UTC)
        deadline = get_app_config().channels.control.runtime_heartbeat_seconds * _HEARTBEAT_STALE_INTERVALS
        return (datetime.now(UTC) - heartbeat_at).total_seconds() > deadline

    @classmethod
    def _serialize_runtime(
        cls,
        binding: ChannelBinding,
        runtime: ChannelRuntimeStatus | None,
        *,
        revision_stale: bool | None = None,
    ) -> dict[str, Any]:
        idle_state = "waiting" if binding.enabled else "stopped"
        if runtime is None or runtime.observed_generation != binding.generation:
            response = ChannelRuntimeResponse(
                binding_id=binding.id,
                desired_generation=binding.generation,
                observed_generation=runtime.observed_generation if runtime is not None else 0,
                state=idle_state,
                runner_id=None,
                heartbeat_at=None,
                connected_at=None,
                last_error_code=None,
            )
        elif cls._heartbeat_is_stale(runtime):
            # Keep the last heartbeat and error code: they are the operator's only
            # evidence of when the runner died and why.
            response = ChannelRuntimeResponse(
                binding_id=binding.id,
                desired_generation=binding.generation,
                observed_generation=runtime.observed_generation,
                state=idle_state,
                runner_id=None,
                heartbeat_at=runtime.heartbeat_at,
                connected_at=None,
                last_error_code=runtime.last_error_code,
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
        if revision_stale and binding.enabled:
            # A stale binding fails every single message in the executor while
            # its runner stays genuinely connected and on-generation, so a fresh
            # heartbeat is not evidence of health here -- it is the thing that
            # makes the fault invisible. The runner fields are kept: the process
            # really is alive, and an operator needs to know which one to look at.
            response = response.model_copy(
                update={"state": "error", "last_error_code": _REVISION_STALE_ERROR_CODE},
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
        """Resolve the target, then authorize the caller against its owner.

        Ownership and authorization are two answers, not one. Matching only the
        caller's own tenant made every team-shared target fail here while the
        frontend dropdown listed it from the team scope -- so picking one
        produced a rejection with no way to act on it. Widening the lookup
        without a role check would be worse: any member could re-publish a
        colleague's Agent to a whole external workspace.
        """

        if _contains_sensitive_key(request.policy):
            raise InvalidChannelConfiguration("Channel policy must not contain credentials.")

        if request.target_type == "multirag.dialog":
            owner = await self._repository.resolve_dialog_owner(request.target_id)
            if owner is None:
                raise InvalidChannelConfiguration("The selected dialog is unavailable.")
            await self._ensure_may_publish_target(tenant_id, owner)
            return

        resolved = await self._repository.resolve_canvas_owner(request.target_id)
        if resolved is None:
            raise InvalidChannelConfiguration("The selected agent is unavailable.")
        owner, permission = resolved
        # Canvases carry an explicit share flag; a private one is only ever
        # bindable by its owner, whatever role the caller holds elsewhere.
        if owner != tenant_id and permission != TenantPermission.TEAM:
            raise ChannelTargetNotAccessible()
        await self._ensure_may_publish_target(tenant_id, owner)

        revision_id = request.target_revision_id
        if revision_id is None or not await self._repository.canvas_revision_is_latest_published(
            request.target_id,
            revision_id,
        ):
            raise InvalidChannelConfiguration("The selected agent revision is not the latest published version.")

    async def _ensure_may_publish_target(self, actor_id: str, owner_tenant_id: str) -> None:
        if owner_tenant_id == actor_id:
            return
        if not await self._repository.user_can_update_tenant_resources(actor_id, owner_tenant_id):
            raise ChannelTargetNotAccessible()

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

    async def _ensure_ready(
        self,
        channel: ChatChannel,
        secret: ChannelSecret | None,
        binding: ChannelBinding,
    ) -> None:
        del binding
        spec = _spec_for(channel)
        missing = missing_required_fields(
            spec,
            _sanitize_public_config(channel.config),
            configured_secrets=secret is not None,
        )
        if missing:
            # Names the actual fields rather than one provider's two, so the
            # message stays true when a provider needs a different set.
            labels = ", ".join(field.label for field in spec.form.fields if field.path in set(missing))
            raise InvalidChannelConfiguration(f"{labels} must be set before enabling the channel.")

        account_id = spec.account_identity(_sanitize_public_config(channel.config))
        if account_id is not None:
            await self._ensure_account_not_already_enabled(channel, account_id)

    async def _ensure_account_not_already_enabled(self, channel: ChatChannel, account_id: str) -> None:
        """Reject a second enabled channel on the same provider account.

        The leader lease is per binding (CHN-S3), so nothing at the Redis layer
        stops two enabled bindings on one provider account from both connecting
        and answering the same message twice. That invariant moves here, to the
        control plane.

        Scoped to one tenant deliberately. A global uniqueness check would
        itself become a cross-tenant squatting vector: whoever registers an
        account id first would lock every other tenant out of an account they
        may legitimately own.
        """

        for other in await self._repository.list_enabled_channels(channel.tenant_id, channel.channel):
            if other.id == channel.id:
                continue
            if _account_identity(other) == account_id:
                raise InvalidChannelConfiguration("Another enabled channel already uses this provider account.")

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

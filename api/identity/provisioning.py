"""Verification-gated orchestration for EIM-I6 provisioning and linking."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from api.identity.contracts import (
    IdentityErrorCode,
    IdentityResolutionResult,
    IdentityResolutionStatus,
    ProviderAliasType,
    ProviderContext,
    ProvisioningAction,
    ProvisioningMode,
    ProvisioningPolicyResolver,
)
from api.identity.principal import Principal
from api.identity.providers.contracts import (
    ProviderDirectoryStatus,
    ProviderIdentity,
    ProviderIdentityStatus,
)
from api.identity.provisioning_contracts import (
    IdentityProvisioningRepository,
    LinkCodeIssueCommand,
    LinkCodeIssueRequest,
    LinkCodeIssueResult,
    LinkCodeIssueStatus,
    ProvisionIdentityRequest,
    ProvisioningRepositoryError,
    ProvisioningResult,
    ProvisioningStatus,
    VerifiedProvisioningAlias,
    VerifiedProvisioningCommand,
)
from api.identity.validation import (
    valid_alias_key,
    valid_opaque_id,
    valid_policy_snapshot,
    valid_provider_context,
    valid_revision,
    valid_text,
    valid_timestamp,
)

_LINK_CODE_DOMAIN = b"multirag.identity.link-code.v1\x00"
_REQUEST_DIGEST_DOMAIN = b"multirag.identity.binding-request.v1\x00"
_LINK_CODE_TOKEN_BYTES = 24
_MIN_HMAC_KEY_BYTES = 32
_MAX_LINK_CODE_LENGTH = 512
_MAX_NICKNAME_LENGTH = 100
_JIT_NICKNAME_FALLBACK = "Enterprise user"
_MAX_PROVIDER_PROOF_AGE = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class LinkCodeMaterial:
    raw_code: str = field(repr=False)
    key_id: str = field(repr=False)
    digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RequestFingerprint:
    key_id: str = field(repr=False)
    digest: str = field(repr=False)


class HmacLinkCodeCodec:
    """CSPRNG link codes and domain-separated keyed fingerprints."""

    def __init__(
        self,
        *,
        keys: Mapping[str, bytes],
        active_key_id: str,
        token_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        copied = dict(keys)
        if (
            not copied
            or not _valid_key_id(active_key_id)
            or active_key_id not in copied
            or any(not _valid_key_id(key_id) or type(key) is not bytes or len(key) < _MIN_HMAC_KEY_BYTES for key_id, key in copied.items())
        ):
            raise ValueError("link code keyring is invalid")
        self._keys = copied
        self._active_key_id = active_key_id
        self._token_bytes = token_bytes

    def issue(self) -> LinkCodeMaterial:
        token_material = self._token_bytes(_LINK_CODE_TOKEN_BYTES)
        if type(token_material) is not bytes or len(token_material) != _LINK_CODE_TOKEN_BYTES:
            raise ValueError("link code entropy source is invalid")
        token = base64.urlsafe_b64encode(token_material).rstrip(b"=").decode("ascii")
        raw_code = f"{self._active_key_id}.{token}"
        digest = self._digest(
            key_id=self._active_key_id,
            domain=_LINK_CODE_DOMAIN,
            payload=raw_code.encode("ascii"),
        )
        return LinkCodeMaterial(
            raw_code=raw_code,
            key_id=self._active_key_id,
            digest=digest,
        )

    def digest(self, raw_code: str) -> LinkCodeMaterial | None:
        if type(raw_code) is not str or not 1 <= len(raw_code) <= _MAX_LINK_CODE_LENGTH:
            return None
        try:
            key_id, token = raw_code.split(".", maxsplit=1)
            token_bytes = _decode_token(token)
        except (TypeError, ValueError):
            return None
        if not _valid_key_id(key_id) or key_id not in self._keys or len(token_bytes) != _LINK_CODE_TOKEN_BYTES:
            return None
        return LinkCodeMaterial(
            raw_code=raw_code,
            key_id=key_id,
            digest=self._digest(
                key_id=key_id,
                domain=_LINK_CODE_DOMAIN,
                payload=raw_code.encode("ascii"),
            ),
        )

    def fingerprint(self, parts: Sequence[str]) -> RequestFingerprint:
        if not parts or any(type(part) is not str for part in parts):
            raise ValueError("request fingerprint input is invalid")
        payload = json.dumps(
            list(parts),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        return RequestFingerprint(
            key_id=self._active_key_id,
            digest=self._digest(
                key_id=self._active_key_id,
                domain=_REQUEST_DIGEST_DOMAIN,
                payload=payload,
            ),
        )

    def _digest(self, *, key_id: str, domain: bytes, payload: bytes) -> str:
        return hmac.new(
            self._keys[key_id],
            domain + payload,
            hashlib.sha256,
        ).hexdigest()


class IdentityProvisioningService:
    """Turn trusted I3/I4 evidence into narrow atomic repository commands."""

    def __init__(
        self,
        repository: IdentityProvisioningRepository,
        policy_resolver: ProvisioningPolicyResolver,
        codec: HmacLinkCodeCodec,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._policy_resolver = policy_resolver
        self._codec = codec
        self._now = now

    async def provision_verified_identity(
        self,
        request: ProvisionIdentityRequest,
    ) -> ProvisioningResult:
        command_or_error = self._verified_command(request)
        if isinstance(command_or_error, IdentityErrorCode):
            return _provisioning_rejection(command_or_error)
        try:
            result = await self._repository.provision_verified_identity(command_or_error)
        except ProvisioningRepositoryError as exc:
            result = _provisioning_rejection(exc.code)
        return _sanitize_link_code_result(command_or_error, result)

    async def issue_link_code(
        self,
        request: LinkCodeIssueRequest,
    ) -> LinkCodeIssueResult:
        context = request.context
        principal = request.principal
        if not valid_provider_context(context) or not isinstance(principal, Principal) or principal.tenant_id != context.tenant_id or not valid_opaque_id(principal.platform_user_id):
            return _link_code_rejection(IdentityErrorCode.ASSERTION_INVALID)
        try:
            policy = await self._policy_resolver.get_policy(context.tenant_id)
        except Exception:
            return _link_code_rejection(IdentityErrorCode.POLICY_UNAVAILABLE)
        if not valid_policy_snapshot(policy, tenant_id=context.tenant_id):
            return _link_code_rejection(IdentityErrorCode.POLICY_UNAVAILABLE)
        if policy.mode is not ProvisioningMode.LINK_ONLY:
            return _link_code_rejection(IdentityErrorCode.TRANSITION_INVALID)

        issued_at = self._now()
        if not valid_timestamp(issued_at):
            return _link_code_rejection(IdentityErrorCode.REPOSITORY_UNAVAILABLE)
        material = self._codec.issue()
        command = LinkCodeIssueCommand(
            context=context,
            target_user_id=principal.platform_user_id,
            digest_key_id=material.key_id,
            code_digest=material.digest,
            policy_revision=policy.revision,
            provider_account_revision=context.provider_account_revision,
            provider_account_last_scope_change_at=context.provider_account_last_scope_change_at,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=policy.link_code_ttl_seconds),
        )
        try:
            grant = await self._repository.issue_link_code(command)
        except ProvisioningRepositoryError as exc:
            return _link_code_rejection(exc.code)
        return LinkCodeIssueResult(
            status=LinkCodeIssueStatus.ISSUED,
            code=material.raw_code,
            expires_at=grant.expires_at,
        )

    def _verified_command(
        self,
        request: ProvisionIdentityRequest,
    ) -> VerifiedProvisioningCommand | IdentityErrorCode:
        resolution_request = request.resolution_request
        resolution = request.resolution
        context = resolution_request.context
        if not valid_provider_context(context) or not valid_alias_key(resolution_request.alias):
            return IdentityErrorCode.ASSERTION_INVALID
        if resolution_request.alias.alias_type is not ProviderAliasType.OPEN_ID:
            return IdentityErrorCode.ASSERTION_INVALID
        if not _valid_resolution_plan(resolution):
            return IdentityErrorCode.TRANSITION_INVALID

        now = self._now()
        if not valid_timestamp(now):
            return IdentityErrorCode.REPOSITORY_UNAVAILABLE
        proof = _valid_provider_proof(request, context, now=now)
        if isinstance(proof, IdentityErrorCode):
            return proof
        aliases = _provider_aliases(proof)
        if aliases is None:
            return IdentityErrorCode.ASSERTION_INVALID

        link_material: LinkCodeMaterial | None = None
        if request.link_code is not None:
            if resolution.provisioning_action is not ProvisioningAction.REQUIRE_LINK:
                return IdentityErrorCode.TRANSITION_INVALID
            link_material = self._codec.digest(request.link_code)
            if link_material is None:
                return IdentityErrorCode.LINK_REQUIRED

        fingerprint = self._codec.fingerprint(
            (
                context.tenant_id,
                context.provider,
                context.provider_tenant_key,
                context.provider_account_id,
                context.provider_account_key,
                str(context.provider_account_revision),
                context.provider_account_last_scope_change_at.isoformat() if context.provider_account_last_scope_change_at is not None else "",
                resolution_request.alias.alias_type.value,
                resolution_request.alias.alias_value,
                resolution.provisioning_action.value if resolution.provisioning_action is not None else "reverify",
                str(resolution.provisioning_policy_revision or ""),
                proof.provider_user_id,
                proof.open_id or "",
                proof.union_id or "",
                proof.verified_at.isoformat(),
                link_material.key_id if link_material is not None else "",
                link_material.digest if link_material is not None else "",
            )
        )
        return VerifiedProvisioningCommand(
            context=context,
            asserted_alias=resolution_request.alias,
            action=resolution.provisioning_action,
            policy_revision=resolution.provisioning_policy_revision,
            subject_value=proof.provider_user_id,
            aliases=aliases,
            verified_at=proof.verified_at,
            jit_nickname=_normalize_nickname(proof.display_name),
            link_code_key_id=(link_material.key_id if link_material is not None else None),
            link_code_digest=(link_material.digest if link_material is not None else None),
            request_digest_key_id=fingerprint.key_id,
            request_digest=fingerprint.digest,
        )


def _valid_resolution_plan(result: IdentityResolutionResult) -> bool:
    if result.status is IdentityResolutionStatus.MISSING:
        action = result.provisioning_action
        revision = result.provisioning_policy_revision
        return bool(
            result.identity is None
            and result.membership is None
            and result.provider_verification_required
            and action
            in {
                ProvisioningAction.BIND_PREPROVISIONED,
                ProvisioningAction.REQUIRE_LINK,
                ProvisioningAction.CREATE_NORMAL_MEMBER,
            }
            and valid_revision(revision)
            and ((action is ProvisioningAction.REQUIRE_LINK and result.error_code is IdentityErrorCode.LINK_REQUIRED) or (action is not ProvisioningAction.REQUIRE_LINK and result.error_code is None))
        )
    return bool(
        result.status is IdentityResolutionStatus.INACTIVE
        and result.error_code is IdentityErrorCode.INACTIVE
        and result.identity is None
        and result.membership is None
        and result.provisioning_action is None
        and result.provisioning_policy_revision is None
        and result.provider_verification_required
    )


def _valid_provider_proof(
    request: ProvisionIdentityRequest,
    context: ProviderContext,
    *,
    now: datetime,
) -> ProviderIdentity | IdentityErrorCode:
    provider_result = request.provider_result
    if provider_result.status is not ProviderIdentityStatus.RESOLVED or provider_result.error_code is not None or provider_result.identity is None:
        return _provider_result_error(provider_result.status)
    proof = provider_result.identity
    if proof.provider != context.provider or proof.provider_account_id != context.provider_account_id:
        return IdentityErrorCode.PROVIDER_MISMATCH
    if proof.provider_tenant_key != context.provider_tenant_key:
        return IdentityErrorCode.TENANT_MISMATCH
    if (
        proof.provider_status is not ProviderDirectoryStatus.ACTIVE
        or not valid_timestamp(proof.verified_at)
        or proof.verified_at < now - _MAX_PROVIDER_PROOF_AGE
        or proof.verified_at > now
        or not valid_text(proof.provider_user_id, max_length=255)
        or not valid_text(proof.open_id, max_length=255)
        or proof.open_id != request.resolution_request.alias.alias_value
        or (proof.union_id is not None and not valid_text(proof.union_id, max_length=255))
        or (proof.display_name is not None and (type(proof.display_name) is not str or len(proof.display_name) > 512))
        or (context.provider_account_last_scope_change_at is not None and proof.verified_at < context.provider_account_last_scope_change_at)
    ):
        return IdentityErrorCode.ASSERTION_INVALID
    return proof


def _provider_aliases(
    proof: ProviderIdentity,
) -> tuple[VerifiedProvisioningAlias, ...] | None:
    if proof.open_id is None:
        return None
    aliases = [
        VerifiedProvisioningAlias(
            alias_type=ProviderAliasType.OPEN_ID,
            alias_value=proof.open_id,
        )
    ]
    if proof.union_id is not None:
        aliases.append(
            VerifiedProvisioningAlias(
                alias_type=ProviderAliasType.UNION_ID,
                alias_value=proof.union_id,
            )
        )
    return tuple(aliases)


def _provider_result_error(status: ProviderIdentityStatus) -> IdentityErrorCode:
    if status is ProviderIdentityStatus.INACTIVE:
        return IdentityErrorCode.INACTIVE
    if status is ProviderIdentityStatus.CONFLICT:
        return IdentityErrorCode.LINK_CONFLICT
    if status is ProviderIdentityStatus.NOT_FOUND:
        return IdentityErrorCode.NOT_FOUND
    return IdentityErrorCode.ASSERTION_INVALID


def _normalize_nickname(display_name: str | None) -> str:
    if display_name is None:
        return _JIT_NICKNAME_FALLBACK
    normalized = " ".join(unicodedata.normalize("NFKC", display_name).split())
    return normalized[:_MAX_NICKNAME_LENGTH] or _JIT_NICKNAME_FALLBACK


def _decode_token(token: str) -> bytes:
    if type(token) is not str or not token or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in token):
        raise ValueError("link code token is invalid")
    padding = "=" * (-len(token) % 4)
    return base64.b64decode(
        token + padding,
        altchars=b"-_",
        validate=True,
    )


def _valid_key_id(value: object) -> bool:
    return type(value) is str and 1 <= len(value) <= 64 and all(character.isascii() and (character.isalnum() or character in "_-") for character in value)


def _provisioning_rejection(code: IdentityErrorCode) -> ProvisioningResult:
    return ProvisioningResult(
        status=ProvisioningStatus.REJECTED,
        error_code=code,
    )


def _link_code_rejection(code: IdentityErrorCode) -> LinkCodeIssueResult:
    return LinkCodeIssueResult(
        status=LinkCodeIssueStatus.REJECTED,
        error_code=code,
    )


def _sanitize_link_code_result(
    command: VerifiedProvisioningCommand,
    result: ProvisioningResult,
) -> ProvisioningResult:
    if (
        command.action is ProvisioningAction.REQUIRE_LINK
        and command.link_code_digest is not None
        and result.status is ProvisioningStatus.REJECTED
        and result.error_code
        in {
            IdentityErrorCode.NOT_FOUND,
            IdentityErrorCode.REVISION_CONFLICT,
            IdentityErrorCode.TRANSITION_INVALID,
            IdentityErrorCode.OWNERSHIP_CONFLICT,
            IdentityErrorCode.INACTIVE,
            IdentityErrorCode.LINK_CONFLICT,
        }
    ):
        return _provisioning_rejection(IdentityErrorCode.LINK_REQUIRED)
    return result


__all__ = [
    "HmacLinkCodeCodec",
    "IdentityProvisioningService",
    "LinkCodeMaterial",
    "RequestFingerprint",
]

"""Shared, side-effect-free validation for EIM identity boundaries."""

from __future__ import annotations

from datetime import datetime

from api.identity.contracts import AliasKey, ProviderAliasType, ProviderContext

_MAX_BIGINT = (1 << 63) - 1


def valid_text(value: object, *, max_length: int) -> bool:
    return type(value) is str and 0 < len(value.strip()) and len(value) <= max_length


def valid_provider_context(context: ProviderContext) -> bool:
    return isinstance(context, ProviderContext) and bool(
        valid_text(context.tenant_id, max_length=32)
        and valid_text(context.provider, max_length=64)
        and valid_text(context.provider_tenant_key, max_length=255)
        and valid_opaque_id(context.provider_account_id)
        and valid_text(context.provider_account_key, max_length=255)
        and valid_revision(context.provider_account_revision)
        and (context.provider_account_last_scope_change_at is None or valid_timestamp(context.provider_account_last_scope_change_at))
    )


def valid_alias_key(alias: AliasKey) -> bool:
    return isinstance(alias, AliasKey) and valid_alias(alias.alias_type, alias.alias_value)


def valid_alias(alias_type: ProviderAliasType, alias_value: str) -> bool:
    return isinstance(alias_type, ProviderAliasType) and valid_text(alias_value, max_length=255)


def valid_opaque_id(value: str) -> bool:
    return valid_text(value, max_length=32)


def valid_revision(value: int) -> bool:
    return type(value) is int and 1 <= value <= _MAX_BIGINT


def valid_timestamp(value: datetime) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None

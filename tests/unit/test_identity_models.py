"""Canonical enterprise identity model safety boundaries."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects.postgresql import JSONB

from api.db.db_models import (
    EXTERNAL_IDENTITY_ATTRIBUTE_KEYS,
    EnterpriseSubjectLink,
    ExternalIdentity,
    ExternalIdentityAlias,
    IdentityEventReceipt,
    IdentityProviderAccount,
    IdentityProviderChannelLink,
    IdentityProviderTenant,
)


def _verified_at() -> datetime:
    return datetime(2026, 8, 12, tzinfo=UTC)


def test_external_identity_projection_redacts_provider_subject_material() -> None:
    identity = ExternalIdentity(
        id="identity-id",
        tenant_id="tenant-id",
        user_id="user-id",
        provider="feishu",
        provider_tenant_key="tenant-key-secret",
        subject_type="user_id",
        subject_value="provider-user-id-secret",
        state="active",
        verified_at=_verified_at(),
        identity_revision=1,
        attributes={"display_name": "Visible only inside identity resolution"},
    )

    projection = identity.to_dict()

    assert projection["id"] == "identity-id"
    assert projection["provider"] == "feishu"
    assert "provider_tenant_key" not in projection
    assert "subject_value" not in projection
    assert "attributes" not in projection
    assert "tenant-key-secret" not in projection.values()
    assert "provider-user-id-secret" not in projection.values()


def test_identity_related_projections_redact_alias_subject_and_receipt_material() -> None:
    alias = ExternalIdentityAlias(
        id="alias-id",
        tenant_id="tenant-id",
        external_identity_id="identity-id",
        provider="feishu",
        provider_tenant_key="tenant-key-secret",
        provider_account_key="app-id-secret",
        alias_type="open_id",
        alias_value="open-id-secret",
        verified_at=_verified_at(),
    )
    subject = EnterpriseSubjectLink(
        id="subject-id",
        tenant_id="tenant-id",
        user_id="user-id",
        subject_type="employee_no",
        subject_value="employee-no-secret",
        issuer="feishu_contact",
        issuer_tenant="issuer-tenant-secret",
        state="active",
        verified_at=_verified_at(),
    )
    receipt = IdentityEventReceipt(
        id="receipt-id",
        tenant_id="tenant-id",
        provider="feishu",
        provider_tenant_key="tenant-key-secret",
        provider_account_key="app-id-secret",
        event_type="contact.user.updated",
        event_id="event-id-secret",
        event_hash="a" * 64,
        processing_state="processing",
    )

    alias_projection = alias.to_dict()
    subject_projection = subject.to_dict()
    receipt_projection = receipt.to_dict()

    assert {"provider_tenant_key", "provider_account_key", "alias_value"}.isdisjoint(alias_projection)
    assert {"subject_value", "issuer_tenant"}.isdisjoint(subject_projection)
    assert {
        "provider_tenant_key",
        "provider_account_key",
        "event_id",
        "event_hash",
    }.isdisjoint(receipt_projection)


def test_provider_ownership_projections_redact_binding_material() -> None:
    provider_tenant = IdentityProviderTenant(
        id="provider-tenant-id",
        tenant_id="tenant-id",
        provider="feishu",
        provider_tenant_key="tenant-key-secret",
        verified_at=_verified_at(),
    )
    provider_account = IdentityProviderAccount(
        id="provider-account-id",
        tenant_id="tenant-id",
        provider="feishu",
        provider_tenant_key="tenant-key-secret",
        provider_account_key="app-id-secret",
        identity_revision=1,
        identity_health_state="error",
        identity_health_error_code="provider-credential-invalid",
    )
    channel_link = IdentityProviderChannelLink(
        id="channel-link-id",
        tenant_id="tenant-id",
        provider="feishu",
        provider_account_id="provider-account-id",
        channel_id="channel-id-secret",
        linked_at=_verified_at(),
    )

    tenant_projection = provider_tenant.to_dict()
    account_projection = provider_account.to_dict()
    link_projection = channel_link.to_dict()

    assert "provider_tenant_key" not in tenant_projection
    assert {
        "provider_tenant_key",
        "provider_account_key",
        "identity_health_error_code",
    }.isdisjoint(account_projection)
    assert {
        "tenant_id",
        "provider_account_id",
        "channel_id",
    }.isdisjoint(link_projection)
    assert "provider-account-id" not in link_projection.values()
    assert "channel-id-secret" not in link_projection.values()


def test_provider_account_is_independent_from_channel_link() -> None:
    account_columns = set(IdentityProviderAccount.__table__.columns.keys())
    link_columns = set(IdentityProviderChannelLink.__table__.columns.keys())

    assert "channel_id" not in account_columns
    assert {
        "tenant_id",
        "provider",
        "provider_account_id",
        "channel_id",
        "linked_at",
    } <= link_columns


def test_external_identity_attributes_are_allowlisted_and_defensively_copied() -> None:
    source = {
        "display_name": "Alice",
        "provider_status": None,
    }

    identity = ExternalIdentity(attributes=source)
    source["display_name"] = "Changed after assignment"

    assert EXTERNAL_IDENTITY_ATTRIBUTE_KEYS == {
        "display_name",
        "provider_status",
    }
    assert identity.attributes == {
        "display_name": "Alice",
        "provider_status": None,
    }
    assert identity.attributes is not source


@pytest.mark.parametrize(
    "attributes",
    [
        {"open_id": "must-not-be-copied"},
        {"email": "alice@example.test"},
        {"display_name": 42},
        ["not", "an", "object"],
    ],
)
def test_external_identity_attributes_reject_provider_ids_pii_and_wrong_types(
    attributes: object,
) -> None:
    with pytest.raises(ValueError, match=r"external identity attributes|unsupported"):
        ExternalIdentity(attributes=attributes)


def test_identity_event_receipt_has_no_raw_event_body_column() -> None:
    columns = set(IdentityEventReceipt.__table__.columns.keys())

    assert {
        "event_id",
        "event_hash",
        "processing_state",
        "error_code",
        "external_identity_id",
    } <= columns
    assert not columns.intersection(
        {
            "body",
            "event_body",
            "payload",
            "raw_body",
            "raw_event",
        }
    )


def test_external_identity_attributes_use_postgresql_jsonb_with_database_checks() -> None:
    table = ExternalIdentity.__table__
    constraint_names = {constraint.name for constraint in table.constraints}

    assert isinstance(table.c.attributes.type, JSONB)
    assert table.c.attributes.nullable is False
    assert table.c.attributes.server_default is not None
    assert {
        "ck_external_identities_attributes_object",
        "ck_external_identities_attributes_keys",
        "ck_external_identities_attributes_values",
    } <= constraint_names

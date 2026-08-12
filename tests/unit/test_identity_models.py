"""Canonical enterprise identity model safety boundaries."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects.postgresql import JSONB

from api.db.db_models import (
    EXTERNAL_IDENTITY_ATTRIBUTE_KEYS,
    EnterpriseSubjectLink,
    ExternalIdentity,
    ExternalIdentityAlias,
    IdentityBindingEvent,
    IdentityEventReceipt,
    IdentityLinkCode,
    IdentityProviderAccount,
    IdentityProviderChannelLink,
    IdentityProviderTenant,
    IdentityTenantPolicy,
    UserTenant,
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


def test_provisioning_models_expose_only_safe_projections() -> None:
    now = _verified_at()
    policy = IdentityTenantPolicy(
        id="tenant-secret",
        tenant_id="tenant-secret",
        mode="link_only",
        revision=7,
        link_code_ttl_seconds=600,
        changed_at=now,
    )
    link_code = IdentityLinkCode(
        id="code-id-secret",
        tenant_id="tenant-secret",
        provider="feishu",
        provider_tenant_key="provider-tenant-secret",
        provider_account_key="app-id-secret",
        target_user_id="user-id-secret",
        digest_key_id="link-hmac-v1",
        code_digest="a" * 64,
        policy_revision=7,
        provider_account_revision=3,
        provider_account_last_scope_change_at=now,
        state="pending",
        issued_at=now,
        expires_at=datetime(2026, 8, 12, 0, 10, tzinfo=UTC),
    )
    event = IdentityBindingEvent(
        id="event-id-secret",
        tenant_id="tenant-secret",
        provider="feishu",
        provider_tenant_key="provider-tenant-secret",
        provider_account_key="app-id-secret",
        external_identity_id="identity-id-secret",
        target_user_id="user-id-secret",
        actor_user_id="user-id-secret",
        link_code_id="code-id-secret",
        binding_method="link_code",
        previous_account_kind="local",
        result_account_kind="hybrid",
        policy_revision=7,
        provider_verified_at=now,
        occurred_at=now,
        request_digest_key_id="event-hmac-v1",
        request_digest="b" * 64,
    )

    policy_projection = policy.to_dict()
    code_projection = link_code.to_dict()
    event_projection = event.to_dict()

    assert policy_projection == {
        "mode": "link_only",
        "revision": 7,
        "link_code_ttl_seconds": 600,
        "changed_at": now,
    }
    assert {
        "id",
        "tenant_id",
        "provider_tenant_key",
        "provider_account_key",
        "target_user_id",
        "digest_key_id",
        "code_digest",
        "provider_account_last_scope_change_at",
    }.isdisjoint(code_projection)
    assert {
        "id",
        "tenant_id",
        "provider_tenant_key",
        "provider_account_key",
        "external_identity_id",
        "target_user_id",
        "actor_user_id",
        "link_code_id",
        "request_digest_key_id",
        "request_digest",
    }.isdisjoint(event_projection)
    assert not {
        "tenant-secret",
        "provider-tenant-secret",
        "app-id-secret",
        "user-id-secret",
        "identity-id-secret",
        "code-id-secret",
        "a" * 64,
        "b" * 64,
    }.intersection(
        {
            *policy_projection.values(),
            *code_projection.values(),
            *event_projection.values(),
        }
    )


def test_provisioning_model_constraints_are_fail_closed() -> None:
    policy_constraints = {constraint.name for constraint in IdentityTenantPolicy.__table__.constraints}
    code_constraints = {constraint.name for constraint in IdentityLinkCode.__table__.constraints}
    event_constraints = {constraint.name for constraint in IdentityBindingEvent.__table__.constraints}
    identity_constraints = {constraint.name for constraint in ExternalIdentity.__table__.constraints}
    membership_indexes = {index.name: index for index in UserTenant.__table__.indexes}
    code_indexes = {index.name: index for index in IdentityLinkCode.__table__.indexes}

    assert {
        "ck_identity_tenant_policies_identity",
        "ck_identity_tenant_policies_mode",
        "ck_identity_tenant_policies_revision",
        "ck_identity_tenant_policies_link_code_ttl",
        "uq_identity_tenant_policies_tenant",
    } <= policy_constraints
    assert {
        "ck_identity_link_codes_hash",
        "ck_identity_link_codes_lifetime",
        "ck_identity_link_codes_state_fields",
        "ck_identity_link_codes_account_revision",
        "uq_identity_link_codes_digest",
        "uq_identity_link_codes_event_scope",
    } <= code_constraints
    assert {
        "ck_identity_binding_events_method",
        "ck_identity_binding_events_method_shape",
        "ck_identity_binding_events_hash",
        "uq_identity_binding_events_identity",
        "uq_identity_binding_events_link_code",
        "uq_identity_binding_events_request",
    } <= event_constraints
    assert "uq_external_identities_tenant_user_provider_subject_type" in identity_constraints
    assert membership_indexes["uq_user_tenants_active_tenant_user"].unique is True
    assert membership_indexes["uq_user_tenants_active_tenant_user"].dialect_options["postgresql"]["where"].text == "status = '1'"
    assert code_indexes["uq_identity_link_codes_pending_target_account"].unique is True
    assert code_indexes["uq_identity_link_codes_pending_target_account"].dialect_options["postgresql"]["where"].text == "state = 'pending'"


def test_provisioning_schema_stores_only_digests_not_raw_codes_or_payloads() -> None:
    code_columns = set(IdentityLinkCode.__table__.columns.keys())
    event_columns = set(IdentityBindingEvent.__table__.columns.keys())

    assert {"digest_key_id", "code_digest"} <= code_columns
    assert {"request_digest_key_id", "request_digest"} <= event_columns
    assert not code_columns.intersection({"code", "raw_code", "link_code", "token", "secret", "payload"})
    assert not event_columns.intersection({"raw_request", "request_body", "payload", "provider_payload"})

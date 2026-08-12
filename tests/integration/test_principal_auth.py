"""Real PostgreSQL evidence tests for the EIM-P1 legacy owner adapter."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from api.db import UserAccountKind, UserTenantRole
from api.db.db_models import Tenant, User, UserTenant
from api.identity.legacy_owner import principal_from_legacy_owner_context
from api.identity.principal import AuthenticationSource
from common.constants import StatusEnum

pytestmark = pytest.mark.integration


def _tenant(tenant_id: str, name: str) -> Tenant:
    return Tenant(
        id=tenant_id,
        name=name,
        llm_id="test-llm",
        embd_id="test-embedding",
        asr_id="test-asr",
        img2txt_id="test-image",
        parser_ids="naive",
        status=StatusEnum.VALID.value,
    )


def _membership(user_id: str, tenant_id: str) -> UserTenant:
    return UserTenant(
        id=uuid.uuid4().hex,
        user_id=user_id,
        tenant_id=tenant_id,
        role=UserTenantRole.OWNER.value,
        invited_by=user_id,
        status=StatusEnum.VALID.value,
    )


def test_legacy_owner_adapter_uses_one_live_personal_membership(
    bootstrapped_engine: Engine,
) -> None:
    user_id = uuid.uuid4().hex
    other_tenant_id = uuid.uuid4().hex
    validated_at = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)

    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        user = User(
            id=user_id,
            nickname="External user",
            email=None,
            password=None,
            account_kind=UserAccountKind.EXTERNAL.value,
            login_channel="feishu",
            is_authenticated=True,
            is_active=True,
            is_anonymous=False,
            status=StatusEnum.VALID.value,
        )
        session.add_all(
            [
                user,
                _tenant(user_id, "Personal owner tenant"),
                _tenant(other_tenant_id, "Another owned tenant"),
            ]
        )
        session.flush()
        session.add_all(
            [
                _membership(user_id, user_id),
                _membership(user_id, other_tenant_id),
            ]
        )
        session.flush()

        principal = principal_from_legacy_owner_context(
            session,
            user,
            AuthenticationSource.SDK_API_TOKEN,
            validated_at=validated_at,
        )

        assert principal is not None
        assert principal.id == user_id
        assert principal.tenant_id == user_id
        assert principal.authentication.validated_at == validated_at
        assert principal.authentication.authenticated_at is None

        # A duplicate live personal-owner membership is ambiguous and must not
        # be reduced with first()/last-write-wins semantics.
        session.add(_membership(user_id, user_id))
        session.flush()
        assert (
            principal_from_legacy_owner_context(
                session,
                user,
                AuthenticationSource.SDK_API_TOKEN,
                validated_at=validated_at,
            )
            is None
        )

        user.is_active = False
        assert (
            principal_from_legacy_owner_context(
                session,
                user,
                AuthenticationSource.SDK_API_TOKEN,
                validated_at=validated_at,
            )
            is None
        )
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()

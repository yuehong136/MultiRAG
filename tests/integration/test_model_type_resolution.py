"""Verify permanent model IDs, capability masks and tenant scoping in PostgreSQL."""

from uuid import uuid4

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from api.db.joint_services.tenant_model_service import resolve_model_type
from tests.support.registered_model import register_model

pytestmark = pytest.mark.integration


def test_same_name_across_tenants_and_factories(bootstrapped_engine: Engine) -> None:
    owner, other = uuid4().hex, uuid4().hex
    with Session(bootstrapped_engine) as db:
        first = register_model(db, tenant_id=owner, provider_name="TextFixture", model_name="same-model")
        second = register_model(db, tenant_id=other, provider_name="TextFixture", model_name="same-model", model_types=("embedding",))
        third = register_model(db, tenant_id=owner, provider_name="VisionFixture", model_name="same-model", model_types=("chat", "image2text"))
        identifiers = first.id, second.id, third.id
        db.commit()

    with Session(bootstrapped_engine) as db:
        assert resolve_model_type(db, owner, identifiers[0]) == ["chat"]
        assert resolve_model_type(db, other, identifiers[1]) == ["embedding"]
        assert resolve_model_type(db, owner, identifiers[2]) == ["chat", "image2text"]
        with pytest.raises(LookupError, match="not authorized"):
            resolve_model_type(db, owner, identifiers[1])
        for alias in ("same-model", "same-model@TextFixture", "42"):
            with pytest.raises(LookupError, match="Invalid model ID"):
                resolve_model_type(db, owner, alias)
        assert not db.new and not db.dirty and not db.deleted


def test_inactive_registration_is_rejected(bootstrapped_engine: Engine) -> None:
    owner = uuid4().hex
    with Session(bootstrapped_engine) as db:
        identifier = register_model(db, tenant_id=owner, provider_name="Fixture", model_name="disabled-model", status="inactive").id
        db.commit()
    with Session(bootstrapped_engine) as db, pytest.raises(LookupError, match="unavailable"):
        resolve_model_type(db, owner, identifier)

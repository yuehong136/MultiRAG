from sqlalchemy import String

from api.db.db_models import Base, TenantModel


def test_tenant_model_uses_independent_string_identity_primary_key() -> None:
    table = TenantModel.__table__
    pk_columns = [column.name for column in table.primary_key.columns]

    assert pk_columns == ["id"]
    assert isinstance(table.c.id.type, String)
    assert table.c.id.type.length == 32
    assert not table.c.id.foreign_keys
    assert "usr_ai.t_ai_tenant_llms" not in Base.metadata.tables
    model_id = table.c.id.default.arg(None)
    assert len(model_id) == 32 and int(model_id, 16) >= 0


def test_model_provider_and_instance_links_keep_instance_local_name_uniqueness() -> None:
    table = TenantModel.__table__
    unique = next(constraint for constraint in table.constraints if constraint.name == "uq_instance_model_name")

    assert list(unique.columns.keys()) == ["instance_id", "model_name"]
    assert {fk.target_fullname for fk in table.foreign_keys} == {
        "usr_ai.t_ai_tenant_model_providers.id",
        "usr_ai.t_ai_tenant_model_instances.id",
    }

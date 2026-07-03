from sqlalchemy import BigInteger

from api.db.db_models import TenantLLM


def test_tenant_llm_uses_integer_identity_primary_key() -> None:
    pk_columns = [column.name for column in TenantLLM.__table__.primary_key.columns]

    assert pk_columns == ["id"]
    assert isinstance(TenantLLM.__table__.c.id.type, BigInteger)
    assert TenantLLM.__table__.c.tenant_id.primary_key is False
    assert TenantLLM.__table__.c.llm_factory.primary_key is False
    assert TenantLLM.__table__.c.llm_name.primary_key is False


def test_tenant_llm_keeps_unique_constraint_on_business_key() -> None:
    unique_constraint_names = {constraint.name for constraint in TenantLLM.__table__.constraints if getattr(constraint, "name", None)}

    assert "idx_tenant_llm_unique" in unique_constraint_names

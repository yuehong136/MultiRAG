"""Say why a model is not healthy, and let "rate limited" be a state of its own.

Health results gain ``failure_class`` (rate limit, credential, quota, model
gone, transient, timeout, unreachable) and ``source`` (a probe, or real
traffic), and ``status`` may be ``rate_limited`` besides healthy and
unhealthy.

Each step is skipped when a table created from the current models already
has it, so both bootstrap paths end in the same schema.

Revision ID: 3d5f7b9c1e2a
Revises: 2c4e6a8b0d1f
"""

import sqlalchemy as sa
from alembic import op

revision: str = "3d5f7b9c1e2a"
down_revision: str | None = "2c4e6a8b0d1f"
branch_labels: str | None = None
depends_on: str | None = None
SCHEMA = "usr_ai"
_HEALTH = "t_ai_gateway_model_health"
_STATUS = "ck_gateway_model_health_status"
_THREE = "status IN ('healthy', 'rate_limited', 'unhealthy')"
_TWO = "status IN ('healthy', 'unhealthy')"


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_HEALTH, schema=SCHEMA)}


def _status_rule() -> str:
    for constraint in sa.inspect(op.get_bind()).get_check_constraints(_HEALTH, schema=SCHEMA):
        if constraint["name"] == _STATUS:
            return str(constraint["sqltext"])
    return ""


def _set_status_rule(rule: str) -> None:
    if _status_rule():
        op.drop_constraint(_STATUS, _HEALTH, schema=SCHEMA, type_="check")
    op.create_check_constraint(_STATUS, _HEALTH, rule, schema=SCHEMA)


def upgrade() -> None:
    present = _columns()
    if "failure_class" not in present:
        op.add_column(_HEALTH, sa.Column("failure_class", sa.String(32), nullable=True), schema=SCHEMA)
    if "source" not in present:
        op.add_column(_HEALTH, sa.Column("source", sa.String(16), nullable=False, server_default="probe"), schema=SCHEMA)
    if "rate_limited" not in _status_rule():
        _set_status_rule(_THREE)


def downgrade() -> None:
    # The older rule has no word for a rate-limited model; those results are simply dropped.
    op.execute(sa.text(f"DELETE FROM {SCHEMA}.{_HEALTH} WHERE status = 'rate_limited'"))
    if "rate_limited" in _status_rule():
        _set_status_rule(_TWO)
    present = _columns()
    for name in ("source", "failure_class"):
        if name in present:
            op.drop_column(_HEALTH, name, schema=SCHEMA)

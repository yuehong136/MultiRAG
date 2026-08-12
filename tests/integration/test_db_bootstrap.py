"""数据库引导路径与 alembic 迁移链结构的真库验证。

本仓库的建库语义（api/db/db_models.py）是双轨制：
- 全新环境：``init_database_tables()`` 按 db_models 建全部表 + stamp head
  （跳过历史迁移）；
- 存量环境：``alembic upgrade head``（历史迁移只含针对老库的列补丁，
  不自举空库——根迁移即 add_column）。

因此这里验证"fresh-install 引导 + 迁移链结构完整性"，而非空库全量上行。
"""

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.db import UserAccountKind
from api.db.db_models import Base, User
from api.db.services.user_service import UserService

_USER_ACCOUNT_REVISION = "7c8d9e0f1a2b"
_PRE_USER_ACCOUNT_REVISION = "e4f6a8b0c2d4"


def test_migration_chain_is_linear_and_loadable(alembic_cfg):
    """迁移脚本链可全量加载、无分叉（单 head）、base→head 链路无断裂。"""
    script = ScriptDirectory.from_config(alembic_cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"迁移链出现分叉 heads: {heads}"

    revisions = list(script.walk_revisions("base", "heads"))
    assert revisions, "迁移目录为空"
    assert revisions[0].revision == heads[0]
    assert revisions[-1].down_revision is None, "链尾不是根迁移（down_revision=None）"


def test_fresh_install_bootstrap_creates_all_model_tables(bootstrapped_engine):
    """fresh-install 引导后，db_models 声明的全部 usr_ai 表真实存在。"""
    inspector = sa.inspect(bootstrapped_engine)
    actual = set(inspector.get_table_names(schema="usr_ai"))
    expected = {table.name for table in Base.metadata.tables.values() if table.schema == "usr_ai"}
    assert expected, "db_models 未声明任何 usr_ai 表？"
    missing = expected - actual
    assert not missing, f"fresh-install 引导后缺表: {sorted(missing)}"


def test_fresh_install_is_stamped_to_head(bootstrapped_engine, alembic_cfg):
    """fresh-install 引导后 alembic_version 已 stamp 到代码 head（镜像生产语义）。"""
    head = ScriptDirectory.from_config(alembic_cfg).get_current_head()
    with bootstrapped_engine.connect() as conn:
        version = conn.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one()
    assert version == head


def test_model_first_existing_database_can_upgrade_candidate_revision(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    """Model-first startup accepts the exact sidecar instead of recreating it."""

    head = ScriptDirectory.from_config(alembic_cfg).get_current_head()
    cfg = Config(alembic_cfg.config_file_name)
    cfg.set_main_option(
        "script_location",
        alembic_cfg.get_main_option("script_location"),
    )
    with bootstrapped_engine.begin() as connection:
        connection.execute(sa.text("UPDATE usr_ai.alembic_version SET version_num = 'c1dbaa153d0a'"))
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
        version = connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one()

    assert version == head


def test_fresh_install_supports_multiple_external_users_without_email(
    bootstrapped_engine: sa.Engine,
) -> None:
    """PostgreSQL keeps non-null email unique while allowing external-only users."""

    inspector = sa.inspect(bootstrapped_engine)
    columns = {column["name"]: column for column in inspector.get_columns("t_ai_users", schema="usr_ai")}
    assert columns["email"]["nullable"] is True
    assert columns["account_kind"]["nullable"] is False
    assert columns["account_kind"]["type"].length == 16

    check_names = {constraint["name"] for constraint in inspector.get_check_constraints("t_ai_users", schema="usr_ai")}
    assert {
        "ck_users_account_kind",
        "ck_users_external_password_null",
        "ck_users_password_account_email",
    } <= check_names

    def _external_values(user_id: str) -> dict[str, object]:
        return {
            "id": user_id,
            "nickname": "External User",
            "email": None,
            "password": None,
            "account_kind": UserAccountKind.EXTERNAL.value,
            "is_authenticated": True,
            "is_active": True,
            "is_anonymous": False,
        }

    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(sa.insert(User.__table__), [_external_values("external-null-1"), _external_values("external-null-2")])
        connection.execute(
            sa.insert(User.__table__),
            [
                {
                    **_external_values("external-with-email"),
                    "email": "external@example.test",
                },
                {
                    **_external_values("legacy-local-no-password"),
                    "email": "local@example.test",
                    "account_kind": UserAccountKind.LOCAL.value,
                },
            ],
        )
        count = connection.execute(sa.select(sa.func.count()).select_from(User).where(User.email.is_(None))).scalar_one()
        assert count >= 2

        with Session(bind=connection) as session:
            assert UserService.query_password_user_by_email(session, "external@example.test") is None
            local_user = UserService.query_password_user_by_email(session, "local@example.test")
            assert local_user is not None
            assert local_user.password is None

        invalid_external = _external_values("external-with-password")
        invalid_external["password"] = "must-not-be-stored"
        savepoint = connection.begin_nested()
        try:
            with pytest.raises(IntegrityError):
                connection.execute(sa.insert(User.__table__).values(**invalid_external))
        finally:
            savepoint.rollback()

        duplicate_email = _external_values("duplicate-non-null-email")
        duplicate_email["email"] = "external@example.test"
        savepoint = connection.begin_nested()
        try:
            with pytest.raises(IntegrityError):
                connection.execute(sa.insert(User.__table__).values(**duplicate_email))
        finally:
            savepoint.rollback()
    finally:
        transaction.rollback()
        connection.close()


def test_existing_user_migration_backfills_local_and_round_trips(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    """The stored-data path is deterministic and reversible before external JIT."""

    cfg = Config(alembic_cfg.config_file_name)
    cfg.set_main_option("script_location", alembic_cfg.get_main_option("script_location"))
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(
            sa.insert(User.__table__).values(
                id="legacy-user",
                nickname="Legacy User",
                email="legacy@example.test",
                password=None,
                account_kind=UserAccountKind.LOCAL.value,
                is_authenticated=True,
                is_active=True,
                is_anonymous=False,
            )
        )
        connection.execute(sa.text("DROP INDEX usr_ai.ix_usr_ai_t_ai_users_account_kind"))
        for constraint in (
            "ck_users_password_account_email",
            "ck_users_external_password_null",
            "ck_users_account_kind",
        ):
            connection.execute(sa.text(f"ALTER TABLE usr_ai.t_ai_users DROP CONSTRAINT {constraint}"))
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_users DROP COLUMN account_kind"))
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_users ALTER COLUMN email SET NOT NULL"))
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _PRE_USER_ACCOUNT_REVISION},
        )
        cfg.attributes["connection"] = connection

        command.upgrade(cfg, _USER_ACCOUNT_REVISION)

        assert connection.execute(sa.text("SELECT account_kind FROM usr_ai.t_ai_users WHERE id = 'legacy-user'")).scalar_one() == UserAccountKind.LOCAL.value
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _USER_ACCOUNT_REVISION

        command.downgrade(cfg, _PRE_USER_ACCOUNT_REVISION)

        downgraded_columns = {column["name"]: column for column in sa.inspect(connection).get_columns("t_ai_users", schema="usr_ai")}
        assert "account_kind" not in downgraded_columns
        assert downgraded_columns["email"]["nullable"] is False

        command.upgrade(cfg, _USER_ACCOUNT_REVISION)
        assert connection.execute(sa.text("SELECT account_kind FROM usr_ai.t_ai_users WHERE id = 'legacy-user'")).scalar_one() == UserAccountKind.LOCAL.value
    finally:
        transaction.rollback()
        connection.close()


def test_existing_oauth_users_migrate_to_external_or_hybrid(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    """Legacy OAuth rows keep their non-password authentication classification."""

    cfg = Config(alembic_cfg.config_file_name)
    cfg.set_main_option("script_location", alembic_cfg.get_main_option("script_location"))
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(
            sa.insert(User.__table__),
            [
                {
                    "id": "legacy-oauth-only",
                    "nickname": "OAuth Only",
                    "email": "oauth-only@example.test",
                    "password": None,
                    "account_kind": UserAccountKind.LOCAL.value,
                    "login_channel": "github",
                    "is_authenticated": True,
                    "is_active": True,
                    "is_anonymous": False,
                },
                {
                    "id": "legacy-oauth-password",
                    "nickname": "OAuth And Password",
                    "email": "oauth-password@example.test",
                    "password": "legacy-hash",
                    "account_kind": UserAccountKind.LOCAL.value,
                    "login_channel": "oidc",
                    "is_authenticated": True,
                    "is_active": True,
                    "is_anonymous": False,
                },
            ],
        )
        connection.execute(sa.text("DROP INDEX usr_ai.ix_usr_ai_t_ai_users_account_kind"))
        for constraint in (
            "ck_users_password_account_email",
            "ck_users_external_password_null",
            "ck_users_account_kind",
        ):
            connection.execute(sa.text(f"ALTER TABLE usr_ai.t_ai_users DROP CONSTRAINT {constraint}"))
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_users DROP COLUMN account_kind"))
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_users ALTER COLUMN email SET NOT NULL"))
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _PRE_USER_ACCOUNT_REVISION},
        )
        cfg.attributes["connection"] = connection

        command.upgrade(cfg, _USER_ACCOUNT_REVISION)

        classified = dict(connection.execute(sa.text("SELECT id, account_kind FROM usr_ai.t_ai_users WHERE id IN ('legacy-oauth-only', 'legacy-oauth-password')")).all())
        assert classified == {
            "legacy-oauth-only": UserAccountKind.EXTERNAL.value,
            "legacy-oauth-password": UserAccountKind.HYBRID.value,
        }

        command.downgrade(cfg, _PRE_USER_ACCOUNT_REVISION)
        downgraded = connection.execute(
            sa.text(
                """
                SELECT id, login_channel, password
                FROM usr_ai.t_ai_users
                WHERE id IN ('legacy-oauth-only', 'legacy-oauth-password')
                ORDER BY id
                """
            )
        ).all()
        assert downgraded == [
            ("legacy-oauth-only", "github", None),
            ("legacy-oauth-password", "oidc", "legacy-hash"),
        ]

        command.upgrade(cfg, _USER_ACCOUNT_REVISION)
        reclassified = dict(connection.execute(sa.text("SELECT id, account_kind FROM usr_ai.t_ai_users WHERE id IN ('legacy-oauth-only', 'legacy-oauth-password')")).all())
        assert reclassified == classified
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.parametrize(
    ("account_kind", "email", "login_channel", "password", "message"),
    [
        (UserAccountKind.EXTERNAL.value, None, "feishu", None, r"users without email"),
        (
            UserAccountKind.EXTERNAL.value,
            "external-downgrade@example.test",
            None,
            None,
            r"account kinds that legacy columns cannot reconstruct",
        ),
        (
            UserAccountKind.HYBRID.value,
            "hybrid-downgrade@example.test",
            None,
            None,
            r"account kinds that legacy columns cannot reconstruct",
        ),
    ],
)
def test_user_migration_refuses_downgrade_that_would_destroy_external_identity(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
    account_kind: str,
    email: str | None,
    login_channel: str | None,
    password: str | None,
    message: str,
) -> None:
    """Downgrade must not invent an email or silently discard an external user."""

    cfg = Config(alembic_cfg.config_file_name)
    cfg.set_main_option("script_location", alembic_cfg.get_main_option("script_location"))
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(
            sa.insert(User.__table__).values(
                id="external-downgrade-guard",
                nickname="External User",
                email=email,
                password=password,
                account_kind=account_kind,
                login_channel=login_channel,
                is_authenticated=True,
                is_active=True,
                is_anonymous=False,
            )
        )
        cfg.attributes["connection"] = connection

        with pytest.raises(RuntimeError, match=message):
            command.downgrade(cfg, _PRE_USER_ACCOUNT_REVISION)

        columns = {column["name"] for column in sa.inspect(connection).get_columns("t_ai_users", schema="usr_ai")}
        assert "account_kind" in columns
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _USER_ACCOUNT_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_sync_cursor_columns_are_native_timestamptz(bootstrapped_engine):
    """Sync cursors must retain timezone semantics in PostgreSQL itself."""
    columns = {column["name"]: column for column in sa.inspect(bootstrapped_engine).get_columns("t_ai_sync_logs", schema="usr_ai")}
    for column_name in ("time_started", "poll_range_start", "poll_range_end"):
        column_type = columns[column_name]["type"]
        assert isinstance(column_type, sa.DateTime)
        assert column_type.timezone is True

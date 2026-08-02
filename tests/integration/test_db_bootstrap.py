"""数据库引导路径与 alembic 迁移链结构的真库验证。

本仓库的建库语义（api/db/db_models.py）是双轨制：
- 全新环境：``init_database_tables()`` 按 db_models 建全部表 + stamp head
  （跳过历史迁移）；
- 存量环境：``alembic upgrade head``（历史迁移只含针对老库的列补丁，
  不自举空库——根迁移即 add_column）。

因此这里验证"fresh-install 引导 + 迁移链结构完整性"，而非空库全量上行。
"""

import sqlalchemy as sa
from alembic.script import ScriptDirectory

from api.db.db_models import Base


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


def test_sync_cursor_columns_are_native_timestamptz(bootstrapped_engine):
    """Sync cursors must retain timezone semantics in PostgreSQL itself."""
    columns = {column["name"]: column for column in sa.inspect(bootstrapped_engine).get_columns("t_ai_sync_logs", schema="usr_ai")}
    for column_name in ("time_started", "poll_range_start", "poll_range_end"):
        column_type = columns[column_name]["type"]
        assert isinstance(column_type, sa.DateTime)
        assert column_type.timezone is True

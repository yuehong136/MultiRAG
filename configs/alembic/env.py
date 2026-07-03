import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# 导入所有模型以便自动检测变化
from alembic import context

from api.db.db_models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


# 设置schema
def include_object(object, name, type_, reflected, compare_to):
    """检查对象是否应该被包含在迁移中"""
    if type_ == "table":
        # 仅包含usr_ai模式中的表
        return hasattr(object, "schema") and (object.schema == "usr_ai" or object.schema is None)
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,  # 包含schema
        include_object=include_object,  # 过滤对象
        version_table_schema="usr_ai",  # 将版本表放在usr_ai模式下
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    # Prefer an externally managed SQLAlchemy connection when provided by caller.
    # This avoids ConfigParser URL interpolation edge-cases for credentials containing `%`.
    connection = config.attributes.get("connection", None)
    if connection is not None:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,  # 包含schema
            include_object=include_object,  # 过滤对象
            version_table_schema="usr_ai",  # 将版本表放在usr_ai模式下
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,  # 包含schema
            include_object=include_object,  # 过滤对象
            version_table_schema="usr_ai",  # 将版本表放在usr_ai模式下
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

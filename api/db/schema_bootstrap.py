"""Database schema bootstrap orchestration for API process startup.

Fresh databases are created from the current ORM metadata and then stamped at
the Alembic head.  Existing databases must take the opposite order: migrations
first, model-first creation second.  Otherwise a newly added table can refer to
constraints that only a pending migration installs on an existing parent.
"""

from sqlalchemy import inspect as sa_inspect

from api.db.db_models import engine, init_database_tables, upgrade_database_tables

_SCHEMA = "usr_ai"


def bootstrap_database_schema() -> None:
    """Bring a fresh or stored database to the current schema safely."""

    is_fresh_install = not sa_inspect(engine).get_table_names(schema=_SCHEMA)
    if is_fresh_install:
        init_database_tables()
        upgrade_database_tables(is_fresh_install=True)
        return

    # Stored schemas may be missing parent constraints required by tables in
    # current ORM metadata.  Run the authoritative migration chain before any
    # model-first creation attempts to reference those parents.
    upgrade_database_tables(is_fresh_install=False)
    init_database_tables()

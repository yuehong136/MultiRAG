"""
Tests for OceanBase SQLAlchemy support as primary database.
"""

import pytest
from sqlalchemy.orm import Session


class TestOceanBaseBuildDatabaseUrl:
    """Test build_database_url maps oceanbase to MySQL-compatible driver."""

    def test_oceanbase_maps_to_mysql_driver(self):
        from api.db.db_models import build_database_url

        db_config = {
            "name": "oceanbase",
            "dbname": "multirag",
            "user": "root@multirag",
            "password": "password",
            "host": "127.0.0.1",
            "port": 2881,
        }
        url = build_database_url(db_config)
        assert url.startswith("mysql+pymysql://"), f"Expected mysql+pymysql driver, got: {url}"

    def test_oceanbase_url_contains_host_and_port(self):
        from api.db.db_models import build_database_url

        db_config = {
            "name": "oceanbase",
            "dbname": "multirag",
            "user": "root@multirag",
            "password": "secret",
            "host": "127.0.0.1",
            "port": 2881,
        }
        url = build_database_url(db_config)
        assert "127.0.0.1" in url
        assert "2881" in url
        assert "multirag" in url

    def test_mysql_url_unchanged(self):
        from api.db.db_models import build_database_url

        db_config = {
            "name": "mysql+pymysql",
            "dbname": "mydb",
            "user": "root",
            "password": "pass",
            "host": "localhost",
            "port": 3306,
        }
        url = build_database_url(db_config)
        assert url.startswith("mysql+pymysql://")

    def test_postgresql_maps_to_psycopg3_driver(self):
        from api.db.db_models import build_database_url

        db_config = {
            "name": "postgresql",
            "dbname": "mydb",
            "user": "usr",
            "password": "pass",
            "host": "localhost",
            "port": 5432,
        }
        url = build_database_url(db_config)
        assert url.startswith("postgresql+psycopg://")


class TestOceanBaseEngineConfig:
    """Test get_engine_config applies MySQL-style settings for OceanBase."""

    def test_oceanbase_uses_mysql_connect_args(self):
        from api.db.db_models import get_engine_config

        db_config = {"name": "oceanbase"}
        config = get_engine_config(db_config)
        assert "connect_args" in config
        assert config["connect_args"].get("charset") == "utf8mb4"
        assert "connect_timeout" in config["connect_args"]

    def test_mysql_still_has_connect_args(self):
        from api.db.db_models import get_engine_config

        db_config = {"name": "mysql"}
        config = get_engine_config(db_config)
        assert "connect_args" in config
        assert config["connect_args"].get("charset") == "utf8mb4"

    def test_postgresql_has_different_connect_args(self):
        from api.db.db_models import get_engine_config

        db_config = {"name": "postgresql"}
        config = get_engine_config(db_config)
        assert "connect_args" in config
        assert "options" in config["connect_args"]
        assert "charset" not in config["connect_args"]


class TestOceanBaseDatabaseLock:
    """Test DatabaseLock.create routes oceanbase to MySQLDatabaseLock."""

    def test_oceanbase_returns_mysql_lock(self):
        from api.db.db_models import DatabaseLock, MySQLDatabaseLock

        lock = DatabaseLock.create(Session(), "test_lock", db_type="oceanbase")
        assert isinstance(lock, MySQLDatabaseLock)

    def test_mysql_returns_mysql_lock(self):
        from api.db.db_models import DatabaseLock, MySQLDatabaseLock

        lock = DatabaseLock.create(Session(), "test_lock", db_type="mysql")
        assert isinstance(lock, MySQLDatabaseLock)

    def test_postgresql_returns_postgresql_lock(self):
        from api.db.db_models import DatabaseLock, PostgreSQLDatabaseLock

        lock = DatabaseLock.create(Session(), "test_lock", db_type="postgresql")
        assert isinstance(lock, PostgreSQLDatabaseLock)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

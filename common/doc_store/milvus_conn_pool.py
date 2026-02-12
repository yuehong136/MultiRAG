#
#  Copyright 2025 The MultiRAG Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import logging
import hashlib
import re
import threading
import time

from pymilvus.orm import utility
from pymilvus.orm.connections import connections

from common import settings
from common.decorator import singleton

ATTEMPT_TIME = 2


@singleton
class MilvusConnectionPool:

    def __init__(self):
        if hasattr(settings, "MILVUS"):
            self.MILVUS_CONFIG = settings.MILVUS
        else:
            self.MILVUS_CONFIG = settings.get_base_config("milvus", {
                "hosts": "http://localhost:19530",
                "db_name": "default"
            })

        uri = self.MILVUS_CONFIG.get("hosts", "http://localhost:19530")
        user = self.MILVUS_CONFIG.get("username", "")
        password = self.MILVUS_CONFIG.get("password", "")
        db_name = self.MILVUS_CONFIG.get("db_name", "")
        token = self.MILVUS_CONFIG.get("token", "")
        timeout = self.MILVUS_CONFIG.get("timeout", None)
        kwargs = self.MILVUS_CONFIG.get("kwargs", {})

        self._using = None
        self._uri = uri
        self._conn_lock = threading.RLock()
        self._db_aliases: dict[str, str] = {}
        self._default_alias = f"milvus_pool_{id(self)}"

        for _ in range(ATTEMPT_TIME):
            try:
                self._using = self._create_connection(
                    uri, user, password, db_name, token, timeout=timeout, alias=self._default_alias, **kwargs
                )
                version = utility.get_server_version(using=self._using)
                self.is_self_hosted = bool(utility.get_server_type(using=self._using) == "milvus")
                logging.info(f"Milvus {uri} connected, server version: {version}")
                break
            except Exception as e:
                logging.warning(f"{str(e)}. Waiting Milvus {uri} to be healthy.")
                time.sleep(5)

        if self._using is None:
            msg = f"Milvus {uri} is unhealthy in {ATTEMPT_TIME * 5}s."
            logging.error(msg)
            raise Exception(msg)

        # Verify connection health
        try:
            version = utility.get_server_version(using=self._using)
            if not version:
                raise Exception("Failed to get server version")
            logging.info(f"Milvus {uri} is healthy.")
        except Exception as e:
            msg = f"Milvus {uri} health check failed: {str(e)}"
            logging.error(msg)
            raise Exception(msg)

    def _create_connection(
        self,
        uri: str,
        user: str = "",
        password: str = "",
        db_name: str = "",
        token: str = "",
        timeout: float | None = None,
        alias: str | None = None,
        **kwargs,
    ) -> str:
        """Create a Milvus connection and return the connection alias."""
        alias = alias or kwargs.pop("alias", None) or f"milvus_pool_{id(self)}"

        # Build connection parameters
        connect_kwargs = {
            "alias": alias,
            "uri": uri,
        }

        if user and password:
            connect_kwargs["user"] = user
            connect_kwargs["password"] = password
        elif token:
            connect_kwargs["token"] = token

        if db_name:
            connect_kwargs["db_name"] = db_name

        if timeout is not None:
            connect_kwargs["timeout"] = timeout

        # Add any additional kwargs
        connect_kwargs.update(kwargs)

        # Establish the connection
        connections.connect(**connect_kwargs)

        return alias

    def get_conn(self) -> str:
        """Return the connection alias (using name)."""
        return self._using

    def get_conn_for_db(self, db_name: str | None) -> str:
        """Return connection alias for specified Milvus database without mutating default alias."""
        normalized_db_name = (db_name or "").strip()
        if not normalized_db_name:
            return self._using

        with self._conn_lock:
            alias = self._db_aliases.get(normalized_db_name)
            if alias:
                try:
                    version = utility.get_server_version(using=alias)
                    if version:
                        return alias
                except Exception as e:
                    logging.warning(
                        "Milvus database alias unhealthy, reconnecting. db=%s alias=%s error=%s",
                        normalized_db_name,
                        alias,
                        e,
                    )
                    try:
                        connections.disconnect(alias=alias)
                    except Exception:
                        pass
                    self._db_aliases.pop(normalized_db_name, None)

            uri = self.MILVUS_CONFIG.get("hosts", "http://localhost:19530")
            user = self.MILVUS_CONFIG.get("username", "")
            password = self.MILVUS_CONFIG.get("password", "")
            token = self.MILVUS_CONFIG.get("token", "")
            timeout = self.MILVUS_CONFIG.get("timeout", None)
            kwargs = self.MILVUS_CONFIG.get("kwargs", {})

            db_name_hash = hashlib.md5(normalized_db_name.encode("utf-8")).hexdigest()[:8]
            db_name_clean = re.sub(r"[^a-zA-Z0-9_]", "_", normalized_db_name)
            alias = f"{self._default_alias}_db_{db_name_clean}_{db_name_hash}"
            try:
                alias = self._create_connection(
                    uri=uri,
                    user=user,
                    password=password,
                    db_name=normalized_db_name,
                    token=token,
                    timeout=timeout,
                    alias=alias,
                    **kwargs,
                )
                utility.get_server_version(using=alias)
                self._db_aliases[normalized_db_name] = alias
                return alias
            except Exception as e:
                raise Exception(f"Failed to connect Milvus database '{normalized_db_name}': {str(e)}") from e

    def refresh_conn(self) -> str:
        """Check health and reconnect if needed."""
        with self._conn_lock:
            try:
                version = utility.get_server_version(using=self._using)
                if version:
                    return self._using
            except Exception as e:
                logging.warning(f"Milvus connection unhealthy: {e}, reconnecting...")

            # Try to disconnect and reconnect
            try:
                connections.disconnect(alias=self._using)
            except Exception:
                pass

            for alias in set(self._db_aliases.values()):
                try:
                    connections.disconnect(alias=alias)
                except Exception:
                    pass
            self._db_aliases.clear()

            uri = self.MILVUS_CONFIG.get("hosts", "http://localhost:19530")
            user = self.MILVUS_CONFIG.get("username", "")
            password = self.MILVUS_CONFIG.get("password", "")
            db_name = self.MILVUS_CONFIG.get("db_name", "")
            token = self.MILVUS_CONFIG.get("token", "")
            timeout = self.MILVUS_CONFIG.get("timeout", None)
            kwargs = self.MILVUS_CONFIG.get("kwargs", {})

            self._using = self._create_connection(
                uri, user, password, db_name, token, timeout=timeout, alias=self._default_alias, **kwargs
            )
            return self._using

    def __del__(self):
        aliases = set()
        if hasattr(self, "_using") and self._using:
            aliases.add(self._using)
        if hasattr(self, "_db_aliases") and self._db_aliases:
            aliases.update(self._db_aliases.values())
        for alias in aliases:
            try:
                connections.disconnect(alias=alias)
            except Exception:
                pass


MILVUS_CONN = MilvusConnectionPool()

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
"""
VastBase connection for message storage.
This module provides a specialized VastBase connection for storing and retrieving messages.
"""

import json
import copy
from typing import Any

from common.decorator import singleton
from common.doc_store.doc_store_base import MatchExpr, OrderByExpr
from common.doc_store.vastbase_conn_base import VastBaseConnectionBase


@singleton
class VastBaseConnection(VastBaseConnectionBase):
    """VastBase connection for message storage."""

    def __init__(self):
        super().__init__(logger_name="multirag.memory.vastbase_conn")

    def search(
        self,
        select_fields: list[str],
        highlight_fields: list[str],
        condition: dict,
        match_expressions: list[MatchExpr],
        order_by: OrderByExpr,
        offset: int,
        limit: int,
        index_names: str | list[str],
        knowledgebase_ids: list[str],
        agg_fields: list[str] | None = None,
        rank_feature: dict | None = None,
    ):
        """Search messages in VastBase."""
        if isinstance(index_names, str):
            index_names = [index_names]

        results = []
        total_count = 0

        for index_name in index_names:
            for memory_id in knowledgebase_ids:
                table_name = f"{index_name}_{memory_id}"

                # Build SQL query
                fields_str = ", ".join(select_fields) if select_fields else "*"
                sql = f"SELECT {fields_str} FROM {table_name}"

                # Build WHERE clause
                where_clauses = [f"memory_id = '{memory_id}'"]
                for k, v in condition.items():
                    if isinstance(v, str):
                        where_clauses.append(f"{k} = '{v}'")
                    elif isinstance(v, int):
                        where_clauses.append(f"{k} = {v}")
                    elif isinstance(v, list) and v:
                        if isinstance(v[0], str):
                            vals = ", ".join([f"'{x}'" for x in v])
                        else:
                            vals = ", ".join([str(x) for x in v])
                        where_clauses.append(f"{k} IN ({vals})")

                if where_clauses:
                    sql += " WHERE " + " AND ".join(where_clauses)

                # Add ORDER BY
                if order_by and order_by.fields:
                    order_parts = []
                    for field, direction in order_by.fields:
                        dir_str = "ASC" if direction == 0 else "DESC"
                        order_parts.append(f"{field} {dir_str}")
                    sql += " ORDER BY " + ", ".join(order_parts)

                # Add LIMIT and OFFSET
                sql += f" LIMIT {limit} OFFSET {offset}"

                try:
                    conn = self._get_connection()
                    cursor = conn.cursor()
                    cursor.execute(sql)
                    columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()
                    cursor.close()
                    self._release_connection(conn)

                    for row in rows:
                        doc = dict(zip(columns, row))
                        results.append(doc)
                    total_count += len(rows)
                except Exception as e:
                    self.logger.warning(f"VastBase search error: {e}")

        return {"hits": results, "total": total_count}

    def get(self, chunk_id: str, index_name: str, knowledgebase_ids: list[str]) -> dict | None:
        """Get a message by ID."""
        for memory_id in knowledgebase_ids:
            table_name = f"{index_name}_{memory_id}"
            sql = f"SELECT * FROM {table_name} WHERE id = '{chunk_id}'"

            try:
                conn = self._get_connection()
                cursor = conn.cursor()
                cursor.execute(sql)
                columns = [desc[0] for desc in cursor.description]
                row = cursor.fetchone()
                cursor.close()
                self._release_connection(conn)

                if row:
                    return dict(zip(columns, row))
            except Exception as e:
                self.logger.warning(f"VastBase get error: {e}")

        return None

    def insert(self, rows: list[dict], index_name: str, knowledgebase_id: str) -> list[str]:
        """Insert messages into VastBase."""
        table_name = f"{index_name}_{knowledgebase_id}"

        # Ensure table exists
        if not self.index_exist(index_name, knowledgebase_id):
            self.create_idx(index_name, knowledgebase_id, 768)

        errors = []
        for row in rows:
            doc = copy.deepcopy(row)
            doc["memory_id"] = knowledgebase_id

            columns = ", ".join(doc.keys())
            values = []
            for v in doc.values():
                if isinstance(v, str):
                    values.append(f"'{v}'")
                elif isinstance(v, (list, dict)):
                    values.append(f"'{json.dumps(v)}'")
                elif v is None:
                    values.append("NULL")
                else:
                    values.append(str(v))
            values_str = ", ".join(values)

            sql = f"INSERT INTO {table_name} ({columns}) VALUES ({values_str}) ON CONFLICT (id) DO UPDATE SET "
            update_parts = [f"{k} = EXCLUDED.{k}" for k in doc.keys() if k != "id"]
            sql += ", ".join(update_parts)

            try:
                conn = self._get_connection()
                cursor = conn.cursor()
                cursor.execute(sql)
                conn.commit()
                cursor.close()
                self._release_connection(conn)
            except Exception as e:
                errors.append(f"{doc.get('id')}: {str(e)}")
                self.logger.warning(f"VastBase insert error: {e}")

        return errors

    def update(self, condition: dict, new_value: dict, index_name: str, knowledgebase_id: str) -> bool:
        """Update messages in VastBase."""
        table_name = f"{index_name}_{knowledgebase_id}"

        # Build SET clause
        set_parts = []
        for k, v in new_value.items():
            if isinstance(v, str):
                set_parts.append(f"{k} = '{v}'")
            elif isinstance(v, (list, dict)):
                set_parts.append(f"{k} = '{json.dumps(v)}'")
            elif v is None:
                set_parts.append(f"{k} = NULL")
            else:
                set_parts.append(f"{k} = {v}")

        # Build WHERE clause
        where_parts = [f"memory_id = '{knowledgebase_id}'"]
        for k, v in condition.items():
            if isinstance(v, str):
                where_parts.append(f"{k} = '{v}'")
            elif isinstance(v, int):
                where_parts.append(f"{k} = {v}")

        sql = f"UPDATE {table_name} SET {', '.join(set_parts)} WHERE {' AND '.join(where_parts)}"

        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(sql)
            conn.commit()
            cursor.close()
            self._release_connection(conn)
            return True
        except Exception as e:
            self.logger.error(f"VastBase update error: {e}")
            return False

    def delete(self, condition: dict, index_name: str, knowledgebase_id: str) -> int:
        """Delete messages from VastBase."""
        table_name = f"{index_name}_{knowledgebase_id}"

        # Build WHERE clause
        where_parts = [f"memory_id = '{knowledgebase_id}'"]
        for k, v in condition.items():
            if isinstance(v, str):
                where_parts.append(f"{k} = '{v}'")
            elif isinstance(v, int):
                where_parts.append(f"{k} = {v}")
            elif isinstance(v, list) and v:
                if isinstance(v[0], str):
                    vals = ", ".join([f"'{x}'" for x in v])
                else:
                    vals = ", ".join([str(x) for x in v])
                where_parts.append(f"{k} IN ({vals})")

        sql = f"DELETE FROM {table_name} WHERE {' AND '.join(where_parts)}"

        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(sql)
            deleted = cursor.rowcount
            conn.commit()
            cursor.close()
            self._release_connection(conn)
            return deleted
        except Exception as e:
            self.logger.warning(f"VastBase delete error: {e}")
            return 0

    def create_idx(self, index_name: str, knowledgebase_id: str, vector_size: int):
        """Create message table in VastBase."""
        table_name = f"{index_name}_{knowledgebase_id}"

        sql = f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id VARCHAR(256) PRIMARY KEY,
            message_id BIGINT,
            message_type VARCHAR(64),
            content TEXT,
            user_id VARCHAR(256),
            agent_id VARCHAR(256),
            session_id VARCHAR(256),
            memory_id VARCHAR(256),
            source_id VARCHAR(256),
            valid_at VARCHAR(64),
            invalid_at VARCHAR(64),
            forget_at VARCHAR(64),
            status INTEGER DEFAULT 1,
            content_embed vector({vector_size})
        )
        """

        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(sql)
            conn.commit()
            cursor.close()
            self._release_connection(conn)
            return True
        except Exception as e:
            self.logger.error(f"VastBase create table error: {e}")
            return False

    def delete_idx(self, index_name: str, knowledgebase_id: str):
        """Delete message table."""
        table_name = f"{index_name}_{knowledgebase_id}"
        sql = f"DROP TABLE IF EXISTS {table_name}"

        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(sql)
            conn.commit()
            cursor.close()
            self._release_connection(conn)
        except Exception as e:
            self.logger.warning(f"VastBase drop table error: {e}")

    def index_exist(self, index_name: str, knowledgebase_id: str) -> bool:
        """Check if message table exists."""
        table_name = f"{index_name}_{knowledgebase_id}"
        sql = f"SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = '{table_name}')"

        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(sql)
            result = cursor.fetchone()[0]
            cursor.close()
            self._release_connection(conn)
            return result
        except Exception as e:
            self.logger.warning(f"VastBase check table error: {e}")
            return False

    def get_total(self, res):
        """Get total count from results."""
        if isinstance(res, dict):
            return res.get("total", 0)
        return 0

    def get_doc_ids(self, res):
        """Get document IDs from results."""
        if isinstance(res, dict):
            return [d.get("id") for d in res.get("hits", [])]
        return []

    def get_fields(self, res, fields: list[str]) -> dict[str, dict]:
        """Get fields from results."""
        result = {}
        if isinstance(res, dict):
            for d in res.get("hits", []):
                doc_id = d.get("id")
                if doc_id:
                    result[doc_id] = {f: d.get(f) for f in fields if f in d}
        return result

    def get_highlight(self, res, keywords: list[str], field_name: str):
        """Get highlight - not supported in VastBase."""
        return {}

    def get_aggregation(self, res, field_name: str):
        """Get aggregation - not supported in VastBase."""
        return []

    def sql(self, sql: str, fetch_size: int, format: str):
        """Execute SQL query."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(sql)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchmany(fetch_size)
            cursor.close()
            self._release_connection(conn)
            return {"columns": columns, "rows": rows}
        except Exception as e:
            self.logger.error(f"VastBase SQL error: {e}")
            return None

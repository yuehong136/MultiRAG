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
Milvus connection base class.
This module provides the base class for Milvus connections with common operations.
"""
import copy
import json
import logging
import os
import re
from abc import abstractmethod
from uuid import uuid4

import numpy as np
from pymilvus.client.constants import DEFAULT_CONSISTENCY_LEVEL
from pymilvus.client.types import ExceptionsMessage
from pymilvus.exceptions import (
    DataTypeNotMatchException,
    MilvusException,
    ParamError,
)
from pymilvus.orm import utility
from pymilvus.orm.collection import CollectionSchema, FieldSchema
from pymilvus.orm.connections import connections
from pymilvus.orm.types import DataType

from common.constants import PAGERANK_FLD
from common.doc_store.doc_store_base import (
    DocStoreConnection,
    FusionExpr,
    MatchDenseExpr,
    MatchExpr,
    MatchTextExpr,
    OrderByExpr,
)
from common.file_utils import get_project_base_directory
from common.float_utils import get_float
from core.nlp import is_english

ATTEMPT_TIME = 2


class MilvusConnectionBase(DocStoreConnection):
    """Base class for Milvus connections."""

    def __init__(self, logger_name: str = "multirag.milvus_conn"):
        from common.doc_store.milvus_conn_pool import MILVUS_CONN

        self.logger = logging.getLogger(logger_name)
        self._using = MILVUS_CONN.get_conn()
        self.is_self_hosted = bool(utility.get_server_type(using=self._using) == "milvus")

        # Load search field configs for BM25
        try:
            self.search_field_configs = self._load_search_field_configs()
            self.logger.info(f"Loaded BM25 field configs: {list(self.search_field_configs.keys())}")
        except Exception as e:
            self.logger.warning(f"Failed to load search field configs: {e}")
            self.search_field_configs = {}

    def _get_connection(self, using: str | None = None):
        """Get the Milvus connection handler."""
        return connections._fetch_handler(using or self._using)

    def _create_connection(
        self,
        uri: str,
        user: str = "",
        password: str = "",
        db_name: str = "",
        token: str = "",
        **kwargs,
    ) -> str:
        """Create a new connection to the Milvus server."""
        using = kwargs.pop("alias", None) or uuid4().hex
        try:
            connections.connect(using, user, password, db_name, token, uri=uri, **kwargs)
        except Exception as ex:
            self.logger.error(f"Failed to create connection {using}: {ex!s}")
            raise ex from ex
        else:
            self.logger.debug(f"Created connection: {using}")
            return using

    """
    Database operations
    """

    def db_type(self) -> str:
        return "milvus"

    def health(self) -> dict:
        try:
            version = utility.get_server_version(using=self._using)
            return {"type": "milvus", "status": "green", "version": version}
        except MilvusException as e:
            return {"type": "milvus", "status": "red", "error": str(e)}

    """
    Table operations
    """

    def create_idx(self, index_name: str | list[str], dataset_id: str, vector_size: int, parser_id: str = None):
        """Create a Milvus collection based on the index name and vector size."""
        if isinstance(index_name, list):
            if not index_name:
                self.logger.error("Index name list is empty")
                return False
            collection_name = index_name[0]
            self.logger.debug(f"Index name is a list, using first element: {collection_name}")
        elif isinstance(index_name, str):
            collection_name = index_name
        else:
            self.logger.error(f"Index name must be string or list of strings, got: {type(index_name)}")
            return False

        try:
            if self.index_exist(index_name, dataset_id):
                self.logger.info(f"Collection {collection_name} already exists")
                return True

            mapping_path = os.path.join(get_project_base_directory(), "configs", "mapping.json")
            if not os.path.exists(mapping_path):
                self.logger.warning(f"Milvus mapping file not found, using default fields: {mapping_path}")
                return self._create_default_collection(index_name, dataset_id, vector_size)

            with open(mapping_path, encoding='utf-8') as f:
                mapping = json.load(f)

            fields = []
            dynamic_templates = mapping.get("mappings", {}).get("dynamic_templates", [])
            auto_dimensions = {f"q_{vector_size}_vec": vector_size}
            primary_field_added = False
            vector_field_added = False

            for template in dynamic_templates:
                for key, value in template.items():
                    match_pattern = value.get("match_pattern", "")
                    if match_pattern == "regex":
                        continue

                    match = value.get("match", "")
                    mapping_type = value.get("mapping", {}).get("type", "")

                    if value.get("mapping", {}).get("is_primary", False):
                        max_length = value.get("mapping", {}).get("max_length", 512)
                        fields.append(FieldSchema(
                            name=match,
                            dtype=DataType.VARCHAR,
                            max_length=max_length,
                            is_primary=True
                        ))
                        primary_field_added = True
                        continue

                    if mapping_type == "FLOAT_VECTOR":
                        dims = value.get("mapping", {}).get("dims", vector_size)
                        if dims == "auto":
                            dims = auto_dimensions.get(match, vector_size)
                        fields.append(FieldSchema(
                            name=match,
                            dtype=DataType.FLOAT_VECTOR,
                            dim=dims
                        ))
                        vector_field_added = True
                        continue

                    if mapping_type == "VARCHAR":
                        max_length = value.get("mapping", {}).get("max_length", 256)
                        fields.append(FieldSchema(name=match, dtype=DataType.VARCHAR, max_length=max_length))
                    elif mapping_type == "FLOAT":
                        fields.append(FieldSchema(name=match, dtype=DataType.FLOAT))
                    elif mapping_type == "INT64":
                        fields.append(FieldSchema(name=match, dtype=DataType.INT64))
                    elif mapping_type == "JSON":
                        fields.append(FieldSchema(name=match, dtype=DataType.JSON))
                    elif mapping_type == "ARRAY":
                        element_type = value.get("mapping", {}).get("element_type", "VARCHAR")
                        max_length = value.get("mapping", {}).get("max_length", 256)
                        max_capacity = value.get("mapping", {}).get("max_capacity", 100)
                        element_data_type = getattr(DataType, element_type) if isinstance(element_type, str) else element_type
                        fields.append(FieldSchema(
                            name=match,
                            dtype=DataType.ARRAY,
                            element_type=element_data_type,
                            max_length=max_length,
                            max_capacity=max_capacity
                        ))

            if not vector_field_added:
                fields.append(FieldSchema(name=f"q_{vector_size}_vec", dtype=DataType.FLOAT_VECTOR, dim=vector_size))

            if not primary_field_added:
                fields.append(FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=512, is_primary=True))

            if not any(field.name == PAGERANK_FLD for field in fields):
                fields.append(FieldSchema(name=PAGERANK_FLD, dtype=DataType.FLOAT))

            schema = CollectionSchema(
                fields=fields,
                description=f"Collection for {index_name} with {dataset_id}",
                enable_dynamic_field=True
            )

            conn = self._get_connection()
            conn.create_collection(collection_name, schema, consistency_level=DEFAULT_CONSISTENCY_LEVEL)

            for field in fields:
                if field.dtype == DataType.FLOAT_VECTOR:
                    index_params = {
                        "index_type": "IVF_FLAT",
                        "metric_type": "COSINE",
                        "params": {"nlist": 1024}
                    }
                    conn.create_index(collection_name, field.name, index_params)

            conn.load_collection(collection_name)
            self.logger.info(f"Successfully created collection {collection_name}, vector size {vector_size}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to create collection {collection_name}: {e!s}")
            raise e

    def _create_default_collection(self, index_name: str | list[str], dataset_id: str, vector_size: int):
        """Create a collection with default fields when no mapping file is found."""
        if isinstance(index_name, list):
            if not index_name:
                self.logger.error("Index name list is empty")
                return False
            collection_name = index_name[0]
        elif isinstance(index_name, str):
            collection_name = index_name
        else:
            self.logger.error(f"Index name must be string or list of strings, got: {type(index_name)}")
            return False

        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=512),
            FieldSchema(name=f"q_{vector_size}_vec", dtype=DataType.FLOAT_VECTOR, dim=vector_size),
            FieldSchema(name="kb_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="available_int", dtype=DataType.INT64),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="text_tks", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="create_time", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="create_timestamp_flt", dtype=DataType.FLOAT),
            FieldSchema(name="important_kwd", dtype=DataType.ARRAY, element_type=DataType.VARCHAR, max_length=256, max_capacity=4096),
            FieldSchema(name="question_kwd", dtype=DataType.ARRAY, element_type=DataType.VARCHAR, max_length=1024, max_capacity=4096),
            FieldSchema(name="position_int", dtype=DataType.JSON),
            FieldSchema(name="page_num_int", dtype=DataType.JSON),
            FieldSchema(name="top_int", dtype=DataType.JSON),
            FieldSchema(name=PAGERANK_FLD, dtype=DataType.FLOAT),
            FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=128),
        ]

        schema = CollectionSchema(
            fields=fields,
            description=f"Default collection for {index_name} with {dataset_id}",
            enable_dynamic_field=True
        )

        conn = self._get_connection()
        conn.create_collection(collection_name, schema, consistency_level=DEFAULT_CONSISTENCY_LEVEL)
        conn.create_index(
            collection_name,
            f"q_{vector_size}_vec",
            {"index_type": "IVF_FLAT", "metric_type": "COSINE", "params": {"nlist": 1024}}
        )
        conn.load_collection(collection_name)
        self.logger.info(f"Successfully created collection {collection_name} (default fields), vector size {vector_size}")
        return True

    def delete_idx(self, index_name: str | list[str], dataset_id: str):
        """Delete a Milvus collection."""
        if isinstance(index_name, list):
            if not index_name:
                self.logger.error("Index name list is empty")
                return False
            collection_name = index_name[0]
        elif isinstance(index_name, str):
            collection_name = index_name
        else:
            self.logger.error(f"Index name must be string or list of strings, got: {type(index_name)}")
            return False

        try:
            conn = self._get_connection()
            if conn.has_collection(collection_name):
                conn.drop_collection(collection_name)
                self.logger.info(f"Successfully deleted collection {collection_name}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to delete collection {collection_name}: {e!s}")
            raise e

    def index_exist(self, index_name: str | list[str], dataset_id: str = None) -> bool:
        """Check if a Milvus collection exists."""
        if isinstance(index_name, list):
            if not index_name:
                self.logger.error("Index name list is empty")
                return False
            collection_name = index_name[0]
        elif isinstance(index_name, str):
            collection_name = index_name
        else:
            self.logger.error(f"Index name must be string or list of strings, got: {type(index_name)}")
            return False

        try:
            conn = self._get_connection()
            return conn.has_collection(collection_name)
        except Exception as e:
            self.logger.warning(f"Failed to check if collection {collection_name} exists: {e!s}")
            return False

    """
    CRUD operations - Abstract methods
    """

    @abstractmethod
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
        dataset_ids: list[str],
        agg_fields: list[str] | None = None,
        rank_feature: dict | None = None,
    ):
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def get(self, doc_id: str, index_name: str, dataset_ids: list[str]) -> dict | None:
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def insert(self, documents: list[dict], index_name: str, dataset_id: str = None) -> list[str]:
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def update(self, condition: dict, new_value: dict, index_name: str, dataset_id: str) -> bool:
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def delete(self, condition: dict, index_name: str, dataset_id: str) -> int:
        raise NotImplementedError("Not implemented")

    """
    Helper functions for search result
    """

    def get_total(self, res) -> int:
        """Get total count from search results."""
        if isinstance(res, tuple):
            return res[1]
        if isinstance(res, list):
            return len(res)
        if isinstance(res, dict):
            return res.get("total", 0)
        return 0

    def get_doc_ids(self, res) -> list[str]:
        """Get document IDs from search results."""
        if isinstance(res, tuple):
            results = res[0]
        else:
            results = res

        if not isinstance(results, list):
            return []

        ids = []
        for item in results:
            if isinstance(item, dict):
                doc_id = item.get("id") or item.get("pk")
                if doc_id:
                    ids.append(str(doc_id))
            elif hasattr(item, "to_dict"):
                # Handle Milvus Hit objects
                item_dict = item.to_dict()
                doc_id = item_dict.get("id") or item_dict.get("pk")
                if doc_id:
                    ids.append(str(doc_id))
            elif hasattr(item, "pk"):
                # Direct access to Hit object attributes
                doc_id = getattr(item, "id", None) or getattr(item, "pk", None)
                if doc_id:
                    ids.append(str(doc_id))
        return ids

    @abstractmethod
    def get_fields(self, res, fields: list[str]) -> dict[str, dict]:
        raise NotImplementedError("Not implemented")

    def get_highlight(self, res, keywords: list[str], field_name: str):
        """Generate highlighted text snippets for search results."""
        if isinstance(res, tuple):
            results = res[0]
        else:
            results = res

        if not isinstance(results, list):
            return {}

        ans = {}
        for item in results:
            # Convert Hit objects to dict
            if isinstance(item, dict):
                item_dict = item
            elif hasattr(item, "to_dict"):
                item_dict = item.to_dict()
            else:
                continue

            doc_id = str(item_dict.get("id", item_dict.get("pk", "")))
            if not doc_id:
                continue

            entity = item_dict.get("entity") or item_dict
            text = entity.get(field_name, "")
            if not text:
                continue

            if re.search(r"<em>[^<>]+</em>", text, flags=re.IGNORECASE | re.MULTILINE):
                ans[doc_id] = text
                continue

            text = re.sub(r"[\r\n]+", " ", text, flags=re.IGNORECASE | re.MULTILINE)
            snippets = []

            for sentence in re.split(r"[.?!；。？！\n]", text):
                if not sentence.strip():
                    continue

                sent = sentence
                if is_english([sent]):
                    for kw in keywords:
                        pattern = rf"(^|[ .?/'\"()!,:;-])({re.escape(kw)})([ .?/'\"()!,:;-])"
                        sent = re.sub(pattern, r"\1<em>\2</em>\3", sent, flags=re.IGNORECASE | re.MULTILINE)
                else:
                    for kw in sorted(keywords, key=len, reverse=True):
                        if not kw:
                            continue
                        sent = re.sub(re.escape(kw), f"<em>{kw}</em>", sent, flags=re.IGNORECASE | re.MULTILINE)

                if re.search(r"<em>[^<>]+</em>", sent, flags=re.IGNORECASE | re.MULTILINE):
                    snippets.append(sent.strip())

            if snippets:
                ans[doc_id] = "...".join(snippets)
            else:
                ans[doc_id] = text

        return ans

    def get_aggregation(self, res, field_name: str) -> list[tuple]:
        """Get aggregation results (Milvus doesn't support native aggregation)."""
        if isinstance(res, tuple):
            results = res[0]
        else:
            results = res

        if not isinstance(results, list):
            return []

        try:
            value_counts = {}
            for item in results:
                if not isinstance(item, dict):
                    continue

                value = None
                if field_name in item:
                    value = item.get(field_name)
                elif "entity" in item and isinstance(item["entity"], dict) and field_name in item["entity"]:
                    value = item["entity"].get(field_name)

                if value is None:
                    continue

                if not isinstance(value, (str, int, float)):
                    value = str(value)

                if value in value_counts:
                    value_counts[value] += 1
                else:
                    value_counts[value] = 1

            return [(str(value), count) for value, count in value_counts.items()]
        except Exception as e:
            self.logger.warning(f"Aggregation calculation failed: {e!s}")
            return []

    """
    SQL functionality
    """

    def sql(self, sql: str, fetch_size: int, format: str):
        """
        Execute SQL-like queries on Milvus by parsing SQL and converting to Milvus query API.
        Supports chunk_data JSON field with path expressions: chunk_data["field"].
        Aggregations (COUNT/SUM/AVG/MIN/MAX) are computed in Python via pandas.
        """
        import pandas as pd
        self.logger.debug(f"Milvus sql() input: {sql}")

        try:
            sql = re.sub(r" +", " ", sql).strip()

            if not re.match(r"^SELECT\b", sql, re.IGNORECASE):
                raise ValueError("Only SELECT statements are supported")

            # --- Parse FROM clause ---
            from_match = re.search(r"\bFROM\s+(\w+)", sql, re.IGNORECASE)
            if not from_match:
                raise ValueError("Cannot identify FROM clause")
            collection_name = from_match.group(1)

            # --- Parse SELECT clause ---
            select_match = re.search(r"SELECT\s+(.*?)\s+FROM\b", sql, re.IGNORECASE | re.DOTALL)
            select_raw = select_match.group(1).strip() if select_match else "*"

            # Detect aggregate functions
            has_aggregate = bool(re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", select_raw, re.IGNORECASE))

            # Parse GROUP BY
            group_by_field = None
            group_match = re.search(r"\bGROUP\s+BY\s+([\w\"\[\]\.]+)", sql, re.IGNORECASE)
            if group_match:
                group_by_field = group_match.group(1).strip().strip('"')

            # --- Parse SELECT fields into (expr, alias) pairs ---
            select_items = []
            literal_columns = {}
            if select_raw != "*" and not has_aggregate:
                for field in self._split_select_fields(select_raw):
                    literal_match = re.match(r"^(['\"])(.+?)\1(\s+as\s+(\S+))?$", field, re.IGNORECASE)
                    if literal_match:
                        literal_columns[literal_match.group(4) or literal_match.group(2)] = literal_match.group(2)
                    else:
                        as_match = re.match(r"(.+?)\s+as\s+(\S+)$", field, re.IGNORECASE)
                        if as_match:
                            select_items.append((as_match.group(1).strip(), as_match.group(2).strip()))
                        else:
                            select_items.append((field.strip(), field.strip()))

            # --- Parse WHERE clause ---
            where_clause = ""
            where_match = re.search(r"\bWHERE\s+(.*?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|$)", sql, re.IGNORECASE | re.DOTALL)
            if where_match:
                where_clause = where_match.group(1).strip()

            if where_clause and re.match(r"^\s*(1\s*=\s*0|0\s*=\s*1|false)\s*", where_clause, re.IGNORECASE):
                return {"rows": [], "columns": []}

            # --- Parse LIMIT ---
            limit = fetch_size
            limit_match = re.search(r"\bLIMIT\s+(\d+)", sql, re.IGNORECASE)
            if limit_match:
                limit = int(limit_match.group(1))

            # --- Convert WHERE to Milvus filter expression ---
            filter_expr = self._where_to_milvus_expr(where_clause) if where_clause else ""
            self.logger.debug(f"Milvus filter_expr: {filter_expr}")

            # --- Execute Milvus query ---
            conn = self._get_connection()
            # Always fetch chunk_data + doc_id + docnm_kwd for table parser queries
            output_fields = ["chunk_data", "doc_id", "docnm_kwd"]
            if has_aggregate:
                # Aggregate queries need all matching rows; use iterator to bypass 16384 limit
                results = self._query_all(conn, collection_name, filter_expr or "", output_fields)
            else:
                results = conn.query(collection_name, expr=filter_expr or "", output_fields=output_fields, limit=limit)

            if not results and not literal_columns:
                return {"rows": [], "columns": []} if format == "json" else []

            # --- Build DataFrame for processing ---
            df = pd.DataFrame(results)

            # Expand chunk_data JSON into columns
            if "chunk_data" in df.columns:
                json_expanded = df["chunk_data"].apply(
                    lambda v: v if isinstance(v, dict) else (json.loads(v) if isinstance(v, str) and v else {})
                )
                json_df = pd.json_normalize(json_expanded)
                df = pd.concat([df.drop(columns=["chunk_data"]), json_df], axis=1)

            # --- Handle aggregation in Python ---
            if has_aggregate:
                return self._execute_aggregate(select_raw, df, group_by_field, format, fetch_size)

            # --- Non-aggregate: select fields and build result ---
            # Determine output columns
            out_cols = []
            col_names = []
            if select_items:
                for expr, alias in select_items:
                    # chunk_data["field"] → field
                    json_path = re.match(r'chunk_data\["(.+?)"\]', expr)
                    col = json_path.group(1) if json_path else expr
                    if col in df.columns:
                        out_cols.append(col)
                        col_names.append(alias)
                    elif expr in df.columns:
                        out_cols.append(expr)
                        col_names.append(alias)
            else:
                # SELECT * - use all columns
                out_cols = list(df.columns)
                col_names = list(df.columns)

            if format == "json":
                columns = [{"name": n} for n in col_names]
                for lit_name, lit_val in literal_columns.items():
                    columns.append({"name": lit_name})
                rows = []
                for _, row in df.head(limit).iterrows():
                    r = [row.get(c) for c in out_cols]
                    for lit_val in literal_columns.values():
                        r.append(lit_val)
                    rows.append(r)
                return {"rows": rows, "columns": columns}
            return results

        except Exception as e:
            self.logger.error(f"Milvus SQL parsing or execution failed: {e!s}")
            return {"error": f"SQL query failed: {e!s}"}

    def _split_select_fields(self, fields_str: str) -> list[str]:
        """Split SELECT field list respecting parentheses and quotes."""
        fields = []
        current = ""
        depth = 0
        in_quotes = False
        quote_char = None
        for ch in fields_str:
            if ch in ("'", '"') and not in_quotes:
                in_quotes = True
                quote_char = ch
                current += ch
            elif ch == quote_char and in_quotes:
                in_quotes = False
                quote_char = None
                current += ch
            elif ch == '(' and not in_quotes:
                depth += 1
                current += ch
            elif ch == ')' and not in_quotes:
                depth -= 1
                current += ch
            elif ch == ',' and depth == 0 and not in_quotes:
                fields.append(current.strip())
                current = ""
            else:
                current += ch
        if current.strip():
            fields.append(current.strip())
        return fields

    def _where_to_milvus_expr(self, where_clause: str) -> str:
        """Convert SQL WHERE clause to Milvus filter expression with JSON path support."""
        expr = where_clause

        # Convert chunk_data["field"] op 'value' → chunk_data["field"] op 'value' (already Milvus-compatible)
        # Convert SQL operators to Milvus
        expr = re.sub(r"(?<!=)=(?!=)", "==", expr)
        expr = re.sub(r"\bAND\b", "&&", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bOR\b", "||", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bNOT\b", "!", expr, flags=re.IGNORECASE)
        # IS NOT NULL → field != ""
        expr = re.sub(r'(\S+)\s+IS\s+NOT\s+NULL', r'\1 != ""', expr, flags=re.IGNORECASE)
        expr = re.sub(r'(\S+)\s+IS\s+NULL', r'\1 == ""', expr, flags=re.IGNORECASE)

        return expr

    def _execute_aggregate(self, select_raw: str, df, group_by_field: str | None, format: str, fetch_size: int) -> dict:
        """Execute aggregate functions (COUNT/SUM/AVG/MIN/MAX) in Python using pandas."""
        # Parse aggregate expressions: COUNT(*), SUM(chunk_data["field"]), etc.
        agg_pattern = re.compile(
            r'(COUNT|SUM|AVG|MIN|MAX)\s*\(\s*(\*|(?:DISTINCT\s+)?[\w\"\[\]\.]+)\s*\)(?:\s+AS\s+(\w+))?',
            re.IGNORECASE
        )

        agg_exprs = []
        for m in agg_pattern.finditer(select_raw):
            func = m.group(1).upper()
            arg = m.group(2).strip()
            alias = m.group(3) or f"{func}({arg})"
            # Resolve chunk_data["field"] → column name
            json_match = re.match(r'chunk_data\["(.+?)"\]', arg)
            col = json_match.group(1) if json_match else arg
            agg_exprs.append((func, col, alias))

        if not agg_exprs:
            return {"rows": [], "columns": []}

        if group_by_field:
            # Resolve group_by field
            json_match = re.match(r'chunk_data\["(.+?)"\]', group_by_field)
            gb_col = json_match.group(1) if json_match else group_by_field

            if gb_col not in df.columns:
                return {"rows": [], "columns": []}

            grouped = df.groupby(gb_col)
            columns = [{"name": gb_col}]
            for func, col, alias in agg_exprs:
                columns.append({"name": alias})

            rows = []
            for group_val, group_df in grouped:
                row = [group_val]
                for func, col, alias in agg_exprs:
                    row.append(self._compute_agg(func, col, group_df))
                rows.append(row)
        else:
            columns = [{"name": alias} for _, _, alias in agg_exprs]
            row = []
            for func, col, alias in agg_exprs:
                row.append(self._compute_agg(func, col, df))
            rows = [row]

        if format == "json":
            return {"rows": rows[:fetch_size] if fetch_size > 0 else rows, "columns": columns}
        return rows

    def _query_all(self, conn, collection_name: str, expr: str, output_fields: list[str]) -> list[dict]:
        """
        Use query_iterator to fetch all matching rows, bypassing the 16384 single-query limit.
        Falls back to regular query() if iterator is not available.
        """
        BATCH_SIZE = 1000
        MAX_ROWS = 100000  # safety cap
        all_results = []
        try:
            iterator = conn.query_iterator(
                collection_name=collection_name,
                expr=expr,
                output_fields=output_fields,
                batch_size=BATCH_SIZE,
            )
            while True:
                batch = iterator.next()
                if not batch:
                    break
                all_results.extend(batch)
                if len(all_results) >= MAX_ROWS:
                    self.logger.warning(f"_query_all hit safety cap {MAX_ROWS}, stopping iteration")
                    break
            iterator.close()
        except Exception as e:
            self.logger.warning(f"query_iterator failed ({e}), falling back to query() with limit 16384")
            all_results = conn.query(collection_name, expr=expr, output_fields=output_fields, limit=16384)
        self.logger.debug(f"_query_all fetched {len(all_results)} rows from {collection_name}")
        return all_results

    @staticmethod
    def _compute_agg(func: str, col: str, df) -> int | float | None:
        """Compute a single aggregate function on a DataFrame."""
        import pandas as pd
        if func == "COUNT":
            if col == "*":
                return len(df)
            if col.upper().startswith("DISTINCT "):
                real_col = col.split(None, 1)[1]
                json_match = re.match(r'chunk_data\["(.+?)"\]', real_col)
                real_col = json_match.group(1) if json_match else real_col
                return df[real_col].nunique() if real_col in df.columns else 0
            return df[col].count() if col in df.columns else 0
        if col not in df.columns:
            return None
        series = pd.to_numeric(df[col], errors="coerce")
        if func == "SUM":
            return series.sum()
        if func == "AVG":
            return round(series.mean(), 4) if not series.empty else None
        if func == "MIN":
            return series.min()
        if func == "MAX":
            return series.max()
        return None

    """
    Internal search helper methods
    """

    def _load_search_field_configs(self) -> dict:
        """Load BM25 field configurations from mapping.json."""
        cfg = {}
        mapping_path = os.path.join(get_project_base_directory(), "configs", "mapping.json")
        if not os.path.exists(mapping_path):
            self.logger.warning(f"Mapping file not found: {mapping_path}")
            return cfg

        try:
            with open(mapping_path, encoding="utf-8") as f:
                mapping = json.load(f)
        except Exception as e:
            self.logger.error(f"Failed to read mapping file: {e}")
            return cfg

        dynamic_templates = mapping.get("mappings", {}).get("dynamic_templates", [])
        for template in dynamic_templates:
            for entry in template.values():
                match = entry.get("match")
                mapping_cfg = entry.get("mapping", {})
                bm25_cfg = mapping_cfg.get("bm25")
                if not match or not bm25_cfg or not bm25_cfg.get("enable"):
                    continue
                sparse_field = bm25_cfg.get("output_field", f"{match}_sparse")
                cfg[match] = {
                    "sparse_field": sparse_field,
                    "weight": bm25_cfg.get("weight", 1.0),
                    "search_params": copy.deepcopy(bm25_cfg.get("search_params", {"drop_ratio_search": 0.1})),
                    "index_params": copy.deepcopy(bm25_cfg.get("index_params", {})),
                }
        return cfg

    @staticmethod
    def _parse_field_weight(field_expr: str) -> tuple[str, float]:
        """Parse field weight from expression like 'field^2.0'."""
        if "^" in field_expr:
            fname, boost = field_expr.split("^", 1)
            try:
                return fname, float(boost)
            except ValueError:
                return fname, 1.0
        return field_expr, 1.0

    @staticmethod
    def _normalize_scores(score_map: dict, reverse: bool = False) -> dict:
        """Normalize scores using Min-Max normalization."""
        if not score_map:
            return {}

        keys = list(score_map.keys())
        values = np.array([get_float(score_map[k]) for k in keys], dtype=np.float64)

        if len(values) == 1:
            return {keys[0]: 1.0}

        min_v = np.min(values)
        max_v = np.max(values)

        if max_v - min_v < 1e-6:
            return dict.fromkeys(keys, 1.0)

        if reverse:
            normalized = (max_v - values) / (max_v - min_v)
        else:
            normalized = (values - min_v) / (max_v - min_v)

        return {k: float(normalized[i]) for i, k in enumerate(keys)}

    def _execute_text_queries(
        self,
        collection_names: list[str],
        text_exprs: list[MatchTextExpr],
        filter_expr: str,
        select_fields: list[str],
        limit: int,
        offset: int,
    ):
        """Execute BM25 text search queries."""
        if not text_exprs:
            return [], {}, 0

        conn = self._get_connection()
        aggregated = {}
        score_map = {}
        total_hits = 0
        search_limit = max(limit + offset, 1)

        for expr in text_exprs:
            expr_limit = expr.topn or search_limit
            for field_expr in expr.fields:
                field_name, boost = self._parse_field_weight(field_expr)
                cfg = self.search_field_configs.get(field_name)
                if not cfg:
                    continue

                sparse_field = cfg["sparse_field"]
                final_weight = boost
                search_params = {
                    "metric_type": "BM25",
                    "params": copy.deepcopy(cfg.get("search_params", {"drop_ratio_search": 0.1}))
                }

                for collection in collection_names:
                    try:
                        collection_select_fields = self._filter_collection_output_fields(conn, collection, select_fields)
                        query_payload = expr.raw_text if hasattr(expr, "raw_text") else expr.matching_text
                        res = conn.search(
                            collection,
                            data=[query_payload],
                            anns_field=sparse_field,
                            param=search_params,
                            limit=expr_limit,
                            expression=filter_expr if filter_expr else None,
                            output_fields=collection_select_fields,
                        )
                    except Exception as e:
                        self.logger.warning(f"BM25 search failed field={field_name}, collection={collection}: {e}")
                        continue

                    hits = res[0] if res else []
                    total_hits += len(hits)
                    for hit in hits:
                        hit_dict = hit.to_dict()
                        doc_id = str(hit_dict.get("id", hit_dict.get("pk", "")))
                        if not doc_id:
                            continue
                        distance = get_float(hit_dict.get("distance", 0.0))
                        entry = aggregated.setdefault(doc_id, {
                            "hit": {"id": doc_id, "pk": doc_id},
                            "entity": {},
                            "distance": 0.0,
                        })
                        entry["distance"] += final_weight * distance
                        score_map[doc_id] = entry["distance"]

                        entity = hit_dict.get("entity") or {}
                        if entity:
                            entry["entity"].update(entity)
                        entry["hit"].update(hit_dict)

        results = []
        for doc_id, data in aggregated.items():
            base_hit = data["hit"]
            if isinstance(base_hit, dict):
                hit = copy.deepcopy(base_hit)
            else:
                try:
                    hit = base_hit.to_dict()
                except Exception:
                    hit = {"raw": str(base_hit)}
            hit["distance"] = data["distance"]
            hit["_score"] = data["distance"]
            if data["entity"]:
                if "entity" not in hit or not isinstance(hit["entity"], dict):
                    hit["entity"] = {}
                hit["entity"].update(data["entity"])
            if "id" not in hit:
                hit["id"] = doc_id
            if "pk" not in hit:
                hit["pk"] = doc_id
            results.append(hit)

        results.sort(key=lambda x: x.get("distance", 0.0), reverse=True)
        return results, score_map, total_hits

    def _execute_dense_query(
        self,
        collection_names: list[str],
        dense_expr: MatchDenseExpr | None,
        filter_expr: str,
        select_fields: list[str],
        limit: int,
        offset: int,
    ):
        """Execute vector search queries."""
        if dense_expr is None:
            return [], {}, 0

        vector_data = dense_expr.embedding_data
        vector_field = dense_expr.vector_column_name
        if vector_data is None or vector_field is None:
            return [], {}, 0

        conn = self._get_connection()
        search_params = {
            "metric_type": dense_expr.distance_type.upper(),
            "params": {"nprobe": 10},
        }
        if dense_expr.extra_options and "similarity" in dense_expr.extra_options:
            search_params["similarity"] = dense_expr.extra_options["similarity"]

        query_limit = dense_expr.topn or max(limit + offset, 1)
        vector_results = []
        score_map = {}
        total_hits = 0

        for collection in collection_names:
            try:
                collection_select_fields = self._filter_collection_output_fields(conn, collection, select_fields)
                res = conn.search(
                    collection,
                    [vector_data],
                    vector_field,
                    search_params,
                    expression=filter_expr,
                    output_fields=collection_select_fields,
                    limit=query_limit,
                )
            except Exception as e:
                self.logger.warning(f"Vector search failed collection={collection}: {e}")
                continue

            hits = res[0] if res else []
            total_hits += len(hits)
            for hit in hits:
                hit_dict = hit.to_dict()
                doc_id = str(hit_dict.get("id", hit_dict.get("pk", "")))
                vector_results.append(hit_dict)
                if doc_id:
                    score_map[doc_id] = get_float(hit_dict.get("distance", 0.0))

        vector_results.sort(key=lambda x: x.get("distance", 0.0), reverse=True)
        return vector_results, score_map, total_hits

    @staticmethod
    def _fusion_weights(fusion_expr: FusionExpr | None) -> tuple[float, float]:
        """Parse fusion weights from expression."""
        default = (0.5, 0.5)
        if not fusion_expr or not fusion_expr.fusion_params:
            return default
        weights = fusion_expr.fusion_params.get("weights")
        if not weights:
            return default
        parts = [p.strip() for p in str(weights).split(",") if p.strip()]
        if len(parts) < 2:
            return default
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            return default

    def _search_with_text_and_dense(
        self,
        select_fields: list[str],
        filter_expr: str,
        text_exprs: list[MatchTextExpr],
        dense_expr: MatchDenseExpr | None,
        fusion_expr: FusionExpr | None,
        index_names: list[str],
        limit: int,
        offset: int,
        rank_boost: dict,
    ):
        """Perform hybrid search combining text and vector search."""
        text_results, text_scores, _ = self._execute_text_queries(
            index_names, text_exprs, filter_expr, select_fields, limit, offset
        )

        vector_results, vector_scores, _ = self._execute_dense_query(
            index_names, dense_expr, filter_expr, select_fields, limit, offset
        )

        combined_results = []
        if text_results and vector_results:
            text_weight, vector_weight = self._fusion_weights(fusion_expr)
            norm_text = self._normalize_scores(text_scores, reverse=False)

            vector_metric_type = dense_expr.distance_type.upper() if dense_expr else "COSINE"
            is_l2 = vector_metric_type == "L2"
            norm_vector = self._normalize_scores(vector_scores, reverse=is_l2)

            final_scores = {}
            for doc_id in set(norm_text.keys()) | set(norm_vector.keys()):
                final_scores[doc_id] = (
                    text_weight * norm_text.get(doc_id, 0.0) +
                    vector_weight * norm_vector.get(doc_id, 0.0)
                )

            lookup = {}
            for hit in vector_results + text_results:
                if isinstance(hit, dict):
                    hit_copy = copy.deepcopy(hit)
                else:
                    try:
                        hit_copy = dict(hit.to_dict())
                    except Exception:
                        hit_copy = {"raw": str(hit)}
                doc_id = str(hit_copy.get("id", hit_copy.get("pk", "")))
                if doc_id and doc_id not in lookup:
                    lookup[doc_id] = hit_copy

            for doc_id, score in final_scores.items():
                base_hit = lookup.get(doc_id, {"id": doc_id, "pk": doc_id, "entity": {}})
                if isinstance(base_hit, dict):
                    hit = copy.deepcopy(base_hit)
                else:
                    try:
                        hit = base_hit.to_dict()
                    except Exception:
                        hit = {"raw": str(base_hit)}
                if "id" not in hit:
                    hit["id"] = doc_id
                if "pk" not in hit:
                    hit["pk"] = doc_id
                hit["distance"] = score
                hit["_score"] = score
                combined_results.append(hit)
        elif text_results:
            combined_results = text_results
        elif vector_results:
            combined_results = vector_results

        for item in combined_results:
            if "_score" not in item:
                item["_score"] = get_float(item.get("distance", item.get("score", 0.0)))
        combined_results.sort(key=lambda x: x.get("distance", 0.0), reverse=True)
        total_hits = len(combined_results)
        if offset > 0:
            combined_results = combined_results[offset:]
        if limit > 0:
            combined_results = combined_results[:limit]

        return combined_results, total_hits

    """
    Low-level Milvus operations
    """

    def describe_collection(self, collection_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        return conn.describe_collection(collection_name, timeout=timeout, **kwargs)

    def _get_collection_schema_fields(self, conn, collection_name: str) -> dict[str, dict] | None:
        try:
            schema = conn.describe_collection(collection_name)
        except Exception:
            self.logger.debug("Failed to describe collection schema: %s", collection_name, exc_info=True)
            return None
        fields = schema.get("fields", [])
        if not fields:
            return None
        return {field.get("name"): field for field in fields if field.get("name")}

    def _filter_collection_output_fields(self, conn, collection_name: str, output_fields: list[str] | None) -> list[str] | None:
        if not output_fields or output_fields == ["*"]:
            return output_fields
        schema_fields = self._get_collection_schema_fields(conn, collection_name)
        if not schema_fields:
            return output_fields
        return [field for field in output_fields if field in schema_fields.keys()]

    def has_collection(self, collection_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        return conn.has_collection(collection_name, timeout=timeout, **kwargs)

    def list_collections(self, **kwargs):
        conn = self._get_connection()
        return conn.list_collections(**kwargs)

    def drop_collection(self, collection_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        conn.drop_collection(collection_name, timeout=timeout, **kwargs)

    def load_collection(self, collection_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        conn.load_collection(collection_name, timeout=timeout, **kwargs)

    def release_collection(self, collection_name: str, timeout: float | None = None, **kwargs):
        conn = self._get_connection()
        conn.release_collection(collection_name, timeout=timeout, **kwargs)

    @classmethod
    def create_schema(cls, **kwargs):
        kwargs["check_fields"] = False
        return CollectionSchema([], **kwargs)

    def query(
        self,
        collection_name: str,
        filter: str = "",
        output_fields: list[str] | None = None,
        timeout: float | None = None,
        ids: list | str | int | None = None,
        partition_names: list[str] | None = None,
        **kwargs,
    ) -> list[dict]:
        """Execute a query on a Milvus collection."""
        if filter and not isinstance(filter, str):
            raise DataTypeNotMatchException(message=ExceptionsMessage.ExprType % type(filter))

        if filter and ids is not None:
            raise ParamError(message=ExceptionsMessage.AmbiguousQueryFilterParam)

        if isinstance(ids, (int, str)):
            ids = [ids]

        conn = self._get_connection()

        if ids:
            try:
                schema_dict = conn.describe_collection(collection_name, timeout=timeout, **kwargs)
            except Exception as ex:
                self.logger.error("Failed to describe collection: %s", collection_name)
                raise ex from ex
            filter = self._pack_pks_expr(schema_dict, ids)

        if not output_fields:
            output_fields = ["*"]

        try:
            res = conn.query(
                collection_name,
                expr=filter,
                output_fields=output_fields,
                partition_names=partition_names,
                timeout=timeout,
                expr_params=kwargs.pop("filter_params", {}),
                **kwargs,
            )
        except Exception as ex:
            self.logger.error("Failed to query collection: %s", collection_name)
            raise ex from ex

        return res

    def _pack_pks_expr(self, schema_dict: dict, pks: list) -> str:
        """Build an expression to filter by primary keys."""
        pk_field = self._extract_primary_field(schema_dict)
        pk_name = pk_field.get("name", "id")
        pk_type = pk_field.get("type", DataType.VARCHAR)

        if pk_type in (DataType.VARCHAR, DataType.STRING):
            pk_values = ", ".join([f"'{pk}'" for pk in pks])
        else:
            pk_values = ", ".join([str(pk) for pk in pks])

        return f"{pk_name} in [{pk_values}]"

    def _extract_primary_field(self, schema_dict: dict) -> dict:
        """Extract primary key field info from schema."""
        fields = schema_dict.get("fields", [])
        if not fields:
            return {}

        for field_dict in fields:
            if field_dict.get("is_primary", None) is not None:
                return field_dict
        return {}

    def search_by_milvus(
        self,
        collection_name: str,
        data: list[list] | list,
        filter: str = "",
        limit: int = 10,
        output_fields: list[str] | None = None,
        search_params: dict | None = None,
        timeout: float | None = None,
        partition_names: list[str] | None = None,
        anns_field: str | None = None,
        **kwargs,
    ) -> list[list[dict]]:
        """Milvus-specific vector search."""
        collections = [collection_name] if isinstance(collection_name, str) else collection_name

        using = kwargs.pop("using", None)
        conn = self._get_connection(using=using)
        all_hits = []
        costs = []
        recalls = []

        expr_params = kwargs.pop("filter_params", {})

        for coll in collections:
            try:
                collection_output_fields = self._filter_collection_output_fields(conn, coll, output_fields)
                res = conn.search(
                    coll,
                    data,
                    anns_field or "",
                    search_params or {},
                    expression=filter,
                    limit=limit,
                    output_fields=collection_output_fields,
                    partition_names=partition_names,
                    expr_params=expr_params,
                    timeout=timeout,
                    **kwargs,
                )
            except Exception:
                self.logger.error(f"Search failed for collection: {coll}", exc_info=True)
                continue

            for hits in res:
                all_hits.extend(hits)

            if hasattr(res, "cost"):
                costs.append(res.cost)
            if hasattr(res, "recalls"):
                recalls.append(res.recalls)

        all_hits.sort(key=lambda h: h.distance, reverse=True)
        top_hits = all_hits[:limit]
        result = [h.to_dict() for h in top_hits]

        if costs or recalls:
            extra = None
            if costs:
                from pymilvus.client.types import construct_cost_extra
                extras = [construct_cost_extra(c) for c in costs]
                extra = extras[0] if len(extras) == 1 else extras
            recalls_val = None
            if recalls:
                recalls_val = recalls[0] if len(recalls) == 1 else recalls
            return type("ExtraList", (list,), {"extra": extra, "recalls": recalls_val})(result)

        return result

    def close(self):
        """Close the Milvus connection."""
        try:
            connections.disconnect(alias=self._using)
        except Exception:
            pass

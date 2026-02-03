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

from common import settings
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

    def _get_connection(self):
        """Get the Milvus connection handler."""
        return connections._fetch_handler(self._using)

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
            self.logger.error(f"Failed to create connection {using}: {str(ex)}")
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

    def create_idx(self, index_name: str | list[str], dataset_id: str, vector_size: int):
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

            with open(mapping_path, 'r', encoding='utf-8') as f:
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
            self.logger.error(f"Failed to create collection {collection_name}: {str(e)}")
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
            self.logger.error(f"Failed to delete collection {collection_name}: {str(e)}")
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
            self.logger.warning(f"Failed to check if collection {collection_name} exists: {str(e)}")
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
            self.logger.warning(f"Aggregation calculation failed: {str(e)}")
            return []

    """
    SQL functionality
    """

    def sql(self, sql: str, fetch_size: int, format: str):
        """Milvus doesn't support direct SQL queries. This method provides limited SQL-to-Milvus conversion."""
        self.logger.debug(f"Attempting to parse SQL query: {sql}")

        try:
            sql = re.sub(r" +", " ", sql).strip()

            if not sql.upper().startswith("SELECT"):
                raise ValueError("Only SELECT statements are supported")

            from_match = re.search(r"FROM\s+(\w+)", sql, re.IGNORECASE)
            if not from_match:
                raise ValueError("Cannot identify FROM clause")

            collection_name = from_match.group(1)

            select_match = re.search(r"SELECT\s+(.*?)\s+FROM", sql, re.IGNORECASE)
            select_fields = []
            literal_columns = {}

            if select_match:
                fields_str = select_match.group(1).strip()
                if fields_str != "*":
                    parsed_fields = []
                    current_field = ""
                    in_quotes = False
                    quote_char = None

                    for char in fields_str:
                        if char in ("'", '"') and not in_quotes:
                            in_quotes = True
                            quote_char = char
                            current_field += char
                        elif char == quote_char and in_quotes:
                            in_quotes = False
                            quote_char = None
                            current_field += char
                        elif char == ',' and not in_quotes:
                            parsed_fields.append(current_field.strip())
                            current_field = ""
                        else:
                            current_field += char

                    if current_field.strip():
                        parsed_fields.append(current_field.strip())

                    for field in parsed_fields:
                        literal_match = re.match(r"^(['\"])(.+?)\1(\s+as\s+(['\"]?)(.+?)\4)?$", field, re.IGNORECASE)
                        if literal_match:
                            literal_value = literal_match.group(2)
                            alias = literal_match.group(5) if literal_match.group(5) else literal_value
                            literal_columns[alias] = literal_value
                        else:
                            if " as " in field.lower():
                                actual_field = field.split(" as ")[0].strip()
                                select_fields.append(actual_field)
                            else:
                                select_fields.append(field)

            where_clause = ""
            where_match = re.search(r"WHERE\s+(.*?)(?:ORDER BY|GROUP BY|LIMIT|$)", sql, re.IGNORECASE)
            if where_match:
                where_clause = where_match.group(1).strip()

            if where_clause and re.match(r"^\s*(1\s*=\s*0|0\s*=\s*1|false)\s*(and|or|&&|\|\|)?", where_clause, re.IGNORECASE):
                self.logger.info(f"Detected always-false condition '{where_clause}', returning empty result")
                return {"rows": [], "columns": []}

            limit = fetch_size
            limit_match = re.search(r"LIMIT\s+(\d+)", sql, re.IGNORECASE)
            if limit_match:
                limit = int(limit_match.group(1))

            filter_expr = ""
            if where_clause:
                filter_expr = where_clause
                filter_expr = re.sub(r"=", "==", filter_expr)
                filter_expr = re.sub(r"AND", "&&", filter_expr, flags=re.IGNORECASE)
                filter_expr = re.sub(r"OR", "||", filter_expr, flags=re.IGNORECASE)
                filter_expr = re.sub(r"NOT", "!", filter_expr, flags=re.IGNORECASE)

            conn = self._get_connection()
            output_fields = select_fields if select_fields else ["*"]
            results = conn.query(collection_name, expr=filter_expr, output_fields=output_fields, limit=limit)

            if format.lower() == "json":
                if not results and not literal_columns:
                    return {"rows": [], "columns": []}

                columns = []
                column_names = []

                if len(results) > 0:
                    column_names = list(results[0].keys())
                    for key in column_names:
                        columns.append({"name": key, "type": "text"})

                for literal_name in literal_columns.keys():
                    columns.append({"name": literal_name, "type": "text"})
                    column_names.append(literal_name)

                rows = []
                if len(results) > 0:
                    for result in results:
                        row = []
                        for col_name in list(results[0].keys()):
                            row.append(result.get(col_name))
                        for literal_name, literal_value in literal_columns.items():
                            row.append(literal_value)
                        rows.append(row)
                elif literal_columns:
                    row = [literal_columns[name] for name in literal_columns.keys()]
                    rows.append(row)

                return {"rows": rows, "columns": columns}
            else:
                return results

        except Exception as e:
            self.logger.error(f"SQL parsing or execution failed: {str(e)}")
            return {"error": f"SQL query failed: {str(e)}"}

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
            with open(mapping_path, "r", encoding="utf-8") as f:
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
            return {k: 1.0 for k in keys}

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
                        query_payload = expr.raw_text if hasattr(expr, "raw_text") else expr.matching_text
                        res = conn.search(
                            collection,
                            data=[query_payload],
                            anns_field=sparse_field,
                            param=search_params,
                            limit=expr_limit,
                            expression=filter_expr if filter_expr else None,
                            output_fields=select_fields,
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
                res = conn.search(
                    collection,
                    [vector_data],
                    vector_field,
                    search_params,
                    expression=filter_expr,
                    output_fields=select_fields,
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

        conn = self._get_connection()
        all_hits = []
        costs = []
        recalls = []

        expr_params = kwargs.pop("filter_params", {})

        for coll in collections:
            try:
                res = conn.search(
                    coll,
                    data,
                    anns_field or "",
                    search_params or {},
                    expression=filter,
                    limit=limit,
                    output_fields=output_fields,
                    partition_names=partition_names,
                    expr_params=expr_params,
                    timeout=timeout,
                    **kwargs,
                )
            except Exception as ex:
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

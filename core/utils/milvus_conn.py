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
Milvus connection for knowledge base operations.
This module provides a specialized Milvus connection for storing and retrieving knowledge base chunks.
"""

import copy
import json
import re
import time
from datetime import datetime

from pymilvus import Collection, Function
from pymilvus.client.constants import DEFAULT_CONSISTENCY_LEVEL
from pymilvus.client.types import OmitZeroDict, FunctionType
from pymilvus.client.abstract import AnnSearchRequest, BaseRanker
from pymilvus.milvus_client.index import IndexParams, IndexParam
from pymilvus.orm import utility
from pymilvus.orm.collection import CollectionSchema, FieldSchema
from pymilvus.orm.types import DataType

from common import settings
from common.constants import TAG_FLD, PAGERANK_FLD
from common.decorator import singleton
from common.doc_store.milvus_conn_base import MilvusConnectionBase
from common.doc_store.doc_store_base import (
    MatchExpr,
    MatchTextExpr,
    MatchDenseExpr,
    FusionExpr,
    OrderByExpr,
)
from common.float_utils import get_float
from common.string_utils import truncate_utf8_bytes

ATTEMPT_TIME = 2


@singleton
class MilvusConnection(MilvusConnectionBase):
    """Milvus connection for knowledge base operations."""

    def __init__(self):
        super().__init__(logger_name="multirag.milvus_conn")

    @staticmethod
    def field_keyword(field_name: str) -> bool:
        """Check if a field is a keyword field."""
        if field_name == "source_id" or (
            field_name.endswith("_kwd")
            and field_name != "docnm_kwd"
            and field_name != "knowledge_graph_kwd"
        ):
            return True
        return False

    """
    CRUD operations
    """

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
    ) -> tuple[list[dict], int]:
        """
        Execute search operation with vector search and condition filtering.

        Args:
            select_fields: Fields to return in results
            highlight_fields: Fields to highlight (application-level implementation)
            condition: Filter conditions
            match_expressions: Match expressions for text/vector search
            order_by: Order by expression
            offset: Pagination offset
            limit: Maximum number of results
            index_names: Index/collection name(s)
            dataset_ids: Dataset IDs to search in (maps to kb_id)
            agg_fields: Aggregation fields (application-level implementation)
            rank_feature: Rank feature adjustments
        """
        if isinstance(index_names, str):
            index_names = index_names.split(",")
        assert isinstance(index_names, list) and len(index_names) > 0

        all_results = []
        total_hits_count = 0

        # Build filter expression
        filter_expr = ""
        filter_parts = []
        if condition:
            for k, v in condition.items():
                if k == "pk" or not v:
                    continue
                if k == "doc_id":
                    if isinstance(v, list):
                        kb_exprs = [f"doc_id == '{kb}'" for kb in v]
                        filter_parts.append(f"({' || '.join(kb_exprs)})")
                    else:
                        filter_parts.append(f"doc_id == '{v}'")
                elif k == "available_int":
                    filter_parts.append(f"available_int != {v - 1}")
                elif k == "auth":
                    filter_parts.append(f"{v}")
                elif k == "content_with_weight":
                    filter_parts.append(f"{v}")
                elif k == "exists":
                    field_name = v
                    filter_parts.append(f'{field_name} != ""')
                elif k == "must_not":
                    if isinstance(v, dict):
                        for kk, vv in v.items():
                            if kk == "exists":
                                filter_parts.append(f'{vv} == ""')
                elif isinstance(v, list):
                    values = [f"'{item}'" if isinstance(item, str) else str(item) for item in v]
                    filter_parts.append(f"{k} in [{','.join(values)}]")
                elif isinstance(v, str):
                    filter_parts.append(f"{k} == '{v}'")
                elif isinstance(v, (int, float)):
                    filter_parts.append(f"{k} == {v}")

            if filter_parts:
                filter_expr = " && ".join(filter_parts)
            else:
                filter_expr = "pk != ''"

        text_exprs: list[MatchTextExpr] = []
        dense_expr: MatchDenseExpr | None = None
        fusion_expr: FusionExpr | None = None

        for expr in match_expressions:
            if isinstance(expr, MatchTextExpr):
                text_exprs.append(expr)
            elif isinstance(expr, MatchDenseExpr):
                dense_expr = expr
            elif isinstance(expr, FusionExpr):
                fusion_expr = expr

        # Build rank boost parameters
        rank_boost = {}
        if rank_feature:
            for field, score in rank_feature.items():
                if field != PAGERANK_FLD:
                    field = f"{TAG_FLD}.{field}"
                rank_boost[field] = score

        has_retrieval = bool(text_exprs or dense_expr)

        if not has_retrieval:
            # Query-only mode without vector search
            for index_name in index_names:
                collection_name = index_name
                try:
                    conn = self._get_connection()
                    if not conn.has_collection(collection_name):
                        continue

                    results = conn.query(
                        collection_name,
                        filter_expr,
                        output_fields=select_fields,
                        limit=10000
                    )

                    if results:
                        if rank_boost and PAGERANK_FLD in select_fields:
                            for result in results:
                                pagerank = get_float(result.get(PAGERANK_FLD, 0))
                                result["SCORE"] = pagerank * rank_boost.get(PAGERANK_FLD, 1.0)
                            results = sorted(results, key=lambda x: x.get("SCORE", 0), reverse=True)

                        all_results.extend(results)
                        total_hits_count += len(results)
                except Exception as e:
                    self.logger.warning(f"Query collection {collection_name} failed: {str(e)}")

            if all_results:
                if offset > 0:
                    all_results = all_results[offset:]
                if limit > 0:
                    all_results = all_results[:limit]
                return all_results, total_hits_count
            else:
                return [], 0
        else:
            combined_results, total_hits_count = self._search_with_text_and_dense(
                select_fields=select_fields,
                filter_expr=filter_expr,
                text_exprs=text_exprs,
                dense_expr=dense_expr,
                fusion_expr=fusion_expr,
                index_names=index_names,
                limit=limit,
                offset=offset,
                rank_boost=rank_boost or {},
            )
            return combined_results, total_hits_count

    def get(self, doc_id: str, index_name: str | list[str], dataset_ids: list[str]) -> dict | None:
        """Get a single document chunk by ID."""
        if isinstance(index_name, list):
            if not index_name:
                self.logger.error("Index name list is empty")
                return None
            collection_name = index_name[0]
            self.logger.debug(f"Index name is a list, using first element: {collection_name}")
        elif isinstance(index_name, str):
            collection_name = index_name
        else:
            self.logger.error(f"Index name must be string or list of strings, got: {type(index_name)}")
            return None

        conn = self._get_connection()
        try:
            if not conn.has_collection(collection_name):
                return None

            filter_expr = f"pk == '{doc_id}'"
            results = conn.query(collection_name, filter_expr, output_fields=["*"])

            if results and len(results) > 0:
                return results[0]
        except Exception as e:
            self.logger.warning(f"Get document {doc_id} from {collection_name} failed: {str(e)}")

        return None

    def insert(
        self,
        documents: list[dict] | dict | str,
        index_name: str | list[str] | dict | list[dict],
        dataset_id: str | None = None
    ) -> list[str]:
        """
        Insert documents into collection.

        Supports two calling conventions:
        1) Standard: insert(documents, index_name, dataset_id)
        2) Legacy: insert(collection_name, data, partition_name) - auto-corrected
        """
        # Handle legacy calling convention: insert(collection_name, data, partition_name)
        if isinstance(documents, str) and (
            isinstance(index_name, dict)
            or (isinstance(index_name, list) and (len(index_name) == 0 or isinstance(index_name[0], dict)))
        ):
            collection_name = documents
            data = index_name
            partition_name = dataset_id
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                msg = f"Wrong type of argument 'data', expected 'Dict' or list of 'Dict', got '{type(data).__name__}'"
                raise TypeError(msg)
            documents = data
            index_name = collection_name
            dataset_id = partition_name

        if isinstance(documents, dict):
            documents = [documents]

        if not isinstance(documents, list):
            msg = f"Wrong type of argument 'documents', expected 'Dict' or list of 'Dict', got '{type(documents).__name__}'"
            raise TypeError(msg)

        # Handle index name parameter
        if isinstance(index_name, list):
            if not index_name:
                self.logger.error("Index name list is empty")
                return []
            collection_name = index_name[0]
            self.logger.debug(f"Index name is a list, using first element: {collection_name}")
        elif isinstance(index_name, str):
            collection_name = index_name
        else:
            self.logger.error(f"Index name must be string or list of strings, got: {type(index_name)}")
            return []

        conn = self._get_connection()

        # Create collection if not exists
        if not conn.has_collection(collection_name):
            vector_size = 0
            pattern = re.compile(r"q_(\d+)_vec")

            for row in documents:
                for key in row.keys():
                    match = pattern.match(key)
                    if match:
                        vector_size = int(match.group(1))
                        break
                if vector_size > 0:
                    break

            if vector_size == 0:
                raise ValueError(f"Cannot determine vector dimension from data, cannot create collection {collection_name}")

            self.create_idx(index_name, dataset_id, vector_size)

        # Preprocess documents
        processed_rows = []
        for row in documents:
            new_row = copy.deepcopy(row)

            # Ensure primary key field
            if "pk" not in new_row and "id" not in new_row:
                raise ValueError("Insert requires primary key 'pk' or 'id'")
            if "pk" not in new_row:
                new_row["pk"] = new_row["id"]
            if "id" not in new_row:
                new_row["id"] = new_row["pk"]

            # Handle special fields
            if "kb_id" in new_row and isinstance(new_row["kb_id"], list):
                new_row["kb_id"] = new_row["kb_id"][0] if new_row["kb_id"] else ""

            # source_id is stored as VARCHAR; serialize list → newline-delimited string
            if "source_id" in new_row and isinstance(new_row["source_id"], list):
                new_row["source_id"] = "\n".join(new_row["source_id"])

            # Handle position fields
            if "position_int" in new_row and isinstance(new_row["position_int"], list):
                flat_array = [num for row in new_row["position_int"] for num in row]
                new_row["position_int"] = "_".join(f"{num:08x}" for num in flat_array)

            for pos_field in ["page_num_int", "top_int"]:
                if pos_field in new_row and isinstance(new_row[pos_field], list):
                    new_row[pos_field] = "_".join(f"{num:08x}" for num in new_row[pos_field])

            processed_rows.append(new_row)

        # Fill defaults for every non-nullable schema field that is absent from a row.
        # This handles mixed-type chunk batches (e.g. graph/subgraph chunks alongside
        # entity/relation chunks) without requiring callers to know the full schema.
        #
        # conn.describe_collection() returns a plain dict (GrpcHandler calls .dict()).
        # Field entries: {"name":..., "type": DataType, "params":{...}, "nullable": True, ...}
        # "nullable" / "is_primary" / "is_function_output" are only present when True.
        try:
            schema_dict = conn.describe_collection(collection_name)
            for fld in schema_dict.get("fields", []):
                # Skip primary keys, auto-generated BM25 outputs, and nullable fields —
                # Milvus only rejects inserts for non-nullable fields with missing values.
                if fld.get("is_primary") or fld.get("is_function_output") or fld.get("nullable"):
                    continue
                name = fld["name"]
                ftype = fld.get("type")
                if ftype == DataType.FLOAT_VECTOR:
                    dim = int(fld.get("params", {}).get("dim", 0))
                    if dim > 0:
                        zero_vec = [0.0] * dim
                        for row in processed_rows:
                            if name not in row:
                                row[name] = zero_vec
                elif ftype == DataType.VARCHAR:
                    max_len = int(fld.get("params", {}).get("max_length", 65535))
                    for row in processed_rows:
                        if name not in row:
                            row[name] = ""
                        elif isinstance(row[name], str) and len(row[name].encode()) > max_len:
                            row[name] = truncate_utf8_bytes(row[name], max_len)
                elif ftype == DataType.FLOAT:
                    for row in processed_rows:
                        if name not in row:
                            row[name] = 0.0
                elif ftype == DataType.INT64:
                    for row in processed_rows:
                        if name not in row:
                            row[name] = 0
                elif ftype == DataType.JSON:
                    for row in processed_rows:
                        if name not in row:
                            row[name] = "{}"
                elif ftype == DataType.ARRAY:
                    for row in processed_rows:
                        if name not in row:
                            row[name] = []
        except Exception:
            pass

        # Execute insert
        try:
            ids = ["'{}'".format(row["id"]) for row in processed_rows]
            if ids:
                id_filter = f"id in [{','.join(ids)}]"
                try:
                    conn.delete(collection_name, expression=id_filter)
                except Exception as e:
                    self.logger.debug(f"Delete existing records failed for {collection_name}: {str(e)}")

            res = conn.insert_rows(collection_name, processed_rows)
            self.logger.debug(f"Successfully inserted {res.insert_count} records into {collection_name}")
            return []
        except Exception as e:
            error_msg = str(e)
            self.logger.error(f"Insert into {collection_name} failed: {error_msg}")
            return [error_msg]

    def update(self, condition: dict, new_value: dict, index_name: str | list[str], dataset_id: str) -> bool:
        """
        Update documents in Milvus.
        Uses query-delete-insert pattern as Milvus doesn't support direct updates.
        """
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

        conn = self._get_connection()

        doc = copy.deepcopy(new_value)
        doc.pop("pk", None)
        doc.pop("id", None)

        # Build filter expression
        filter_parts = []
        chunk_id = condition.get("id") or condition.get("pk")
        if chunk_id and isinstance(chunk_id, str):
            filter_parts.append(f"pk == '{chunk_id}'")
        else:
            for k, v in condition.items():
                if k == "_id" or not v:
                    continue
                if k == "exists":
                    self.logger.warning(f"Milvus doesn't support exists query: {k}={v}, this condition will be ignored")
                    continue
                if isinstance(v, list):
                    values = [f"'{item}'" if isinstance(item, str) else str(item) for item in v]
                    filter_parts.append(f"{k} in [{','.join(values)}]")
                elif isinstance(v, str):
                    filter_parts.append(f"{k} == '{v}'")
                elif isinstance(v, (int, float)):
                    filter_parts.append(f"{k} == {v}")
                else:
                    self.logger.warning(f"Condition `{k}={v}` type {type(v)} not supported, will be ignored")

        if not filter_parts:
            self.logger.warning("No valid query conditions")
            return False

        filter_expr = " && ".join(filter_parts)

        # Handle special operations (remove and add)
        remove_fields = []
        add_operations = {}

        if "remove" in doc:
            remove_val = doc.pop("remove")
            if isinstance(remove_val, str):
                remove_fields.append(remove_val)
            elif isinstance(remove_val, dict):
                for k, v in remove_val.items():
                    self.logger.warning(f"Milvus doesn't support removing specific elements from list: {k}={v}, will be ignored")

        if "add" in doc:
            add_val = doc.pop("add")
            if isinstance(add_val, dict):
                add_operations = add_val

        # Retry mechanism
        for attempt in range(ATTEMPT_TIME):
            try:
                results = conn.query(collection_name, expr=filter_expr, output_fields=["*"])

                if not results:
                    self.logger.warning(f"No records found matching condition: {filter_expr}")
                    return False

                updated_records = []
                current_time = datetime.now()
                time_str = current_time.strftime("%Y-%m-%d %H:%M:%S")
                timestamp = current_time.timestamp()

                for record in results:
                    # Handle remove fields
                    for field in remove_fields:
                        if field in record:
                            if field == "pagerank_fea":
                                record[field] = 0.0
                            elif field.endswith("_int"):
                                record[field] = 0
                            elif field.endswith("_flt"):
                                record[field] = 0.0
                            elif isinstance(record[field], list):
                                record[field] = []
                            elif isinstance(record[field], str):
                                record[field] = ""

                    # Handle add operations
                    for field, value in add_operations.items():
                        if field in record and isinstance(record[field], list):
                            if value not in record[field]:
                                record[field].append(value)
                        elif field in record and isinstance(record[field], str):
                            record[field] += value.strip()
                        else:
                            record[field] = value

                    updated_record = {**record, **doc}
                    updated_record["create_time"] = time_str
                    updated_record["create_timestamp_flt"] = timestamp
                    updated_records.append(updated_record)

                try:
                    delete_res = conn.delete(collection_name, expression=filter_expr)
                    self.logger.debug(f"Deleted {getattr(delete_res, 'delete_count', 0)} records")
                except Exception as e:
                    self.logger.error(f"Delete records failed: {str(e)}")
                    return False

                if updated_records:
                    try:
                        insert_res = conn.insert_rows(collection_name, updated_records)
                        self.logger.debug(f"Inserted {getattr(insert_res, 'insert_count', len(updated_records))} updated records")
                        return True
                    except Exception as e:
                        self.logger.error(f"Insert updated records failed: {str(e)}")
                        return False

                return True

            except Exception as e:
                self.logger.error(f"Update operation failed (attempt {attempt + 1}/{ATTEMPT_TIME}): {str(e)}")
                if re.search(r"(timeout|connection|conflict)", str(e).lower()):
                    time.sleep(1)
                    continue
                break

        return False

    def delete(self, condition: dict, index_name: str | list[str], dataset_id: str | None = None) -> int:
        """Delete documents from collection."""
        if isinstance(index_name, list):
            if not index_name:
                self.logger.error("Index name list is empty")
                return 0
            collection_name = index_name[0]
            self.logger.debug(f"Index name is a list, using first element: {collection_name}")
        elif isinstance(index_name, str):
            collection_name = index_name
        else:
            self.logger.error(f"Index name must be string or list of strings, got: {type(index_name)}")
            return 0

        conn = self._get_connection()

        if not conn.has_collection(collection_name):
            self.logger.warning(f"Collection {collection_name} does not exist")
            return 0

        # Mirror ES behaviour: inject kb_id from dataset_id into the condition so that
        # the delete is always scoped to the correct knowledge base.
        condition = dict(condition)
        if dataset_id and "kb_id" not in condition:
            condition["kb_id"] = dataset_id

        # Build filter expression - use "id" field preferentially (consistent with ES)
        filter_expr = ""
        chunk_ids = condition.get("id") or condition.get("pk")
        if chunk_ids:
            if not isinstance(chunk_ids, list):
                chunk_ids = [chunk_ids]
            id_strs = [f"'{id}'" for id in chunk_ids]
            filter_expr = f"pk in [{','.join(id_strs)}]"
        else:
            filter_parts = []
            for k, v in condition.items():
                if k in ("pk", "id") or not v:
                    continue
                if isinstance(v, list):
                    values = [f"'{item}'" if isinstance(item, str) else str(item) for item in v]
                    filter_parts.append(f"{k} in [{','.join(values)}]")
                elif isinstance(v, str):
                    filter_parts.append(f"{k} == '{v}'")
                elif isinstance(v, (int, float)):
                    filter_parts.append(f"{k} == {v}")

            if filter_parts:
                filter_expr = " && ".join(filter_parts)

        if not filter_expr:
            self.logger.error("Delete operation requires valid conditions")
            return 0

        try:
            res = conn.delete(collection_name, expression=filter_expr)
            return res.delete_count
        except Exception as e:
            self.logger.error(f"Delete from {collection_name} failed: {str(e)}")
            return 0

    """
    Helper functions for search result
    """

    def get_fields(self, res, fields: list[str]) -> dict[str, dict]:
        """Get field values from results, handling Milvus version compatibility."""
        result = {}
        result["distance"] = []

        if isinstance(res, tuple):
            results = res[0]
        else:
            results = res

        if not isinstance(results, list):
            return {}

        for item in results:
            doc_id = None
            if "pk" in item:
                doc_id = item.get("pk")
            elif "id" in item:
                doc_id = item.get("id")

            if doc_id is None:
                continue

            row_dict = {}

            for field in fields:
                if field == "_score":
                    value = get_float(item.get("distance", item.get("score", 0.0)))
                    row_dict[field] = value
                    continue

                if field in item:
                    value = item.get(field)
                elif "entity" in item and isinstance(item["entity"], dict) and field in item["entity"]:
                    value = item["entity"].get(field)
                else:
                    continue

                # Special field processing - 处理关键词和问题列表字段
                if field == "source_id":
                    if isinstance(value, list):
                        value = [str(v) for v in value if v]
                    elif isinstance(value, str):
                        value = [v for v in value.split("\n") if v]
                    else:
                        value = []
                elif field in ["important_kwd", "question_kwd", "entities_kwd"]:
                    if isinstance(value, list):
                        # ARRAY 类型直接返回列表，确保元素是字符串
                        value = [str(v).strip() for v in value if v]
                    elif isinstance(value, str):
                        # VARCHAR 类型用换行符分隔以保持与存储时一致
                        value = [v.strip() for v in value.split("\n") if v.strip()] if value else []
                    else:
                        value = []
                elif field == "position_int" and isinstance(value, str):
                    if value:
                        try:
                            if value.startswith('[') and value.endswith(']'):
                                value = eval(value) if value != '[]' else []
                            else:
                                arr = [int(hex_val, 16) for hex_val in value.split('_')]
                                value = [arr[i:i + 5] for i in range(0, len(arr), 5)]
                        except Exception:
                            value = []
                    else:
                        value = []
                elif field in ["page_num_int", "top_int"] and isinstance(value, str):
                    if value:
                        try:
                            if value.startswith('[') and value.endswith(']'):
                                value = eval(value) if value != '[]' else []
                            else:
                                value = [int(hex_val, 16) for hex_val in value.split('_')]
                        except Exception:
                            value = []
                    else:
                        value = []

                row_dict[field] = value

            if "pk" not in row_dict and "id" not in row_dict:
                if "pk" in item:
                    row_dict["pk"] = item["pk"]
                elif "id" in item:
                    row_dict["id"] = item["id"]

            if row_dict:
                result[doc_id] = row_dict
                if "distance" in item:
                    result["distance"].append(item["distance"])

        return result

    """
    Collection management
    """

    def create_collection(
        self,
        collection_name: str,
        dimension: int | None = None,
        primary_field_name: str = "id",
        id_type: str = "int",
        vector_field_name: str = "vector",
        metric_type: str = "COSINE",
        auto_id: bool = False,
        timeout: float | None = None,
        schema: CollectionSchema | None = None,
        index_params: IndexParams | None = None,
        **kwargs,
    ):
        """Create a Milvus collection with specified parameters."""
        if schema is None:
            return self._fast_create_collection(
                collection_name,
                dimension,
                primary_field_name=primary_field_name,
                id_type=id_type,
                vector_field_name=vector_field_name,
                metric_type=metric_type,
                auto_id=auto_id,
                timeout=timeout,
                **kwargs,
            )

        return self._create_collection_with_schema(
            collection_name, schema, index_params, timeout=timeout, **kwargs
        )

    def _fast_create_collection(
        self,
        collection_name: str,
        dimension: int,
        primary_field_name: str = "id",
        id_type: DataType | str = DataType.INT64,
        vector_field_name: str = "vector",
        metric_type: str = "COSINE",
        auto_id: bool = False,
        timeout: float | None = None,
        **kwargs,
    ):
        """Fast create collection with minimal configuration."""
        from pymilvus.exceptions import PrimaryKeyException
        from pymilvus.client.types import ExceptionsMessage

        if dimension is None:
            msg = "Missing required argument: 'dimension'"
            raise TypeError(msg)
        if "enable_dynamic_field" not in kwargs:
            kwargs["enable_dynamic_field"] = True

        schema = self.create_schema(auto_id=auto_id, **kwargs)

        if id_type in ("int", DataType.INT64):
            pk_data_type = DataType.INT64
        elif id_type in ("string", "str", DataType.VARCHAR):
            pk_data_type = DataType.VARCHAR
        else:
            raise PrimaryKeyException(message=ExceptionsMessage.PrimaryFieldType)

        pk_args = {}
        if "max_length" in kwargs and pk_data_type == DataType.VARCHAR:
            pk_args["max_length"] = kwargs["max_length"]

        schema.add_field(primary_field_name, pk_data_type, is_primary=True, **pk_args)
        schema.add_field(vector_field_name, DataType.FLOAT_VECTOR, dim=dimension)
        schema.verify()

        conn = self._get_connection()
        if "consistency_level" not in kwargs:
            kwargs["consistency_level"] = DEFAULT_CONSISTENCY_LEVEL
        try:
            conn.create_collection(collection_name, schema, timeout=timeout, **kwargs)
            self.logger.debug(f"Successfully created collection: {collection_name}")
        except Exception as ex:
            self.logger.error(f"Failed to create collection: {collection_name}")
            raise ex from ex

        index_params = IndexParams()
        index_params.add_index(vector_field_name, index_type="AUTOINDEX", metric_type=metric_type)
        self.create_index(collection_name, index_params, timeout=timeout)
        self.load_collection(collection_name, timeout=timeout)

    def _create_collection_with_schema(
        self,
        collection_name: str,
        schema: CollectionSchema,
        index_params: IndexParams | None = None,
        timeout: float | None = None,
        **kwargs,
    ):
        """Create collection with specified schema."""
        schema.verify()

        conn = self._get_connection()
        if "consistency_level" not in kwargs:
            kwargs["consistency_level"] = DEFAULT_CONSISTENCY_LEVEL
        try:
            conn.create_collection(collection_name, schema, timeout=timeout, **kwargs)
            self.logger.debug(f"Successfully created collection: {collection_name}")
        except Exception as ex:
            self.logger.error(f"Failed to create collection: {collection_name}")
            raise ex from ex

        if index_params:
            self.create_index(collection_name, index_params, timeout=timeout)
            self.load_collection(collection_name, timeout=timeout)

    def create_index(
        self,
        collection_name: str,
        index_params: IndexParams | dict,
        timeout: float | None = None,
        **kwargs,
    ):
        """Create index on collection."""
        if isinstance(index_params, IndexParams):
            for ip in index_params:
                self._create_index(collection_name, ip, timeout=timeout, **kwargs)
            return

        if isinstance(index_params, IndexParam):
            self._create_index(collection_name, index_params, timeout=timeout, **kwargs)
            return

        if isinstance(index_params, (list, tuple)):
            for ip in index_params:
                self._create_index(collection_name, ip, timeout=timeout, **kwargs)
            return

        self._create_index(collection_name, index_params, timeout=timeout, **kwargs)

    def _create_index(
        self, collection_name: str, index_param, timeout: float | None = None, **kwargs
    ):
        """Create single index on collection."""
        conn = self._get_connection()
        try:
            if isinstance(index_param, IndexParam):
                field_name = index_param.field_name
                index_name = index_param.index_name or field_name
                params = index_param.get_index_configs()
            else:
                field_name = index_param.get("field_name")
                index_name = index_param.get("index_name", field_name)
                params = {k: v for k, v in index_param.items() if k not in ("field_name", "index_name")}

            conn.create_index(
                collection_name,
                field_name,
                params,
                timeout=timeout,
                index_name=index_name,
                **kwargs,
            )
            self.logger.debug(f"Successfully created index on collection: {collection_name}")
        except Exception as ex:
            self.logger.error(f"Failed to create index on collection: {collection_name}")
            raise ex from ex

    def hybrid_search(
        self,
        collection_name: str | list[str],
        reqs: list[AnnSearchRequest],
        ranker: BaseRanker,
        limit: int = 10,
        output_fields: list[str] | None = None,
        timeout: float | None = None,
        partition_names: list[str] | None = None,
        **kwargs,
    ) -> list[list[dict]]:
        """Perform hybrid search across multiple collections."""
        collections = [collection_name] if isinstance(collection_name, str) else collection_name

        conn = self._get_connection()
        all_hits = []
        costs = []

        for coll in collections:
            try:
                res = conn.hybrid_search(
                    coll,
                    reqs,
                    ranker,
                    limit=limit,
                    partition_names=partition_names,
                    output_fields=output_fields,
                    timeout=timeout,
                    **kwargs,
                )
            except Exception as ex:
                self.logger.error(f"Hybrid search collection failed: {coll}", exc_info=True)
                continue

            for hits in res:
                all_hits.extend(hits)

            if hasattr(res, "cost"):
                costs.append(res.cost)

        all_hits.sort(key=lambda h: h.distance, reverse=True)
        top_hits = all_hits[:limit]
        result = [h.to_dict() for h in top_hits]

        if costs:
            from pymilvus.client.types import construct_cost_extra
            extras = [construct_cost_extra(c) for c in costs]
            extra_val = extras[0] if len(extras) == 1 else extras
            return type("ExtraList", (list,), {"extra": extra_val})(result)

        return result

    def upsert(
        self,
        collection_name: str,
        data: dict | list[dict],
        timeout: float | None = None,
        partition_name: str | None = None,
        **kwargs,
    ) -> dict:
        """Upsert data into collection."""
        if isinstance(data, dict):
            data = [data]

        msg = f"Wrong type of argument 'data', expected 'Dict' or list of 'Dict', got '{type(data).__name__}'"

        if not isinstance(data, list):
            raise TypeError(msg)

        if len(data) == 0:
            return {"upsert_count": 0}

        conn = self._get_connection()
        try:
            res = conn.upsert_rows(
                collection_name, data, partition_name=partition_name, timeout=timeout, **kwargs
            )
        except Exception as ex:
            raise ex from ex

        return OmitZeroDict(
            {
                "upsert_count": res.upsert_count,
                "cost": res.cost,
            }
        )

    def create_collection_with_mapping(self, collection_name: str, mapping: dict, auto_dimensions: dict | None = None):
        """
        Create Milvus collection based on mapping configuration.
        This method is idempotent - returns early if collection already exists.
        """
        if self.has_collection(collection_name):
            self.logger.info(f"Collection {collection_name} already exists, skipping creation")
            return

        dynamic_templates = mapping.get("mappings", {}).get("dynamic_templates", [])
        fields = []

        if auto_dimensions is None:
            auto_dimensions = {}

        bm25_configs = []
        bm25_config_map = {}
        existing_field_names = set()

        for template in dynamic_templates:
            for key, value in template.items():
                match_pattern = value.get("match_pattern", "")
                if match_pattern == "regex":
                    continue

                match = value.get("match", "")
                mapping_cfg = value.get("mapping", {})
                mapping_type = mapping_cfg.get("type", "")

                if mapping_type == "FLOAT_VECTOR":
                    dims = mapping_cfg.get("dims", 1024)
                    if match in auto_dimensions:
                        dims = auto_dimensions.get(match, 1024)
                    fields.append(FieldSchema(name=match, dtype=DataType.FLOAT_VECTOR, dim=dims))
                    existing_field_names.add(match)
                elif mapping_type == "VARCHAR":
                    max_length = mapping_cfg.get("max_length", 256)
                    is_primary = mapping_cfg.get("is_primary", False)
                    bm25_cfg = mapping_cfg.get("bm25")
                    nullable_value = not is_primary
                    if bm25_cfg and bm25_cfg.get("enable"):
                        nullable_value = False
                    enable_analyzer = mapping_cfg.get("enable_analyzer", False)
                    enable_match = mapping_cfg.get("enable_match", False)
                    analyzer_params = mapping_cfg.get("analyzer_params")

                    field_kwargs = {
                        "name": match,
                        "dtype": DataType.VARCHAR,
                        "max_length": max_length,
                        "is_primary": is_primary,
                        "nullable": nullable_value,
                    }
                    if enable_analyzer:
                        field_kwargs["enable_analyzer"] = True
                    if analyzer_params:
                        field_kwargs["analyzer_params"] = analyzer_params
                    if enable_match:
                        field_kwargs["enable_match"] = True

                    fields.append(FieldSchema(**field_kwargs))
                    existing_field_names.add(match)

                    if bm25_cfg and bm25_cfg.get("enable"):
                        sparse_field = bm25_cfg.get("output_field", f"{match}_sparse")
                        cfg_entry = {
                            "input_field": match,
                            "sparse_field": sparse_field,
                            "search_params": copy.deepcopy(bm25_cfg.get("search_params", {"drop_ratio_search": 0.1})),
                            "index_params": copy.deepcopy(bm25_cfg.get("index_params", {
                                "inverted_index_algo": "DAAT_WAND",
                                "bm25_k1": 1.5,
                                "bm25_b": 0.75
                            }))
                        }
                        bm25_configs.append(cfg_entry)
                        bm25_config_map[sparse_field] = cfg_entry
                elif mapping_type == "FLOAT":
                    fields.append(FieldSchema(name=match, dtype=DataType.FLOAT, nullable=True))
                    existing_field_names.add(match)
                elif mapping_type == "INT64":
                    fields.append(FieldSchema(name=match, dtype=DataType.INT64, nullable=True))
                    existing_field_names.add(match)
                elif mapping_type == "JSON":
                    fields.append(FieldSchema(name=match, dtype=DataType.JSON, nullable=True))
                    existing_field_names.add(match)
                elif mapping_type == "ARRAY":
                    element_type = mapping_cfg.get("element_type", DataType.VARCHAR)
                    max_length = mapping_cfg.get("max_length", 256)
                    max_capacity = mapping_cfg.get("max_capacity", 4096)
                    fields.append(FieldSchema(
                        name=match,
                        dtype=DataType.ARRAY,
                        element_type=getattr(DataType, element_type) if isinstance(element_type, str) else element_type,
                        max_length=max_length,
                        max_capacity=max_capacity,
                        nullable=True
                    ))
                    existing_field_names.add(match)

        # Add dimension-specific vector fields
        for vector_field, dim in auto_dimensions.items():
            if vector_field in existing_field_names:
                continue
            if re.match(r'q_\d+_vec', vector_field):
                fields.append(FieldSchema(name=vector_field, dtype=DataType.FLOAT_VECTOR, dim=dim))
                existing_field_names.add(vector_field)

        # Add BM25 sparse vector fields
        for cfg in bm25_configs:
            if cfg["sparse_field"] in existing_field_names:
                continue
            fields.append(FieldSchema(
                name=cfg["sparse_field"],
                dtype=DataType.SPARSE_FLOAT_VECTOR
            ))
            existing_field_names.add(cfg["sparse_field"])

        # Create collection schema
        schema = CollectionSchema(
            fields=fields,
            description="Created from mapping.json, supports hybrid search",
            enable_dynamic_field=True
        )
        for cfg in bm25_configs:
            bm25_function = Function(
                name=f"bm25_function_{cfg['sparse_field']}",
                function_type=FunctionType.BM25,
                input_field_names=[cfg["input_field"]],
                output_field_names=cfg["sparse_field"],
            )
            schema.add_function(bm25_function)

        self.create_collection(collection_name, schema=schema)
        self.logger.info(f"Successfully created collection: {collection_name} with fields: {[field.name for field in fields]}")

        # Create indexes
        for field in fields:
            if field.dtype == DataType.FLOAT_VECTOR:
                index_params = IndexParams()
                index_params.add_index(
                    field.name,
                    "IVF_FLAT",
                    field.name,
                    metric_type="COSINE",
                    params={"nlist": 128}
                )
                try:
                    self.create_index(collection_name, index_params)
                    self.logger.info(f"Index created | collection: {collection_name} | index config: {index_params}")
                except Exception:
                    self.logger.exception(f"Index creation failed | collection: {collection_name} | index config: {index_params}")
            elif field.dtype == DataType.SPARSE_FLOAT_VECTOR:
                sparse_index_params = IndexParams()
                cfg = bm25_config_map.get(field.name, {})
                idx_params = cfg.get("index_params", {})
                sparse_index_params.add_index(
                    field.name,
                    "SPARSE_INVERTED_INDEX",
                    field.name,
                    metric_type=idx_params.get("metric_type", "BM25"),
                    params={
                        "inverted_index_algo": idx_params.get("inverted_index_algo", "DAAT_WAND"),
                        "bm25_k1": idx_params.get("bm25_k1", 1.5),
                        "bm25_b": idx_params.get("bm25_b", 0.75),
                    }
                )
                try:
                    self.create_index(collection_name, sparse_index_params)
                    self.logger.info(f"Sparse index created | collection: {collection_name} | field: {field.name}")
                except Exception:
                    self.logger.exception(f"Sparse index creation failed | collection: {collection_name} | field: {field.name}")

        # Load collection
        try:
            self.load_collection(collection_name)
            self.logger.info(f"Collection {collection_name} loaded")
        except Exception as e:
            self.logger.warning(f"Collection {collection_name} load failed: {str(e)}")

    def get_cluster_stats(self):
        """Get Milvus cluster statistics."""
        try:
            from common.misc_utils import convert_bytes

            server_version = utility.get_server_version(using=self._using)
            server_type = utility.get_server_type(using=self._using)

            res = {
                'cluster_name': settings.MILVUS.get("hosts", "milvus"),
                'status': 'green',
                'server_version': server_version,
                'server_type': server_type,
                'is_self_hosted': self.is_self_hosted
            }

            # Get resource groups info
            try:
                resource_groups = utility.list_resource_groups(using=self._using)
                res['resource_groups'] = resource_groups
                res['resource_groups_count'] = len(resource_groups)
            except Exception as e:
                self.logger.debug(f"Cannot get resource groups (may be cloud version): {e}")
                res['resource_groups'] = []
                res['resource_groups_count'] = 0

            # Get all collections
            collections = self.list_collections()
            res['collections'] = len(collections)

            # Collection statistics
            total_rows = 0
            total_fields = 0
            total_shards = 0
            total_partitions = 0
            total_indexes = 0
            total_replicas = 0
            total_loaded_collections = 0
            collection_details = []

            for collection_name in collections:
                try:
                    coll = Collection(name=collection_name, using=self._using)
                    
                    # Get stats
                    conn = self._get_connection()
                    stats = conn.get_collection_stats(collection_name)
                    stat_dict = {stat.key: stat.value for stat in stats}
                    row_count = int(stat_dict.get('row_count', 0))
                    total_rows += row_count

                    # Collection description
                    desc = self.describe_collection(collection_name)
                    field_count = len(desc.get('fields', []))
                    total_fields += field_count

                    # Shards
                    num_shards = coll.num_shards
                    total_shards += num_shards

                    # Partitions
                    partitions = coll.partitions
                    partition_count = len(partitions)
                    total_partitions += partition_count

                    # Indexes
                    indexes = coll.indexes
                    index_count = len(indexes)
                    total_indexes += index_count

                    index_details = []
                    for idx in indexes:
                        try:
                            index_details.append({
                                'field_name': idx.field_name,
                                'index_type': idx.params.get('index_type', 'Unknown'),
                                'metric_type': idx.params.get('metric_type', 'Unknown')
                            })
                        except Exception:
                            pass

                    # Load status
                    try:
                        load_status = utility.load_state(collection_name, using=self._using)
                        is_loaded = (load_status.name == 'Loaded')
                        if is_loaded:
                            total_loaded_collections += 1
                    except Exception:
                        load_status = None
                        is_loaded = False

                    # Replicas
                    replica_info = []
                    replica_count = 0
                    try:
                        if is_loaded:
                            replicas = coll.get_replicas()
                            replica_count = len(replicas.groups)
                            total_replicas += replica_count
                            for replica in replicas.groups:
                                replica_info.append({
                                    'replica_id': replica.id,
                                    'shard_count': len(replica.shards),
                                    'node_ids': replica.node_ids
                                })
                    except Exception as e:
                        self.logger.debug(f"Get replicas for {collection_name} failed: {e}")

                    collection_details.append({
                        'name': collection_name,
                        'row_count': row_count,
                        'field_count': field_count,
                        'shard_count': num_shards,
                        'partition_count': partition_count,
                        'index_count': index_count,
                        'indexes': index_details,
                        'is_loaded': is_loaded,
                        'load_state': load_status.name if load_status else 'Unknown',
                        'replica_count': replica_count,
                        'replicas': replica_info,
                    })

                except Exception as e:
                    self.logger.warning(f"Get stats for collection {collection_name} failed: {e}")
                    continue

            res.update({
                'total_rows': total_rows,
                'total_fields': total_fields,
                'total_shards': total_shards,
                'total_partitions': total_partitions,
                'total_indexes': total_indexes,
                'total_replicas': total_replicas,
                'loaded_collections': total_loaded_collections,
                'collection_details': collection_details
            })

            self.logger.debug(f"MilvusConnection.get_cluster_stats: {res}")
            return res

        except Exception as e:
            self.logger.exception(f"MilvusConnection.get_cluster_stats: {e}")
            return None

    @classmethod
    def prepare_index_params(cls, field_name: str = "", **kwargs) -> IndexParams:
        """Prepare index parameters."""
        from pymilvus.exceptions import ParamError

        index_params = IndexParams()
        if field_name:
            if not isinstance(field_name, str):
                raise ParamError(message=f"Wrong type of argument 'field_name', expected str, got {type(field_name).__name__}")
            index_params.add_index(field_name, **kwargs)
        return index_params

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
Milvus connection for message storage.
This module provides a specialized Milvus connection for storing and retrieving messages.
"""

import json
import re
from collections.abc import Callable
from typing import Any

from pymilvus import Collection, CollectionSchema, DataType, FieldSchema
from pymilvus.orm import utility

from common.decorator import singleton
from common.doc_store.doc_store_base import MatchDenseExpr, MatchExpr, OrderByExpr
from common.doc_store.milvus_capabilities import supports_native_query_order_by, supports_verified_milvus_3
from common.doc_store.milvus_conn_base import MilvusConnectionBase
from memory.utils.milvus_read_preparation import MilvusReadPreparation, SourceQuery


def _message_collection_name(index_name: str | list[str], memory_id: str) -> str:
    if isinstance(index_name, list):
        if not index_name:
            raise ValueError("Index name list is empty")
        index_name = index_name[0]
    return f"{index_name}_{memory_id}"


def _source_id(value) -> int:
    """Normalize source_id to the integer message id used by RAGFlow/Infinity."""
    if value in (None, ""):
        return 0
    return int(value)


def _string_field(value) -> str:
    if value is None:
        return ""
    return str(value)


def _vector_field(value) -> list[float]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list):
        return []
    return [float(item) for item in value]


def _is_vector_field_name(field_name: str) -> bool:
    return re.match(r"q_\d+_vec", field_name) is not None


def _upsert_rows(collection: Collection, rows: list[dict[str, Any]], *, partial_update: bool = False) -> None:
    """Submit once and require an exact acknowledgement; an error may mean unknown commit.

    SDK 3.x uses timeout as a retry budget ahead of retry_times. Leave that
    budget unset and disable RPC/rate-limit retries; never replay an unknown write.
    """
    result = collection.upsert(rows, partial_update=partial_update, retry_times=0, retry_on_rate_limit=False)
    count = getattr(result, "upsert_count", None)
    if type(count) is not int or count != len(rows):
        raise RuntimeError("Milvus upsert acknowledgement does not match the submitted row count")


def _message_update_values(values: dict[str, Any], convert_field: Callable[[str], str]) -> dict[str, Any]:
    patch = {convert_field(key): value for key, value in values.items()}
    if {"id", "memory_id", "message_id"}.intersection(patch):
        raise ValueError("Message identity cannot be changed by an update")
    if "status_int" in patch:
        status = patch["status_int"]
        if not (type(status) is bool or (type(status) is int and status in (0, 1))):
            raise ValueError("Message status must be a boolean or zero/one")
        patch["status_int"] = int(status)
    for field in ("forget_at", "invalid_at", "valid_at"):
        if field in patch:
            if patch[field] is not None and not isinstance(patch[field], str):
                raise ValueError("Message timestamps must be strings or None")
            patch[field] = _string_field(patch[field])
    return patch


@singleton
class MilvusConnection(MilvusConnectionBase):
    """Milvus connection for message storage."""

    def __init__(self):
        super().__init__(logger_name="multirag.memory.milvus_conn")

    def supports_recent_message_query(self) -> bool:
        """Enable scalar ordering only for the verified SDK and actual server 3.x pair."""
        return supports_native_query_order_by(self._using)

    def query_recent_candidates(self, select_fields: list[str], condition: dict[str, Any], index_name: str, memory_id: str, limit: int) -> list[dict[str, Any]]:
        """Strict bounded scalar read for older servers; the service sorts the full set."""
        if not index_name or not memory_id or type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("An owning index, memory and bounded recent-message scan are required")
        if set(condition) - {"agent_id", "session_id", "message_type", "user_id", "status"}:
            raise ValueError("Unsupported recent-message condition")
        collection_name = _message_collection_name(index_name, memory_id)
        if not utility.has_collection(collection_name, using=self._using):
            return []
        collection = Collection(collection_name, using=self._using)
        collection.load()
        return collection.query(
            expr=self._build_filter_expr(condition, memory_id, hide_forgotten=True),
            output_fields=self._prepare_output_fields(collection, select_fields),
            limit=limit,
            consistency_level="Strong",
        )

    def query_recent_rows(
        self,
        select_fields: list[str],
        condition: dict[str, Any],
        index_name: str,
        memory_id: str,
        limit: int,
        *,
        before: tuple[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        """Read one bank with native scalar ordering and an optional descending seek key.

        The service confirms SDK/server support before calling this method. Failures
        propagate, rather than turning an unavailable ordered read into an empty page.
        """
        if not index_name or not memory_id or type(limit) is not int or not 1 <= limit <= 101:
            raise ValueError("An owning index, memory and bounded recent-message limit are required")
        if set(condition) - {"agent_id", "session_id", "message_type", "user_id", "status", "message_id"}:
            raise ValueError("Unsupported recent-message condition")
        filter_expr = self._build_filter_expr(condition, memory_id, hide_forgotten=True)
        if before is not None:
            valid_at, message_id = before
            if not isinstance(valid_at, str) or len(valid_at) > 64 or type(message_id) is not int or not 0 < message_id <= 2**63 - 1:
                raise ValueError("Invalid recent-message seek key")
            timestamp = json.dumps(valid_at)
            filter_expr = f"({filter_expr}) and (valid_at < {timestamp} or (valid_at == {timestamp} and message_id < {message_id}))"
        collection_name = _message_collection_name(index_name, memory_id)
        if not utility.has_collection(collection_name, using=self._using):
            return []
        collection = Collection(collection_name, using=self._using)
        collection.load()
        return collection.query(
            expr=filter_expr,
            output_fields=self._prepare_output_fields(collection, select_fields),
            order_by=["valid_at:desc", "message_id:desc"],
            limit=limit,
            consistency_level="Strong",
        )

    def query_source_rows(self, select_fields: list[str], message_ids: list[int], index_name: str, memory_id: str) -> list[dict[str, Any]]:
        """Strict source read with one sentinel row to expose duplicate scalar IDs.

        message_id is not the primary key. A canonical row and a legacy alias
        must both reach the caller's binding validator rather than be truncated.
        """
        if not index_name or not memory_id or not 1 <= len(message_ids) <= 100:
            raise ValueError("An owning index, memory and bounded source-message batch are required")
        if any(type(message_id) is not int or not 0 < message_id <= 2**63 - 1 for message_id in message_ids):
            raise ValueError("Source message IDs must be positive signed 64-bit integers")
        collection_name = _message_collection_name(index_name, memory_id)
        if not utility.has_collection(collection_name, using=self._using):
            return []
        collection = Collection(collection_name, using=self._using)
        collection.load()
        unique_ids = list(dict.fromkeys(message_ids))
        return collection.query(
            expr=self._build_filter_expr({"message_id": unique_ids}, memory_id, hide_forgotten=False),
            output_fields=self._prepare_output_fields(collection, select_fields),
            limit=len(unique_ids) + 1,
            consistency_level="Strong",
        )

    def query_management_rows(self, select_fields: list[str], index_name: str, memory_id: str, limit: int) -> list[dict[str, Any]]:
        """Strict physical inventory, including inactive/forgotten rows and vectors."""
        if not index_name or not memory_id or type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("An owning index, memory and bounded management limit are required")
        name = _message_collection_name(index_name, memory_id)
        if not utility.has_collection(name, using=self._using):
            return []
        collection = Collection(name, using=self._using)
        collection.load()
        return collection.query(
            expr=self._build_filter_expr({}, memory_id, hide_forgotten=False),
            output_fields=self._prepare_output_fields(collection, select_fields),
            limit=limit,
            consistency_level="Strong",
        )

    def prepare_source_reads(self, select_fields: list[str], index_name: str, memory_id: str) -> MilvusReadPreparation:
        """Create a lazy one-pass reader using only public Collection APIs."""
        if not index_name or not memory_id:
            raise ValueError("An owning index and memory are required")
        collection_name = _message_collection_name(index_name, memory_id)
        requested, using = select_fields.copy(), self._using

        def prepare() -> SourceQuery | None:
            if not utility.has_collection(collection_name, using=using):
                return None
            collection = Collection(collection_name, using=using)
            collection.load()
            output_fields = self._prepare_output_fields(collection, requested)

            def query(message_ids: list[int]) -> list[dict[str, Any]]:
                return collection.query(
                    expr=self._build_filter_expr({"message_id": message_ids}, memory_id, hide_forgotten=False),
                    output_fields=output_fields.copy(),
                    limit=len(message_ids) + 1,
                    consistency_level="Strong",
                )

            return query

        return MilvusReadPreparation(prepare)

    def index_exists_strict(self, index_name: str, memory_id: str) -> bool:
        """Check physical absence without treating a failed metadata RPC as absence."""
        return utility.has_collection(_message_collection_name(index_name, memory_id), using=self._using)

    def ensure_memory_index(self, index_name: str, memory_id: str, dimension: int) -> None:
        """Create once within an admitted writer and reject incompatible vectors."""
        if not index_name or not memory_id or type(dimension) is not int or dimension <= 0:
            raise ValueError("An owning memory and supported vector dimension are required")
        name = _message_collection_name(index_name, memory_id)
        if not utility.has_collection(name, using=self._using):
            self._create_message_collection(name, dimension)
        collection = Collection(name, using=self._using)
        vectors = [field for field in collection.schema.fields if field.dtype == DataType.FLOAT_VECTOR]
        if len(vectors) != 1 or vectors[0].name != f"q_{dimension}_vec" or vectors[0].params.get("dim") != dimension:
            raise ValueError("Memory embedding dimension does not match the existing collection")
        collection.load()

    @staticmethod
    def field_keyword(field_name: str) -> bool:
        """Check if a field is a keyword field. Message storage does not use keyword fields."""
        return False

    """
    Field conversion methods
    """

    @staticmethod
    def convert_field_name(field_name: str) -> str:
        """Convert message field name to Milvus storage field name."""
        match field_name:
            case "message_type":
                return "message_type_kwd"
            case "status":
                return "status_int"
            case "content":
                return "content_ltks"
            case _:
                return field_name

    def _prepare_output_fields(self, collection: Collection, select_fields: list[str]) -> list[str]:
        milvus_fields = []
        for field_name in select_fields:
            converted_name = self.convert_field_name(field_name)
            if field_name == "content_embed":
                continue
            milvus_fields.append(converted_name)

        if "content_embed" in select_fields:
            for field in collection.schema.fields:
                if _is_vector_field_name(field.name):
                    milvus_fields.append(field.name)

        if "id" not in milvus_fields:
            milvus_fields.append("id")
        return list(dict.fromkeys(milvus_fields))

    @staticmethod
    def map_message_to_milvus_fields(message: dict) -> dict:
        """
        Map message dictionary fields to Milvus document fields.

        :param message: A dictionary containing message details.
        :return: A dictionary formatted for Milvus indexing.
        """
        storage_doc = {
            "id": _string_field(message.get("id")),
            "message_id": message["message_id"],
            "message_type_kwd": _string_field(message["message_type"]),
            "source_id": _source_id(message.get("source_id", 0)),
            "memory_id": _string_field(message["memory_id"]),
            "user_id": _string_field(message.get("user_id")),
            "agent_id": _string_field(message["agent_id"]),
            "session_id": _string_field(message["session_id"]),
            "valid_at": _string_field(message.get("valid_at")),
            "invalid_at": _string_field(message.get("invalid_at")),
            "forget_at": _string_field(message.get("forget_at")),
            "status_int": 1 if message.get("status") else 0,
            "zone_id": message.get("zone_id", 0),
            "content_ltks": _string_field(message.get("content")),
            f"q_{len(message['content_embed'])}_vec": message["content_embed"],
        }
        return storage_doc

    @staticmethod
    def get_message_from_milvus_doc(doc: dict) -> dict:
        """
        Convert a Milvus document back to a message dictionary.

        :param doc: A dictionary representing the Milvus document.
        :return: A dictionary formatted as a message.
        """
        embd_field_name = next((key for key in doc.keys() if re.match(r"q_\d+_vec", key)), None)
        message = {
            "message_id": doc.get("message_id"),
            "message_type": doc.get("message_type_kwd", ""),
            "source_id": _source_id(doc.get("source_id", 0)),
            "memory_id": doc.get("memory_id", ""),
            "user_id": doc.get("user_id", ""),
            "agent_id": doc.get("agent_id", ""),
            "session_id": doc.get("session_id", ""),
            "zone_id": doc.get("zone_id", 0),
            "valid_at": doc.get("valid_at", ""),
            "invalid_at": doc.get("invalid_at", "-"),
            "forget_at": doc.get("forget_at", "-"),
            "status": bool(int(doc.get("status_int", 0))),
            "content": doc.get("content_ltks", ""),
            "content_embed": _vector_field(doc.get(embd_field_name)) if embd_field_name else [],
        }
        if doc.get("id"):
            message["id"] = doc["id"]
        return message

    """
    CRUD operations
    """

    def index_exist(self, index_name: str | list[str], memory_id: str = None) -> bool:
        if not memory_id:
            return False
        collection_name = _message_collection_name(index_name, memory_id)
        try:
            return utility.has_collection(collection_name, using=self._using)
        except Exception as e:
            self.logger.warning(f"Failed to check if collection {collection_name} exists: {e}")
            return False

    def create_idx(self, index_name: str | list[str], memory_id: str, vector_size: int, parser_id: str = None):
        collection_name = _message_collection_name(index_name, memory_id)
        if utility.has_collection(collection_name, using=self._using):
            self.logger.info(f"Message collection {collection_name} already exists")
            return True
        self._create_message_collection(collection_name, vector_size)
        return True

    def delete_idx(self, index_name: str | list[str], memory_id: str):
        collection_name = _message_collection_name(index_name, memory_id)
        try:
            if utility.has_collection(collection_name, using=self._using):
                utility.drop_collection(collection_name, using=self._using)
                self.logger.info(f"Successfully deleted message collection {collection_name}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to delete message collection {collection_name}: {e}")
            raise

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
        memory_ids: list[str],
        agg_fields: list[str] | None = None,
        rank_feature: dict | None = None,
        hide_forgotten: bool = True,
        *,
        strict: bool = False,
        eligibility_by_memory: dict[str, dict[str, list[int]]] | None = None,
    ) -> tuple[list[dict], int]:
        """
        Search messages in Milvus.

        :param select_fields: Fields to return in results
        :param highlight_fields: Fields to highlight (not supported in Milvus)
        :param condition: Filter conditions
        :param match_expressions: Match expressions for text/vector search
        :param order_by: Order by expression
        :param offset: Pagination offset
        :param limit: Maximum number of results
        :param index_names: Index/collection name(s)
        :param memory_ids: Memory IDs to search in
        :param agg_fields: Aggregation fields (not fully supported in Milvus)
        :param rank_feature: Rank feature adjustments
        :param hide_forgotten: Whether to hide forgotten messages (default True)
        """
        if isinstance(index_names, str):
            index_names = [index_names]

        results = []
        total_hits = 0

        for index_name in index_names:
            for memory_id in memory_ids:
                collection_name = f"{index_name}_{memory_id}"
                if not utility.has_collection(collection_name, using=self._using):
                    continue

                collection = Collection(collection_name, using=self._using)
                collection.load()

                # Build filter expression with hide_forgotten support
                filter_expr = self._build_filter_expr(condition, memory_id, hide_forgotten)
                exclusions = (eligibility_by_memory or {}).get(memory_id, {})
                for name, field in (("message_ids", "message_id"), ("source_ids", "source_id")):
                    values = exclusions.get(name, [])
                    if any(type(value) is not int or value <= 0 for value in values):
                        raise ValueError("Invalid managed message exclusion")
                    if values:
                        filter_expr = f"({filter_expr}) and {field} not in {json.dumps(values)}"

                # Convert select fields to Milvus field names
                milvus_select_fields = self._prepare_output_fields(collection, select_fields)

                # Check if we have vector search expressions
                has_vector_search = any(isinstance(m, MatchDenseExpr) for m in match_expressions)

                try:
                    if has_vector_search:
                        # Perform vector search
                        for m in match_expressions:
                            if isinstance(m, MatchDenseExpr):
                                vector_field = self.convert_field_name(m.vector_column_name)
                                search_params = {"metric_type": "COSINE", "params": {"nprobe": 10}}
                                search_results = collection.search(
                                    data=[m.embedding_data],
                                    anns_field=vector_field,
                                    param=search_params,
                                    limit=limit,
                                    expr=filter_expr if filter_expr else None,
                                    output_fields=milvus_select_fields,
                                    **({"consistency_level": "Strong"} if strict else {}),
                                )
                                for hits in search_results:
                                    for hit in hits:
                                        result_dict = {}
                                        for field in milvus_select_fields:
                                            value = hit.entity.get(field)
                                            if value is not None:
                                                result_dict[field] = value
                                        result_dict["id"] = hit.id
                                        result_dict["_score"] = hit.distance
                                        results.append(result_dict)
                                total_hits += len(search_results[0]) if search_results else 0
                    else:
                        # Perform regular query
                        query_results = collection.query(
                            expr=filter_expr if filter_expr else "id != ''",
                            output_fields=milvus_select_fields,
                            limit=limit,
                            offset=offset,
                            **({"consistency_level": "Strong"} if strict else {}),
                        )
                        results.extend(query_results)
                        total_hits += len(query_results)

                except Exception as e:
                    if strict:
                        raise
                    self.logger.warning(f"Milvus search error in {collection_name}: {e}")

        # Sort by score if available
        if results and "_score" in results[0]:
            results.sort(key=lambda x: x.get("_score", 0), reverse=True)

        # Return tuple format (results, total) consistent with ragflow ES/Infinity
        return results, total_hits

    def search_strict(self, **kwargs: Any) -> tuple[list[dict], int]:
        """Use the existing retrieval algorithm while propagating storage failures."""
        return self.search(**kwargs, strict=True)

    def search_managed(self, **kwargs: Any) -> tuple[list[dict], int]:
        """Apply SQL-derived eligibility before the existing retrieval limit."""
        return self.search(**kwargs, strict=True)

    def _build_filter_expr(self, condition: dict, memory_id: str, hide_forgotten: bool = True) -> str:
        """
        Build Milvus filter expression from condition.

        :param condition: Filter conditions dictionary
        :param memory_id: Memory ID to filter by
        :param hide_forgotten: Whether to exclude forgotten messages
        :return: Milvus filter expression string
        """
        exprs = [f"memory_id == {json.dumps(memory_id)}"]

        # Add hide_forgotten filter
        if hide_forgotten:
            exprs.append('forget_at == ""')

        for k, v in condition.items():
            # Convert field name
            field_name = self.convert_field_name(k)

            # Preserve legacy scalar handling; lists use exact membership below.
            if field_name == "session_id" and isinstance(v, str) and v:
                exprs.append(f"{field_name} == {json.dumps(v)}")
                continue

            if not v:
                continue

            if field_name == "source_id":
                if isinstance(v, list):
                    vals = ", ".join([str(_source_id(x)) for x in v])
                    exprs.append(f"{field_name} in [{vals}]")
                else:
                    exprs.append(f"{field_name} == {_source_id(v)}")
                continue

            if isinstance(v, str):
                exprs.append(f"{field_name} == {json.dumps(v)}")
            elif isinstance(v, int):
                exprs.append(f"{field_name} == {v}")
            elif isinstance(v, list):
                if v:
                    exprs.append(f"{field_name} in {json.dumps(v)}")

        return " and ".join(exprs) if exprs else ""

    def get_forgotten_messages(self, select_fields: list[str], index_name: str, memory_id: str, limit: int = 512):
        """
        Get forgotten messages list, sorted by forget_at from old to new.

        :param select_fields: Fields to return
        :param index_name: Index/collection name
        :param memory_id: Memory ID
        :param limit: Maximum number of results
        :return: List of result documents (consistent with search return format)
        """
        collection_name = f"{index_name}_{memory_id}"
        if not utility.has_collection(collection_name, using=self._using):
            return []

        collection = Collection(collection_name, using=self._using)
        collection.load()

        # Convert select fields
        milvus_select_fields = [self.convert_field_name(f) for f in select_fields]
        if "id" not in milvus_select_fields:
            milvus_select_fields.append("id")
        if "forget_at" not in milvus_select_fields:
            milvus_select_fields.append("forget_at")

        # Filter: forget_at is not empty (forgotten messages)
        filter_expr = f'memory_id == "{memory_id}" and forget_at != ""'

        try:
            results = collection.query(
                expr=filter_expr,
                output_fields=milvus_select_fields,
                limit=limit,
            )

            # Sort by forget_at (ascending - old to new)
            results.sort(key=lambda x: x.get("forget_at", ""))

            return results
        except Exception as e:
            self.logger.warning(f"Milvus get_forgotten_messages error: {e}")
            return []

    def get_missing_field_message(self, select_fields: list[str], index_name: str, memory_id: str, field_name: str, limit: int = 512):
        """
        Get messages missing a specific field.

        :param select_fields: Fields to return
        :param index_name: Index/collection name
        :param memory_id: Memory ID
        :param field_name: Field name to check for missing
        :param limit: Maximum number of results
        :return: List of result documents
        """
        collection_name = f"{index_name}_{memory_id}"
        if not utility.has_collection(collection_name, using=self._using):
            return []

        collection = Collection(collection_name, using=self._using)
        collection.load()

        milvus_select_fields = [self.convert_field_name(f) for f in select_fields]
        if "id" not in milvus_select_fields:
            milvus_select_fields.append("id")

        # Milvus with enable_dynamic_field: query records where field doesn't exist
        # Since Milvus doesn't natively support "field not exists" filter,
        # we query all and filter in Python
        filter_expr = f'memory_id == "{memory_id}"'
        try:
            results = collection.query(
                expr=filter_expr,
                output_fields=milvus_select_fields + [field_name],
                limit=limit,
            )
            # Filter records where the field is missing or empty
            missing = [r for r in results if not r.get(field_name)]
            return missing
        except Exception as e:
            self.logger.warning(f"Milvus get_missing_field_message error: {e}")
            return []

    def get(self, doc_id: str, index_name: str, memory_ids: list[str]) -> dict | None:
        """
        Get a message by ID.

        :param doc_id: Message ID to retrieve
        :param index_name: Index/collection name
        :param memory_ids: Memory IDs to search in
        :return: Message dict or None if not found
        """
        for memory_id in memory_ids:
            collection_name = f"{index_name}_{memory_id}"
            if not utility.has_collection(collection_name, using=self._using):
                continue

            collection = Collection(collection_name, using=self._using)
            collection.load()

            try:
                results = collection.query(expr=f"id == {json.dumps(doc_id)}", output_fields=["*"], limit=1, consistency_level="Strong")
                if results:
                    return self.get_message_from_milvus_doc(results[0])
            except Exception as e:
                self.logger.warning(f"Milvus get error: {e}")

        return None

    def insert(self, documents: list[dict[str, Any]], index_name: str, memory_id: str | None = None) -> list[str]:
        """
        Replace complete messages with one acknowledged native upsert and no pre-delete.

        :param documents: List of message dictionaries to insert
        :param index_name: Index/collection name
        :param memory_id: Memory ID
        :return: List of error messages (empty if successful)
        """
        if not documents:
            return []
        try:
            if not index_name or not memory_id:
                raise ValueError("An owning index and memory are required")
            collection_name = _message_collection_name(index_name, memory_id)
            vector_size = len(documents[0].get("content_embed", []))
            if not vector_size:
                raise ValueError("Cannot determine vector size from documents")
            docs: list[dict[str, Any]] = []
            ids: set[str] = set()
            for document in documents:
                message_id = document.get("message_id")
                if "_id" in document or type(message_id) is not int or not 0 < message_id <= 2**63 - 1 or document.get("memory_id") != memory_id or document.get("id") != f"{memory_id}_{message_id}":
                    raise ValueError("Message identity does not match the target memory")
                if document["id"] in ids or len(document.get("content_embed", [])) != vector_size:
                    raise ValueError("Message IDs must be unique and vectors must share one dimension")
                ids.add(document["id"])
                docs.append(self.map_message_to_milvus_fields(document))
            if not utility.has_collection(collection_name, using=self._using):
                self._create_message_collection(collection_name, vector_size)
            collection = Collection(collection_name, using=self._using)
            _upsert_rows(collection, docs)
            self.logger.debug("MILVUS upserted %d messages into %s", len(docs), collection_name)
            return []
        except Exception as e:
            self.logger.error("Milvus message upsert was not confirmed (%s)", type(e).__name__)
            return ["Milvus message upsert was not confirmed; check stored state before retrying"]

    def _create_message_collection(self, collection_name: str, vector_size: int):
        """
        Create message collection in Milvus with proper schema.

        :param collection_name: Collection name
        :param vector_size: Dimension of the embedding vector
        """
        # Define schema with message-specific fields
        fields = [
            # Primary key
            FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=256, is_primary=True),
            # Message fields
            FieldSchema(name="message_id", dtype=DataType.INT64),
            FieldSchema(name="message_type_kwd", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="source_id", dtype=DataType.INT64),
            FieldSchema(name="memory_id", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="user_id", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="agent_id", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="session_id", dtype=DataType.VARCHAR, max_length=256),
            # Time fields
            FieldSchema(name="valid_at", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="invalid_at", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="forget_at", dtype=DataType.VARCHAR, max_length=64),
            # Status and zone
            FieldSchema(name="status_int", dtype=DataType.INT64),
            FieldSchema(name="zone_id", dtype=DataType.INT64),
            # Content
            FieldSchema(name="content_ltks", dtype=DataType.VARCHAR, max_length=65535),
            # Embedding vector
            FieldSchema(name=f"q_{vector_size}_vec", dtype=DataType.FLOAT_VECTOR, dim=vector_size),
        ]

        schema = CollectionSchema(fields=fields, description="Message collection for memory storage", enable_dynamic_field=True)

        collection = Collection(name=collection_name, schema=schema, using=self._using)

        # Create index on vector field
        index_params = {"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 50}}
        collection.create_index(field_name=f"q_{vector_size}_vec", index_params=index_params)

        # Load collection
        collection.load()

        self.logger.info(f"MILVUS created message collection {collection_name} with vector size {vector_size}")

    def update(self, condition: dict[str, Any], new_value: dict[str, Any], index_name: str, memory_id: str) -> bool:
        """
        Upsert updates without deleting the existing message before validation or write.

        Verified 3.x status/forget patches use server-side partial updates. Other
        patches and older servers preserve complete source rows in a full upsert.

        :param condition: Filter conditions to find records to update
        :param new_value: New values to set
        :param index_name: Index/collection name
        :param memory_id: Memory ID
        :return: True if successful
        """
        try:
            if not index_name or not memory_id:
                raise ValueError("An owning index and memory are required")
            update_dict = _message_update_values(new_value, self.convert_field_name)
            collection_name = _message_collection_name(index_name, memory_id)
            if not utility.has_collection(collection_name, using=self._using):
                return False
            use_partial = bool(update_dict) and set(update_dict) <= {"status_int", "forget_at"} and supports_verified_milvus_3(self._using)
            collection = Collection(collection_name, using=self._using)
            collection.load()
            filter_expr = self._build_filter_expr(condition, memory_id, hide_forgotten=False)
            results = collection.query(
                expr=filter_expr,
                output_fields=["id", "memory_id"] if use_partial else ["*"],
                limit=10000,
                consistency_level="Strong",
            )
            # Reaching the cap cannot establish the complete mutation scope.
            if not results or len(results) >= 10000:
                return False
            updated_docs: list[dict[str, Any]] = []
            ids: set[str] = set()
            for record in results:
                identity = record.get("id")
                if not isinstance(identity, str) or not identity or identity in ids or record.get("memory_id") != memory_id:
                    raise ValueError("Stored message identity does not match the update scope")
                ids.add(identity)
                original = {"id": identity} if use_partial else record
                updated_docs.append({**original, **update_dict})
            if not update_dict:
                return True
            _upsert_rows(collection, updated_docs, partial_update=use_partial)
            self.logger.debug("MILVUS updated %d messages in %s", len(updated_docs), collection_name)
            return True
        except Exception as e:
            self.logger.error("Milvus message update was not confirmed (%s)", type(e).__name__)
            return False

    def delete(self, condition: dict, index_name: str, memory_id: str) -> int:
        """
        Delete messages from Milvus.

        :param condition: Filter conditions for deletion
        :param index_name: Index/collection name
        :param memory_id: Memory ID
        :return: Number of deleted records
        """
        collection_name = f"{index_name}_{memory_id}"
        if not utility.has_collection(collection_name, using=self._using):
            return 0

        collection = Collection(collection_name, using=self._using)
        collection.load()

        # Convert condition field names
        condition_dict = {self.convert_field_name(k): v for k, v in condition.items()}

        # Handle direct ID deletion
        if "id" in condition_dict:
            message_ids = condition_dict["id"]
            if not isinstance(message_ids, list):
                message_ids = [message_ids]
            if message_ids:
                ids = [f'"{mid}"' for mid in message_ids]
                filter_expr = f"id in [{', '.join(ids)}]"
            else:
                filter_expr = self._build_filter_expr(condition, memory_id, hide_forgotten=False)
        else:
            filter_expr = self._build_filter_expr(condition, memory_id, hide_forgotten=False)

        try:
            # Query first to get count
            results = collection.query(expr=filter_expr, output_fields=["id"], limit=10000)
            count = len(results)

            if count > 0:
                collection.delete(filter_expr)
                self.logger.debug(f"MILVUS deleted {count} messages from {collection_name}")

            return count
        except Exception as e:
            self.logger.warning(f"Milvus delete error: {e}")
            return 0

    """
    Helper functions for search result
    """

    def _extract_results(self, res) -> list:
        """
        Extract results list from various return formats.

        Handles:
        - tuple: (results, total) from search
        - list: direct results list
        - dict: ES-compatible format (legacy)
        """
        if isinstance(res, tuple):
            return res[0] if res[0] else []
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            # Legacy ES-compatible format
            hits = res.get("hits", {})
            if isinstance(hits, dict):
                return hits.get("hits", [])
            return []
        return []

    def get_fields(self, res: Any, fields: list[str]) -> dict[str, dict[str, Any]]:
        """
        Get fields from results, converting to message format.

        :param res: Search results (list or tuple)
        :param fields: Fields to extract
        :return: Dict mapping document ID to field values
        """
        result: dict[str, dict[str, Any]] = {}
        results = self._extract_results(res)

        for doc in results:
            # Convert Milvus doc to message format
            message = self.get_message_from_milvus_doc(doc)
            doc_id = message.get("id")
            if doc_id:
                # Extract requested fields
                field_dict: dict[str, Any] = {}
                for f in fields:
                    if f in message:
                        v = message[f]
                        if isinstance(v, list):
                            field_dict[f] = v
                        elif f in ["message_id", "source_id", "valid_at", "invalid_at", "forget_at", "status", "zone_id"] and isinstance(v, (int, float, bool)):
                            field_dict[f] = v
                        elif v is not None:
                            field_dict[f] = str(v) if not isinstance(v, str) else v
                if field_dict:
                    result[doc_id] = field_dict
        return result

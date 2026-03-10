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

import re
import copy

from pymilvus import Collection, FieldSchema, CollectionSchema, DataType
from pymilvus.orm import utility

from common.decorator import singleton
from common.doc_store.doc_store_base import MatchExpr, MatchTextExpr, MatchDenseExpr, FusionExpr, OrderByExpr
from common.doc_store.milvus_conn_base import MilvusConnectionBase
from common.float_utils import get_float
from common.constants import PAGERANK_FLD, TAG_FLD


@singleton
class MilvusConnection(MilvusConnectionBase):
    """Milvus connection for message storage."""

    def __init__(self):
        super().__init__(logger_name="multirag.memory.milvus_conn")

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

    @staticmethod
    def map_message_to_milvus_fields(message: dict) -> dict:
        """
        Map message dictionary fields to Milvus document fields.

        :param message: A dictionary containing message details.
        :return: A dictionary formatted for Milvus indexing.
        """
        storage_doc = {
            "id": message.get("id"),
            "message_id": message["message_id"],
            "message_type_kwd": message["message_type"],
            "source_id": message.get("source_id", ""),
            "memory_id": message["memory_id"],
            "user_id": message.get("user_id", ""),
            "agent_id": message["agent_id"],
            "session_id": message["session_id"],
            "valid_at": message.get("valid_at", ""),
            "invalid_at": message.get("invalid_at", ""),
            "forget_at": message.get("forget_at", ""),
            "status_int": 1 if message.get("status") else 0,
            "zone_id": message.get("zone_id", 0),
            "content_ltks": message.get("content", ""),
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
            "source_id": doc.get("source_id") if doc.get("source_id") else None,
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
            "content_embed": doc.get(embd_field_name, []) if embd_field_name else [],
        }
        if doc.get("id"):
            message["id"] = doc["id"]
        return message

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
        memory_ids: list[str],
        agg_fields: list[str] | None = None,
        rank_feature: dict | None = None,
        hide_forgotten: bool = True,
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

                # Convert select fields to Milvus field names
                milvus_select_fields = [self.convert_field_name(f) for f in select_fields]
                if "id" not in milvus_select_fields:
                    milvus_select_fields.append("id")

                # Check if we have vector search expressions
                has_vector_search = any(isinstance(m, MatchDenseExpr) for m in match_expressions)

                try:
                    if has_vector_search:
                        # Perform vector search
                        for m in match_expressions:
                            if isinstance(m, MatchDenseExpr):
                                vector_field = self.convert_field_name(m.vector_column_name)
                                search_params = {
                                    "metric_type": "COSINE",
                                    "params": {"nprobe": 10}
                                }
                                search_results = collection.search(
                                    data=[m.embedding_data],
                                    anns_field=vector_field,
                                    param=search_params,
                                    limit=limit,
                                    expr=filter_expr if filter_expr else None,
                                    output_fields=milvus_select_fields,
                                )
                                for hits in search_results:
                                    for hit in hits:
                                        result_dict = {k: v for k, v in hit.entity.items()}
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
                        )
                        results.extend(query_results)
                        total_hits += len(query_results)

                except Exception as e:
                    self.logger.warning(f"Milvus search error in {collection_name}: {e}")

        # Sort by score if available
        if results and "_score" in results[0]:
            results.sort(key=lambda x: x.get("_score", 0), reverse=True)

        # Return tuple format (results, total) consistent with ragflow ES/Infinity
        return results, total_hits

    def _build_filter_expr(self, condition: dict, memory_id: str, hide_forgotten: bool = True) -> str:
        """
        Build Milvus filter expression from condition.

        :param condition: Filter conditions dictionary
        :param memory_id: Memory ID to filter by
        :param hide_forgotten: Whether to exclude forgotten messages
        :return: Milvus filter expression string
        """
        exprs = [f'memory_id == "{memory_id}"']

        # Add hide_forgotten filter
        if hide_forgotten:
            exprs.append('forget_at == ""')

        for k, v in condition.items():
            # Convert field name
            field_name = self.convert_field_name(k)

            # Handle session_id with wildcard support
            if field_name == "session_id" and v:
                if isinstance(v, str):
                    exprs.append(f'{field_name} == "{v}"')
                continue

            if not v:
                continue

            if isinstance(v, str):
                exprs.append(f'{field_name} == "{v}"')
            elif isinstance(v, int):
                exprs.append(f"{field_name} == {v}")
            elif isinstance(v, list):
                if v:
                    if isinstance(v[0], str):
                        vals = ", ".join([f'"{x}"' for x in v])
                    else:
                        vals = ", ".join([str(x) for x in v])
                    exprs.append(f"{field_name} in [{vals}]")

        return " and ".join(exprs) if exprs else ""

    def get_forgotten_messages(
        self,
        select_fields: list[str],
        index_name: str,
        memory_id: str,
        limit: int = 512
    ):
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
                results = collection.query(
                    expr=f'id == "{doc_id}"',
                    output_fields=["*"],
                    limit=1
                )
                if results:
                    return self.get_message_from_milvus_doc(results[0])
            except Exception as e:
                self.logger.warning(f"Milvus get error: {e}")

        return None

    def insert(self, documents: list[dict], index_name: str, memory_id: str = None) -> list[str]:
        """
        Insert messages into Milvus.

        :param documents: List of message dictionaries to insert
        :param index_name: Index/collection name
        :param memory_id: Memory ID
        :return: List of error messages (empty if successful)
        """
        if not documents:
            return []

        collection_name = f"{index_name}_{memory_id}"

        # Determine vector size from first document
        vector_size = len(documents[0].get("content_embed", []))
        if vector_size == 0:
            self.logger.error("Cannot determine vector size from documents")
            return ["Cannot determine vector size from documents"]

        # Create collection if not exists
        if not utility.has_collection(collection_name, using=self._using):
            self._create_message_collection(collection_name, vector_size)

        collection = Collection(collection_name, using=self._using)

        # Convert and prepare documents
        docs = []
        for d in documents:
            assert "_id" not in d
            assert "id" in d

            # Use field conversion
            d_copy = self.map_message_to_milvus_fields(d)
            d_copy["memory_id"] = memory_id
            docs.append(d_copy)

        # Delete existing records with same IDs (upsert behavior)
        try:
            ids = [f'"{d["id"]}"' for d in docs]
            if ids:
                delete_expr = f'id in [{", ".join(ids)}]'
                collection.delete(delete_expr)
        except Exception as e:
            self.logger.debug(f"Delete before insert failed (may not exist): {e}")

        try:
            collection.insert(docs)
            collection.flush()
            self.logger.debug(f"MILVUS inserted {len(docs)} messages into {collection_name}")
            return []
        except Exception as e:
            self.logger.error(f"Milvus insert error: {e}")
            return [str(e)]

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
            FieldSchema(name="source_id", dtype=DataType.VARCHAR, max_length=256),
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

        schema = CollectionSchema(
            fields=fields,
            description="Message collection for memory storage",
            enable_dynamic_field=True
        )

        collection = Collection(name=collection_name, schema=schema, using=self._using)

        # Create index on vector field
        index_params = {
            "metric_type": "COSINE",
            "index_type": "HNSW",
            "params": {"M": 16, "efConstruction": 50}
        }
        collection.create_index(field_name=f"q_{vector_size}_vec", index_params=index_params)

        # Load collection
        collection.load()

        self.logger.info(f"MILVUS created message collection {collection_name} with vector size {vector_size}")

    def update(self, condition: dict, new_value: dict, index_name: str, memory_id: str) -> bool:
        """
        Update messages in Milvus.
        Milvus doesn't support direct updates, so this uses query-delete-insert pattern.

        :param condition: Filter conditions to find records to update
        :param new_value: New values to set
        :param index_name: Index/collection name
        :param memory_id: Memory ID
        :return: True if successful
        """
        collection_name = f"{index_name}_{memory_id}"
        if not utility.has_collection(collection_name, using=self._using):
            self.logger.warning(f"Collection {collection_name} does not exist")
            return False

        collection = Collection(collection_name, using=self._using)
        collection.load()

        # Convert condition and new_value field names
        update_dict = {self.convert_field_name(k): v for k, v in new_value.items()}

        # Build filter expression
        filter_expr = self._build_filter_expr(condition, memory_id, hide_forgotten=False)

        try:
            # Query existing records
            results = collection.query(
                expr=filter_expr,
                output_fields=["*"],
                limit=10000,
            )

            if not results:
                self.logger.warning(f"No records found matching condition: {filter_expr}")
                return False

            # Update records
            updated_docs = []
            for record in results:
                # Merge original record with new values
                updated_record = {**record, **update_dict}
                updated_docs.append(updated_record)

            # Delete old records
            ids = [f'"{r["id"]}"' for r in results]
            delete_expr = f'id in [{", ".join(ids)}]'
            collection.delete(delete_expr)

            # Insert updated records
            collection.insert(updated_docs)
            collection.flush()

            self.logger.debug(f"MILVUS updated {len(updated_docs)} messages in {collection_name}")
            return True

        except Exception as e:
            self.logger.error(f"Milvus update error: {e}")
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
                filter_expr = f'id in [{", ".join(ids)}]'
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

    def get_fields(self, res, fields: list[str]) -> dict[str, dict]:
        """
        Get fields from results, converting to message format.

        :param res: Search results (list or tuple)
        :param fields: Fields to extract
        :return: Dict mapping document ID to field values
        """
        result = {}
        results = self._extract_results(res)

        for doc in results:
            # Convert Milvus doc to message format
            message = self.get_message_from_milvus_doc(doc)
            doc_id = message.get("id")
            if doc_id:
                # Extract requested fields
                field_dict = {}
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

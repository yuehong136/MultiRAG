"""Shared ES/OpenSearch metadata dispatch without leaking client attributes."""

import logging
from collections.abc import Iterator, Mapping
from typing import Any

from elastic_transport import ConnectionError as ESConnectionError
from elastic_transport import ConnectionTimeout as ESConnectionTimeout
from elasticsearch import NotFoundError as ESNotFoundError
from opensearchpy import ConnectionError as OSConnectionError
from opensearchpy import ConnectionTimeout as OSConnectionTimeout
from opensearchpy import NotFoundError as OSNotFoundError

logger = logging.getLogger(__name__)
NOT_FOUND = (ESNotFoundError, OSNotFoundError)
TRANSIENT = (ESConnectionError, ESConnectionTimeout, OSConnectionError, OSConnectionTimeout)


def _complete_shards(response: Any, *, noop: bool = False) -> bool:
    raw = getattr(response, "body", response)
    shards = raw.get("_shards") if isinstance(raw, Mapping) else None
    if not isinstance(shards, Mapping):
        return False
    total, successful, failed = (shards.get(key) for key in ("total", "successful", "failed"))
    if any(type(value) is not int for value in (total, successful, failed)) or failed != 0:
        return False
    # An update whose script explicitly does nothing may perform no shard writes.
    if noop and total == successful == 0:
        return True
    return 0 < successful <= total


class SearchMetadata:
    """The client SDKs share these metadata operations and response contracts."""

    def _doc_meta_client(self) -> Any:
        raise NotImplementedError

    def doc_meta_index_exists(self, index_name: str) -> bool:
        response = self._doc_meta_client().indices.exists(index=index_name)
        exists = getattr(response, "body", response)
        if type(exists) is not bool:
            raise RuntimeError("Malformed metadata index existence response")
        return exists

    def refresh_idx(self, index_name: str) -> bool:
        try:
            response = self._doc_meta_client().indices.refresh(index=index_name)
            shards = response.get("_shards", {})
            total, successful, failed = (shards.get(key) for key in ("total", "successful", "failed"))
            return all(type(value) is int for value in (total, successful, failed)) and failed == 0 and 0 < successful <= total
        except Exception:
            logger.exception("Metadata index refresh failed: %s", index_name)
            return False

    def count_idx(self, index_name: str) -> int:
        """A failed or malformed count is unknown (-1), never an empty index."""
        try:
            response = self._doc_meta_client().count(index=index_name)
            count = response.get("count")
            if type(count) is not int or count < 0 or not _complete_shards(response):
                return -1
            return count
        except NOT_FOUND:
            return 0
        except Exception:
            logger.exception("Metadata count failed: %s", index_name)
            return -1

    def replace_meta_fields(self, index_name: str, doc_id: str, meta_fields: dict[str, Any]) -> bool:
        """Idempotent whole-map assignment; retry transport failures twice at most."""
        body = {"script": {"source": "ctx._source.meta_fields = params.meta_fields", "params": {"meta_fields": meta_fields}}}
        for attempt in range(2):
            try:
                response = self._doc_meta_client().update(index=index_name, id=doc_id, body=body, refresh=True)
                result = response.get("result")
                return result in {"updated", "noop"} and _complete_shards(response, noop=result == "noop")
            except TRANSIENT:
                if attempt == 1:
                    logger.exception("Metadata replacement transport failed: %s/%s", index_name, doc_id)
            except NOT_FOUND:
                return False
            except Exception:
                logger.exception("Metadata replacement failed: %s/%s", index_name, doc_id)
                return False
        return False

    def doc_meta_mapping(self, index_name: str) -> dict[str, Any]:
        return self._doc_meta_client().indices.get_mapping(index=index_name)[index_name]["mappings"]

    def iter_doc_metadata(self, index_name: str, kb_ids: list[str], query: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
        """Read a complete scoped scroll snapshot and release even on early close."""
        client = self._doc_meta_client()
        scroll_id = None
        try:
            response = client.search(
                index=index_name,
                body={"query": query, "sort": ["id"], "_source": ["id", "kb_id", "meta_fields"]},
                size=1000,
                scroll="2m",
                allow_partial_search_results=False,
            )
            while True:
                scroll_id = response.get("_scroll_id", scroll_id)
                if response.get("timed_out") is not False or response.get("_shards", {}).get("failed") != 0:
                    raise RuntimeError("Incomplete metadata search")
                hits = response["hits"]["hits"]
                if not isinstance(hits, list):
                    raise RuntimeError("Malformed metadata response")
                if not hits:
                    break
                if not scroll_id:
                    raise RuntimeError("Metadata search omitted its scroll cursor")
                for hit in hits:
                    source = hit["_source"]
                    if source.get("kb_id") not in kb_ids or not isinstance(hit.get("_id"), str):
                        raise RuntimeError("Metadata response escaped the requested scope")
                    yield hit["_id"], source
                response = client.scroll(scroll_id=scroll_id, scroll="2m")
        finally:
            if scroll_id:
                client.clear_scroll(scroll_id=scroll_id)

    def get_doc_metadata_rows(self, index_name: str, doc_ids: list[str], kb_id: str) -> list[dict[str, Any]]:
        if any(type(doc_id) is not str for doc_id in doc_ids):
            raise RuntimeError("Invalid metadata batch identity")
        requested = set(doc_ids)
        if not requested:
            return []
        response = self._doc_meta_client().mget(index=index_name, body={"ids": list(dict.fromkeys(doc_ids))}, _source=True)
        raw = getattr(response, "body", response)
        hits = raw.get("docs") if isinstance(raw, Mapping) else None
        if not isinstance(hits, list) or len(hits) != len(requested):
            raise RuntimeError("Incomplete metadata batch response")
        rows = []
        seen: set[str] = set()
        for hit in hits:
            if not isinstance(hit, Mapping) or "error" in hit or type(hit.get("found")) is not bool:
                raise RuntimeError("Malformed metadata batch result")
            doc_id = hit.get("_id")
            if type(doc_id) is not str or doc_id not in requested or doc_id in seen:
                raise RuntimeError("Metadata batch identity mismatch")
            seen.add(doc_id)
            if hit["found"] is False:
                continue
            source = hit.get("_source")
            if not isinstance(source, Mapping) or source.get("kb_id") != kb_id or not isinstance(source.get("meta_fields"), dict):
                raise RuntimeError("Metadata batch read escaped the requested scope")
            if "id" in source and source["id"] != doc_id:
                raise RuntimeError("Metadata source identity mismatch")
            rows.append({**source, "id": doc_id})
        if seen != requested:
            raise RuntimeError("Incomplete metadata batch response")
        return rows

    def delete_doc_metadata(self, index_name: str, doc_id: str, kb_id: str) -> bool:
        """Confirm the scoped delete and independently read its search visibility."""
        client = self._doc_meta_client()
        query = {"bool": {"filter": [{"ids": {"values": [doc_id]}}, {"term": {"kb_id": kb_id}}]}}
        try:
            response = client.delete_by_query(index=index_name, body={"query": query}, refresh=True)
            deleted, total, conflicts = (response.get(key) for key in ("deleted", "total", "version_conflicts"))
            if (
                any(type(value) is not int for value in (deleted, total, conflicts))
                or not 0 <= deleted == total
                or conflicts != 0
                or response.get("timed_out") is not False
                or response.get("failures") != []
            ):
                return False
            result = client.search(index=index_name, body={"query": query, "size": 0, "track_total_hits": True}, allow_partial_search_results=False)
            hits = result.get("hits", {})
            count = hits.get("total", {})
            return (
                _complete_shards(result)
                and result.get("timed_out") is False
                and isinstance(count, Mapping)
                and type(count.get("value")) is int
                and count["value"] == 0
                and count.get("relation") == "eq"
                and hits.get("hits") == []
            )
        except NOT_FOUND:
            try:
                return not self.doc_meta_index_exists(index_name)
            except Exception:
                logger.exception("Metadata deletion absence check failed: %s/%s", index_name, doc_id)
                return False
        except Exception:
            logger.exception("Metadata deletion failed: %s/%s", index_name, doc_id)
            return False

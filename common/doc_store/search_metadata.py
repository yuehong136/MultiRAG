"""Shared ES/OpenSearch metadata dispatch without leaking client attributes."""

import logging
from collections.abc import Iterator
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


class SearchMetadata:
    """The client SDKs share these metadata operations and response contracts."""

    def _doc_meta_client(self) -> Any:
        raise NotImplementedError

    def doc_meta_index_exists(self, index_name: str) -> bool:
        return bool(self._doc_meta_client().indices.exists(index=index_name))

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
            if type(count) is not int or count < 0 or response.get("_shards", {}).get("failed", 0) != 0:
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
                shards = response.get("_shards", {})
                return response.get("result") in {"updated", "noop"} and shards.get("failed") == 0
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
        response = self._doc_meta_client().mget(index=index_name, body={"ids": doc_ids}, _source=True)
        rows = []
        for hit in response["docs"]:
            if "error" in hit:
                raise RuntimeError("Metadata batch read failed")
            if not hit.get("found"):
                continue
            source = hit["_source"]
            if hit["_id"] not in doc_ids or source.get("kb_id") != kb_id:
                raise RuntimeError("Metadata batch read escaped the requested scope")
            rows.append({**source, "id": hit["_id"]})
        return rows

import json
import logging
import random
import time
from collections import OrderedDict
from enum import StrEnum
from typing import Annotated

import click
import httpx
from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.http import RequestContextMiddleware, create_sse_app, create_streamable_http_app
from pydantic import Field
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class LaunchMode(StrEnum):
    SELF_HOST = "self-host"
    HOST = "host"


# ---------------------------------------------------------------------------
# Global configuration (populated by CLI / environment variables)
# ---------------------------------------------------------------------------
BASE_URL = "http://127.0.0.1:8123"
HOST = "127.0.0.1"
PORT = "9382"
HOST_API_KEY = ""
MODE = ""
TRANSPORT_SSE_ENABLED = True
TRANSPORT_STREAMABLE_HTTP_ENABLED = True
JSON_RESPONSE = True


def _extract_token_from_headers(headers: dict[str, str]) -> str | None:
    """Extract bearer token or API key from HTTP headers (string keys, lowercase)."""
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token

    for key in ("api_key", "x-api-key"):
        val = headers.get(key, "")
        if val:
            return val.strip()

    return None


def _extract_token_from_scope(scope: dict) -> str | None:
    """Extract bearer token or API key from ASGI scope headers (bytes keys)."""
    raw_headers = dict(scope.get("headers", []))

    auth_header = raw_headers.get(b"authorization", b"")
    if auth_header.lower().startswith(b"bearer "):
        token = auth_header[7:].strip().decode(errors="ignore")
        if token:
            return token

    for key in (b"api_key", b"x-api-key"):
        val = raw_headers.get(key)
        if val:
            return val.decode(errors="ignore").strip()

    return None


def _resolve_api_key() -> str:
    """Resolve the API key for the current request based on launch mode.

    In self-host mode returns the static HOST_API_KEY.
    In host mode extracts the per-request token via fastmcp ``get_http_headers()``.
    """
    if MODE == LaunchMode.SELF_HOST:
        return HOST_API_KEY
    headers = get_http_headers()
    token = _extract_token_from_headers(headers)
    if not token:
        raise ValueError("MultiRAG API key or Bearer token is required.")
    return token


# ---------------------------------------------------------------------------
# MultiRAGConnector — async httpx-based backend API client
# ---------------------------------------------------------------------------
class MultiRAGConnector:
    _MAX_DATASET_CACHE = 32
    _CACHE_TTL = 300

    def __init__(self, base_url: str, version: str = "v1"):
        self.base_url = base_url
        self.version = version
        self.api_url = f"{self.base_url}/api/{self.version}"
        self._client: httpx.AsyncClient | None = None
        self._dataset_metadata_cache: OrderedDict[str, tuple[dict, float | int]] = OrderedDict()
        self._document_metadata_cache: OrderedDict[str, tuple[list[tuple[str, dict]], float | int]] = OrderedDict()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, api_key: str, params: dict | None = None):
        if not api_key:
            return None
        client = await self._get_client()
        return await client.get(
            url=self.api_url + path,
            params=params,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def _post(self, path: str, api_key: str, json: dict | None = None):
        if not api_key:
            return None
        client = await self._get_client()
        return await client.post(
            url=self.api_url + path,
            json=json,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    # -- Cache helpers (unchanged logic) ------------------------------------

    def _is_cache_valid(self, ts: float | int) -> bool:
        return time.time() < ts

    def _get_expiry_timestamp(self) -> float:
        offset = random.randint(-30, 30)
        return time.time() + self._CACHE_TTL + offset

    def _get_cached_dataset_metadata(self, dataset_id: str) -> dict | None:
        entry = self._dataset_metadata_cache.get(dataset_id)
        if entry:
            data, ts = entry
            if self._is_cache_valid(ts):
                self._dataset_metadata_cache.move_to_end(dataset_id)
                return data
        return None

    def _set_cached_dataset_metadata(self, dataset_id: str, metadata: dict):
        self._dataset_metadata_cache[dataset_id] = (metadata, self._get_expiry_timestamp())
        self._dataset_metadata_cache.move_to_end(dataset_id)
        if len(self._dataset_metadata_cache) > self._MAX_DATASET_CACHE:
            self._dataset_metadata_cache.popitem(last=False)

    def _get_cached_document_metadata_by_dataset(self, dataset_id: str) -> dict | None:
        entry = self._document_metadata_cache.get(dataset_id)
        if entry:
            data_list, ts = entry
            if self._is_cache_valid(ts):
                self._document_metadata_cache.move_to_end(dataset_id)
                return dict(data_list)
        return None

    def _set_cached_document_metadata_by_dataset(self, dataset_id: str, doc_id_meta_list: list):
        self._document_metadata_cache[dataset_id] = (doc_id_meta_list, self._get_expiry_timestamp())
        self._document_metadata_cache.move_to_end(dataset_id)

    # -- Public API ---------------------------------------------------------

    async def list_datasets_structured(self, api_key: str) -> list[dict]:
        """Return all datasets with normalized SDK-facing metadata fields."""
        res = await self._get("/datasets", api_key, {"page": 1, "page_size": 1000, "orderby": "create_time", "desc": True})
        if not res:
            raise ValueError("Cannot process this operation.")
        data = res.json()
        if data.get("code") == 0:
            datasets: list[dict] = []
            for d in data.get("data", []):
                embedding_model = d.get("embedding_model") or d.get("embd_id", "")
                datasets.append(
                    {
                        "id": d["id"],
                        "name": d.get("name", ""),
                        "description": d.get("description", ""),
                        "embedding_model": embedding_model,
                        # Keep the legacy alias for older MCP consumers.
                        "embd_id": embedding_model,
                    }
                )
            return datasets
        return []

    @staticmethod
    def _embedding_model_from_dataset(dataset: dict) -> str:
        return dataset.get("embedding_model") or dataset.get("embd_id") or ""

    async def _resolve_retrieval_dataset_ids(self, api_key: str, dataset_ids: list[str]) -> list[str]:
        datasets = await self.list_datasets_structured(api_key)
        if not datasets:
            raise ValueError("No accessible datasets available for retrieval.")

        selected = datasets
        if dataset_ids:
            dataset_id_set = set(dataset_ids)
            selected = [d for d in datasets if d["id"] in dataset_id_set]
            found_ids = {d["id"] for d in selected}
            missing_ids = [dataset_id for dataset_id in dataset_ids if dataset_id not in found_ids]
            if missing_ids:
                raise ValueError("Unknown or inaccessible dataset_ids: " + ", ".join(missing_ids))

        grouped: dict[str, list[dict]] = {}
        for dataset in selected:
            grouped.setdefault(self._embedding_model_from_dataset(dataset), []).append(dataset)

        if len(grouped) > 1:
            group_desc = "; ".join(f"{embedding_model or '<unknown>'}: " + ", ".join(dataset.get("name") or dataset["id"] for dataset in group) for embedding_model, group in grouped.items())
            raise ValueError(f"Selected datasets use different embedding_model values. Choose dataset_ids from a single embedding_model group. Available groups: {group_desc}")

        return [dataset["id"] for dataset in selected]

    async def retrieval(
        self,
        api_key: str,
        dataset_ids: list[str],
        document_ids: list[str] | None = None,
        question: str = "",
        page: int = 1,
        page_size: int = 30,
        similarity_threshold: float = 0.2,
        vector_similarity_weight: float = 0.3,
        top_k: int = 1024,
        rerank_id: str | None = None,
        keyword: bool = False,
        force_refresh: bool = False,
    ) -> dict:
        if document_ids is None:
            document_ids = []

        dataset_ids = await self._resolve_retrieval_dataset_ids(api_key, dataset_ids)

        data_json = {
            "page": page,
            "page_size": page_size,
            "similarity_threshold": similarity_threshold,
            "vector_similarity_weight": vector_similarity_weight,
            "top_k": top_k,
            "rerank_id": rerank_id,
            "keyword": keyword,
            "question": question,
            "dataset_ids": dataset_ids,
            "document_ids": document_ids,
        }

        res = await self._post("/retrieval", api_key, json=data_json)
        if not res:
            raise ValueError("Cannot process this operation.")

        res_data = res.json()
        if res_data.get("code") != 0:
            msg = res_data.get("message") or res_data.get("retmsg") or json.dumps(res_data, ensure_ascii=False)
            raise ValueError(f"Retrieval API error: {msg}")
        data = res_data.get("data")
        if not isinstance(data, dict):
            raise ValueError("Retrieval API returned success without a valid data payload: " + json.dumps(res_data, ensure_ascii=False))

        chunks = []

        document_cache, dataset_cache = await self._get_document_metadata_cache(dataset_ids, api_key=api_key, force_refresh=force_refresh)

        for chunk_data in data.get("chunks", []):
            enhanced_chunk = self._map_chunk_fields(chunk_data, dataset_cache, document_cache)
            chunks.append(enhanced_chunk)

        return {
            "chunks": chunks,
            "pagination": {
                "page": data.get("page", page),
                "page_size": data.get("page_size", page_size),
                "total_chunks": data.get("total", len(chunks)),
                "total_pages": (data.get("total", len(chunks)) + page_size - 1) // page_size,
            },
            "query_info": {
                "question": question,
                "similarity_threshold": similarity_threshold,
                "vector_weight": vector_similarity_weight,
                "keyword_search": keyword,
                "dataset_count": len(dataset_ids),
            },
        }

    async def _get_document_metadata_cache(self, dataset_ids: list[str], *, api_key: str, force_refresh: bool = False) -> tuple[dict, dict]:
        """Cache document metadata for all documents in the specified datasets."""
        document_cache: dict = {}
        dataset_cache: dict = {}

        try:
            for dataset_id in dataset_ids:
                dataset_meta = None if force_refresh else self._get_cached_dataset_metadata(dataset_id)
                if not dataset_meta:
                    dataset_res = await self._get("/datasets", api_key, {"id": dataset_id, "page_size": 1})
                    if dataset_res and dataset_res.status_code == 200:
                        dataset_data = dataset_res.json()
                        if dataset_data.get("code") == 0 and dataset_data.get("data"):
                            dataset_info = dataset_data["data"][0]
                            dataset_meta = {
                                "name": dataset_info.get("name", "Unknown"),
                                "description": dataset_info.get("description", ""),
                            }
                            self._set_cached_dataset_metadata(dataset_id, dataset_meta)
                if dataset_meta:
                    dataset_cache[dataset_id] = dataset_meta

                docs = None if force_refresh else self._get_cached_document_metadata_by_dataset(dataset_id)
                if docs is None:
                    pg = 1
                    pg_size = 30
                    doc_id_meta_list: list[tuple[str, dict]] = []
                    docs = {}
                    while pg:
                        docs_res = await self._get(f"/datasets/{dataset_id}/documents", api_key, {"page": pg})
                        docs_data = docs_res.json()
                        if docs_data.get("code") == 0 and docs_data.get("data", {}).get("docs"):
                            for doc in docs_data["data"]["docs"]:
                                doc_id = doc.get("id")
                                if not doc_id:
                                    continue
                                doc_meta = {
                                    "document_id": doc_id,
                                    "name": doc.get("name", ""),
                                    "location": doc.get("location", ""),
                                    "type": doc.get("type", ""),
                                    "size": doc.get("size"),
                                    "chunk_count": doc.get("chunk_count"),
                                    "create_date": doc.get("create_date", ""),
                                    "update_date": doc.get("update_date", ""),
                                    "token_count": doc.get("token_count"),
                                    "thumbnail": doc.get("thumbnail", ""),
                                    "dataset_id": doc.get("dataset_id", dataset_id),
                                    "meta_fields": doc.get("meta_fields", {}),
                                }
                                doc_id_meta_list.append((doc_id, doc_meta))
                                docs[doc_id] = doc_meta

                            pg += 1
                            if docs_data.get("data", {}).get("total", 0) - pg * pg_size <= 0:
                                pg = None
                        else:
                            pg = None

                    self._set_cached_document_metadata_by_dataset(dataset_id, doc_id_meta_list)
                if docs:
                    document_cache.update(docs)

        except Exception as e:
            logging.error(f"Problem building the document metadata cache: {e}")

        return document_cache, dataset_cache

    def _map_chunk_fields(self, chunk_data: dict, dataset_cache: dict, document_cache: dict) -> dict:
        """Preserve all original API fields and add per-chunk document metadata."""
        mapped = dict(chunk_data)

        dataset_id = chunk_data.get("dataset_id") or chunk_data.get("kb_id")
        if dataset_id and dataset_id in dataset_cache:
            mapped["dataset_name"] = dataset_cache[dataset_id]["name"]
        else:
            mapped["dataset_name"] = "Unknown"

        mapped["document_name"] = chunk_data.get("document_keyword", "")

        document_id = chunk_data.get("document_id")
        if document_id and document_id in document_cache:
            mapped["document_metadata"] = document_cache[document_id]

        return mapped


# ---------------------------------------------------------------------------
# Auth middleware — handles token extraction for both modes
# ---------------------------------------------------------------------------
class AuthMiddleware:
    """
    ASGI-level gate that runs before FastMCP's ``RequestContextMiddleware``.

    self-host mode: validates that the Bearer token matches HOST_API_KEY.
    host mode: ensures a valid Bearer token or API key header is present.
               The actual token is later read inside tool handlers via
               ``get_http_headers()`` (fastmcp dependency injection).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        if path.startswith("/messages/") or path.startswith("/sse") or path.startswith("/mcp"):
            token = _extract_token_from_scope(scope)

            if not token:
                response = JSONResponse({"error": "Missing or invalid authorization header"}, status_code=401)
                await response(scope, receive, send)
                return

            if MODE == LaunchMode.SELF_HOST and token != HOST_API_KEY:
                response = JSONResponse({"error": "Invalid API key"}, status_code=401)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Module-level connector singleton
# ---------------------------------------------------------------------------
_connector: MultiRAGConnector | None = None


def _get_connector() -> MultiRAGConnector:
    global _connector
    if _connector is None:
        _connector = MultiRAGConnector(base_url=BASE_URL)
    return _connector


# ---------------------------------------------------------------------------
# FastMCP server instance (created lazily by create_mcp_server)
# ---------------------------------------------------------------------------
mcp: FastMCP | None = None


def create_mcp_server() -> FastMCP:
    global mcp
    mcp = FastMCP("multirag-mcp-server")
    _register_tools(mcp)
    return mcp


def _register_tools(server: FastMCP):
    """Register all MCP tools on the FastMCP server instance."""

    @server.tool(
        name="list_datasets",
        description=("List all available datasets with their IDs, names, descriptions, and embedding_model values. Use this to discover dataset IDs for the multirag_retrieval tool."),
    )
    async def tool_list_datasets(ctx: Context) -> list[dict]:
        api_key = _resolve_api_key()
        connector = _get_connector()
        await ctx.info("Listing available datasets")
        return await connector.list_datasets_structured(api_key=api_key)

    @server.tool(
        name="multirag_retrieval",
        description=(
            "Retrieve relevant chunks from the MultiRAG retrieval interface based on the question. "
            "You can specify dataset_ids to search only specific datasets. If dataset_ids is omitted, "
            "the tool will search all accessible datasets only when they share the same embedding_model. "
            "If multiple embedding_model groups exist, call list_datasets first and pass compatible "
            "dataset_ids from a single embedding_model group. You can also optionally specify "
            "document_ids to search within specific documents."
        ),
    )
    async def tool_multirag_retrieval(
        question: Annotated[str, Field(description="The question or query to search for.")],
        dataset_ids: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Optional array of dataset IDs to search. If multiple dataset_ids are provided, they must belong "
                    "to the same embedding_model group. If omitted, all accessible datasets will be searched only "
                    "when they share the same embedding_model."
                )
            ),
        ] = None,
        document_ids: Annotated[
            list[str] | None,
            Field(description="Optional array of document IDs to search within."),
        ] = None,
        page: Annotated[
            int,
            Field(description="Page number for pagination", ge=1),
        ] = 1,
        page_size: Annotated[
            int,
            Field(description="Number of results to return per page (default: 10, max recommended: 50 to avoid token limits)", ge=1, le=100),
        ] = 10,
        similarity_threshold: Annotated[
            float,
            Field(description="Minimum similarity threshold for results", ge=0.0, le=1.0),
        ] = 0.2,
        vector_similarity_weight: Annotated[
            float,
            Field(description="Weight for vector similarity vs term similarity", ge=0.0, le=1.0),
        ] = 0.3,
        keyword: Annotated[
            bool,
            Field(description="Enable keyword-based search"),
        ] = False,
        top_k: Annotated[
            int,
            Field(description="Maximum results to consider before ranking", ge=1, le=1024),
        ] = 1024,
        rerank_id: Annotated[
            str | None,
            Field(description="Optional reranking model identifier"),
        ] = None,
        force_refresh: Annotated[
            bool,
            Field(description="Set to true only if fresh dataset and document metadata is explicitly required. Otherwise, cached metadata is used (default: false)."),
        ] = False,
        ctx: Context | None = None,
    ) -> str:
        api_key = _resolve_api_key()
        connector = _get_connector()

        if ctx:
            await ctx.info(f"Searching for: {question}")

        result = await connector.retrieval(
            api_key=api_key,
            dataset_ids=dataset_ids or [],
            document_ids=document_ids,
            question=question,
            page=page,
            page_size=page_size,
            similarity_threshold=similarity_threshold,
            vector_similarity_weight=vector_similarity_weight,
            keyword=keyword,
            top_k=top_k,
            rerank_id=rerank_id,
            force_refresh=force_refresh,
        )

        return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# ASGI application factory — combines SSE and Streamable HTTP transports
# ---------------------------------------------------------------------------
def create_starlette_app() -> Starlette:
    assert mcp is not None, "MCP server must be created before building the ASGI app"

    routes: list = []
    lifespans: list = []

    if TRANSPORT_SSE_ENABLED:
        sse_app = create_sse_app(
            server=mcp,
            message_path="/messages/",
            sse_path="/sse",
        )
        routes.extend(sse_app.routes)

    if TRANSPORT_STREAMABLE_HTTP_ENABLED:
        http_app = create_streamable_http_app(
            server=mcp,
            streamable_http_path="/mcp",
            json_response=JSON_RESPONSE,
            stateless_http=True,
        )
        routes.extend(http_app.routes)
        lifespans.append(http_app.lifespan)

    if lifespans:
        from fastmcp.utilities.lifespan import combine_lifespans

        combined_lifespan = combine_lifespans(*lifespans)
    else:
        combined_lifespan = None

    # AuthMiddleware handles token validation/extraction for both modes.
    # RequestContextMiddleware is required by FastMCP for per-request context.
    parent_middleware = [
        Middleware(AuthMiddleware),
        Middleware(RequestContextMiddleware),
    ]

    return Starlette(
        debug=True,
        routes=routes,
        middleware=parent_middleware,
        lifespan=combined_lifespan,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
@click.command()
@click.option("--base-url", type=str, default="http://127.0.0.1:8123", help="API base URL for MultiRAG backend")
@click.option("--host", type=str, default="127.0.0.1", help="Host to bind the MultiRAG MCP server")
@click.option("--port", type=int, default=9382, help="Port to bind the MultiRAG MCP server")
@click.option(
    "--mode",
    type=click.Choice(["self-host", "host"]),
    default="self-host",
    help=("Launch mode:\n  self-host: run MCP for a single tenant (requires --api-key)\n  host: multi-tenant mode, users must provide Authorization headers"),
)
@click.option("--api-key", type=str, default="", help="API key to use when in self-host mode")
@click.option(
    "--transport-sse-enabled/--no-transport-sse-enabled",
    default=True,
    help="Enable or disable legacy SSE transport mode (default: enabled)",
)
@click.option(
    "--transport-streamable-http-enabled/--no-transport-streamable-http-enabled",
    default=True,
    help="Enable or disable streamable-http transport mode (default: enabled)",
)
@click.option(
    "--json-response/--no-json-response",
    default=True,
    help="Enable or disable JSON response mode for streamable-http (default: enabled)",
)
def main(base_url, host, port, mode, api_key, transport_sse_enabled, transport_streamable_http_enabled, json_response):
    import os

    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()

    def parse_bool_flag(key: str, default: bool) -> bool:
        val = os.environ.get(key, str(default))
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    global BASE_URL, HOST, PORT, MODE, HOST_API_KEY, TRANSPORT_SSE_ENABLED, TRANSPORT_STREAMABLE_HTTP_ENABLED, JSON_RESPONSE
    BASE_URL = os.environ.get("MULTIRAG_MCP_BASE_URL", base_url)
    HOST = os.environ.get("MULTIRAG_MCP_HOST", host)
    PORT = os.environ.get("MULTIRAG_MCP_PORT", str(port))
    MODE = os.environ.get("MULTIRAG_MCP_LAUNCH_MODE", mode)
    HOST_API_KEY = os.environ.get("MULTIRAG_MCP_HOST_API_KEY", api_key)
    TRANSPORT_SSE_ENABLED = parse_bool_flag("MULTIRAG_MCP_TRANSPORT_SSE_ENABLED", transport_sse_enabled)
    TRANSPORT_STREAMABLE_HTTP_ENABLED = parse_bool_flag("MULTIRAG_MCP_TRANSPORT_STREAMABLE_ENABLED", transport_streamable_http_enabled)
    JSON_RESPONSE = parse_bool_flag("MULTIRAG_MCP_JSON_RESPONSE", json_response)

    if MODE == LaunchMode.SELF_HOST and not HOST_API_KEY:
        raise click.UsageError("--api-key is required when --mode is 'self-host'")

    if not TRANSPORT_STREAMABLE_HTTP_ENABLED and JSON_RESPONSE:
        JSON_RESPONSE = False

    print(
        r"""
__  __  ____ ____       ____  _____ ______     _______ ____
|  \/  |/ ___|  _ \     / ___|| ____|  _ \ \   / / ____|  _ \
| |\/| | |   | |_) |    \___ \|  _| | |_) \ \ / /|  _| | |_) |
| |  | | |___|  __/      ___) | |___|  _ < \ V / | |___|  _ <
|_|  |_|\____|_|        |____/|_____|_| \_\ \_/  |_____|_| \_\
        """,
        flush=True,
    )
    print(f"MCP launch mode: {MODE}", flush=True)
    print(f"MCP host: {HOST}", flush=True)
    print(f"MCP port: {PORT}", flush=True)
    print(f"MCP base_url: {BASE_URL}", flush=True)

    if not any([TRANSPORT_SSE_ENABLED, TRANSPORT_STREAMABLE_HTTP_ENABLED]):
        print("At least one transport should be enabled, enable streamable-http automatically", flush=True)
        TRANSPORT_STREAMABLE_HTTP_ENABLED = True

    if TRANSPORT_SSE_ENABLED:
        print("SSE transport enabled: yes", flush=True)
        print("SSE endpoint available at /sse", flush=True)
    else:
        print("SSE transport enabled: no", flush=True)

    if TRANSPORT_STREAMABLE_HTTP_ENABLED:
        print("Streamable HTTP transport enabled: yes", flush=True)
        print("Streamable HTTP endpoint available at /mcp", flush=True)
        if JSON_RESPONSE:
            print("Streamable HTTP mode: JSON response enabled", flush=True)
        else:
            print("Streamable HTTP mode: SSE over HTTP enabled", flush=True)
    else:
        print("Streamable HTTP transport enabled: no", flush=True)
        if JSON_RESPONSE:
            print("Warning: --json-response ignored because streamable transport is disabled.", flush=True)

    create_mcp_server()

    uvicorn.run(
        create_starlette_app(),
        host=HOST,
        port=int(PORT),
    )


if __name__ == "__main__":
    """
    Launch examples:

    1. Self-host mode with both SSE and Streamable HTTP (in JSON response mode) enabled (default):
        uv run mcp/server/server.py --host=127.0.0.1 --port=9382 \
            --base-url=http://127.0.0.1:8123 \
            --mode=self-host --api-key=multirag-xxxxx

    2. Host mode (multi-tenant, clients must provide Authorization headers):
        uv run mcp/server/server.py --host=127.0.0.1 --port=9382 \
            --base-url=http://127.0.0.1:8123 \
            --mode=host

    3. Disable legacy SSE (only streamable HTTP will be active):
        uv run mcp/server/server.py --no-transport-sse-enabled \
            --mode=self-host --api-key=multirag-xxxxx

    4. Disable streamable HTTP (only legacy SSE will be active):
        uv run mcp/server/server.py --no-transport-streamable-http-enabled \
            --mode=self-host --api-key=multirag-xxxxx

    5. Use streamable HTTP with SSE-style events (disable JSON response):
        uv run mcp/server/server.py --transport-streamable-http-enabled --no-json-response \
            --mode=self-host --api-key=multirag-xxxxx

    6. Disable both transports (for testing):
        uv run mcp/server/server.py --no-transport-sse-enabled --no-transport-streamable-http-enabled \
            --mode=self-host --api-key=multirag-xxxxx
    """
    main()

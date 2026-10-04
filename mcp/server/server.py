import hashlib
import json
import logging
import random
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from enum import StrEnum
from importlib import metadata as importlib_metadata
from typing import Annotated
from urllib.parse import quote, unquote

import click
import httpx
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.resources.base import Resource
from fastmcp.server.auth import StaticTokenVerifier
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware
from fastmcp.server.providers.base import Provider
from fastmcp.utilities.lifespan import combine_lifespans
from fastmcp.utilities.versions import VersionSpec
from pydantic import AnyUrl, BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.types import ToolAnnotations


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
# Host/Origin 防护（DNS rebinding）额外信任名单；空列表 = fastmcp "auto" 默认
# （仅保护 localhost 绑定）。经公网域名部署时必须把该域名加进来。
ALLOWED_HOSTS: list[str] = []
ALLOWED_ORIGINS: list[str] = []
# 每客户端每秒请求上限（host 模式按 token 哈希区分租户；进程内计数，
# 多 worker 部署时各 worker 独立）
RATE_LIMIT_RPS = 10.0
SKILLS_RESOURCES_ENABLED = False


# ---------------------------------------------------------------------------
# Tool output models — 字段级 output_schema，客户端可直接反序列化 result.data
# ---------------------------------------------------------------------------
class DatasetInfo(BaseModel):
    """list_datasets 单条数据集条目。"""

    id: str
    name: str = ""
    description: str = ""
    embedding_model: str = ""
    embd_id: str = Field("", description="Legacy alias of embedding_model for older MCP consumers.")


class ChunkInfo(BaseModel):
    """检索命中 chunk。extra=allow：后端原始字段（content_with_weight、
    similarity、document_metadata 等开放字段集）全部透传，钉板测试锁此契约。"""

    model_config = ConfigDict(extra="allow")

    dataset_name: str = "Unknown"
    document_name: str = ""


class RetrievalPagination(BaseModel):
    page: int
    page_size: int
    total_chunks: int
    total_pages: int


class RetrievalQueryInfo(BaseModel):
    question: str
    similarity_threshold: float
    vector_weight: float
    keyword_search: bool
    dataset_count: int


class RetrievalResult(BaseModel):
    chunks: list[ChunkInfo]
    pagination: RetrievalPagination
    query_info: RetrievalQueryInfo


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
    # FastMCP strips Authorization by default. This is an explicit inbound
    # credential exchange with our own backend, not generic header forwarding.
    headers = get_http_headers(include={"authorization"})
    token = _extract_token_from_headers(headers)
    if not token:
        raise ToolError("MultiRAG API key or Bearer token is required.")
    return token


# ---------------------------------------------------------------------------
# MultiRAGConnector — async httpx-based backend API client
# ---------------------------------------------------------------------------
class MultiRAGConnector:
    _MAX_DATASET_CACHE = 32
    _CACHE_TTL = 300
    # 后端 /api/v1/datasets 目前未对 page_size 设上限；若 RESTful 层将来引入
    # max page size，此值必须同步下调，否则超限请求会被静默截断。
    _DATASET_PAGE_SIZE = 1000

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

    async def _fetch_datasets_page(
        self,
        *,
        api_key: str,
        page: int,
        page_size: int,
        orderby: str = "create_time",
        desc: bool = True,
        id: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Fetch one structured page of accessible datasets from the backend API."""
        params: dict = {"page": page, "page_size": page_size, "orderby": orderby, "desc": desc}
        if id:
            params["id"] = id
        if name:
            params["name"] = name

        res = await self._get("/datasets", api_key, params)
        if res is None or res.status_code != 200:
            error_message = None
            if res is not None:
                try:
                    error_message = res.json().get("message")
                except Exception:
                    error_message = None
            raise ToolError(error_message or "Cannot process this operation.")

        res_json = res.json()
        if res_json.get("code") != 0:
            raise ToolError(res_json.get("message") or "Cannot process this operation.")

        return res_json

    async def list_datasets_structured(self, api_key: str) -> list[dict]:
        """Return all accessible datasets with normalized SDK-facing metadata fields."""
        datasets: list[dict] = []
        seen_ids: set[str] = set()
        page = 1

        while True:
            logging.debug("list_datasets_structured fetching /datasets page=%s page_size=%s", page, self._DATASET_PAGE_SIZE)
            res_json = await self._fetch_datasets_page(api_key=api_key, page=page, page_size=self._DATASET_PAGE_SIZE)
            page_datasets = res_json.get("data", [])
            for d in page_datasets:
                if not d.get("id") or d["id"] in seen_ids:
                    continue
                seen_ids.add(d["id"])
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
            total = res_json.get("total")
            # total 缺失时保守收敛到单页，避免异常响应导致死循环。
            if not page_datasets or total is None or len(seen_ids) >= total:
                break
            page += 1

        logging.info("list_datasets_structured resolved %s accessible datasets", len(datasets))
        return datasets

    @staticmethod
    def _embedding_model_from_dataset(dataset: dict) -> str:
        return dataset.get("embedding_model") or dataset.get("embd_id") or ""

    async def _resolve_retrieval_dataset_ids(self, api_key: str, dataset_ids: list[str]) -> list[str]:
        if not dataset_ids:
            logging.info("MCP retrieval omitted dataset_ids; resolving accessible datasets")
        datasets = await self.list_datasets_structured(api_key)
        if not datasets:
            raise ToolError("No accessible datasets available for retrieval.")

        selected = datasets
        if dataset_ids:
            dataset_id_set = set(dataset_ids)
            selected = [d for d in datasets if d["id"] in dataset_id_set]
            found_ids = {d["id"] for d in selected}
            missing_ids = [dataset_id for dataset_id in dataset_ids if dataset_id not in found_ids]
            if missing_ids:
                raise ToolError("Unknown or inaccessible dataset_ids: " + ", ".join(missing_ids))

        grouped: dict[str, list[dict]] = {}
        for dataset in selected:
            grouped.setdefault(self._embedding_model_from_dataset(dataset), []).append(dataset)

        if len(grouped) > 1:
            group_desc = "; ".join(f"{embedding_model or '<unknown>'}: " + ", ".join(dataset.get("name") or dataset["id"] for dataset in group) for embedding_model, group in grouped.items())
            raise ToolError(f"Selected datasets use different embedding_model values. Choose dataset_ids from a single embedding_model group. Available groups: {group_desc}")

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
            raise ToolError("Cannot process this operation.")

        res_data = res.json()
        if res_data.get("code") != 0:
            msg = res_data.get("message") or res_data.get("retmsg") or json.dumps(res_data, ensure_ascii=False)
            raise ToolError(f"Retrieval API error: {msg}")
        data = res_data.get("data")
        if not isinstance(data, dict):
            raise ToolError("Retrieval API returned success without a valid data payload: " + json.dumps(res_data, ensure_ascii=False))

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
# Token-compat middleware — normalizes legacy auth headers for both modes
# ---------------------------------------------------------------------------
_PROTECTED_PREFIXES = ("/messages/", "/sse", "/mcp")


class TokenCompatMiddleware:
    """
    ASGI shim in front of the transport apps.

    Rewrites legacy ``api_key`` / ``x-api-key`` headers into a standard
    ``Authorization: Bearer`` header so the single downstream validation path
    (fastmcp ``StaticTokenVerifier`` in self-host mode; the backend API per
    request in host mode) sees every credential form we historically accept.
    Requests without any credential are rejected early in both modes; token
    VALUE validation lives downstream, not here.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http" or not scope["path"].startswith(_PROTECTED_PREFIXES):
            await self.app(scope, receive, send)
            return

        token = _extract_token_from_scope(scope)
        if not token:
            response = JSONResponse({"error": "Missing or invalid authorization header"}, status_code=401)
            await response(scope, receive, send)
            return

        headers = [(k, v) for k, v in scope["headers"] if k.lower() != b"authorization"]
        headers.append((b"authorization", f"Bearer {token}".encode()))
        scope = dict(scope)
        scope["headers"] = headers
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


def _server_version() -> str:
    """serverInfo.version 用我方项目版本；缺省会误报为 fastmcp 库版本。

    项目不以包形式安装（uv 非 package 布局），主路径是读仓库 pyproject.toml；
    importlib.metadata 留作未来打包安装形态的快路径。
    """
    try:
        return importlib_metadata.version("multirag")
    except importlib_metadata.PackageNotFoundError:
        pass
    try:
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        with open(pyproject, "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        return "0.0.0-dev"


def _rate_limit_client_id(_context: object) -> str:
    """host 模式按 token 哈希区分租户限流；self-host 单租户共用一个桶。"""
    if MODE == LaunchMode.SELF_HOST:
        return "self-host"
    token = _extract_token_from_headers(get_http_headers(include={"authorization"}))
    return hashlib.sha256(token.encode()).hexdigest()[:16] if token else "anonymous"


async def _skill_asset_json(path: str, api_key: str, params: dict[str, object] | None = None) -> dict[str, object]:
    """Use the asset API's authorization and lifecycle checks on every access."""
    response = await _get_connector()._get("/skill-assets" + path, api_key, params)
    if response is None or response.status_code != 200:
        raise ToolError("Skill asset is unavailable or access was denied.")
    payload = response.json()
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise ToolError("Skill asset request failed.")
    return payload["data"]


async def _skill_asset_rows(path: str, field: str, api_key: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    page = 1
    total: int | None = None
    while True:
        payload = await _skill_asset_json(path, api_key, {"page": page, "page_size": 100})
        batch, count = payload.get(field), payload.get("total")
        if not isinstance(batch, list) or not isinstance(count, int) or count < 0 or (total is not None and count != total):
            raise ToolError("Skill catalog changed or returned invalid pagination; retry discovery.")
        total = count
        for row in batch:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in seen:
                raise ToolError("Skill catalog returned duplicate or invalid assets.")
            seen.add(row["id"])
            rows.append(row)
        if len(rows) == total:
            return rows
        if not batch or len(rows) > total or len(rows) > 100_000:
            raise ToolError("Skill catalog pagination is incomplete.")
        page += 1


class SkillAssetResource(Resource):
    """Fixed-version, tenant-authorized resource with no filesystem cache."""

    space_id: str
    skill_id: str
    version_id: str
    asset_path: str
    literal_path: str | None = None

    async def read(self) -> str | bytes:
        api_key = _resolve_api_key()
        detail = await _skill_asset_json(f"/spaces/{self.space_id}/skills/{self.skill_id}", api_key)
        skill, versions = detail.get("skill"), detail.get("versions")
        if not isinstance(skill, dict) or skill.get("state") != "active" or not isinstance(versions, list):
            raise ToolError("Skill asset is unavailable.")
        if not any(isinstance(v, dict) and v.get("id") == self.version_id and v.get("state") == "installed" for v in versions):
            raise ToolError("Skill version is unavailable.")
        base = f"/spaces/{self.space_id}/versions/{self.version_id}"
        files = (await _skill_asset_json(base + "/files", api_key)).get("files")
        if not isinstance(files, list):
            raise ToolError("Invalid skill manifest.")
        if self.asset_path == "_manifest":
            if any(f.get("path") == "_manifest" for f in files):
                raise ToolError("This package contains the reserved MCP path _manifest; use its asset ZIP or rename that attachment in a new version.")
            return json.dumps({"skill": str(self.uri).split("/")[2], "files": [{"path": f["path"], "size": f["size"], "hash": f["sha256"]} for f in files]})
        # FastMCP's downloader appends manifest paths without URI quoting.
        # Prefer that exact filename, then accept an explicitly encoded URI.
        entry = next((f for f in files if f.get("path") == self.literal_path), None)
        if entry is None:
            entry = next((f for f in files if f.get("path") == self.asset_path), None)
        if entry is None:
            raise ToolError("Skill file is unavailable.")
        response = await _get_connector()._get("/skill-assets" + base + "/file", api_key, {"path": entry["path"]})
        if response is None or response.status_code != 200:
            raise ToolError("Skill file is unavailable.")
        content = response.content
        if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ToolError("Skill file did not match its manifest.")
        if entry["path"] == "SKILL.md":
            return content.decode("utf-8")
        return content


class SkillAssetProvider(Provider):
    """Discover active publications, then read bodies and attachments on demand."""

    async def get_tasks(self) -> list[Resource]:
        # Resources are read-only and request scoped. Startup task discovery
        # has no tenant credentials and must not enumerate the asset catalog.
        return []

    @staticmethod
    def _resource(space: str, skill: str, version: str, path: str, description: str = "") -> SkillAssetResource:
        key = f"{space}-{skill}-{version}"
        mime = "application/json" if path == "_manifest" else "text/markdown" if path == "SKILL.md" else "application/octet-stream"
        return SkillAssetResource(
            uri=AnyUrl(f"skill://{key}/{quote(path, safe='/')}"),
            name=f"{key}/{path}",
            description=description,
            mime_type=mime,
            space_id=space,
            skill_id=skill,
            version_id=version,
            asset_path=path,
        )

    async def _list_resources(self) -> list[Resource]:
        if not SKILLS_RESOURCES_ENABLED:
            return []
        api_key = _resolve_api_key()
        resources: list[Resource] = []
        for space in await _skill_asset_rows("/spaces", "spaces", api_key):
            for skill in await _skill_asset_rows(f"/spaces/{space['id']}/skills", "skills", api_key):
                version = skill.get("active_version_id")
                if skill.get("state") == "active" and isinstance(version, str):
                    resources.append(self._resource(str(space["id"]), str(skill["id"]), version, "SKILL.md", str(skill.get("description", ""))))
        return resources

    async def _get_resource(self, uri: str, version: VersionSpec | None = None) -> Resource | None:
        if not SKILLS_RESOURCES_ENABLED:
            return None
        match = re.fullmatch(r"skill://([a-f0-9]{32})-([a-f0-9]{32})-([a-f0-9]{32})/(.+)", uri)
        if match is None:
            return None
        space, skill, identity, encoded = match.groups()
        path = unquote(encoded)
        if any(part in ("", ".", "..") for part in path.split("/")) or "\\" in path or any(ord(c) < 32 for c in path):
            return None
        resource = self._resource(space, skill, identity, path)
        resource.literal_path = encoded
        return resource


def create_mcp_server() -> FastMCP:
    global mcp
    auth = None
    if MODE == LaunchMode.SELF_HOST and HOST_API_KEY:
        # self-host：静态密钥交给 fastmcp 官方验证器（标准 401 + WWW-Authenticate）；
        # host 模式不挂验证器，token 逐请求透传给后端 API 校验。
        auth = StaticTokenVerifier(tokens={HOST_API_KEY: {"client_id": "self-host", "sub": "self-host"}})
    mcp = FastMCP(
        "multirag-mcp-server",
        instructions=(
            "MultiRAG retrieval server. Call list_datasets first to discover dataset IDs "
            "and their embedding_model values, then call multirag_retrieval with a question "
            "and dataset_ids from a single embedding_model group. The dataset catalog is "
            "also readable as the datasets://list resource."
        ),
        version=_server_version(),
        auth=auth,
        mask_error_details=True,
    )
    # A failed tenant catalog request must not become a successful empty list.
    mcp.provider_error_strategy = "raise"
    # MCP 级中间件（先加=最外层）。刻意不加 ResponseCachingMiddleware：其 cache key
    # 只含操作名+参数、不含用户身份，host 多租户下会跨租户泄漏检索结果。
    mcp.add_middleware(ErrorHandlingMiddleware())
    mcp.add_middleware(RateLimitingMiddleware(max_requests_per_second=RATE_LIMIT_RPS, get_client_id=_rate_limit_client_id))
    mcp.add_middleware(TimingMiddleware())
    mcp.add_middleware(StructuredLoggingMiddleware(include_payloads=False))
    _register_tools(mcp)
    mcp.add_provider(SkillAssetProvider())
    return mcp


def _register_tools(server: FastMCP):
    """Register all MCP tools on the FastMCP server instance."""

    @server.tool(
        name="list_datasets",
        description=("List all available datasets with their IDs, names, descriptions, and embedding_model values. Use this to discover dataset IDs for the multirag_retrieval tool."),
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    async def tool_list_datasets(ctx: Context) -> list[DatasetInfo]:
        api_key = _resolve_api_key()
        connector = _get_connector()
        await ctx.info("Listing available datasets")
        datasets = await connector.list_datasets_structured(api_key=api_key)
        return [DatasetInfo(**d) for d in datasets]

    @server.resource(
        "datasets://list",
        mime_type="application/json",
        description="Dataset catalog (same payload as the list_datasets tool), readable as context without spending a tool call.",
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    async def resource_list_datasets() -> str:
        api_key = _resolve_api_key()
        datasets = await _get_connector().list_datasets_structured(api_key=api_key)
        return json.dumps(datasets, ensure_ascii=False)

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
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
        # 元数据缓存冷启动时会对多数据集全量分页拉取（多次 30s 下游调用累加），
        # MCP 层兜底超时防止单次调用无界拖长
        timeout=120.0,
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
    ) -> RetrievalResult:
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

        # 类型化返回：fastmcp 生成字段级 output schema + structured content，
        # 传统 TextContent 仍是 JSON 文本，旧消费者不受影响；
        # ChunkInfo extra=allow 保证后端原始字段全部透传。
        return RetrievalResult.model_validate(result)


# ---------------------------------------------------------------------------
# ASGI application factory — combines SSE and Streamable HTTP transports
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _connector_lifespan(app):
    try:
        yield
    finally:
        if _connector is not None:
            await _connector.close()


async def _health(_request) -> JSONResponse:
    return JSONResponse({"status": "ok", "mode": str(MODE)})


class _TransportDispatchApp:
    """按路径把请求原样分发到对应传输子 app。

    子 app 用完整内部路径构建（/mcp、/sse），分发不剥前缀——既保住
    既有消费者的 /mcp 端点形态，又让每个子 app 自带的中间件栈
    （RequestContext、鉴权、streamable HTTP 的 Host/Origin DNS-rebinding
    防护）原样生效。绝不抽子 app 的 routes 平铺：平铺会剥掉这些
    app 级中间件（旧实现的教训）。
    """

    def __init__(self, streamable_app: ASGIApp | None, sse_app: ASGIApp | None):
        self.streamable_app = streamable_app
        self.sse_app = sse_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        path = scope.get("path", "")
        if self.streamable_app is not None and (path == "/mcp" or path.startswith("/mcp/")):
            await self.streamable_app(scope, receive, send)
            return
        if self.sse_app is not None:
            await self.sse_app(scope, receive, send)
            return
        assert self.streamable_app is not None, "at least one transport must be enabled"
        await self.streamable_app(scope, receive, send)


def create_starlette_app() -> Starlette:
    assert mcp is not None, "MCP server must be created before building the ASGI app"

    streamable_app = None
    sse_app = None
    lifespans: list = [_connector_lifespan]

    if TRANSPORT_STREAMABLE_HTTP_ENABLED:
        streamable_app = mcp.http_app(
            path="/mcp",
            json_response=JSON_RESPONSE,
            stateless_http=True,
            allowed_hosts=ALLOWED_HOSTS or None,
            allowed_origins=ALLOWED_ORIGINS or None,
        )
        lifespans.append(streamable_app.lifespan)

    if TRANSPORT_SSE_ENABLED:
        sse_app = mcp.http_app(path="/sse", transport="sse")
        lifespans.append(sse_app.lifespan)

    routes: list = [
        Route("/health", _health, methods=["GET"]),
        Mount("/", app=_TransportDispatchApp(streamable_app, sse_app)),
    ]

    return Starlette(
        debug=False,
        routes=routes,
        middleware=[Middleware(TokenCompatMiddleware)],
        lifespan=combine_lifespans(*lifespans),
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
@click.option(
    "--allowed-hosts",
    type=str,
    default="",
    help="Comma-separated extra hostnames trusted by streamable-http Host/Origin protection (needed when serving behind a public hostname)",
)
@click.option(
    "--allowed-origins",
    type=str,
    default="",
    help="Comma-separated browser origins trusted by streamable-http Host/Origin protection",
)
@click.option(
    "--rate-limit-rps",
    type=float,
    default=10.0,
    help="Per-client requests-per-second cap (host mode buckets by token hash; in-process counter)",
)
def main(
    base_url: str,
    host: str,
    port: int,
    mode: str,
    api_key: str,
    transport_sse_enabled: bool,
    transport_streamable_http_enabled: bool,
    json_response: bool,
    allowed_hosts: str,
    allowed_origins: str,
    rate_limit_rps: float,
) -> None:
    import os

    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()

    def parse_bool_flag(key: str, default: bool) -> bool:
        val = os.environ.get(key, str(default))
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def parse_csv(key: str, default: str) -> list[str]:
        val = os.environ.get(key, default)
        return [item.strip() for item in val.split(",") if item.strip()]

    global BASE_URL, HOST, PORT, MODE, HOST_API_KEY, TRANSPORT_SSE_ENABLED, TRANSPORT_STREAMABLE_HTTP_ENABLED, JSON_RESPONSE, ALLOWED_HOSTS, ALLOWED_ORIGINS, RATE_LIMIT_RPS
    global SKILLS_RESOURCES_ENABLED
    SKILLS_RESOURCES_ENABLED = parse_bool_flag("MULTIRAG_MCP_SKILLS_RESOURCES_ENABLED", False)
    BASE_URL = os.environ.get("MULTIRAG_MCP_BASE_URL", base_url)
    HOST = os.environ.get("MULTIRAG_MCP_HOST", host)
    PORT = os.environ.get("MULTIRAG_MCP_PORT", str(port))
    MODE = os.environ.get("MULTIRAG_MCP_LAUNCH_MODE", mode)
    HOST_API_KEY = os.environ.get("MULTIRAG_MCP_HOST_API_KEY", api_key)
    TRANSPORT_SSE_ENABLED = parse_bool_flag("MULTIRAG_MCP_TRANSPORT_SSE_ENABLED", transport_sse_enabled)
    TRANSPORT_STREAMABLE_HTTP_ENABLED = parse_bool_flag("MULTIRAG_MCP_TRANSPORT_STREAMABLE_ENABLED", transport_streamable_http_enabled)
    JSON_RESPONSE = parse_bool_flag("MULTIRAG_MCP_JSON_RESPONSE", json_response)
    ALLOWED_HOSTS = parse_csv("MULTIRAG_MCP_ALLOWED_HOSTS", allowed_hosts)
    ALLOWED_ORIGINS = parse_csv("MULTIRAG_MCP_ALLOWED_ORIGINS", allowed_origins)
    RATE_LIMIT_RPS = float(os.environ.get("MULTIRAG_MCP_RATE_LIMIT_RPS", rate_limit_rps))

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

    2b. Public-hostname deployment (feed the DNS-rebinding guard) + OpenTelemetry
        (zero-code: SDK must exist before fastmcp import, so use the wrapper):
        OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318 \
        uv run opentelemetry-instrument python mcp/server/server.py \
            --mode=host --allowed-hosts=mcp.example.com

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

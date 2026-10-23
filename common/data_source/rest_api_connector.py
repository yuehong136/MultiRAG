"""Configuration-driven REST ingestion with bounded, DNS-pinned HTTP requests."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Generator, Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

import requests
from jsonpath_ng.exceptions import JSONPathError
from jsonpath_ng.ext import parse as parse_jsonpath
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from xxhash import xxh128_hexdigest as hash128

from common.data_source.config import INDEX_BATCH_SIZE, DocumentSource
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError
from common.data_source.interfaces import LoadConnector, PollConnector
from common.data_source.models import Document
from common.ssrf_guard import assert_url_is_safe, pin_dns

_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _object(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _text_to_dict(value: Any) -> dict[str, str]:
    if isinstance(value, str) and value.strip() and not value.lstrip().startswith("{"):
        result = {}
        for line in value.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, sep, val = line.partition("=")
            if not sep or not key.strip():
                raise ValueError("Expected key=value lines")
            result[key.strip()] = val.strip()
        return result
    return {str(k): str(v) for k, v in _object(value).items()}


class RestAPIConnectorConfig(BaseModel):
    """Stored REST config. Validation does no network I/O and hides input values."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    url: str
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    auth_type: Literal["none", "api_key_header", "bearer", "basic"] = "none"
    auth_config: dict[str, Any] = Field(default_factory=dict)
    items_path: str | None = None
    id_field: str | None = None
    content_fields: list[str]
    metadata_fields: list[str] = Field(default_factory=list)
    pagination_type: Literal["none", "page", "offset", "cursor"] = "none"
    pagination_config: dict[str, Any] = Field(default_factory=dict)
    poll_timestamp_field: str | None = None
    request_body: dict[str, Any] = Field(default_factory=dict)
    field_type_hints: dict[str, Literal["string", "number", "date"]] = Field(default_factory=dict)
    field_default_values: dict[str, Any] = Field(default_factory=dict)
    content_template: str | None = None
    batch_size: int = Field(default=INDEX_BATCH_SIZE, ge=1, le=10000)
    max_pages: int = Field(default=1000, ge=1, le=10000)
    request_delay: float = Field(default=0.5, ge=0, le=60, allow_inf_nan=False)

    @field_validator("method", mode="before")
    @classmethod
    def uppercase_method(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("headers", "query_params", mode="before")
    @classmethod
    def text_dict(cls, value: Any) -> dict[str, str]:
        return _text_to_dict(value)

    @field_validator("request_body", "auth_config", "pagination_config", "field_type_hints", "field_default_values", mode="before")
    @classmethod
    def json_object(cls, value: Any) -> dict[str, Any]:
        return _object(value)

    @field_validator("content_fields", "metadata_fields", mode="before")
    @classmethod
    def field_list(cls, value: Any) -> Any:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @model_validator(mode="after")
    def validate_settings(self) -> RestAPIConnectorConfig:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("Expected an http(s) URL without embedded credentials")
        if not self.content_fields or any(not field.strip() for field in self.content_fields):
            raise ValueError("At least one nonempty content field is required")
        for path in [*self.content_fields, *self.metadata_fields, self.id_field, self.poll_timestamp_field, self.items_path]:
            if path:
                parse_jsonpath(path)
        for key in ("next_cursor_path", "has_next_page_path"):
            if path := self.pagination_config.get(key):
                parse_jsonpath(path)
        for key in ("page_size", "limit"):
            if key in self.pagination_config and int(self.pagination_config[key]) <= 0:
                raise ValueError(f"{key} must be positive")
        for key in ("start_page", "start_offset"):
            if key in self.pagination_config and int(self.pagination_config[key]) < 0:
                raise ValueError(f"{key} must be nonnegative")
        for key in ("page_param", "page_size_param", "offset_param", "limit_param", "cursor_param"):
            if key in self.pagination_config and (not isinstance(self.pagination_config[key], str) or not self.pagination_config[key]):
                raise ValueError(f"{key} must be a nonempty parameter name")
        if self.pagination_type == "cursor" and not (self.pagination_config.get("next_cursor_path") or self.pagination_config.get("next_cursor_field")):
            raise ValueError("Cursor pagination requires next_cursor_path or next_cursor_field")
        return self


class RestAPIConnector(LoadConnector, PollConnector):
    def __init__(self, url: str, **config: Any) -> None:
        self.config = self.parse_storage_config({"url": url, **config})
        self._credentials: dict[str, Any] = {}
        self._auth_headers: dict[str, str] = {}
        self._basic_auth: tuple[str, str] | None = None
        self._paths: dict[str, Any] = {}

    @classmethod
    def parse_storage_config(cls, raw: dict[str, Any]) -> RestAPIConnectorConfig:
        try:
            return RestAPIConnectorConfig.model_validate(raw)
        except (ValidationError, ValueError, TypeError, JSONPathError):
            # Do not include Pydantic inputs, URLs, headers or vendor responses.
            raise ConnectorValidationError("Invalid REST API configuration. Check URL, fields and pagination settings.") from None

    @classmethod
    def from_parsed_config(cls, config: RestAPIConnectorConfig) -> RestAPIConnector:
        return cls(**config.model_dump())

    def load_credentials(self, credentials: dict[str, Any]) -> None:
        if not isinstance(credentials, dict):
            raise ConnectorValidationError("REST API credentials must be an object.")
        self._credentials = credentials
        cfg = self.config
        values = {**cfg.auth_config, **credentials}
        self._auth_headers = {}
        self._basic_auth = None
        if cfg.auth_type == "api_key_header":
            header = cfg.auth_config.get("header_name")
            key = credentials.get("api_key") or values.get("api_key_value") or values.get("api_key")
            if not header or not key:
                raise ConnectorMissingCredentialError("REST API: header_name and api_key required")
            self._auth_headers[str(header)] = str(key)
        elif cfg.auth_type == "bearer":
            if not values.get("token"):
                raise ConnectorMissingCredentialError("REST API: token required")
            self._auth_headers["Authorization"] = f"Bearer {values['token']}"
        elif cfg.auth_type == "basic":
            if not values.get("username") or values.get("password") is None:
                raise ConnectorMissingCredentialError("REST API: username and password required")
            self._basic_auth = str(values["username"]), str(values["password"])

    @classmethod
    def validate_config(cls, config: dict[str, Any], credentials: dict[str, Any] | None = None) -> RestAPIConnectorConfig:
        cfg = cls.parse_storage_config(config)
        connector = cls.from_parsed_config(cfg)
        connector.load_credentials(credentials or config.get("credentials") or {})
        # Fetch and convert the entire first page; valid empty arrays are OK.
        for item in connector._iter_items(first_page_only=True):
            connector._item_to_document(item)
        return cfg

    def _path(self, data: Any, path: str) -> list[Any]:
        if path not in self._paths:
            self._paths[path] = parse_jsonpath(path)
        return [match.value for match in self._paths[path].find(data)]

    def _fetch_page(self, params: dict[str, Any]) -> Any:
        cfg = self.config
        parsed = urlsplit(cfg.url)
        query = {**dict(parse_qsl(parsed.query, keep_blank_values=True)), **cfg.query_params, **params}
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        for key in list(query):
            if "{" + key + "}" in url:
                url = url.replace("{" + key + "}", str(query.pop(key)))
        headers = {**cfg.headers, **self._auth_headers}
        auth = self._basic_auth
        for attempt in range(3):
            try:
                return self._request_json(url, query, headers, auth)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 2:
                    raise ConnectorValidationError("REST API network request failed.") from None
            except requests.RequestException:
                raise ConnectorValidationError("REST API request failed.") from None
            time.sleep(2**attempt)
        raise ConnectorValidationError("REST API request failed.")

    def _request_json(self, url: str, params: dict[str, Any], headers: dict[str, str], auth: tuple[str, str] | None) -> Any:
        method = self.config.method
        body = self.config.request_body
        with requests.Session() as session:
            # Never use environment proxies, .netrc or cookies across redirects.
            session.trust_env = False
            for hop in range(6):
                try:
                    parsed = urlsplit(url)
                    if parsed.username is not None or parsed.password is not None:
                        raise ValueError("URL credentials are forbidden")
                    # Match Requests' actual IDNA/escaping before validating and
                    # pinning, including URLs supplied by a redirect hop.
                    prepared_url = requests.Request(method, url).prepare().url
                    if prepared_url is None:
                        raise ValueError("URL is missing")
                    url = prepared_url
                    hostname, ip = assert_url_is_safe(url)
                except ValueError:
                    raise ConnectorValidationError("REST API destination is not allowed or cannot be resolved.") from None
                with pin_dns(hostname, ip):
                    response = session.request(method, url, params=params, headers=headers, auth=auth, json=body if method == "POST" else None, timeout=(10, 60), allow_redirects=False, stream=True)
                with response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if hop == 5 or not response.headers.get("Location"):
                            raise ConnectorValidationError("REST API redirect limit or missing location.")
                        try:
                            target = urljoin(url, response.headers["Location"])
                            old, new = urlsplit(url), urlsplit(target)
                            same_origin = (old.scheme, old.hostname, old.port) == (new.scheme, new.hostname, new.port)
                        except ValueError:
                            raise ConnectorValidationError("REST API redirect URL is malformed.") from None
                        if not same_origin:
                            if method == "POST":
                                raise ConnectorValidationError("Cross-origin POST redirects are not allowed.")
                            headers, auth = {}, None
                        if response.status_code == 303 or (response.status_code in {301, 302} and method == "POST"):
                            method, body = "GET", {}
                        session.cookies.clear()
                        url, params = target, {}
                        continue
                    if response.status_code in {401, 403}:
                        raise ConnectorMissingCredentialError(f"REST API authentication failed (HTTP {response.status_code})")
                    if response.status_code >= 400:
                        if response.status_code == 429 or response.status_code >= 500:
                            raise requests.ConnectionError("Retryable REST API status")
                        raise ConnectorValidationError(f"REST API request failed (HTTP {response.status_code}).")
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > _MAX_RESPONSE_BYTES:
                            raise ConnectorValidationError("REST API response exceeds 16 MiB.")
                        chunks.append(chunk)
                    try:
                        return json.loads(b"".join(chunks))
                    except (ValueError, UnicodeError):
                        raise ConnectorValidationError("REST API response is not valid JSON.") from None
        raise ConnectorValidationError("REST API request failed.")

    def _resolve_page_size(self) -> int:
        cfg = self.config.pagination_config
        key = cfg.get("page_size_param") or cfg.get("limit_param")
        query = {**dict(parse_qsl(urlsplit(self.config.url).query)), **self.config.query_params}
        return int(cfg.get("page_size") or (query.get(key) if key else None) or self.config.batch_size)

    def _extract_items(self, data: Any) -> list[dict[str, Any]]:
        items: Any
        if self.config.items_path:
            matches = self._path(data, self.config.items_path)
            if not matches:
                if self.config.items_path.endswith("[*]") and self._path(data, self.config.items_path[:-3]) == [[]]:
                    return []
                raise ConnectorValidationError("Configured items_path does not exist in response.")
            items = matches[0] if len(matches) == 1 and isinstance(matches[0], list) else matches
        elif isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = next((data[k] for k in ("items", "results", "data", "records") if isinstance(data.get(k), list)), None)
            if items is None:
                items = next((v for v in data.values() if isinstance(v, list)), None)
        else:
            items = None
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ConnectorValidationError("REST API response must contain an array of objects.")
        return items

    def _next_cursor(self, data: Any) -> str | None:
        cfg = self.config.pagination_config
        if cfg.get("next_cursor_path"):
            matches = self._path(data, cfg["next_cursor_path"])
            if not matches:
                raise ConnectorValidationError("Configured cursor path is missing.")
            value = matches[0]
        else:
            if not isinstance(data, dict) or cfg["next_cursor_field"] not in data:
                raise ConnectorValidationError("Configured cursor field is missing.")
            value = data[cfg["next_cursor_field"]]
        if value is None or value == "":
            return None
        if isinstance(value, (dict, list, bool)):
            raise ConnectorValidationError("REST API cursor must be a string or number.")
        return str(value)

    def _iter_items(self, *, first_page_only: bool = False) -> Generator[dict[str, Any], None, None]:
        cfg, pc = self.config, self.config.pagination_config
        page = int(pc.get("start_page", 1))
        offset = int(pc.get("start_offset", 0))
        size = self._resolve_page_size()
        limit = int(pc.get("limit", size))
        if size <= 0:
            raise ConnectorValidationError("Page size must be positive.")
        initial_cursor = pc.get("initial_cursor")
        cursor = None if initial_cursor is None or initial_cursor == "" else str(initial_cursor)
        seen = {cursor} if cursor is not None else set()
        for count in range(cfg.max_pages):
            params: dict[str, Any] = {}
            if cfg.pagination_type == "page":
                params[pc.get("page_param", "page")] = page
                if pc.get("page_size_param"):
                    params[pc["page_size_param"]] = size
            elif cfg.pagination_type == "offset":
                params[pc.get("offset_param", "offset")] = offset
                if pc.get("limit_param"):
                    params[pc["limit_param"]] = limit
            elif cfg.pagination_type == "cursor" and cursor is not None:
                params[pc.get("cursor_param", "cursor")] = cursor
            if count and cfg.request_delay:
                time.sleep(cfg.request_delay)
            data = self._fetch_page(params)
            items = self._extract_items(data)
            yield from items
            if first_page_only or cfg.pagination_type == "none":
                return
            explicit_more = None
            if path := pc.get("has_next_page_path") or pc.get("has_next_page_field"):
                matches = self._path(data, path)
                if len(matches) != 1 or type(matches[0]) is not bool:
                    raise ConnectorValidationError("has_next_page_path must resolve to one boolean.")
                explicit_more = matches[0]
            if cfg.pagination_type == "cursor":
                if explicit_more is False:
                    return
                cursor = self._next_cursor(data)
                if cursor is None and explicit_more is None:
                    return
                if cursor is None or cursor in seen:
                    raise ConnectorValidationError("REST API cursor is missing or repeated before completion.")
                seen.add(cursor)
            elif explicit_more is False or (explicit_more is None and len(items) < (limit if cfg.pagination_type == "offset" else size)):
                return
            page += 1
            offset += limit
        raise ConnectorValidationError("REST API max_pages reached before pagination completed.")

    def load_from_state(self) -> Generator[list[Document], None, None]:
        return self._yield_documents(None)

    def poll_source(self, start: float, end: float) -> Generator[list[Document], None, None]:
        return self._yield_documents((datetime.fromtimestamp(start, UTC), datetime.fromtimestamp(end, UTC)))

    def _yield_documents(self, window: tuple[datetime, datetime] | None) -> Generator[list[Document], None, None]:
        batch = []
        for item in self._iter_items():
            doc = self._item_to_document(item)
            if window and self.config.poll_timestamp_field and not (window[0] <= doc.doc_updated_at < window[1]):
                continue
            batch.append(doc)
            if len(batch) >= self.config.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def _field(self, item: Mapping[str, Any], path: str) -> Any:
        values = self._path(item, path)
        if not values:
            return self.config.field_default_values.get(path)
        hint = self.config.field_type_hints.get(path)
        converted = []
        for value in values:
            if hint == "string":
                value = self._text(value)
            elif hint == "number":
                try:
                    value = float(value)
                    if not math.isfinite(value):
                        raise ValueError
                except (ValueError, TypeError):
                    raise ConnectorValidationError("A mapped number field is invalid.") from None
                value = int(value) if value.is_integer() else value
            elif hint == "date":
                value = self._timestamp(value).isoformat()
            converted.append(value)
        return converted[0] if len(converted) == 1 else ", ".join(self._text(v) for v in converted)

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        try:
            if isinstance(value, bool) or value is None:
                raise ValueError
            if isinstance(value, (int, float)):
                return datetime.fromtimestamp(value, UTC)
            dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)
        except (ValueError, TypeError, OverflowError):
            raise ConnectorValidationError("A mapped timestamp is missing or invalid.") from None

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _strip_html(value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip() if "<" in value and ">" in value else value

    def _item_to_document(self, item: Mapping[str, Any]) -> Document:
        cfg = self.config
        raw_id = self._field(item, cfg.id_field) if cfg.id_field else hash128(json.dumps(item, sort_keys=True, ensure_ascii=False))
        if raw_id is None or isinstance(raw_id, (dict, list, bool)):
            raise ConnectorValidationError("Configured document ID field is missing or invalid.")
        fields = {path: self._field(item, path) for path in set(cfg.content_fields + cfg.metadata_fields)}
        if cfg.content_template:
            values = {re.sub(r"\[\d+\]|\[\*\]", "", key).replace(".", "_"): self._text(value) for key, value in fields.items()}
            try:
                text = cfg.content_template.format_map(values)
            except (ValueError, KeyError, IndexError, AttributeError):
                raise ConnectorValidationError("Content template references an invalid field or format.") from None
        else:
            text = "\n".join(self._strip_html(self._text(fields[field])) for field in cfg.content_fields if fields[field] is not None)
        text = self._strip_html(text)
        if not text.strip():
            raise ConnectorValidationError("Mapped document content is empty.")
        metadata = {path: fields[path] if isinstance(fields[path], (str, int, float, bool)) else self._text(fields[path]) for path in cfg.metadata_fields if fields[path] is not None}
        timestamp = self._timestamp(self._field(item, cfg.poll_timestamp_field)) if cfg.poll_timestamp_field else datetime.now(UTC)
        blob = text.encode()
        # Metadata-only changes must also pass the upload duplicate gate.
        fingerprint = hash128(json.dumps({"text": text, "metadata": metadata}, sort_keys=True, ensure_ascii=False))
        return Document(
            id=hash128(f"rest_api:{raw_id}"),
            source=DocumentSource.REST_API,
            semantic_identifier=self._strip_html(self._text(fields[cfg.content_fields[0]])).replace("\n", " ")[:100] or str(raw_id),
            extension=".txt",
            blob=blob,
            size_bytes=len(blob),
            doc_updated_at=timestamp,
            metadata=metadata or None,
            fingerprint=fingerprint,
        )

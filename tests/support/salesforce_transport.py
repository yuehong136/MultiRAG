"""Controlled requests adapter for Salesforce OAuth, describe and SOQL pages."""

from __future__ import annotations

import copy
import io
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import HTTPAdapter
from requests.models import PreparedRequest, Response

ORIGIN = "https://owned.my.salesforce.com"
TOKEN = "controlled-salesforce-access-token"
SECRET = "controlled-salesforce-client-secret"
MODIFIED = "2026-01-01T00:00:00Z"
ACCOUNT_A = "001000000000001"
ACCOUNT_B = "001000000000002"


def configuration(**options: Any) -> dict[str, Any]:
    return {
        "objects": ["Account"],
        "batch_size": 1,
        "credentials": {"instance_url": ORIGIN, "client_id": "controlled-client", "client_secret": SECRET},
        **options,
    }


class SalesforceBody(io.BytesIO):
    """Model urllib3's explicit release after the stream is exhausted."""

    def release_conn(self) -> None:
        self.close()


class SalesforceTransport(HTTPAdapter):
    """Real Session preparation, with no network socket or cloud account."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[PreparedRequest] = []
        self.responses: list[Response] = []
        self.send_options: list[dict[str, Any]] = []
        self.closed = False
        self.close_count = 0
        self.mode = "normal"
        self.canonical: Any = ORIGIN
        self.available: dict[str, bool] = dict.fromkeys(("Account", "Contact", "Opportunity", "Case"), True)
        self.records: dict[str, list[dict[str, Any]]] = {
            "Account": [
                {"Id": ACCOUNT_A, "SystemModstamp": MODIFIED, "Name": "First account", "Number": 0, "Flag": False, "Empty": "", "Nested": {"z": 1, "a": 2}, "attributes": {"url": "ignored"}},
                {"Id": ACCOUNT_B, "SystemModstamp": MODIFIED, "Name": "Second account"},
            ],
            "Contact": [],
            "Opportunity": [],
            "Case": [],
        }
        self.describe_status: dict[str, int] = {}
        self.fields: list[dict[str, Any]] = [{"name": name, "type": "string"} for name in ("Id", "SystemModstamp", "Name", "Number", "Flag", "Empty", "Nested")]
        self.fields.append({"name": "BillingAddress", "type": "address"})

    def close(self) -> None:
        self.closed = True
        self.close_count += 1
        super().close()

    def _response(self, request: PreparedRequest, status: int, payload: Any, headers: dict[str, str] | None = None, *, raw_body: bytes | None = None) -> Response:
        response = Response()
        response.status_code = status
        response.url = request.url
        response.request = request
        response.headers.update({"Content-Type": "application/json", **(headers or {})})
        response.raw = SalesforceBody(raw_body if raw_body is not None else json.dumps(payload).encode())
        self.responses.append(response)
        return response

    def send(self, request: PreparedRequest, **kwargs: Any) -> Response:
        self.requests.append(request)
        self.send_options.append(kwargs)
        self.closed = False
        target = urlsplit(request.url or "")
        assert f"{target.scheme}://{target.netloc}" == ORIGIN, "connector sent credentials to an untrusted origin"
        if target.path == "/services/oauth2/token":
            if self.mode == "token-error":
                return self._response(request, 400, {"error_description": SECRET})
            if self.mode == "token-redirect":
                return self._response(request, 307, {}, {"Location": "https://outside.invalid/steal"})
            return self._response(request, 200, {"access_token": TOKEN, "instance_url": self.canonical})
        assert request.headers.get("Authorization") == "Bearer " + TOKEN
        if target.path.endswith("/sobjects"):
            if self.mode == "inventory-error":
                return self._response(request, 403, {"secret": SECRET})
            return self._response(request, 200, {"sobjects": [{"name": name, "queryable": queryable} for name, queryable in self.available.items()]})
        if target.path.endswith("/describe"):
            obj = target.path.split("/")[-2]
            return self._response(request, self.describe_status.get(obj, 200), {"fields": self.fields})
        if target.path.endswith("/query"):
            query = parse_qs(target.query)["q"][0]
            for literal in re.findall(r"SystemModstamp\s+(?:>=|<=)\s+(\S+)", query):
                assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", literal), "SOQL WHERE requires a supported whole-second dateTime literal"
            match = re.search(r" FROM ([A-Za-z][A-Za-z0-9_]*)", query)
            assert match is not None, "missing SOQL object"
            obj, offset = match.group(1), 0
        else:
            match = re.fullmatch(r"/services/data/v[0-9]+\.[0-9]+/query/([A-Za-z][A-Za-z0-9_]*)-([0-9]+)", target.path)
            assert match is not None, "unexpected Salesforce endpoint"
            obj, offset = match.group(1), int(match.group(2))
        if self.mode == "request-error":
            raise requests.ConnectionError("transport-secret=" + SECRET)
        if self.mode == "later-page" and offset:
            return self._response(request, 503, {"error": SECRET})
        if self.mode == "query-redirect":
            return self._response(request, 307, {}, {"Location": "https://outside.invalid/steal"})
        records = self.records.get(obj, [])
        selected = copy.deepcopy(records[offset : offset + 1])
        done = offset + len(selected) >= len(records)
        payload: dict[str, Any] = {"records": selected, "done": done, "totalSize": len(records)}
        if not done:
            payload["nextRecordsUrl"] = target.path.split("/query", 1)[0] + f"/query/{obj}-{offset + 1}"
        if self.mode == "cross-origin" and not done:
            payload["nextRecordsUrl"] = "https://other-org.my.salesforce.com/services/data/v59.0/query/stolen"
        elif self.mode == "scheme-relative" and not done:
            payload["nextRecordsUrl"] = "//outside.invalid/services/data/v59.0/query/stolen"
        elif self.mode == "malformed-host" and not done:
            payload["nextRecordsUrl"] = f"https://[{TOKEN}]/services/data/v59.0/query/stolen"
        elif self.mode == "wrong-endpoint" and not done:
            payload["nextRecordsUrl"] = "/services/data/v59.0/sobjects/Account"
        elif self.mode == "encoded-path" and not done:
            payload["nextRecordsUrl"] = "/services/data/v59.0/query/%2e%2e/Account"
        elif self.mode == "repeat" and not done:
            payload["nextRecordsUrl"] = f"/services/data/v59.0/query/{obj}-0"
        elif self.mode == "missing-next" and not done:
            payload.pop("nextRecordsUrl")
        elif self.mode == "missing-done":
            payload.pop("done")
        elif self.mode == "bad-records":
            payload["records"] = {}
        elif self.mode == "wrong-total":
            payload["totalSize"] = len(records) + 1
        elif self.mode == "malformed-json":
            return self._response(request, 200, {}, raw_body=b"{" + SECRET.encode())
        elif self.mode == "done-with-next":
            payload["done"] = True
            payload["nextRecordsUrl"] = f"/services/data/v59.0/query/{obj}-1"
        return self._response(request, 200, payload)


def install_transport(monkeypatch: Any, boundary: SalesforceTransport) -> list[requests.Session]:
    """Replace only Session's transport while retaining prepared HTTP requests."""
    from common.data_source import salesforce_connector as salesforce

    sessions: list[requests.Session] = []

    def send(value: requests.Session, method: str, url: str, **kwargs: Any) -> Response:
        # Production may install its tracked socket adapter immediately before
        # this seam. Keep Session serialization, replacing only the transport.
        value.mount("https://", boundary)
        if value not in sessions:
            sessions.append(value)
        return value.request(method, url, **kwargs)

    monkeypatch.setattr(salesforce, "_send_request", send, raising=False)
    return sessions

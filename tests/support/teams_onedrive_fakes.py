"""Controlled Graph HTTPS responses; the installed Teams SDK stays real."""

import json
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import requests

BASE = "https://graph.microsoft.com/v1.0"
OLD = "2026-01-01T00:00:00Z"
MODIFIED = "2026-01-02T00:00:00Z"
CREDS = {"tenant_id": "tenant", "client_id": "client", "client_secret": "controlled-secret"}


class MicrosoftHTTP:
    def __init__(self, source: str) -> None:
        self.source = source
        self.calls: list[str] = []
        self.mode = "complete"
        self.body = b'{"empty":[],"count":0}'
        self.modified = MODIFIED
        self.reply = "new reply <script>"
        self.after_request: Any = None

    def file(self, identifier: str, name: str) -> dict[str, Any]:
        return {"id": identifier, "name": name, "file": {}, "lastModifiedDateTime": self.modified, "size": 999, "webUrl": "https://contoso.sharepoint.com/" + name}

    def message(self, identifier: str, text: str, modified: str = OLD, html: bool = False) -> dict[str, Any]:
        return {"id": identifier, "body": {"content": text, "contentType": "html" if html else "text"}, "lastModifiedDateTime": modified, "webUrl": "https://teams.microsoft.com/" + identifier}

    def __call__(self, url: str, **kwargs: Any) -> requests.Response:
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        assert 0 < kwargs["timeout"][0] <= 10 and 0 < kwargs["timeout"][1] <= 60
        assert kwargs["headers"].get("Authorization") == "Bearer controlled-token"
        self.calls.append(url)
        path = unquote(urlsplit(url).path)
        page = parse_qs(urlsplit(url).query).get("page", [""])[0]
        status, payload = 200, None
        if self.source == "teams":
            if path == "/v1.0/teams":
                payload = {"value": [{"id": "t1", "displayName": "Team"}] if not page else [{"id": "t2"}]}
                if not page:
                    payload["@odata.nextLink"] = BASE + "/teams?page=2"
            elif path.endswith("/channels"):
                payload = {"value": [{"id": "c1", "displayName": "Channel"}] if "/t1/" in path else []}
            elif path.endswith("/messages"):
                parent = self.message("m1", "<b>old parent</b>", html=True)
                parent["attachments"] = [{"name": "report.txt", "contentUrl": "https://contoso.sharepoint.com/report.txt"}]
                payload = {"value": [parent] if not page else [self.message("m2", "standalone", self.modified)]}
                if not page:
                    payload["@odata.nextLink"] = BASE + "/teams/t1/channels/c1/messages?page=2"
            elif path.endswith("/replies"):
                payload = {"value": []}
                if "/m1/" in path:
                    payload = {"value": [self.message("r1", "older reply")] if not page else [self.message("r2", self.reply, self.modified)]}
                    if not page:
                        payload["@odata.nextLink"] = BASE + "/teams/t1/channels/c1/messages/m1/replies?page=2"
            else:
                raise AssertionError(path)
        else:
            if path in {"/v1.0/drives/A", "/v1.0/drives/B"}:
                payload = {"id": path.rsplit("/", 1)[-1]}
            elif "/root:" in path:
                payload = {"id": "scoped-folder", "folder": {}}
            elif path.endswith("/delta"):
                files = [self.file("same", "report.txt"), {"id": "folder", "folder": {}}]
                if "/A/" in path and not page:
                    payload = {"value": files, "@odata.nextLink": BASE + path.removeprefix("/v1.0") + "?page=2"}
                else:
                    payload = {
                        "value": [self.file("same", "report.txt"), self.file("nested", "日本語.md")] if "/A/" in path else [self.file("same", "second.txt")],
                        "@odata.deltaLink": BASE + path.removeprefix("/v1.0") + "?token=saved",
                    }
            elif path.endswith("/content"):
                payload = None
            else:
                raise AssertionError(path)
        if self.mode == "empty":
            payload = {"value": []}
            if path.endswith("/delta"):
                payload["@odata.deltaLink"] = BASE + path.removeprefix("/v1.0") + "?token=empty"
        if self.mode == "permission" or (self.mode == "later-page" and page):
            status, payload = 403, {"error": {"message": "controlled-secret-must-not-leak"}}
        if self.mode == "content" and (path.endswith("/content") or path.endswith("/replies")):
            status, payload = 403, {"error": {"message": "controlled-secret-must-not-leak"}}
        if self.mode == "malformed":
            payload = {"unexpected": []}
        if self.mode == "repeat" and page:
            payload = {"value": [], "@odata.nextLink": url}
        if self.mode == "untrusted":
            payload = {"value": [], "@odata.nextLink": "https://untrusted.test/next"}
        if self.mode == "missing-delta" and path.endswith("/delta"):
            payload = {"value": []}
        response = requests.Response()
        response.status_code, response.url = status, url
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(payload).encode() if payload is not None else self.body
        response._content_consumed = True
        if self.after_request:
            self.after_request(path)
        return response


def install_graph(monkeypatch: Any, http: MicrosoftHTTP) -> dict[str, Any]:
    from common.data_source import microsoft_graph

    auth: dict[str, Any] = {"constructed": 0, "acquired": 0}

    class App:
        def __init__(self, **kwargs: Any) -> None:
            auth["constructed"] += 1
            assert 0 < kwargs["timeout"] <= 30

        def acquire_token_for_client(self, **kwargs: Any) -> dict[str, Any]:
            auth["acquired"] += 1
            return auth.get("result", {"access_token": "controlled-token", "token_type": "Bearer", "expires_in": 3600})

    original = microsoft_graph._send_request
    monkeypatch.setattr(
        microsoft_graph, "_send_request", lambda session, method, url, **kwargs: http(url, **kwargs) if url.startswith("https://graph.microsoft.com/") else original(session, method, url, **kwargs)
    )
    monkeypatch.setattr(microsoft_graph.msal, "ConfidentialClientApplication", App)
    return auth

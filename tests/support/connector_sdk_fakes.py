"""Controlled HTTP boundaries exercising the installed Graph and Slack SDKs."""

import json
from typing import Any
from urllib.parse import unquote, urlsplit

import requests
from slack_sdk.web.slack_response import SlackResponse

SITE_URL = "https://contoso.sharepoint.com/sites/MySite"
MODIFIED = "2026-01-02T00:00:00Z"


class GraphHTTP:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.mode = "complete"
        self.body = b"controlled file bytes"
        self.content_type = "application/octet-stream"
        self.after_request: Any = None

    def __call__(self, url: str, **kwargs: Any) -> requests.Response:
        assert 0 < kwargs["timeout"][0] <= 10 and 0 < kwargs["timeout"][1] <= 60
        assert kwargs["headers"]["Authorization"] == "Bearer controlled-token"
        path = unquote(urlsplit(url).path)
        self.calls.append(url)
        data: Any = None
        status = 200
        if ":/sites/MySite" in path:
            data = {"id": "site-1"}
        elif path.endswith("/sites/site-1/drives"):
            data = {"value": [{"id": "drive-A", "name": "Documents"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/drives-page-2"}
        elif path.endswith("/drives-page-2"):
            data = {"value": [{"id": "drive-B", "name": "Archive"}]}
        elif path.endswith("/root/children"):
            if "/drive-A/" in path:
                data = {"value": [self.file("same", "hello.txt")], "@odata.nextLink": "https://graph.microsoft.com/v1.0/children-page-2"}
            else:
                data = {"value": [self.file("same", "second.txt")]}
        elif path.endswith("/children-page-2"):
            data = {"value": [{"id": "folder", "name": "sub", "folder": {}}]}
            if self.mode == "permission":
                status, data = 403, {"error": {"code": "accessDenied", "message": "controlled permission failure"}}
            elif self.mode == "malformed":
                data = {"unexpected": []}
            elif self.mode == "repeat":
                data = {"value": [], "@odata.nextLink": url}
        elif path.endswith("/items/folder/children"):
            data = {"value": [self.file("nested", "日本語.md")]}
        elif path.endswith("/content"):
            if self.mode == "download":
                status, data = 403, {"error": {"code": "accessDenied"}}
        else:
            raise AssertionError(f"Unexpected Graph request: {path}")
        response = requests.Response()
        response.status_code, response.url = status, url
        response.headers["Content-Type"] = "application/json" if data is not None else self.content_type
        response._content = json.dumps(data).encode() if data is not None else self.body
        if self.after_request is not None:
            self.after_request(path)
        return response

    @staticmethod
    def file(identifier: str, name: str) -> dict[str, Any]:
        return {"id": identifier, "name": name, "file": {"mimeType": "text/plain"}, "size": 0, "lastModifiedDateTime": MODIFIED, "webUrl": SITE_URL + "/" + name}


class SlackHTTP:
    """Runs WebClient.api_call and SlackResponse.validate; replace network only."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.mode = "complete"
        self.reply_ts = "1767312000.0"
        self.reply_text = "新回复"
        self.after_request: Any = None

    def __call__(self, client: Any, api_url: str, req_args: dict[str, Any]) -> SlackResponse:
        method = urlsplit(api_url).path.rsplit("/", 1)[-1]
        params = req_args.get("params") or req_args.get("data") or {}
        self.calls.append({"method": method, "params": params})
        cursor = params.get("cursor")
        data: dict[str, Any] = {"ok": True}
        if method == "conversations.list":
            data.update(
                channels=[{"id": "C1", "name": "general", "is_member": True, "is_private": False}] if not cursor else [{"id": "C2", "name": "private", "is_member": True, "is_private": True}],
                response_metadata={"next_cursor": "channels-2" if not cursor else ""},
            )
            if self.mode == "permission":
                data = {"ok": False, "error": "missing_scope"}
        elif method == "conversations.history":
            channel = params["channel"]
            messages = [] if channel == "C2" else [{"ts": "1767225600.0", "user": "U1", "text": "古い親", "reply_count": 1}, {"ts": "1767225601.0", "user": "U1", "text": "standalone"}]
            if cursor:
                messages = [{"ts": self.reply_ts, "thread_ts": "1767225600.0", "user": "U2", "text": "reply seen twice"}]
            data.update(messages=messages, has_more=channel == "C1" and not cursor, response_metadata={"next_cursor": "history-2" if channel == "C1" and not cursor else ""})
            if self.mode == "later-page" and cursor:
                data = {"ok": False, "error": "not_in_channel"}
            if self.mode == "repeat":
                data.update(response_metadata={"next_cursor": "same"}, messages=[])
            if self.mode == "malformed":
                data = {"ok": True, "has_more": True}
        elif method == "conversations.replies":
            data.update(
                messages=[{"ts": "1767225600.0", "thread_ts": "1767225600.0", "user": "U1", "text": "古い親"}]
                if not cursor
                else [{"ts": self.reply_ts, "thread_ts": "1767225600.0", "user": "U2", "text": self.reply_text}],
                has_more=not cursor,
                response_metadata={"next_cursor": "replies-2" if not cursor else ""},
            )
            if self.mode == "thread-error":
                data = {"ok": False, "error": "missing_scope"}
        elif method == "users.info":
            if params.get("user") == "UBAD" and self.mode.startswith("mention-"):
                data = {"ok": False, "error": "missing_scope" if self.mode == "mention-scope" else "ratelimited"}
            else:
                data.update(user={"real_name": "Alice", "profile": {"display_name": "Alice"}})
        elif method == "auth.test":
            data.update(url="https://controlled.slack.com")
        else:
            raise AssertionError(method)
        if self.mode.startswith("mention-") and method == "conversations.replies":
            for message in data.get("messages", []):
                message["text"] = "hello <@UBAD>"
        if self.after_request is not None:
            self.after_request(method)
        return SlackResponse(client=client, http_verb="GET", api_url=api_url, req_args=req_args, data=data, headers={}, status_code=200)

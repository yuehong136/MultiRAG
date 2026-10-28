"""Controlled Graph responses at the shared transport replacement boundary."""

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests

from tests.support.teams_onedrive_fakes import BASE, MODIFIED, OLD


class OutlookHTTP:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.mode = "complete"
        self.modified = MODIFIED
        self.after_request: Any = None
        self.transform: Any = None

    def message(self, identifier: str, subject: str = "Latest <subject>") -> dict[str, Any]:
        return {
            "id": identifier,
            "subject": subject,
            "body": {"contentType": "html", "content": "<p>Actual mail body &amp; bytes</p>"},
            "receivedDateTime": OLD,
            "lastModifiedDateTime": self.modified,
            "from": {"emailAddress": {"name": "<Sender>", "address": "sender@example.test"}},
            "toRecipients": [{"emailAddress": {"address": "receiver@example.test"}}],
            "ccRecipients": [],
            "hasAttachments": True,
            "conversationId": "thread",
            "webLink": "https://outlook.office.com/mail/message",
        }

    def __call__(self, url: str, **kwargs: Any) -> requests.Response:
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        assert kwargs["headers"].get("Authorization") == "Bearer controlled-token"
        self.calls.append(url)
        path = urlsplit(url).path
        page = parse_qs(urlsplit(url).query).get("page", [""])[0]
        assert "/mailFolders/" in path  # No directory/tenant enumeration.
        if path.endswith("/messages"):
            payload: Any = {"value": [{"id": "probe"}]}
        elif path.endswith("/messages/delta"):
            delta = BASE + path.removeprefix("/v1.0")
            if not page:
                payload = {"value": [self.message("same", "old state"), self.message("removed")], "@odata.nextLink": delta + "?page=2"}
            else:
                payload = {"value": [self.message("same"), {"id": "removed", "@removed": {"reason": "deleted"}}, self.message("second")], "@odata.deltaLink": delta + "?token=saved"}
        else:
            raise AssertionError(path)
        status = 200
        if self.mode in {"permission", "unauthorized", "not-found", "server-error", "throttled"} or (self.mode == "later-page" and page):
            status = {"unauthorized": 401, "not-found": 404, "server-error": 503, "throttled": 429}.get(self.mode, 403)
            payload = {"error": {"message": "controlled-secret-must-not-leak"}}
        elif self.mode == "empty":
            payload = {"value": [], "@odata.deltaLink": url.split("?")[0] + "?token=empty"}
        elif self.mode == "repeat" and page:
            payload = {"value": [], "@odata.nextLink": url}
        elif self.mode == "missing-delta" and page:
            payload = {"value": []}
        elif self.mode == "ambiguous":
            payload["@odata.deltaLink"] = url.split("?")[0] + "?token=saved"
        elif self.mode == "cross-mailbox":
            payload = {"value": [], "@odata.nextLink": BASE + "/users/unconfigured/mailFolders/inbox/messages/delta?x=1"}
        elif self.mode == "cross-folder":
            payload = {"value": [], "@odata.nextLink": path.replace("/inbox/", "/archive/")}
        elif self.mode == "malformed":
            payload = {"value": {}}
        elif self.mode == "bad-id":
            payload = {"value": [{"id": ""}]}
        if self.transform:
            payload = self.transform(payload)
        response = requests.Response()
        response.status_code, response.url = status, url
        response.headers["Content-Type"] = "application/json"
        response._content = b"not json" if self.mode == "json" else json.dumps(payload).encode()
        response._content_consumed = True
        if self.after_request:
            self.after_request(path)
        return response

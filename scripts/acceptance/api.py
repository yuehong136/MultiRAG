"""Independent HTTP readback of upload, configuration, parsing and retrieval."""

import re
import time
from typing import Any

import requests

from scripts.acceptance.evidence import AcceptanceError, require

MARKER = "MULTIRAG_ACCEPTANCE_ORCHID_73"
SOURCE = f"Product acceptance reference.\nThe orchid approval code is {MARKER}.\nThe approval period is exactly 17 days.\n".encode()


def response_data(body: Any, *, label: str, http_status: int = 200) -> Any:
    require(200 <= http_status < 300, f"{label}: HTTP {http_status}")
    require(isinstance(body, dict), f"{label}: JSON object required")
    codes = [body[key] for key in ("code", "retcode") if key in body]
    require(bool(codes) and all(type(code) is int and code == 0 for code in codes), f"{label}: business failure ({codes})")
    require("data" in body, f"{label}: missing data")
    return body["data"]


class ProductAPI:
    def __init__(self, base: str, token: str, dataset: str) -> None:
        self.base = base.rstrip("/")
        self.token = token
        require(bool(re.fullmatch(r"[a-zA-Z0-9_-]+", dataset)), "Invalid scratch dataset ID")
        self.dataset = dataset
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def request(self, method: str, suffix: str, **kwargs: Any) -> Any:
        label = f"{method} {suffix}"
        try:
            response = self.session.request(method, self.base + "/api/v1" + suffix, timeout=30, allow_redirects=False, **kwargs)
        except requests.RequestException as exc:
            raise AcceptanceError(f"{label}: transport {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise AcceptanceError(f"{label}: invalid JSON (HTTP {response.status_code})") from None
        return response_data(body, label=label, http_status=response.status_code)

    @property
    def path(self) -> str:
        return f"/datasets/{self.dataset}"

    def configuration(self) -> dict[str, Any]:
        parser = {"chunk_token_num": 128, "overlapped_percent": 0.1, "delimiter": "\n", "enable_children": True, "parent_child": {"use_parent_child": True, "children_delimiter": "\n"}, "metadata": []}
        self.request("PUT", self.path, json={"parser_config": parser})
        saved = self.request("GET", self.path)["parser_config"]
        for key, value in parser.items():
            require(saved.get(key) == value, f"Configuration readback mismatch: {key}")
        fields = [{"key": "acceptance_tag", "description": "Synthetic acceptance field", "enum": ["orchid"]}]
        metadata_path = self.path + "/metadata/config"
        self.request("PUT", metadata_path, json={"metadata": fields})
        require(self.request("GET", metadata_path)["metadata"] == fields, "Metadata save/readback mismatch")
        self.request("PUT", self.path, json={"description": "Acceptance readback"})
        require(self.request("GET", metadata_path)["metadata"] == fields, "Omitted metadata unexpectedly cleared fields")
        self.request("PUT", metadata_path, json={"metadata": []})
        require(self.request("GET", metadata_path)["metadata"] == [], "Explicit [] did not clear metadata")
        self.request("PUT", self.path, json={"parser_config": {"enable_children": False, "parent_child": {}}})
        saved = self.request("GET", self.path)["parser_config"]
        require(saved.get("enable_children") is False and saved.get("parent_child") == {}, "Explicit parent-child disable was not persisted")
        require(saved.get("chunk_token_num") == 128 and saved.get("overlapped_percent") == 0.1, "Partial configuration update lost sibling fields")
        return {"chunk_token_num": 128, "overlapped_percent": 0.1, "metadata_omission_preserves": True, "metadata_empty_clears": True, "parent_child_disabled": True}

    def document(self, identifier: str) -> dict[str, Any]:
        data = self.request("GET", self.path + "/documents", params={"id": identifier})
        require(data["total"] == 1 and len(data["docs"]) == 1, "Independent document readback must return exactly one row")
        doc = data["docs"][0]
        require(doc["id"] == identifier, "Document identity mismatch")
        return doc

    def upload(self, filename: str = "acceptance-api.txt") -> str:
        data = self.request("POST", self.path + "/documents", files={"file": (filename, SOURCE, "text/plain")})
        require(isinstance(data, list) and len(data) == 1, "Upload did not return exactly one document")
        identifier = data[0]["id"]
        self.verify_upload(identifier, filename)
        return identifier

    def verify_upload(self, identifier: str, filename: str) -> dict[str, Any]:
        doc = self.document(identifier)
        require(doc["name"] == filename and doc["size"] == len(SOURCE), "Uploaded document name/size readback mismatch")
        return {"document_id": identifier, "filename": filename, "bytes": len(SOURCE)}

    def wait_parsed(self, identifier: str, *, timeout: float = 120) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            doc = self.document(identifier)
            status = str(doc.get("run", ""))
            progress = float(doc.get("progress", 0))
            if status in {"4", "FAIL", "FAILED", "2", "CANCEL"} or progress < 0:
                raise AcceptanceError(f"Parsing failed/cancelled: run={status}, progress={progress}")
            if status in {"3", "DONE"} and progress >= 1:
                require(int(doc.get("chunk_count", doc.get("chunk_num", 0))) > 0, "Parse completed with zero chunks")
                return {"document_id": identifier, "run": status, "progress": progress, "chunk_count": doc.get("chunk_count", doc.get("chunk_num"))}
            if time.monotonic() >= deadline:
                raise AcceptanceError(f"Parsing timed out: run={status}, progress={progress}")
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))

    def chunks(self, identifier: str) -> dict[str, Any]:
        data = self.request("GET", self.path + f"/documents/{identifier}/chunks", params={"page_size": 100})
        require(data["total"] > 0 and bool(data["chunks"]), "Chunk readback is empty")
        require(any(MARKER in chunk.get("content", chunk.get("content_with_weight", "")) for chunk in data["chunks"]), "Chunk content lost the source marker")
        return {"document_id": identifier, "total": data["total"]}

    def retrieval(self, identifier: str) -> dict[str, Any]:
        data = self.request("POST", self.path + "/search", json={"question": "orchid approval code", "doc_ids": [identifier], "similarity_threshold": 0, "highlight": False})
        self.verify_retrieval(data, identifier)
        return {"document_id": identifier, "total": data["total"], "matched_marker": MARKER}

    @staticmethod
    def verify_retrieval(data: Any, identifier: str) -> None:
        require(isinstance(data, dict) and data.get("total", 0) > 0 and bool(data.get("chunks")), "Retrieval returned no chunks")
        matches = [chunk for chunk in data["chunks"] if chunk.get("doc_id", chunk.get("document_id")) == identifier]
        require(bool(matches), "Retrieval did not return the uploaded document")
        require(any(MARKER in chunk.get("content_with_weight", chunk.get("content", "")) for chunk in matches), "Retrieval result lost the source marker")

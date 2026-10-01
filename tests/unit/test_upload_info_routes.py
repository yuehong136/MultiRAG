"""FileService.upload_info、SDK/Agent 上传契约与旧文档别名退役。

服务层：健康检查走 run_sync，PDF 修复 + 存储写入在工作线程执行；
路由层：file_api 的 code/data 与 canvas 的 retcode 两套形状钉板。
"""

import threading
import types
from contextlib import nullcontext

import crawl4ai
import pytest
from fastapi.testclient import TestClient

import api.db.services.file_service as file_service_module
from api.db.services.canvas_service import UserCanvasService
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService

# ---------------------------------------------------------------------------
# 服务层（文件分支：run_sync 健康检查 + 工作线程 structured）
# ---------------------------------------------------------------------------


async def test_upload_info_file_branch_bridges_db_and_thread(async_db, monkeypatch):
    seen: dict[str, object] = {}

    def _check_doc_health(s, user_id, filename):
        seen["health"] = (user_id, filename)
        return True

    def _put_blob(user_id, location, blob):
        seen["off_loop"] = threading.current_thread() is not threading.main_thread()
        if not location.endswith(".upload.json"):
            seen["blob"] = blob
        return True

    monkeypatch.setattr(DocumentService, "check_doc_health", classmethod(lambda cls, s, user_id, filename: _check_doc_health(s, user_id, filename)))
    monkeypatch.setattr(FileService, "put_blob", staticmethod(_put_blob))

    fake_file = types.SimpleNamespace(read=lambda: b"hello", filename="a.txt", content_type="text/plain")
    result = await FileService.upload_info(async_db, "user-unit", fake_file, None)

    assert seen["health"] == ("user-unit", "a.txt")
    assert seen["off_loop"] is True  # 存储写入必须在工作线程执行
    assert seen["blob"] == b"hello"
    assert result["name"] == "a.txt"
    assert result["created_by"] == "user-unit"
    assert result["mime_type"] == "text/plain"
    assert result["size"] == len(b"hello")


async def test_upload_info_rejects_invalid_file_object(async_db):
    with pytest.raises(ValueError, match="Invalid file object"):
        await FileService.upload_info(async_db, "user-unit", types.SimpleNamespace(filename="x"), None)


class _RedirectResponse:
    def __init__(self, status_code: int, location: str | None = None) -> None:
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}

    def close(self) -> None:
        return None


def test_resolve_safe_crawl_url_blocks_private_redirect_before_request(monkeypatch):
    calls: list[tuple[str, bool]] = []

    def validate(url: str) -> tuple[str, str]:
        if "metadata.internal" in url:
            raise ValueError("URL resolves to a non-public address")
        return "example.com", "93.184.216.34"

    def get(url: str, *, timeout: int, allow_redirects: bool):
        del timeout
        calls.append((url, allow_redirects))
        return _RedirectResponse(302, "http://metadata.internal/latest/meta-data")

    monkeypatch.setattr(FileService, "_validate_url_for_crawl", staticmethod(validate))
    monkeypatch.setattr(file_service_module, "pin_dns", lambda *_args: nullcontext())
    monkeypatch.setattr(file_service_module.requests, "get", get)

    with pytest.raises(ValueError, match="non-public"):
        FileService._resolve_safe_crawl_url("https://example.com/start")

    assert calls == [("https://example.com/start", False)]


def test_resolve_safe_crawl_url_validates_and_pins_each_redirect(monkeypatch):
    validations: list[str] = []
    pins: list[tuple[str, str]] = []
    responses = iter(
        [
            _RedirectResponse(302, "/next"),
            _RedirectResponse(301, "https://cdn.example.net/final"),
            _RedirectResponse(200),
        ]
    )

    def validate(url: str) -> tuple[str, str]:
        validations.append(url)
        if "cdn.example.net" in url:
            return "cdn.example.net", "203.0.113.20"
        return "example.com", "93.184.216.34"

    def pinned(hostname: str, ip: str):
        pins.append((hostname, ip))
        return nullcontext()

    monkeypatch.setattr(FileService, "_validate_url_for_crawl", staticmethod(validate))
    monkeypatch.setattr(file_service_module, "pin_dns", pinned)
    monkeypatch.setattr(file_service_module.requests, "get", lambda *_args, **_kwargs: next(responses))

    final_url, host_pins = FileService._resolve_safe_crawl_url("https://example.com/start")

    assert final_url == "https://cdn.example.net/final"
    assert validations == [
        "https://example.com/start",
        "https://example.com/next",
        "https://cdn.example.net/final",
    ]
    assert pins == [
        ("example.com", "93.184.216.34"),
        ("example.com", "93.184.216.34"),
        ("cdn.example.net", "203.0.113.20"),
    ]
    assert host_pins == {"example.com": "93.184.216.34", "cdn.example.net": "203.0.113.20"}


async def test_upload_info_url_uses_validated_final_url_and_browser_dns_pins(async_db, monkeypatch):
    seen: dict[str, object] = {}

    class FakeBrowserConfig:
        def __init__(self, **kwargs) -> None:
            seen["browser_config"] = kwargs

    class FakeCrawlerRunConfig:
        def __init__(self, **kwargs) -> None:
            seen["crawler_config"] = kwargs

    class FakeCrawler:
        def __init__(self, *, config) -> None:
            del config
            self.crawler_strategy = types.SimpleNamespace(set_hook=lambda *_args: None)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def arun(self, *, url: str, config):
            seen["crawl_url"] = url
            seen["run_config"] = config
            return types.SimpleNamespace(
                success=True,
                status_code=200,
                pdf=None,
                markdown="safe content",
                response_headers={"content-type": "text/html"},
            )

    monkeypatch.setattr(
        FileService,
        "_resolve_safe_crawl_url",
        staticmethod(lambda _url: ("https://cdn.example.net/final", {"example.com": "93.184.216.34", "cdn.example.net": "1.1.1.1"})),
    )
    monkeypatch.setattr(FileService, "put_blob", staticmethod(lambda _user_id, _location, blob: seen.setdefault("blob", blob)))
    monkeypatch.setattr(crawl4ai, "AsyncWebCrawler", FakeCrawler)
    monkeypatch.setattr(crawl4ai, "BrowserConfig", FakeBrowserConfig)
    monkeypatch.setattr(crawl4ai, "CrawlerRunConfig", FakeCrawlerRunConfig)
    monkeypatch.setattr(crawl4ai, "DefaultMarkdownGenerator", lambda **_kwargs: object())
    monkeypatch.setattr(crawl4ai, "PruningContentFilter", lambda **_kwargs: object())

    result = await FileService.upload_info(async_db, "user-unit", None, "https://example.com/start")

    assert seen["crawl_url"] == "https://cdn.example.net/final"
    config = seen["browser_config"]
    assert config["headless"] is True and config["ignore_https_errors"] is False
    assert config["proxy_config"]["server"].startswith("http://127.0.0.1:")
    assert "--proxy-bypass-list=<-loopback>" in config["extra_args"]
    assert seen["blob"] == b"safe content"
    assert result["mime_type"] == "text/html"


@pytest.mark.parametrize("status", [404, 500])
def test_resolve_safe_crawl_url_rejects_http_failure(monkeypatch, status: int) -> None:
    monkeypatch.setattr(FileService, "_validate_url_for_crawl", staticmethod(lambda _url: ("example.com", "93.184.216.34")))
    monkeypatch.setattr(file_service_module, "pin_dns", lambda *_args: nullcontext())
    monkeypatch.setattr(file_service_module.requests, "get", lambda *_args, **_kwargs: _RedirectResponse(status))
    with pytest.raises(ValueError, match=f"HTTP {status}"):
        FileService._resolve_safe_crawl_url("https://example.com/missing")


@pytest.mark.parametrize(
    "result",
    [
        types.SimpleNamespace(success=False, status_code=200, pdf=None, markdown="error page"),
        types.SimpleNamespace(success=True, status_code=404, pdf=None, markdown="not found"),
        types.SimpleNamespace(success=True, status_code=200, pdf=None, markdown=None, response_headers=None),
    ],
)
async def test_upload_info_crawl_failure_does_not_store(async_db, monkeypatch, result: object) -> None:
    class FailedCrawler:
        def __init__(self, **_kwargs: object) -> None:
            self.crawler_strategy = types.SimpleNamespace(set_hook=lambda *_args: None)

        async def __aenter__(self) -> "FailedCrawler":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def arun(self, **_kwargs: object) -> object:
            return result

    monkeypatch.setattr(FileService, "_resolve_safe_crawl_url", staticmethod(lambda _url: ("https://example.com", {"example.com": "93.184.216.34"})))
    monkeypatch.setattr(crawl4ai, "AsyncWebCrawler", FailedCrawler)

    def forbidden_store(*_args: object) -> None:
        pytest.fail("failed crawl must not write storage")

    monkeypatch.setattr(FileService, "put_blob", staticmethod(forbidden_store))
    with pytest.raises(ValueError, match=r"crawl|content"):
        await FileService.upload_info(async_db, "user-unit", None, "https://example.com")


# ---------------------------------------------------------------------------
# 路由层（FileService.upload_info 打桩，锁 SDK/Agent 响应及旧别名退役）
# ---------------------------------------------------------------------------


@pytest.fixture
def upload_info_stub(monkeypatch):
    calls: list[tuple] = []

    async def _fake(db, user_id, file, url=None):
        calls.append((user_id, getattr(file, "filename", None), url))
        return {"id": "loc1", "name": getattr(file, "filename", None) or "from-url"}

    monkeypatch.setattr(FileService, "upload_info", staticmethod(_fake))
    return calls


def test_file_api_upload_info_shape(client, upload_info_stub):
    resp = client.post("/api/v1/files/upload_info", files={"files": ("a.txt", b"data", "text/plain")})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"id": "loc1", "name": "a.txt"}
    assert upload_info_stub == [("tenant-unit", "a.txt", None)]


def test_file_api_upload_info_rejects_file_plus_url(client, upload_info_stub):
    resp = client.post("/api/v1/files/upload_info?url=http://x", files={"files": ("a.txt", b"data", "text/plain")})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] != 0
    assert "not both" in body["message"]
    assert upload_info_stub == []


def test_canvas_upload_shape(client, upload_info_stub, monkeypatch):
    monkeypatch.setattr(UserCanvasService, "get_by_canvas_id", classmethod(lambda cls, s, cid: (True, {"user_id": "owner-1"})))

    resp = client.post("/api/v1/agents/c1/upload", files={"file": ("b.txt", b"data", "text/plain")})

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"] == {"id": "loc1", "name": "b.txt"}
    assert upload_info_stub == [("owner-1", "b.txt", None)]


def test_canvas_upload_missing_canvas_shape(client, upload_info_stub, monkeypatch):
    monkeypatch.setattr(UserCanvasService, "get_by_canvas_id", classmethod(lambda cls, s, cid: (False, None)))

    resp = client.post("/api/v1/agents/missing/upload", files={"file": ("b.txt", b"data", "text/plain")})

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] != 0
    assert body["retmsg"] == "canvas not found."
    assert upload_info_stub == []


def test_document_upload_info_is_retired(client: TestClient, upload_info_stub: list[tuple]) -> None:
    resp = client.post("/v1/document/upload_info", files={"file": ("c.txt", b"data", "text/plain")})

    assert resp.status_code == 404
    assert resp.json() == {"code": 404, "message": "Not Found: /v1/document/upload_info", "data": None, "error": "Not Found"}
    assert upload_info_stub == []

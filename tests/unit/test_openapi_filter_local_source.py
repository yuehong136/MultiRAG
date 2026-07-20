"""OpenAPI 过滤服务本地文档源钉板。

回归背景：_fetch_local_document 曾手工调 get_openapi(title/version/description/
routes/tags)——只传五个参数，app 上配置的 servers/webhooks/
separate_input_output_schemas 等一概丢失，过滤源文档与 /openapi.json 实际输出
漂移。修复后统一走 FastAPI 公开入口 app.openapi()（自带缓存与全参数）。
"""

from fastapi import FastAPI

from api.db.services.openapi_filter_service import OpenApiFilterService


def _make_app() -> FastAPI:
    app = FastAPI(title="probe", version="9.9.9", servers=[{"url": "https://probe.example.com"}])

    @app.get("/ping")
    def ping():
        return {"ok": True}

    return app


async def test_local_document_is_exactly_app_openapi_output():
    app = _make_app()
    doc, etag = await OpenApiFilterService()._fetch_local_document(app)

    assert etag is None
    assert doc == app.openapi()  # 回归点：手工 get_openapi 会丢 servers 等 app 级参数
    assert doc["servers"] == [{"url": "https://probe.example.com"}]


async def test_local_document_rejects_object_without_openapi_method():
    class NotAnApp:
        pass

    try:
        await OpenApiFilterService()._fetch_local_document(NotAnApp())
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for non-FastAPI object")

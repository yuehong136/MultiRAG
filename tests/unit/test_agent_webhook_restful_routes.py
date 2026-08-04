def test_agent_webhook_routes_are_restful_only(client) -> None:
    registered = {(method.upper(), path) for path, operations in client.app.openapi()["paths"].items() for method in operations}

    for method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
        assert (method, "/api/v1/agents/{agent_id}/webhook") in registered
        assert (method, "/api/v1/agents/{agent_id}/webhook/test") in registered

    assert ("GET", "/api/v1/agents/{agent_id}/webhook/logs") in registered
    assert not any(path.startswith("/api/v1/webhook") for _, path in registered)

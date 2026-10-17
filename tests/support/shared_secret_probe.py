"""Cold API import probe; deterministic non-production key and no live services."""

import json
import os
import socket
import sys


def main() -> None:
    import pytest_socket

    serving = len(sys.argv) > 1 and sys.argv[1] == "serve"
    if serving:
        pytest_socket.enable_socket()
        pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    else:
        pytest_socket.disable_socket(allow_unix_socket=True)
    from common import resources, settings

    key = "test-only-cold-start-shared-auth-key-" + "x" * 32
    resources._state.update(secret_key=None, doc_store=object(), msg_store=object(), storage=object(), retriever=object(), kg_retriever=object())
    calls: list[str] = []

    def initialize() -> None:
        calls.append("initialized")
        if serving:
            from common.app_config import get_app_config
            from core.utils.redis_conn import REDIS_CONN

            configured = get_app_config().multirag.secret_key
            assert configured is not None and configured.get_secret_value() == key

            def forbidden_lookup(*args: object, **kwargs: object) -> str:
                raise AssertionError("fixed-key startup must not access shared Redis")

            REDIS_CONN.get_or_create_secret_key = forbidden_lookup
            resources._state["secret_key"] = resources._create_secret_key()
        else:
            resources._state["secret_key"] = key

    settings.init_settings = initialize
    from starlette.middleware.sessions import SessionMiddleware

    import api.apps as apps

    assert calls == ["initialized"]
    assert apps.manager.secret.secret_for_encode == key.encode()
    session = next(m for m in apps.app.user_middleware if m.cls is SessionMiddleware)
    assert session.kwargs["secret_key"] == key
    assert apps.admin.authentication_backend.middlewares[0].kwargs["secret_key"] == key
    jwt = apps.manager.create_access_token(data={"sub": "synthetic@example.invalid"})
    assert apps.manager._get_payload(jwt)["sub"] == "synthetic@example.invalid"
    evidence = {"startup_before_all_signers": True, "jwt_roundtrip": True, "api_root": os.path.dirname(apps.__file__)}
    if not serving:
        print(json.dumps(evidence))
        return

    import uvicorn
    from fastapi import Request

    @apps.app.post("/__secret_probe/session")
    def create_session(request: Request) -> dict[str, str]:
        request.session["probe"] = "shared"
        return {"token": apps.manager.create_access_token(data={"sub": "synthetic@example.invalid"})}

    @apps.app.get("/__secret_probe/check")
    def read_session(request: Request) -> dict[str, bool]:
        raw = request.headers.get("Authorization", "").removeprefix("Bearer ")
        claims = apps.manager._get_payload(raw)
        return {"jwt": claims.get("sub") == "synthetic@example.invalid", "cookie": request.session.get("probe") == "shared"}

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    evidence["base"] = f"http://127.0.0.1:{listener.getsockname()[1]}"
    print(json.dumps(evidence), flush=True)
    server = uvicorn.Server(uvicorn.Config(apps.app, lifespan="off", log_level="error"))
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()


if __name__ == "__main__":
    main()

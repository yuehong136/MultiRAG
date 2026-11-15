"""Process entry of the model gateway data plane.

Run with ``uv run python -m api.model_gateway.server``. The process refuses
to start unless ``model_gateway.enabled`` is true.
"""

import logging
import sys

import uvicorn

from common.log_utils import init_root_logger


def main() -> None:
    init_root_logger("model_gateway")
    from common.app_config import get_app_config
    from common.bootstrap import ensure_initialized

    # Configuration and tracing only: the data plane needs neither document
    # stores nor object storage. Tracing must be set up before the database
    # models are imported, which is why the application import comes later.
    ensure_initialized(initialize_resources=False)
    settings = get_app_config().model_gateway
    if not settings.enabled:
        raise SystemExit("model_gateway.enabled is false; set MULTIRAG_MODEL_GATEWAY__ENABLED=true to start the model gateway")

    from api.model_gateway.app import create_app

    # psycopg's async driver needs add_reader/add_writer, which the Windows
    # proactor loop lacks; see the same choice in api/multirag_server.py.
    loop = "asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto"
    logging.info("Model gateway listening on %s:%s", settings.host, settings.http_port)
    uvicorn.run(create_app(config=settings), host=settings.host, port=settings.http_port, log_level="info", loop=loop)


if __name__ == "__main__":
    main()

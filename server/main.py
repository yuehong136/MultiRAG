from fastapi import FastAPI
from configs import VERSION
from configs import OPEN_CROSS_DOMAIN
from server.utils import make_fastapi_offline, add_cors_middleware
from server.api import chat
import argparse
import uvicorn

def create_app():
    app = FastAPI(title="LLM API Server", version=VERSION)
    make_fastapi_offline(app)
    if OPEN_CROSS_DOMAIN:
        add_cors_middleware(app)
    app.include_router(chat.router, prefix="/api")
    return app

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='LLM API Server')
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--ssl_keyfile", type=str)
    parser.add_argument("--ssl_certfile", type=str)
    args = parser.parse_args()

    app = create_app()

    if args.ssl_keyfile and args.ssl_certfile:
        uvicorn.run(app, host=args.host, port=args.port, ssl_keyfile=args.ssl_keyfile, ssl_certfile=args.ssl_certfile)
    else:
        uvicorn.run(app, host=args.host, port=args.port)

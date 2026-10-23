"""HTTP fixture startup survives both application import orders."""

import json
import subprocess
import sys

import pytest


@pytest.mark.parametrize("order", ["nest-first", "uvicorn-first"])
def test_server_loop_survives_nested_asyncio_import_order(order: str) -> None:
    source = """
import http.client
import json
import socket
import sys
import threading
import time

if sys.argv[1] == "uvicorn-first":
    import uvicorn
import nest_asyncio
nest_asyncio.apply()
import uvicorn
from fastapi import FastAPI
from tests.support.uvicorn_runtime import run_server

app = FastAPI()
@app.get("/ping")
async def ping():
    return {"ready": True}

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
thread = threading.Thread(target=run_server, args=(server,), kwargs={"sockets": [listener]}, daemon=True)
thread.start()
try:
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/ping")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"ready": True}
    finally:
        connection.close()
finally:
    server.should_exit = True
    thread.join(timeout=10)
    listener.close()
    assert not thread.is_alive()
    with socket.socket() as reader:
        assert reader.connect_ex(("127.0.0.1", port)) != 0
print(json.dumps({"ready": True, "closed": True}))
"""
    result = subprocess.run([sys.executable, "-c", source, order], capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ready": True, "closed": True}

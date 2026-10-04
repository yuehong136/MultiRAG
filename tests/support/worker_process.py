"""Finite production worker harness: real queue, parser and stores in a child.

Only the embedding provider and the requested crash boundary are controlled.
Private configuration is installed by the parent before this module imports the app.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np


def main() -> None:
    from common.config_utils import CONFIGS

    assert str(CONFIGS["postgresql"]["dbname"]).startswith("multirag_test_")
    assert str(CONFIGS["minio"]["bucket"]).startswith("upload-test-")
    context = json.loads(Path(os.environ["MULTIRAG_WORKER_TEST_CONTEXT"]).read_text())
    assert context["queue"].startswith("parse-retirement:")
    from pytest_socket import socket_allow_hosts

    from common.bootstrap import ensure_initialized
    from scripts.run_integration import service_hosts

    socket_allow_hosts(service_hosts(CONFIGS), allow_unix_socket=True)
    ensure_initialized()
    from common import settings
    from core.svr import task_executor as worker
    from core.utils.redis_conn import RedisMsg

    worker.CONSUMER_NAME = context["consumer"]
    worker.SVR_CONSUMER_GROUP_NAME = context["group"]
    settings.get_svr_queue_names = lambda: [context["queue"]]
    settings.get_svr_queue_name = lambda priority: context["queue"]

    class Embedding:
        max_length = 8192
        llm_name = "system-test-embedding"

        def encode(self, texts: list[str]) -> tuple[np.ndarray, int]:
            return np.asarray([[0.1] * 768 for _ in texts]), sum(len(text) for text in texts)

    worker.get_model_config_by_type_and_name = lambda *args, **kwargs: {}
    worker.LLMBundle = lambda *args, **kwargs: Embedding()

    def boundary(name: str) -> None:
        if context["boundary"] == name:
            Path(context["ready"]).write_text(json.dumps({"pid": os.getpid(), "boundary": name}))
            sys.stdin.readline()

    original_ack = RedisMsg.ack

    def ack(self: RedisMsg) -> bool:
        boundary("before_ack")
        return original_ack(self)

    original_build = worker.build_chunks

    async def build(*args: Any, **kwargs: Any) -> Any:
        chunks = await original_build(*args, **kwargs)
        boundary("after_parse")
        return chunks

    RedisMsg.ack = ack
    worker.build_chunks = build
    asyncio.run(worker.handle_task())
    assert worker.FAILED_TASKS == 0 or context["boundary"] == "resume_cancelled"


if __name__ == "__main__":
    main()

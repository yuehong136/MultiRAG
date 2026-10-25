"""Shared worker limits without importing the task executor or its parsers."""

import os

from common.asyncio_utils import LoopLocalSemaphore

MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "5"))
MAX_CONCURRENT_CHUNK_BUILDERS = int(os.environ.get("MAX_CONCURRENT_CHUNK_BUILDERS", "3"))
MAX_CONCURRENT_MINIO = int(os.environ.get("MAX_CONCURRENT_MINIO", "10"))

task_limiter = LoopLocalSemaphore(MAX_CONCURRENT_TASKS)
chunk_limiter = LoopLocalSemaphore(MAX_CONCURRENT_CHUNK_BUILDERS)
embed_limiter = LoopLocalSemaphore(MAX_CONCURRENT_CHUNK_BUILDERS)
minio_limiter = LoopLocalSemaphore(MAX_CONCURRENT_MINIO)
kg_limiter = LoopLocalSemaphore(2)

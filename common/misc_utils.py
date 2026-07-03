import asyncio
import base64
import functools
import hashlib
import logging
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor


def get_uuid():
    return uuid.uuid1().hex


async def download_img(url):
    if not url:
        return ""
    from common.http_client import async_request
    response = await async_request("GET", url)
    return "data:" + \
        response.headers.get('Content-Type', 'image/jpg') + ";" + \
        "base64," + base64.b64encode(response.content).decode("utf-8")


def hash_str2int(line: str, mod: int = 10 ** 8) -> int:
    return int(hashlib.sha1(line.encode("utf-8")).hexdigest(), 16) % mod


def convert_bytes(size_in_bytes: int) -> str:
    """
    Format size in bytes.
    """
    if size_in_bytes == 0:
        return "0 B"

    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    i = 0
    size = float(size_in_bytes)

    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1

    if i == 0 or size >= 100:
        return f"{size:.0f} {units[i]}"
    elif size >= 10:
        return f"{size:.1f} {units[i]}"
    else:
        return f"{size:.2f} {units[i]}"


def once(func):
    """
    A thread-safe decorator that ensures the decorated function runs exactly once,
    caching and returning its result for all subsequent calls. This prevents
    race conditions in multi-thread environments by using a lock to protect
    the execution state.

    Args:
        func (callable): The function to be executed only once.

    Returns:
        callable: A wrapper function that executes `func` on the first call
                  and returns the cached result thereafter.

    Example:
        @once
        def compute_expensive_value():
            print("Computing...")
            return 42

        # First call: executes and prints
        # Subsequent calls: return 42 without executing
    """
    executed = False
    result = None
    lock = threading.Lock()
    def wrapper(*args, **kwargs):
        nonlocal executed, result
        with lock:
            if not executed:
                executed = True
                result = func(*args, **kwargs)
        return result
    return wrapper


@once
def pip_install_torch():
    device = os.getenv("DEVICE", "cpu")
    if device=="cpu":
        return
    logging.info("Installing pytorch")
    pkg_names = ["torch>=2.5.0,<3.0.0"]
    subprocess.check_call([sys.executable, "-m", "pip", "install", *pkg_names])


@once
def _thread_pool_executor():
    max_workers_env = os.getenv("THREAD_POOL_MAX_WORKERS", "128")
    try:
        max_workers = int(max_workers_env)
    except ValueError:
        max_workers = 128
    if max_workers < 1:
        max_workers = 1
    return ThreadPoolExecutor(max_workers=max_workers)


async def thread_pool_exec(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    if kwargs:
        func = functools.partial(func, *args, **kwargs)
        return await loop.run_in_executor(_thread_pool_executor(), func)
    return await loop.run_in_executor(_thread_pool_executor(), func, *args)

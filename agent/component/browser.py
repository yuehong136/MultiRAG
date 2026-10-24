# Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
# Licensed under the Apache License, Version 2.0.
"""AI browser actions with owner-scoped files and an asynchronous lifetime.

URL upload protection applies to the file fetcher only. Browser navigation needs
network egress policy at the deployment boundary. No user browser is attached.
"""

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx
import portalocker
import requests
from sqlalchemy import delete, select

from agent.component.base import ComponentBase
from agent.component.llm import LLMParam
from api.db import FileType
from api.db.db_models import File, async_db_connection
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name, resolve_model_type
from api.db.services.file_service import FileService
from api.db.services.tenant_llm_service import TenantLLMService
from api.utils.file_utils import filename_type
from common import settings
from common.misc_utils import get_uuid
from common.ssrf_guard import assert_url_is_safe, pin_dns
from core.llm import FACTORY_DEFAULT_BASE_URL

MAX_UPLOAD_URL_REDIRECTS = 5


async def _blocking(fn: Callable[..., Any], *args: Any) -> Any:
    """Join a blocking operation before cancellation can remove its resources."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class BrowserParam(LLMParam):
    def __init__(self) -> None:
        super().__init__()
        self.prompts = "{sys.query}"
        self.max_steps = 30
        self.headless = True
        self.enable_default_extensions = False
        self.chromium_sandbox = False
        self.persist_session = True
        self.upload_sources: list[Any] = []
        self.outputs = {"content": {"type": "string", "value": ""}, "downloaded_files": {"type": "Array<Object>", "value": []}}

    def check(self) -> bool:
        if not isinstance(self.llm_id, str) or not self.llm_id.strip():
            raise ValueError("[Browser] LLM is required")
        if not isinstance(self.prompts, str) or not self.prompts.strip():
            raise ValueError("[Browser] Prompts must be nonempty text")
        if type(self.max_steps) is not int or self.max_steps <= 0:
            raise ValueError("[Browser] Max steps must be a positive integer")
        for name in ("headless", "enable_default_extensions", "chromium_sandbox", "persist_session"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"[Browser] {name} must be boolean")
        if not isinstance(self.upload_sources, list):
            raise ValueError("[Browser] Upload sources must be an array")
        if isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float)) or not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2:
            raise ValueError("[Browser] Temperature must be finite and between 0 and 2")
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("[Browser] Max retries must be a nonnegative integer")
        return True


class Browser(ComponentBase):
    component_name = "Browser"

    def get_input_elements(self) -> dict[str, dict[str, Any]]:
        return self.get_input_elements_from_text(self._param.prompts + "\n" + json.dumps(self._param.upload_sources, ensure_ascii=False))

    def get_input_form(self) -> dict[str, dict[str, str]]:
        return {key: {"type": "line", "name": value.get("name", key)} for key, value in self.get_input_elements().items()}

    def _resolve_text(self, value: str, inputs: dict[str, Any] | None = None) -> str:
        supplied = inputs or {}
        return self.string_format(value, {key: supplied.get(key, meta.get("value")) for key, meta in self.get_input_elements_from_text(value).items()})

    def _upload_values(self, value: Any, depth: int = 0, inputs: dict[str, Any] | None = None) -> list[Any]:
        if depth > 20:
            raise ValueError("Browser upload references are recursive")
        if isinstance(value, str) and re.fullmatch(self.variable_ref_patt, value.strip()):
            key = value.strip().strip("{}").strip()
            resolved = inputs[key] if inputs is not None and key in inputs else self._canvas.get_variable_value(value.strip())
            return self._upload_values(resolved, depth + 1, inputs)
        if isinstance(value, list):
            return [item for child in value for item in self._upload_values(child, depth + 1, inputs)]
        if value is None or value == "":
            return []
        if not isinstance(value, (str, dict)):
            raise ValueError("Invalid Browser upload reference")
        return [value]

    @staticmethod
    def _safe_url_filename(candidate: Any) -> str:
        name = unquote(str(candidate or "")).replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or name in {".", ".."} or any(ord(char) < 32 for char in name) or len(name.encode()) > 240:
            return ""
        return name

    @staticmethod
    def _extract_url_filename(url: str, headers: Any) -> str:
        disposition = headers.get("Content-Disposition", "")
        for pattern in (r"filename\*\s*=\s*(?:UTF-8'')?([^;]+)", r'filename\s*=\s*"([^"]+)"', r"filename\s*=\s*([^;]+)"):
            match = re.search(pattern, disposition, re.IGNORECASE)
            if match and (name := Browser._safe_url_filename(match[1].strip().strip('"'))):
                return name
        return Browser._safe_url_filename(urlparse(url).path) or f"url_file_{get_uuid()}.bin"

    @staticmethod
    def _write_upload(upload_dir: str, name: str, blob: bytes) -> str:
        safe_name = Browser._safe_url_filename(name)
        if not safe_name:
            raise ValueError("Invalid upload filename")
        path = Path(upload_dir) / f"{get_uuid()}_{safe_name}"
        with path.open("xb") as stream:
            stream.write(blob)
        return str(path)

    def _prepare_upload_url_file(self, url: str, upload_dir: str) -> dict[str, Any]:
        max_bytes = int(os.getenv("MULTIRAG_BROWSER_UPLOAD_URL_MAX_BYTES", str(100 * 1024 * 1024)))
        if max_bytes <= 0:
            raise ValueError("Browser upload limit must be positive")
        deadline = time.monotonic() + 120
        response = None
        path: Path | None = None
        with requests.Session() as session:
            session.trust_env = False
            try:
                current_url = url
                for hop in range(MAX_UPLOAD_URL_REDIRECTS + 1):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Browser URL upload timed out")
                    host, ip = assert_url_is_safe(current_url)
                    with pin_dns(host, ip):
                        response = session.get(current_url, stream=True, timeout=(10, 30), allow_redirects=False, headers={"User-Agent": "MultiRAG-Browser/1.0"})
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        break
                    location = response.headers.get("Location")
                    response.close()
                    response = None
                    if not location or hop == MAX_UPLOAD_URL_REDIRECTS:
                        raise ValueError("Invalid or excessive Browser upload redirects")
                    current_url = urljoin(current_url, location)
                assert response is not None
                response.raise_for_status()
                name = self._extract_url_filename(current_url, response.headers)
                path = Path(upload_dir) / f"{get_uuid()}_{name}"
                size = 0
                with path.open("xb") as stream:
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Browser URL upload timed out")
                        # read1 returns available bytes after one socket read,
                        # so a peer cannot evade the deadline by slowly filling
                        # a large iter_content chunk indefinitely.
                        chunk = response.raw.read1(64 * 1024, decode_content=True)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError("Browser URL upload exceeds size limit")
                        stream.write(chunk)
                if not size:
                    raise ValueError("Browser URL upload is empty")
                return {"name": name, "size": size, "local_path": str(path)}
            except BaseException:
                if path is not None:
                    path.unlink(missing_ok=True)
                raise
            finally:
                if response is not None:
                    response.close()

    def _scope(self) -> tuple[str, str]:
        tenant, owner = self._canvas.get_tenant_id(), self._canvas.artifact_owner_id
        if not tenant or not owner:
            raise PermissionError("Browser requires an authenticated artifact owner")
        return tenant, owner

    async def _prepare_upload_files(self, upload_dir: str, inputs: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        tenant, owner = self._scope()
        prepared = []
        # Resolve all authorities before reading any payload or fetching any URL.
        sources = self._upload_values((inputs or {}).get("upload_sources", self._param.upload_sources), inputs=inputs)
        authorized: list[tuple[str, Any]] = []
        async with async_db_connection() as db:
            for value in sources:
                if isinstance(value, str) and urlparse(value).scheme in {"http", "https"}:
                    await _blocking(assert_url_is_safe, value)
                    authorized.append(("url", value))
                else:
                    identifier = value.get("id") or value.get("file_id") if isinstance(value, dict) else value
                    if not isinstance(identifier, str) or not identifier:
                        raise PermissionError("Invalid Browser file reference")
                    file = await db.scalar(select(File).where(File.id == identifier, File.tenant_id == tenant, File.created_by == owner, File.type != FileType.FOLDER.value))
                    if file is not None:
                        authorized.append(("file", file.to_dict()))
                    elif isinstance(value, dict):
                        descriptor = await _blocking(FileService.resolve_runtime_file_reference, owner, value)
                        if descriptor is None:
                            raise PermissionError("Invalid runtime upload descriptor")
                        authorized.append(("runtime", descriptor))
                    else:
                        raise PermissionError("Browser file is missing or unavailable to this actor")
        for kind, source in authorized:
            if kind == "url":
                prepared.append(await _blocking(self._prepare_upload_url_file, source, upload_dir))
                continue
            if kind == "runtime":
                blob = await _blocking(FileService.get_blob, owner, source["id"])
            else:
                blob = await _blocking(settings.STORAGE_IMPL.get, source["parent_id"], source["location"])
            if not isinstance(blob, bytes):
                raise FileNotFoundError("Browser upload payload is unavailable")
            path = await _blocking(self._write_upload, upload_dir, source["name"], blob)
            prepared.append({"file_id": source["id"], "name": source["name"], "size": len(blob), "local_path": path})
        return prepared

    async def _build_browser_llm(self) -> Any:
        from browser_use.llm import ChatOpenAI

        tenant, _owner = self._scope()
        async with async_db_connection() as db:
            # TODO(async-phase4): tenant model resolver is a legacy sync service.
            def resolve(session: Any) -> dict[str, Any]:
                types = resolve_model_type(session, tenant, self._param.llm_id)
                if types[0] not in {"chat", "image2text"}:
                    raise ValueError("Browser requires a chat or vision model")
                return get_model_config_by_type_and_name(session, tenant, types[0], self._param.llm_id)

            cfg = await db.run_sync(resolve)
        model = cfg.get("llm_name") or cfg.get("model_name") or cfg.get("model")
        base_url = cfg.get("api_base") or cfg.get("base_url") or FACTORY_DEFAULT_BASE_URL.get(cfg.get("llm_factory"), "")
        key = cfg.get("api_key")
        if not model or not key or not base_url:
            raise ValueError("Browser requires tenant model credentials and an OpenAI-compatible endpoint")
        model, _factory = TenantLLMService.split_model_name_and_factory(str(model))
        suffix = TenantLLMService._FACTORY_SUFFIX.get(cfg.get("llm_factory"), "")
        if suffix and model.endswith(suffix):
            model = model[: -len(suffix)]
        return ChatOpenAI(
            model=model, api_key=key, base_url=base_url, temperature=self._param.temperature, max_retries=self._param.max_retries, add_schema_to_system_prompt=True, dont_force_structured_output=True
        )

    def _resolve_persistent_profile_dir(self) -> Path:
        tenant, owner = self._scope()
        graph = getattr(self._canvas, "_id", None)
        if not graph:
            graph = json.dumps(self._canvas.dsl.get("graph", {}), sort_keys=True)
        key = hashlib.sha256(json.dumps([tenant, owner, graph, self._id]).encode()).hexdigest()
        return Path(os.getenv("MULTIRAG_BROWSER_PROFILE_ROOT", str(Path(tempfile.gettempdir()) / "multirag_browser_profiles"))) / f"browser-use-user-data-dir-{key}"

    @asynccontextmanager
    async def _profile(self, *, persistent: bool | None = None) -> AsyncIterator[Path]:
        if not (self._param.persist_session if persistent is None else persistent):
            with tempfile.TemporaryDirectory(prefix="browser-use-user-data-dir-") as directory:
                yield Path(directory)
            return
        profile = self._resolve_persistent_profile_dir()
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Lock beside the profile so Chrome and reset never replace its inode.
        with profile.with_suffix(".lock").open("a") as lock:
            while True:
                try:
                    portalocker.lock(lock, portalocker.LOCK_EX | portalocker.LOCK_NB)
                    break
                except portalocker.LockException:
                    if self.is_canceled():
                        raise asyncio.CancelledError
                    await asyncio.sleep(0.1)
            try:
                yield profile
            finally:
                portalocker.unlock(lock)

    async def clear_persistent_profile(self) -> None:
        """Explicit owner-scoped reset; never called by generic node reset."""
        async with self._profile(persistent=True) as profile:
            await _blocking(shutil.rmtree, profile)

    async def _run_browser_use_async(self, task_text: str, download_dir: str, available_file_paths: list[str], profile_dir: Path) -> Any:
        from browser_use import Agent
        from browser_use import Browser as BrowserSession

        llm = await self._build_browser_llm()
        kwargs: dict[str, Any] = {
            "headless": self._param.headless,
            "downloads_path": download_dir,
            "chromium_sandbox": self._param.chromium_sandbox,
            "enable_default_extensions": self._param.enable_default_extensions,
            "user_data_dir": str(profile_dir),
            "profile_directory": "Default",
            "keep_alive": False,
        }
        executable = os.getenv("BROWSER_USE_EXECUTABLE_PATH")
        if executable:
            kwargs["executable_path"] = executable
        # 0.11.13 clones Chrome profiles unless this SDK-recognized basename
        # is used. Our owner-hashed path must remain the actual profile.
        with tempfile.TemporaryDirectory(prefix="multirag_browser_agent_") as workspace:
            async with httpx.AsyncClient() as client:
                llm.http_client = client
                browser = BrowserSession(**kwargs)
                try:
                    agent = Agent(task=task_text, llm=llm, browser=browser, available_file_paths=available_file_paths, calculate_cost=False, file_system_path=workspace)
                    run = asyncio.create_task(agent.run(max_steps=self._param.max_steps))
                    try:
                        while not run.done():
                            if self.is_canceled():
                                raise asyncio.CancelledError
                            await asyncio.wait({run}, timeout=0.1)
                        return await run
                    finally:
                        if not run.done():
                            run.cancel()
                            await asyncio.gather(run, return_exceptions=True)
                finally:
                    # 0.11 kill() stops local Chromium; stop() can retain it.
                    close = asyncio.create_task(browser.kill())
                    try:
                        await asyncio.shield(close)
                    except asyncio.CancelledError:
                        await close
                        raise

    @staticmethod
    def _extract_history_text(history: Any) -> str:
        success = getattr(history, "is_successful", None)
        if callable(success) and success() is not True:
            raise RuntimeError("Browser agent did not complete its task")
        result = getattr(history, "final_result", None)
        value = result() if callable(result) else result
        return value if isinstance(value, str) else ""

    @staticmethod
    def _download_payloads(directory: str) -> list[tuple[str, bytes]]:
        root = Path(directory).resolve()
        payloads = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("Browser download escapes its directory")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError("Browser download is not a regular file")
            payloads.append((path.name, path.read_bytes()))
        return payloads

    async def _save_downloads(self, download_dir: str) -> list[dict[str, Any]]:
        tenant, owner = self._scope()
        payloads = await _blocking(self._download_payloads, download_dir)
        if not payloads:
            return []
        storage = settings.STORAGE_IMPL
        stored: list[tuple[str, str]] = []
        identifiers: list[str] = []
        created_folder: str | None = None
        try:
            async with async_db_connection() as db:
                folder = await db.scalar(select(File).where(File.tenant_id == tenant, File.created_by == owner, File.parent_id == File.id, File.type == FileType.FOLDER.value))
                if folder is None:
                    folder_id = get_uuid()
                    created_folder = folder_id
                    folder = File(id=folder_id, parent_id=folder_id, tenant_id=tenant, created_by=owner, name="/", type=FileType.FOLDER.value, size=0, location="")
                    db.add(folder)
                result = []
                for name, blob in payloads:
                    identifier = get_uuid()
                    identifiers.append(identifier)
                    location = f"{identifier}_{self._safe_url_filename(name)}"
                    # Include even uncertain/failed writes in compensation.
                    stored.append((folder.id, location))
                    receipt = await _blocking(storage.put, folder.id, location, blob)
                    if receipt is False or await _blocking(storage.get_bytes, folder.id, location) != blob:
                        raise RuntimeError("Browser download storage readback failed")
                    file = File(id=identifier, parent_id=folder.id, tenant_id=tenant, created_by=owner, name=name, location=location, size=len(blob), type=filename_type(name))
                    db.add(file)
                    result.append({"file_id": identifier, "name": name, "size": len(blob), "parent_id": folder.id})
                await db.commit()
                return result
        except BaseException as original:
            # A lost commit reply can mean SQL committed. Confirm/remove only this
            # batch's generated rows before deleting its objects; otherwise keep
            # payloads available for operator recovery.
            try:
                async with async_db_connection() as cleanup_db:
                    owned = select(File.id).where(File.id.in_(identifiers), File.tenant_id == tenant, File.created_by == owner)
                    existing = list(await cleanup_db.scalars(owned))
                    folder = await cleanup_db.scalar(select(File).where(File.id == created_folder, File.tenant_id == tenant, File.created_by == owner)) if created_folder else None
                    if existing or folder is not None:
                        if existing:
                            await cleanup_db.execute(delete(File).where(File.id.in_(existing), File.tenant_id == tenant, File.created_by == owner))
                        if folder is not None:
                            children = await cleanup_db.scalar(select(File.id).where(File.parent_id == folder.id, File.id != folder.id).limit(1))
                            if children is None:
                                await cleanup_db.delete(folder)
                        try:
                            await cleanup_db.commit()
                        except Exception:
                            # A second lost acknowledgement is resolved by the
                            # independent SQL readback below, never by guessing.
                            await cleanup_db.rollback()
                async with async_db_connection() as readback_db:
                    if await readback_db.scalar(owned.limit(1)) is not None:
                        raise RuntimeError("Browser SQL artifacts remain after compensation")
            except Exception as cleanup_error:
                raise RuntimeError("Browser download failed; SQL cleanup could not be confirmed, payloads retained") from cleanup_error
            errors = []
            for bucket, key in reversed(stored):
                try:
                    receipt = await _blocking(storage.rm, bucket, key)
                    if receipt is False or await _blocking(storage.get_bytes, bucket, key) is not None:
                        raise RuntimeError("Object remains after compensation")
                except Exception as error:
                    errors.append(error)
            if errors:
                raise RuntimeError("Browser download failed; storage cleanup could not be confirmed") from original
            raise

    async def _invoke_async(self, **kwargs: Any) -> dict[str, Any]:
        self.set_output("content", "")
        self.set_output("downloaded_files", [])
        self._param.outputs.pop("_ERROR", None)
        for key, meta in self.get_input_elements().items():
            self.set_input_value(key, kwargs.get(key, meta.get("value")))
        prompt = self._resolve_text(kwargs.get("prompts", self._param.prompts), kwargs)
        self.set_input_value("prompts", prompt)
        async with asyncio.timeout(int(os.getenv("COMPONENT_EXEC_TIMEOUT", "1200"))):
            with tempfile.TemporaryDirectory(prefix="multirag_browser_upload_") as uploads, tempfile.TemporaryDirectory(prefix="multirag_browser_download_") as downloads:
                prepared = await self._prepare_upload_files(uploads, inputs=kwargs)
                task = prompt
                if prepared:
                    task += "\n\nFiles available for upload:\n" + "\n".join(item["local_path"] for item in prepared)
                async with self._profile() as profile:
                    history = await self._run_browser_use_async(task, downloads, [item["local_path"] for item in prepared], profile)
                # Parse before committing artifacts; broken result APIs must fail.
                content = self._extract_history_text(history)
                files = await self._save_downloads(downloads)
                self.set_output("content", content)
                self.set_output("downloaded_files", files)
        return self.output()

    def _invoke(self, **kwargs: Any) -> dict[str, Any]:
        return asyncio.run(self._invoke_async(**kwargs))

    def thoughts(self) -> str:
        return "Planning and executing browser actions..."

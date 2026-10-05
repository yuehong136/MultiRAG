"""Owned product runtime; only model output and namespace routing are controlled."""

import asyncio
import copy
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from scripts.acceptance.evidence import Evidence, require
from tests.support.document_parse_retirement import parse_api as parse_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def product_evidence(request: pytest.FixtureRequest) -> Iterator[Evidence]:
    root = request.config.getoption("--test-report-dir")
    require(bool(root), "Use make acceptance to retain results")
    report = Evidence(Path(root) / "product", mode=os.environ.get("MULTIRAG_ACCEPTANCE_MODE", "full"))
    yield report
    if report.cleanup_record is not None:

        def cleanup_readback() -> dict[str, Any]:
            assert report.cleanup_record is not None, "Missing cleanup record"
            record = json.loads(report.cleanup_record.read_text())
            parse = record.get("parse", {})
            require(record.get("bucket_removed") is True and record.get("listener_closed") is True, "Bucket/API listener cleanup not confirmed")
            require(parse.get("collection_removed") is True and parse.get("queue_removed") is True and parse.get("cache_removed") is True, "Milvus/Redis cleanup not confirmed")
            require(bool(record.get("base_remaining")) and not any(record["base_remaining"].values()), "Owned user/token SQL rows remain")
            require(bool(parse.get("remaining")) and not any(parse["remaining"].values()), "Owned dataset/document SQL rows remain")
            return {"sql_rows_remaining": 0, "bucket_removed": True, "collection_removed": True, "queue_removed": True, "api_listener_closed": True}

        report.check("resources.cleanup_readback", cleanup_readback)
    required = {
        "api.configuration.save_readback_omission_clear",
        "api.upload.readback",
        "api.parse.completed_nonzero_chunks",
        "api.chunks.source_content",
        "api.retrieval.uploaded_document_content",
        "resources.cleanup_readback",
    }
    if report.metadata["mode"] == "full":
        required.update({"web.execution", "web.configuration.save_readback_reload", "web.upload_parse_chunks", "web.workflow.errors"})
    missing = sorted(required - {item["name"] for item in report.results})
    if missing:
        report.blocked("coverage.required_checks", f"Required checks did not execute: {', '.join(missing)}")
    report.contact_sheets()
    report.finish()
    require(report.successful, f"Product acceptance failed; inspect {report.directory / 'acceptance.md'}")


@pytest.fixture
def product_runtime(product_evidence: Evidence, parse_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    from api.apps.services import dataset_search_service
    from api.db import db_models
    from api.db.db_models import LLM, Knowledgebase, LLMFactories, Tenant, TenantLLM
    from api.db.services.llm_service import LLMBundle
    from core.nlp import search

    env = parse_api
    report = product_evidence
    report.secrets.append(env["jwt"])
    report.cleanup_record = env["record_path"]
    report.metadata["resources"] = {key: env[key] for key in ("kb", "collection", "queue", "bucket")}
    report.metadata["resources"]["database"] = env["engine"].url.database
    report.save()
    factory, model = "Builtin", "scratch-embedding"
    with Session(env["engine"]) as db:
        db.merge(LLMFactories(name=factory, tags="Text Embedding", status="1"))
        db.merge(LLM(fid=factory, llm_name=model, mdl_type="embedding", max_tokens=8192, tags="Text Embedding", status="1"))
        registered = TenantLLM(tenant_id=env["owners"][0], llm_factory=factory, llm_name=model, mdl_type="embedding", api_key="", max_tokens=8192)
        db.add(registered)
        db.flush()
        registered_id = registered.id
        canonical_model = f"{model}@{factory}"
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["kb"]).values(tenant_embd_id=registered_id, embd_id=canonical_model))
        db.execute(sa.update(Tenant).where(Tenant.id == env["owners"][0]).values(embd_id=canonical_model))
        db.commit()

    class Embedding(LLMBundle):
        def __init__(self) -> None:
            self.db = None
            self.llm_name = model
            self.max_length = 8192

        def encode_queries(self, text: str) -> tuple[np.ndarray, int]:
            return np.asarray([0.1] * 768), len(text)

    monkeypatch.setattr(dataset_search_service, "LLMBundle", lambda *args, **kwargs: Embedding())
    async_engine = create_async_engine(env["engine"].url, poolclass=NullPool)
    monkeypatch.setattr(db_models, "async_session_factory", async_sessionmaker(async_engine, expire_on_commit=False))

    @contextmanager
    def scratch_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    monkeypatch.setattr(search, "db_connection", scratch_db)
    try:
        yield env
    finally:
        asyncio.run(async_engine.dispose())
        with env["engine"].begin() as db:
            db.execute(sa.delete(TenantLLM).where(TenantLLM.id == registered_id))


def finite_worker(env: dict[str, Any], evidence: Evidence) -> Callable[[], None]:
    """Consume one owned text task in the production worker, then sync status."""
    consumer = f"product-{uuid4().hex}"
    group = f"product-{uuid4().hex}"

    def run() -> None:
        from api.db.db_models import Document
        from api.db.services.document_service import DocumentService
        from common.config_utils import CONFIGS

        require(str(env["engine"].url.database).startswith("multirag_test_"), "Worker must use a scratch database")
        with tempfile.TemporaryDirectory(prefix="multirag-product-worker-") as temporary:
            directory = Path(temporary)
            overlay = copy.deepcopy(CONFIGS)
            url = env["engine"].url
            overlay["postgresql"].update(host=url.host, port=url.port, user=url.username, password=url.password, dbname=url.database)
            overlay["minio"].update(bucket=env["bucket"], prefix_path="")
            # Reuse this run's consumer group: a new group starts from stream
            # history and would replay the API upload instead of the Web task.
            context = {"queue": env["queue"], "consumer": consumer, "group": group, "boundary": "complete"}
            for name, value in (("config.json", overlay), ("context.json", context)):
                path = directory / name
                path.touch(mode=0o600)
                path.write_text(json.dumps(value))
            log_path = evidence.directory / f"worker-{uuid4().hex[:8]}.log"
            log_path.touch(mode=0o600)
            with log_path.open("wb") as log:
                child = subprocess.Popen(
                    [sys.executable, "-m", "tests.support.worker_process"],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env={**os.environ, "MULTIRAG_CONFIG_OVERLAY_FILE": str(directory / "config.json"), "MULTIRAG_WORKER_TEST_CONTEXT": str(directory / "context.json")},
                )
                try:
                    code = child.wait(timeout=120)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=15)
            require(code == 0, f"Worker exited {code}; inspect {log_path.name}")
        # Production's periodic status reconciler; the client still verifies
        # completion through independent HTTP rather than assigning success.
        with Session(env["engine"]) as db:
            ids = list(db.scalars(sa.select(Document.id).where(Document.kb_id == env["kb"])))
            DocumentService.update_progress_immediately(db, [{"id": identifier} for identifier in ids])

    return run


@contextmanager
def product_web_server(api_base: str, evidence: Evidence) -> Iterator[str]:
    selected = os.environ.get("MULTIRAG_ACCEPTANCE_WEB_BASE_URL")
    if selected:
        yield selected.rstrip("/")
        return
    checkout = Path(os.environ.get("MULTIRAG_ACCEPTANCE_WEB_CHECKOUT", str(Path(__file__).resolve().parents[3] / "web"))).resolve()
    require((checkout / "package.json").is_file() and (checkout / "node_modules").is_dir(), "Web checkout/dependencies missing; supply --web-checkout or --web-base-url")
    npm = shutil.which("npm")
    require(npm is not None, "npm is required to start the Web checkout")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    log_path = evidence.directory / "vite.log"
    log_path.touch(mode=0o600)
    with log_path.open("wb") as log:
        child = subprocess.Popen(
            [str(npm), "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
            cwd=checkout,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env={**os.environ, "VITE_API_BASE_URL": api_base},
        )
        try:
            deadline = time.monotonic() + 45
            ready = False
            while child.poll() is None and time.monotonic() < deadline:
                try:
                    if requests.get(base, timeout=2).status_code == 200:
                        ready = True
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.2)
            require(ready, "Web server did not start; inspect vite.log")
            evidence.metadata["web_checkout"] = str(checkout)
            evidence.save()
            yield base
        finally:
            # npm starts Vite as a child; stop this owned process group only.
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if child.poll() is None:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=10)

            def closed_listener() -> dict[str, bool]:
                with socket.socket() as probe:
                    probe.settimeout(2)
                    require(probe.connect_ex(("127.0.0.1", port)) != 0, "Owned Web listener still accepts connections")
                return {"web_listener_closed": True}

            evidence.check("web.server.cleanup", closed_listener)

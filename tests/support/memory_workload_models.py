"""Real configured providers with scratch-only bindings and observed usage.

Only synthetic evaluation uses this helper. Credentials stay in memory and the
owned scratch database; reports contain model references and hashes only.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from typing import Any, cast
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from api.db.db_models import TenantLLM
from api.db.joint_services.tenant_model_service import get_model_config_by_id
from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import PreparedTenantLLM, TenantLLMService
from common.config_utils import CONFIGS
from tests.support.database import _pg_url
from tests.support.memory_workload_cost import STAGES
from tests.support.resource_journal import ResourceJournal


def configured_models(identifiers: dict[str, int]) -> dict[str, dict[str, Any]]:
    """Read server-owned configuration without constructing or calling a model."""
    engine = sa.create_engine(_pg_url(CONFIGS["postgresql"]["dbname"]))
    try:
        with Session(engine) as db:
            db.execute(sa.text("SET TRANSACTION READ ONLY"))
            configs = {}
            for kind, identifier in identifiers.items():
                record = db.get(TenantLLM, identifier)
                if record is None:
                    raise LookupError("configured_workload_model_missing")
                configs[kind] = dict(get_model_config_by_id(db, identifier, tenant_id=record.tenant_id, model_type=kind))
            return configs
    finally:
        engine.dispose()


def copy_models(engine: sa.Engine, tenant_id: str, configs: dict[str, dict[str, Any]], journal: ResourceJournal) -> dict[str, int]:
    """Copy only resolved provider configuration into the caller's scratch tenant."""
    if not re.fullmatch(r"multirag_test_[0-9a-f]{12}", str(engine.url.database)):
        raise ValueError("workload_requires_owned_scratch_database")
    identities = {}
    with Session(engine) as db:
        for kind, config in configs.items():
            journal.event("reserve-workload-model", tenant_id=tenant_id, model_type=kind, model_reference=f"{config['llm_name']}@{config['llm_factory']}")
            row = TenantLLM(
                tenant_id=tenant_id,
                llm_factory=config["llm_factory"],
                llm_name=config["llm_name"],
                mdl_type=kind,
                api_key=config.get("api_key"),
                api_base=config.get("api_base"),
                max_tokens=config.get("max_tokens", 8192),
            )
            db.add(row)
            db.flush()
            assert row.id is not None
            identities[kind] = row.id
        db.commit()
    return identities


def _mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        result = value.model_dump()
        return result if isinstance(result, dict) else {}
    return dict(value) if isinstance(value, Mapping) else {}


class ProviderObserver:
    """Capture each actual SDK request, including known failed-attempt usage."""

    def __init__(self, journal: ResourceJournal, *, max_attempts: int = 48, capture_synthetic_content: bool = False) -> None:
        self.journal = journal
        self.max_attempts = max_attempts
        self.capture_synthetic_content = capture_synthetic_content
        self.attempts: list[dict[str, Any]] = []
        self.stage = "source_embedding"
        self.phase = ""
        self.lock = threading.RLock()
        self.embedding_reference = ""
        self.embedding_config = ""

    def set_stage(self, stage: str, *, phase: str = "") -> None:
        if stage not in STAGES or not isinstance(phase, str) or len(phase) > 96:
            raise ValueError("invalid_workload_stage")
        with self.lock:
            self.stage = stage
            self.phase = phase

    def _start(self, model: str, config_reference: str) -> dict[str, Any]:
        with self.lock:
            if len(self.attempts) >= self.max_attempts:
                raise RuntimeError("synthetic_workload_attempt_limit")
            row: dict[str, Any] = {
                "attempt_id": uuid4().hex,
                "stage": self.stage,
                "workload_phase": self.phase,
                "model_reference": model,
                "config_reference": config_reference,
                "state": "unknown",
                "provider_total_tokens": None,
                "usage": {},
            }
            self.journal.event("real-provider-attempt-started", **row)
            self.attempts.append(row)
            return row

    def _finish(self, row: dict[str, Any], started: float) -> None:
        row["elapsed_seconds"] = time.monotonic() - started
        self.journal.event("real-provider-attempt-finished", **row)

    def _request_limit(self, options: dict[str, Any]) -> int:
        maximum = options.get("max_tokens", options.get("max_completion_tokens"))
        allowed = 1024 if self.stage == "analysis" else 512
        if type(maximum) is not int or not 1 <= maximum <= allowed:
            raise RuntimeError("workload_unbounded_chat_request")
        return maximum

    def _chat_usage(self, row: dict[str, Any], response: Any) -> None:
        choices = _mapping(response).get("choices")
        if isinstance(choices, list) and choices:
            row["finish_reason"] = _mapping(choices[0]).get("finish_reason")
        if self.capture_synthetic_content:
            if isinstance(choices, list) and choices:
                content = _mapping(_mapping(choices[0]).get("message")).get("content")
                if isinstance(content, str) and len(content.encode("utf-8")) <= 65535:
                    row["synthetic_response_content"] = content
        raw = _mapping(getattr(response, "usage", None))
        details = raw.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        gross = raw.get("prompt_tokens")
        valid_cache = type(gross) is int and gross >= 0 and type(cached) is int and 0 <= cached <= gross
        row.update(
            state="completed",
            provider_total_tokens=raw.get("total_tokens"),
            provider_usage_raw=raw,
            provider_prompt_tokens=gross,
            provider_model=getattr(response, "model", None),
            reasoning_tokens=(raw.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            reasoning_tokens_included_in_output=True,
            usage={
                "input_tokens": cast(int, gross) - cast(int, cached) if valid_cache else None,
                "output_tokens": raw.get("completion_tokens"),
                "cache_read_input_tokens": cached if valid_cache else None,
                "cache_creation_input_tokens": None,
            },
            usage_basis="OpenAI-compatible prompt total includes cached reads; unreported cache-write usage remains unknown",
        )

    def observe_openai_embedding(self, bundle: LLMBundle, reference: str, fingerprint: str) -> None:
        """Observe the real SDK boundary without substituting vectors or usage."""
        client = bundle.mdl.client
        client.max_retries = 0
        original = client.embeddings.create

        def observed(*args: Any, **options: Any) -> Any:
            row, started = self._start(reference, fingerprint), time.monotonic()
            row["request_model"] = options.get("model")
            try:
                response = original(*args, **options)
                raw = _mapping(getattr(response, "usage", None))
                total = raw.get("total_tokens")
                row.update(
                    state="completed",
                    provider_total_tokens=total,
                    provider_usage_raw=raw,
                    provider_model=getattr(response, "model", None),
                    usage={"input_tokens": total, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                    usage_basis="OpenAI embedding input-only response usage; no context-cache creation requested",
                )
                return response
            except BaseException as error:
                row.update(state="failed", error_code=type(error).__name__)
                raise
            finally:
                self._finish(row, started)

        client.embeddings.create = observed

    @contextmanager
    def install(self, patch: pytest.MonkeyPatch, scratch: sa.Engine, allowed_model_ids: set[int]) -> Iterator[None]:
        import dashscope

        if not re.fullmatch(r"multirag_test_[0-9a-f]{12}", str(scratch.url.database)):
            raise ValueError("workload_requires_owned_scratch_database")
        original_construct = cast(Any, LLMBundle.from_prepared).__func__
        original_init = LLMBundle.__init__
        original_embedding = dashscope.TextEmbedding.call

        def account(cls: type[TenantLLMService], identifier: int | None, tokens: int) -> int:
            if identifier is None or tokens <= 0:
                return 0
            if identifier not in allowed_model_ids:
                raise RuntimeError("workload_usage_model_outside_scratch_scope")
            with Session(scratch) as db:
                result = cast(CursorResult[Any], db.execute(sa.update(TenantLLM).where(TenantLLM.id == identifier).values(used_tokens=TenantLLM.used_tokens + tokens)))
                db.commit()
                return int(result.rowcount)

        async def account_async(cls: type[TenantLLMService], identifier: int | None, tokens: int) -> int:
            import asyncio

            return await asyncio.to_thread(account, cls, identifier, tokens)

        def observe_bundle(bundle: LLMBundle, config: dict[str, Any]) -> LLMBundle:
            if config.get("id") not in allowed_model_ids:
                raise RuntimeError("workload_provider_model_outside_scratch_scope")
            reference = f"{config['llm_name']}@{config['llm_factory']}"
            fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()
            if config["mdl_type"] == "embedding":
                self.embedding_reference, self.embedding_config = reference, fingerprint
                if config["llm_factory"] == "OpenAI":
                    self.observe_openai_embedding(bundle, reference, fingerprint)
            else:
                client = getattr(bundle.mdl, "async_client", None)
                if hasattr(bundle.mdl, "_completion"):
                    original_completion = bundle.mdl._completion

                    @asynccontextmanager
                    async def observed_completion(options: dict[str, Any]) -> AsyncIterator[Any]:
                        maximum = self._request_limit(options)
                        if options.get("stream"):
                            raise RuntimeError("workload_requires_nonstream_usage_receipt")
                        row, started = self._start(reference, fingerprint), time.monotonic()
                        row["request_max_tokens"] = maximum
                        try:
                            async with original_completion(options) as response:
                                self._chat_usage(row, response)
                                yield response
                        except BaseException as error:
                            row.update(state="failed", error_code=type(error).__name__)
                            raise
                        finally:
                            self._finish(row, started)

                    bundle.mdl._completion = observed_completion
                    return bundle
                if client is None or not hasattr(client, "chat"):
                    raise RuntimeError("workload_chat_usage_observer_unsupported")
                client.max_retries = 0
                original_create = client.chat.completions.create

                async def observed_create(*args: Any, **options: Any) -> Any:
                    maximum = self._request_limit(options)
                    row, started = self._start(reference, fingerprint), time.monotonic()
                    row["request_max_tokens"] = maximum
                    try:
                        response = await original_create(*args, **options)
                        self._chat_usage(row, response)
                        return response
                    except BaseException as error:
                        row.update(state="failed", error_code=type(error).__name__)
                        raise
                    finally:
                        self._finish(row, started)

                client.chat.completions.create = observed_create
            return bundle

        def construct(cls: type[LLMBundle], prepared: PreparedTenantLLM, lang: str = "Chinese", **kwargs: Any) -> LLMBundle:
            kwargs["max_retries"] = 0
            return observe_bundle(original_construct(cls, prepared, lang, **kwargs), prepared.model_config)

        def initialize(bundle: LLMBundle, db: Session | None, tenant_id: str, model_config: dict[str, Any], lang: str = "Chinese", **kwargs: Any) -> None:
            kwargs["max_retries"] = 0
            original_init(bundle, db, tenant_id, model_config, lang, **kwargs)
            observe_bundle(bundle, model_config)

        def observed_embedding(*args: Any, **options: Any) -> Any:
            row, started = self._start(self.embedding_reference, self.embedding_config), time.monotonic()
            try:
                response = original_embedding(*args, **options)
                raw = _mapping(response.get("usage") if isinstance(response, Mapping) else getattr(response, "usage", None))
                total = raw.get("total_tokens")
                row.update(
                    state="completed" if (response.get("status_code") if isinstance(response, Mapping) else getattr(response, "status_code", None)) == 200 else "failed",
                    provider_total_tokens=total,
                    provider_usage_raw=raw,
                    usage={"input_tokens": total, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                    usage_basis="text-embedding-v4 input-only billing; provider documents no context-cache support",
                )
                return response
            except BaseException as error:
                row.update(state="failed", error_code=type(error).__name__)
                raise
            finally:
                self._finish(row, started)

        patch.setattr(TenantLLMService, "increase_usage_by_id", classmethod(account))
        patch.setattr(TenantLLMService, "increase_usage_by_id_async", classmethod(account_async))
        patch.setattr(LLMBundle, "from_prepared", classmethod(construct))
        patch.setattr(LLMBundle, "__init__", initialize)
        patch.setattr(dashscope.TextEmbedding, "call", staticmethod(observed_embedding))
        yield

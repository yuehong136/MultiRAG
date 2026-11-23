"""Production Web defaults and dataset creation with independent SQL readback."""

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Knowledgebase, Tenant, TenantModel, TenantModelInstance, TenantModelProvider
from api.db.model_contract import MODEL_TYPE_BITS
from common.constants import RetCode
from tests.support.rest_document_contract import call
from tests.support.rest_document_contract import rest_documents as rest_documents

pytestmark = pytest.mark.external_consumer


def test_actual_web_defaults_create_embedding_dataset_by_model_id(rest_documents: dict[str, Any], record_property: Callable[[str, object], None]) -> None:
    env = rest_documents
    journal = env["journal"]
    owner = env["owners"][0]
    provider, instance = uuid4().hex, uuid4().hex
    model_id, missing_model_id = uuid4().hex, uuid4().hex
    name = "after53_default_" + uuid4().hex
    checkout = Path(os.environ["WEB_COMPLETION_CHECKOUT"])
    script = checkout / "scripts/verify-dataset-create-routes.ts"
    node = shutil.which("node")
    assert node and script.is_file(), "Current Web consumer and installed Node are required"
    journal.event("reserve-default-model-create", database=env["engine"].url.database, owner=owner, provider=provider, instance=instance, model_id=model_id, dataset_name=name)
    try:
        with Session(env["engine"]) as db:
            db.add(TenantModelProvider(id=provider, tenant_id=owner, provider_name="Ollama", status="active"))
            db.flush()
            db.add(TenantModelInstance(id=instance, provider_id=provider, instance_name="after53", api_key="controlled-no-vendor", base_url="http://controlled.invalid", status="active"))
            db.flush()
            db.add(TenantModel(id=model_id, provider_id=provider, instance_id=instance, model_name="after53-embedding", model_type=MODEL_TYPE_BITS["embedding"]))
            db.execute(sa.update(Tenant).where(Tenant.id == owner).values(embd_id=model_id, tenant_embd_id=model_id))
            db.commit()
        completed = subprocess.run(
            [node, "--import", str(checkout / "node_modules/tsx/dist/loader.mjs"), str(script)],
            cwd=checkout,
            env={
                **os.environ,
                "DOMAIN_ACCEPTANCE_BASE": env["base"],
                "DOMAIN_ACCEPTANCE_TOKEN": env["jwt"][0],
                "DOMAIN_ACCEPTANCE_MODEL_REFERENCE": model_id,
                "DOMAIN_ACCEPTANCE_DATASET_NAME": name,
                "DOMAIN_ACCEPTANCE_MODE": "create",
                "MULTIRAG_RUN_JOURNAL": str(journal.path),
                "TSX_TSCONFIG_PATH": str(checkout / "tsconfig.app.json"),
            },
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        result = json.loads(completed.stdout.splitlines()[-1])
        with Session(env["engine"]) as reader:
            dataset = reader.get(Knowledgebase, result["id"])
            assert dataset is not None and dataset.name == name and dataset.tenant_id == owner
            assert dataset.embd_id == dataset.tenant_embd_id == model_id
            assert dataset.parser_id == "naive"
            row = reader.get(Tenant, owner)
            assert row is not None and row.tenant_embd_id == model_id
        receipts = result["receipts"]
        assert len(receipts) == 3 and all(row["httpStatus"] == 200 and row["code"] == 0 for row in receipts)
        assert receipts[0]["path"] == "/api/v1/models/default"
        assert receipts[1]["path"] == "/api/v1/datasets" and receipts[1]["requestBody"]["embedding_model"] == model_id
        legacy_id = "17001"
        journal.event("reserve-denied-dataset-request", names=[name + "_denied"], model_ids=[model_id, missing_model_id, legacy_id])
        with env["engine"].connect() as reader:
            before = list(reader.execute(sa.select(Knowledgebase.__table__).order_by(Knowledgebase.id)))
        for actor, reference in [(1, model_id), (0, missing_model_id)]:
            response = call(env, "POST", "/datasets", credential=env["keys"][actor], json={"name": name + "_denied", "embedding_model": reference})
            assert response.status_code == 200 and response.json()["code"] == RetCode.DATA_ERROR, response.text
        legacy = call(env, "POST", "/datasets", credential=env["keys"][0], json={"name": name + "_denied", "embedding_model": legacy_id})
        assert legacy.status_code == 422
        assert any(issue["type"] == "format_invalid" and issue["loc"] == ["body", "embedding_model"] for issue in legacy.json()["detail"])
        with env["engine"].begin() as db:
            db.execute(sa.update(TenantModelInstance).where(TenantModelInstance.id == instance).values(status="inactive"))
        response = call(env, "POST", "/datasets", credential=env["keys"][0], json={"name": name + "_denied", "embedding_model": model_id})
        assert response.status_code == 200 and response.json()["code"] == RetCode.DATA_ERROR, response.text
        with env["engine"].connect() as reader:
            assert list(reader.execute(sa.select(Knowledgebase.__table__).order_by(Knowledgebase.id))) == before
        journal.event("web-default-model-sql-readback", dataset_id=result["id"], name=name, model_id=model_id, tenant_id=owner, receipts=receipts)
        record_property("web_default_model_create_sql", json.dumps(result))
    finally:
        with env["engine"].begin() as db:
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.name.in_([name, name + "_denied"])))
            db.execute(sa.update(Tenant).where(Tenant.id == owner).values(embd_id="", tenant_embd_id=None))
            db.execute(sa.delete(TenantModel).where(TenantModel.id == model_id))
            db.execute(sa.delete(TenantModelInstance).where(TenantModelInstance.id == instance))
            db.execute(sa.delete(TenantModelProvider).where(TenantModelProvider.id == provider))
        with env["engine"].connect() as reader:
            remaining = {
                "dataset": reader.scalar(sa.select(sa.func.count()).select_from(Knowledgebase).where(Knowledgebase.name.in_([name, name + "_denied"]))),
                "model": reader.scalar(sa.select(sa.func.count()).select_from(TenantModel).where(TenantModel.id == model_id)),
                "instance": reader.scalar(sa.select(sa.func.count()).select_from(TenantModelInstance).where(TenantModelInstance.id == instance)),
                "provider": reader.scalar(sa.select(sa.func.count()).select_from(TenantModelProvider).where(TenantModelProvider.id == provider)),
            }
        journal.event("web-default-model-cleanup-readback", remaining=remaining)
        assert not any(remaining.values()), remaining

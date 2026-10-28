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

from api.db.db_models import Knowledgebase, Tenant, TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider
from common.constants import RetCode
from tests.support.rest_document_contract import call
from tests.support.rest_document_contract import rest_documents as rest_documents

pytestmark = pytest.mark.external_consumer


def test_actual_web_defaults_create_numeric_embedding_dataset(rest_documents: dict[str, Any], record_property: Callable[[str, object], None]) -> None:
    env = rest_documents
    journal = env["journal"]
    owner = env["owners"][0]
    provider, instance = uuid4().hex, uuid4().hex
    model_id = 10**15 + int(uuid4().hex[:13], 16)
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
            db.add(TenantLLM(id=model_id, tenant_id=owner, llm_factory="Ollama", llm_name="model:" + uuid4().hex, mdl_type="embedding", api_key="controlled-no-vendor", status="1"))
            db.flush()
            db.add(TenantModel(id=model_id, instance_id=instance, model_name="after53-embedding", model_types=["embedding"], is_tools=False))
            db.execute(sa.update(Tenant).where(Tenant.id == owner).values(embd_id=str(model_id), tenant_embd_id=model_id))
            db.commit()
        completed = subprocess.run(
            [node, "--import", str(checkout / "node_modules/tsx/dist/loader.mjs"), str(script)],
            cwd=checkout,
            env={
                **os.environ,
                "DOMAIN_ACCEPTANCE_BASE": env["base"],
                "DOMAIN_ACCEPTANCE_TOKEN": env["jwt"][0],
                "DOMAIN_ACCEPTANCE_MODEL_REFERENCE": str(model_id),
                "DOMAIN_ACCEPTANCE_DATASET_NAME": name,
                "DOMAIN_ACCEPTANCE_MODE": "create",
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
            assert dataset.embd_id == str(model_id) and dataset.tenant_embd_id == model_id
            assert dataset.parser_id == "naive"
            row = reader.get(Tenant, owner)
            assert row is not None and row.tenant_embd_id == model_id
        receipts = result["receipts"]
        assert len(receipts) == 3 and all(row["httpStatus"] == 200 and row["code"] == 0 for row in receipts)
        assert receipts[0]["path"] == "/api/v1/models/default"
        assert receipts[1]["path"] == "/api/v1/datasets" and receipts[1]["requestBody"]["embedding_model"] == str(model_id)
        journal.event("reserve-denied-dataset-request", names=[name + "_denied"], model_ids=[model_id, model_id + 1])
        with env["engine"].connect() as reader:
            before = list(reader.execute(sa.select(Knowledgebase.__table__).order_by(Knowledgebase.id)))
        for reference in [str(model_id), str(model_id + 1)]:
            response = call(env, "POST", "/datasets", credential=env["keys"][1], json={"name": name + "_denied", "embedding_model": reference})
            assert response.status_code == 200 and response.json()["code"] == RetCode.DATA_ERROR, response.text
        with env["engine"].begin() as db:
            db.execute(sa.update(TenantModelInstance).where(TenantModelInstance.id == instance).values(status="inactive"))
        response = call(env, "POST", "/datasets", credential=env["keys"][0], json={"name": name + "_denied", "embedding_model": str(model_id)})
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
            db.execute(sa.delete(TenantLLM).where(TenantLLM.id == model_id))
            db.execute(sa.delete(TenantModelInstance).where(TenantModelInstance.id == instance))
            db.execute(sa.delete(TenantModelProvider).where(TenantModelProvider.id == provider))
        with env["engine"].connect() as reader:
            remaining = {
                "dataset": reader.scalar(sa.select(sa.func.count()).select_from(Knowledgebase).where(Knowledgebase.name.in_([name, name + "_denied"]))),
                "model": reader.scalar(sa.select(sa.func.count()).select_from(TenantModel).where(TenantModel.id == model_id)),
                "llm": reader.scalar(sa.select(sa.func.count()).select_from(TenantLLM).where(TenantLLM.id == model_id)),
                "instance": reader.scalar(sa.select(sa.func.count()).select_from(TenantModelInstance).where(TenantModelInstance.id == instance)),
                "provider": reader.scalar(sa.select(sa.func.count()).select_from(TenantModelProvider).where(TenantModelProvider.id == provider)),
            }
        journal.event("web-default-model-cleanup-readback", remaining=remaining)
        assert not any(remaining.values()), remaining

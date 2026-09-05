from typing import Any

from sqlalchemy.orm import Session

from api.apps.services import dataset_api_service
from api.db.db_models import Knowledgebase
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.metadata_utils import turn2jsonschema


def test_config_roundtrip_preserves_extraction_fields(db: Session, monkeypatch: Any) -> None:
    kb = Knowledgebase(id="kb", tenant_id="tenant", parser_config={"chunk_token_num": 128})
    monkeypatch.setattr(KnowledgebaseService, "get_or_none", lambda *args, **kwargs: kb)

    def update(session: Session, kb_id: str, values: dict[str, Any]) -> bool:
        kb.parser_config = values["parser_config"]
        return True

    monkeypatch.setattr(KnowledgebaseService, "update_by_id", update)
    fields = [{"key": "author", "description": "Author", "enum": ["Alice"], "restrictDefinedValues": True}]
    config = {"enabled": True, "fields": fields}
    assert dataset_api_service.update_auto_metadata(db, "tenant", "kb", config) == (True, config)
    assert dataset_api_service.get_auto_metadata(db, "tenant", "kb") == (True, config)
    assert kb.parser_config["chunk_token_num"] == 128
    schema = turn2jsonschema(kb.parser_config["metadata"])
    assert schema["properties"]["author"]["enum"] == ["Alice"]

"""Parser flags survive the same normalization and SQL storage used by canvases."""

import json
from uuid import uuid4

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from api.apps.services.canvas_replica_service import CanvasReplicaService
from api.db.db_models import UserCanvas
from api.db.services.canvas_service import UserCanvasService
from core.flow.parser.parser import ParserParam


def test_parser_cleanup_flags_sql_save_reload(pg_scratch_engine: Engine) -> None:
    UserCanvas.__table__.create(pg_scratch_engine, checkfirst=True)
    canvas_id = uuid4().hex
    for enabled in (False, True, False):
        param = ParserParam()
        param.setups = {kind: param.setups[kind] for kind in ("pdf", "doc", "docx", "html")}
        for setup in param.setups.values():
            setup.update(remove_header_footer=enabled, remove_toc=enabled)
        param.check()
        dsl = {"components": {"Parser:cleanup": {"obj": {"component_name": "Parser", "params": {"setups": param.setups}}}}}
        normalized = CanvasReplicaService.normalize_dsl(json.dumps(dsl))
        with Session(pg_scratch_engine) as db:
            if db.get(UserCanvas, canvas_id) is None:
                UserCanvasService.save(db, id=canvas_id, user_id="parser-cleanup-scratch", canvas_category="dataflow_canvas", dsl=normalized)
            else:
                assert UserCanvasService.update_by_id(db, canvas_id, {"dsl": normalized}) == 1
            db.commit()
        # Read in a new transaction rather than trusting the save response/identity map.
        with pg_scratch_engine.connect() as conn:
            stored = conn.execute(select(UserCanvas.dsl).where(UserCanvas.id == canvas_id)).scalar_one()
        assert stored == dsl
        reloaded = ParserParam()
        reloaded.update(stored["components"]["Parser:cleanup"]["obj"]["params"])
        reloaded.check()
        for setup in reloaded.setups.values():
            assert setup["remove_header_footer"] is enabled
            assert setup["remove_toc"] is enabled

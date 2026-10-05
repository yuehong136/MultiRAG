"""Zero-hit relaxation keeps the caller's document and availability scope."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from common.doc_store.doc_store_base import MatchDenseExpr, MatchTextExpr
from core.nlp.search import Dealer


@pytest.mark.parametrize("backend", ["milvus", "infinity", "elasticsearch", "opensearch"])
@pytest.mark.parametrize("documents", [["selected"], ["-999"]])
async def test_zero_hit_retry_keeps_document_scope(backend: str, documents: list[str]) -> None:
    calls: list[tuple[dict[str, Any], int, int]] = []

    def search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((deepcopy(args[2]), args[5], args[6]))
        # A relaxed lexical match must never make an outside document visible.
        if "doc_id" not in args[2]:
            return {"outside": {"doc_id": "outside", "content_with_weight": "keywords"}}
        return {}

    dealer = Dealer.__new__(Dealer)
    dealer.qryr = SimpleNamespace(question=lambda *_a, **_k: (MatchTextExpr(["content_ltks"], "keywords", 100), ["keywords"]))
    dealer.get_vector = AsyncMock(return_value=MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine", 100, {"similarity": 0.5}))
    dealer.dataStore = SimpleNamespace(
        db_type=lambda: backend,
        search=search,
        get_total=len,
        get_doc_ids=lambda result: list(result),
        get_highlight=lambda *_: {},
        get_aggregation=lambda *_: [],
        get_fields=lambda result, _: result,
    )
    result = await dealer.search({"question": "keywords", "doc_ids": documents, "available_int": 1, "search_mode": {"fusion": {}}, "page": 3, "size": 2}, ["scratch"], ["selected-dataset"], object())
    assert result.ids == [] and result.total == 0
    assert calls == [({"doc_id": documents, "available_int": 1}, 4, 2)] * 2


@pytest.mark.parametrize("with_text", [False, True])
def test_infinity_dense_search_receives_only_scalar_scope(with_text: bool) -> None:
    from unittest.mock import MagicMock

    import pandas as pd

    from common.constants import PAGERANK_FLD
    from common.doc_store.doc_store_base import FusionExpr, MatchTextExpr, OrderByExpr
    from core.utils.infinity_conn import InfinityConnection

    cls = next(cell.cell_contents for cell in InfinityConnection.__closure__ if isinstance(cell.cell_contents, type))
    conn = object.__new__(cls)
    conn.logger = MagicMock()
    builder = MagicMock()
    for method in ("output", "match_text", "match_dense", "fusion", "offset", "limit", "option"):
        getattr(builder, method).return_value = builder
    builder.to_df.return_value = (pd.DataFrame([{"id": "in-scope", "SIMILARITY": 0.9, "SCORE": 0.9, PAGERANK_FLD: 0.0}]), {"total_hits_count": 1})
    pool = MagicMock()
    pool.get_conn.return_value.get_database.return_value.get_table.return_value = builder
    conn.connPool = pool
    conn.dbName = "scratch"
    conn.equivalent_condition_to_str = lambda *_: "doc_id IN ('selected') AND available_int=1"
    conn.convert_select_fields = lambda fields: fields
    conn.convert_matching_field = lambda field: field
    dense = MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine", 10, {"similarity": 0.2})
    expressions = [MatchTextExpr(["content"], "keywords", 10), dense, FusionExpr("weighted_sum", 10, {"weights": "0.5,0.5"})] if with_text else [dense]
    result, _ = conn.search(["id"], [], {"doc_id": ["selected"], "available_int": 1}, expressions, OrderByExpr(), 0, 10, ["tenant"], ["kb"])
    assert result["id"].tolist() == ["in-scope"]
    assert builder.match_dense.call_args.args[5]["filter"] == "doc_id IN ('selected') AND available_int=1"

"""ESConnection.get_fields 契约钉板。

两条契约缺一不可：
1. ES 9.x 起 dense_vector 走顶层 fields 响应（exclude_source_vectors 下不在
   _source 内），get_fields 必须能从 hit["fields"] 回退取到并解开双层包装。
2. get_fields 必须继续输出 hit 顶层的 id/_score —— Dealer 的融合打分
   （core/nlp/search.py 的 sres.field[id].get("_score")）与 KG 相似度阈值
   （core/graphrag/search.py 的 sim < sim_thr 过滤）都经 get_fields 读
   _score，丢失即静默退化为 0 分/全量过滤，不报任何错误。
"""

from types import SimpleNamespace

from core.utils.es_conn import ESConnection


def _unwrap_singleton(singleton_factory):
    # 与 test_infinity_row_id 同款：@singleton 把类关进闭包，从 cell 取回。
    return next(cell.cell_contents for cell in singleton_factory.__closure__ if isinstance(cell.cell_contents, type))


def _conn():
    # get_fields 不触碰实例状态；用未绑定实例绕过 __init__ 的真实连接。
    return object.__new__(_unwrap_singleton(ESConnection))


def _hit(doc_id: str, source: dict, score: float | None = None, hit_fields: dict | None = None) -> dict:
    hit = {"_id": doc_id, "_source": source}
    if score is not None:
        hit["_score"] = score
    if hit_fields is not None:
        hit["fields"] = hit_fields
    return hit


def _res(*hits: dict) -> dict:
    return {"hits": {"hits": list(hits)}}


def test_score_and_id_surface_from_hit_level() -> None:
    res = _res(_hit("doc1", {"content_with_weight": "text"}, score=0.83))

    fields = _conn().get_fields(res, ["content_with_weight", "_score", "id"])

    assert "doc1" in fields
    assert fields["doc1"]["_score"] == "0.83"
    assert fields["doc1"]["id"] == "doc1"


def test_missing_score_stays_absent() -> None:
    # 带 sort 的查询 ES 返回 _score: null；此时不应出现在结果里，
    # 调用方的 .get("_score", 0) 默认值兜底。
    res = _res(_hit("doc1", {"content_with_weight": "text"}))

    fields = _conn().get_fields(res, ["content_with_weight", "_score"])

    assert "_score" not in fields["doc1"]
    assert fields["doc1"]["content_with_weight"] == "text"


def test_dense_vector_falls_back_to_fields_response() -> None:
    # ES 9.x：向量不在 _source，在顶层 fields 响应且双层包装。
    res = _res(_hit("doc1", {"content_with_weight": "text"}, score=0.5, hit_fields={"q_768_vec": [[0.1, 0.2, 0.3]]}))

    fields = _conn().get_fields(res, ["content_with_weight", "q_768_vec", "_score"])

    assert fields["doc1"]["q_768_vec"] == [0.1, 0.2, 0.3]
    assert fields["doc1"]["content_with_weight"] == "text"
    assert fields["doc1"]["_score"] == "0.5"


def test_source_value_wins_over_fields_response() -> None:
    res = _res(_hit("doc1", {"tag_kwd": ["a", "b"]}, hit_fields={"tag_kwd": ["stale"]}))

    fields = _conn().get_fields(res, ["tag_kwd"])

    assert fields["doc1"]["tag_kwd"] == ["a", "b"]


def test_empty_fields_returns_empty() -> None:
    assert _conn().get_fields(_res(_hit("doc1", {"x": 1})), []) == {}


def _captured_search_query(condition: dict) -> dict:
    conn = _conn()
    captured: dict = {}
    conn.logger = SimpleNamespace(debug=lambda *_args, **_kwargs: None)

    def fake_search(_index_names, query, *, track_total_hits):
        captured.update(query)
        assert track_total_hits is True
        return {"hits": {"hits": []}}

    conn._es_search_once = fake_search
    conn.search([], [], condition, [], None, 0, 10, "tenant", ["kb1"])
    return captured


def test_search_id_list_matches_source_and_metadata_ids() -> None:
    query = _captured_search_query({"id": ["chunk-1", "chunk-2"]})

    filters = query["query"]["bool"]["filter"]
    assert {
        "bool": {
            "minimum_should_match": 1,
            "should": [
                {"terms": {"id": ["chunk-1", "chunk-2"]}},
                {"terms": {"_id": ["chunk-1", "chunk-2"]}},
            ],
        }
    } in filters


def test_search_scalar_id_matches_source_and_metadata_ids() -> None:
    query = _captured_search_query({"id": "chunk-1"})

    filters = query["query"]["bool"]["filter"]
    assert {
        "bool": {
            "minimum_should_match": 1,
            "should": [
                {"term": {"id": "chunk-1"}},
                {"term": {"_id": "chunk-1"}},
            ],
        }
    } in filters

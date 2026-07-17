"""钉板：tag_feas 只作数据解析，检索打分路径绝不执行其内容。

回归背景：_rank_feature_scores 曾用 eval() 解析存储的 tag_feas，恶意 chunk
可借检索打分执行任意代码。现约定：读侧 parse_tag_features 安全解析并把
结果严格过滤为 dict[str, 有限数值]；写侧 validate_tag_features 在入库前
拒绝非法负载（chunk_app / sdk doc 四个写点共用）。

恶意负载哨兵：`(_ for _ in ()).throw(SystemExit)` 一旦被 eval 立即抛
SystemExit 使测试当场爆炸——安全解析下它只是解析失败的普通字符串。
"""

import math
import types

import pytest

from common.constants import PAGERANK_FLD, TAG_FLD
from common.tag_feature_utils import parse_tag_features, validate_tag_features
from core.nlp.search import Dealer

_EVAL_BOMB = "(_ for _ in ()).throw(SystemExit)"


# ---- parse_tag_features（读侧安全解析） ----


def test_parse_executable_string_is_not_evaluated():
    assert parse_tag_features(_EVAL_BOMB, allow_json_string=True, allow_python_literal=True) == {}
    assert parse_tag_features("__import__('os').system('true')", allow_json_string=True, allow_python_literal=True) == {}


def test_parse_json_string():
    assert parse_tag_features('{"t1": 2, "t2": 0.5}') == {"t1": 2.0, "t2": 0.5}


def test_parse_python_literal_requires_opt_in():
    legacy = "{'t1': 3}"
    assert parse_tag_features(legacy) == {}
    assert parse_tag_features(legacy, allow_python_literal=True) == {"t1": 3.0}


def test_parse_filters_to_finite_numeric_values():
    parsed = parse_tag_features(
        {
            "ok": 1,
            "boolean": True,
            "inf": math.inf,
            "nan": math.nan,
            "text": "1",
            "": 2,
            "  ": 3,
            42: 4,
        }
    )
    assert parsed == {"ok": 1.0}


def test_parse_non_dict_inputs_yield_empty():
    assert parse_tag_features(None) == {}
    assert parse_tag_features("") == {}
    assert parse_tag_features("[1, 2]") == {}
    assert parse_tag_features(["t1"]) == {}


# ---- validate_tag_features（写侧拒绝） ----


def test_validate_passes_clean_payload_as_floats():
    assert validate_tag_features(None) is None
    assert validate_tag_features({"t1": 1, "t2": 0.5}) == {"t1": 1.0, "t2": 0.5}


@pytest.mark.parametrize(
    "payload",
    [
        _EVAL_BOMB,
        "not a dict",
        ["t1"],
        {"t1": "1"},
        {"t1": True},
        {"t1": math.inf},
        {"t1": math.nan},
        {"": 1},
        {42: 1},
    ],
)
def test_validate_rejects_invalid_payloads(payload):
    with pytest.raises(ValueError):
        validate_tag_features(payload)


# ---- _rank_feature_scores（打分路径不执行存储负载） ----


def _rank_scores(query_rfea, field_by_id):
    sres = Dealer.SearchResult(total=len(field_by_id), ids=list(field_by_id), field=field_by_id)
    # _rank_feature_scores 不触碰 self，无需构造完整 Dealer
    return Dealer._rank_feature_scores(types.SimpleNamespace(), query_rfea, sres)


def test_rank_scores_ignore_stored_eval_payload():
    scores = _rank_scores(
        {"t1": 1.0},
        {
            "malicious": {TAG_FLD: _EVAL_BOMB},
            "clean-json": {TAG_FLD: '{"t1": 1}'},
            "clean-legacy": {TAG_FLD: "{'t1': 1}"},
            "no-tag": {},
        },
    )
    malicious, clean_json, clean_legacy, no_tag = scores
    assert malicious == 0
    assert clean_json == pytest.approx(10.0)
    assert clean_legacy == pytest.approx(10.0)
    assert no_tag == 0


def test_rank_scores_fall_back_to_pagerank_only():
    scores = _rank_scores(
        {"t1": 1.0, PAGERANK_FLD: 5},
        {"malicious": {TAG_FLD: _EVAL_BOMB, PAGERANK_FLD: 2}},
    )
    assert scores == pytest.approx([2.0])

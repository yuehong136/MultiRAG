"""Paragraph queries must preserve the pretokenized words used by auto-tagging."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common.constants import TAG_FLD
from common.doc_store.doc_store_base import MatchTextExpr
from core.nlp import query, search
from core.nlp.query import FulltextQueryer


def _queryer() -> tuple[FulltextQueryer, Mock, Mock]:
    queryer = FulltextQueryer.__new__(FulltextQueryer)
    weights = Mock(side_effect=lambda tokens, *, preprocess: [(token, 1.0) for token in tokens])
    synonyms = Mock(return_value=[])
    queryer.tw = SimpleNamespace(weights=weights)
    queryer.syn = SimpleNamespace(lookup=synonyms)
    queryer.query_fields = ["content_ltks^2"]
    return queryer, weights, synonyms


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("machine learning model", ["machine", "learning", "model"]),
        (" \tmachine  learning\nmodel\r\n", ["machine", "learning", "model"]),
        ("机器 学习\t模型", ["机器", "学习", "模型"]),
        ("机器学习 model", ["机器学习", "model"]),
        ("machine\u2003learning\u00a0model", ["machine", "learning", "model"]),
        ("", []),
        (" \t\n\r ", []),
        (["machine", "learning model", "机器学习"], ["machine", "learning model", "机器学习"]),
        ([], []),
    ],
)
def test_pretokenized_words_reach_weights_and_synonyms(content: str | list[str], expected: list[str]) -> None:
    queryer, weights, synonyms = _queryer()
    before = content.copy() if isinstance(content, list) else content

    expression = queryer.paragraph(content)

    weights.assert_called_once_with(expected, preprocess=False)
    assert [call.args[0] for call in synonyms.call_args_list] == expected
    assert expression.fields == ["content_ltks^2"]
    assert expression.topn == 100
    assert expression.extra_options["original_query"] == ""
    assert expression.matching_text == " ".join(f'"{token}"^1.0' if " " in token else f"{token}^1.0" for token in expected)
    assert content == before


def test_weight_order_topn_synonyms_and_keywords_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    queryer, weights, synonyms = _queryer()
    weights.side_effect = None
    weights.return_value = [("machine", 0.1), ("learning", 0.7), ("model", 0.2)]
    synonyms.side_effect = lambda token: ["deep learning", ""] if token == "learning" else []
    fine_tokenize = Mock(side_effect=lambda text: text)
    monkeypatch.setattr(query.rag_tokenizer, "fine_grained_tokenize", fine_tokenize)
    keywords = ["  featured term  ", "机器学习"]

    expression = queryer.paragraph("machine learning model", keywords, keywords_topn=2)

    weights.assert_called_once_with(["machine", "learning", "model"], preprocess=False)
    assert [call.args[0] for call in synonyms.call_args_list] == ["learning", "model"]
    fine_tokenize.assert_called_once_with("deep learning")
    assert expression.matching_text == '"featured term" "机器学习" (learning OR ("deep learning")^0.2)^0.7 model^0.2'
    assert expression.extra_options == {"minimum_should_match": 0, "original_query": "  featured term   机器学习"}
    assert keywords == ["  featured term  ", "机器学习"]


def test_default_keywords_do_not_leak_between_calls() -> None:
    queryer, _weights, _synonyms = _queryer()
    assert queryer.paragraph("first").matching_text == "first^1.0"
    assert queryer.paragraph("second").matching_text == "second^1.0"
    assert queryer.paragraph("").matching_text == ""


def test_auto_tagging_sends_whole_title_and_content_tokens_to_store(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.services.knowledgebase_service import KnowledgebaseService

    queryer, weights, synonyms = _queryer()
    dealer = search.Dealer.__new__(search.Dealer)
    dealer.qryr = queryer
    store = SimpleNamespace(search=Mock(return_value={"controlled": "response"}), get_aggregation=Mock(return_value=[("machine.learning", 3)]))
    dealer.dataStore = store
    monkeypatch.setattr(search, "db_connection", lambda: nullcontext(object()))
    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", lambda _db, kb_ids: [SimpleNamespace(kb_name="tag_source")] if kb_ids == ["kb"] else [])
    keywords = ["featured"]
    doc = {"title_tks": "machine\tlearning", "content_ltks": "model\n机器 学习", "important_kwd": keywords}

    assert dealer.tag_content("tenant", ["kb"], doc, {"machine.learning": 0.0001}, keywords_topn=3) is True

    weights.assert_called_once_with(["machine", "learning", "model", "机器", "学习"], preprocess=False)
    assert [call.args[0] for call in synonyms.call_args_list] == ["machine", "learning", "model"]
    args = store.search.call_args.args
    expression = args[3][0]
    assert isinstance(expression, MatchTextExpr)
    assert expression.matching_text == '"featured" machine^1.0 learning^1.0 model^1.0'
    assert args[7:] == (["multirag_tenant_tag_source"], ["kb"], ["tag_kwd"])
    store.get_aggregation.assert_called_once_with({"controlled": "response"}, "tag_kwd")
    assert doc[TAG_FLD] == {"machine_learning": 4.0}
    assert doc["important_kwd"] is keywords and keywords == ["featured"]

"""Language decisions retain local filtering and the strict 80% boundary."""

import pytest

from core.nlp import is_english
from core.nlp.search import Dealer


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        ("This is English", True),
        (["The quick brown fox jumps.", "Hello world today."], True),
        (["This is a normal English answer."], True),
        ("Hello 世界", False),
        (["这是中文段落。", "另一个中文段落。"], False),
        (["one", "two", "three", "four", "中文"], False),
        (["one", "two", "three", "four", "five", "中文"], True),
        (["hello中文"], False),
        ([None, 42, " ", "hello world"], True),
        ([None, 42, " "], False),
        ([], False),
        ("", False),
        (" \t\n", False),
        ([" \t\n"], False),
        (None, False),
        (123, False),
        (("hello",), False),
        ({"hello": "world"}, False),
    ],
)
def test_language_decision(texts: object, expected: bool) -> None:
    assert is_english(texts) is expected


def test_english_highlight_uses_source_sentences() -> None:
    dealer = Dealer.__new__(Dealer)
    result = ([{"entity": {"doc_id": "doc", "highlight": {"body": ["Hello world"]}}, "_source": {"body": "Hello world today. Other sentence."}}],)
    assert dealer.get_highlight(result, ["world"], "body") == {"doc": "Hello <em>world</em> today"}

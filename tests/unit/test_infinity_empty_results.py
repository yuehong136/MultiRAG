import pandas as pd

from common.doc_store.infinity_conn_base import InfinityConnectionBase


def test_get_doc_ids_returns_empty_for_unparsed_document_results() -> None:
    empty_result = (pd.DataFrame(), 0)

    assert InfinityConnectionBase.get_doc_ids(None, empty_result) == []
    assert InfinityConnectionBase.get_doc_ids(None, pd.DataFrame()) == []


def test_get_highlight_returns_empty_for_unparsed_document_results() -> None:
    empty_result = (pd.DataFrame(), 0)

    assert InfinityConnectionBase.get_highlight(None, empty_result, ["term"], "content_with_weight") == {}


def test_get_highlight_uses_dataframe_from_tuple_and_content_fallback() -> None:
    result = (
        pd.DataFrame(
            {
                "id": ["chunk-1"],
                "content": ["A matching term appears here."],
            }
        ),
        1,
    )

    assert InfinityConnectionBase.get_highlight(None, result, ["term"], "content_with_weight") == {"chunk-1": "A matching <em>term</em> appears here"}

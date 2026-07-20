from types import SimpleNamespace

from common.doc_store.infinity_conn_base import InfinityConnectionBase


def test_equivalent_condition_escapes_single_quotes_in_string_filters() -> None:
    connection = SimpleNamespace(
        field_keyword=lambda field: field == "source_id",
        convert_matching_field=lambda field: field,
    )

    condition = InfinityConnectionBase.equivalent_condition_to_str(
        connection,
        {
            "source_id": "O'Reilly",
            "entity_name": "投影直线L'",
        },
    )

    assert condition == "filter_fulltext('source_id', 'O''Reilly') AND entity_name='投影直线L'''"

"""Stable SDK LIKE parsing against a real, isolated Infinity database."""

import os
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import infinity
import pytest
from infinity.common import ConflictType, NetworkAddress

from common.config_utils import CONFIGS


@pytest.fixture(scope="module")
def stable_table() -> Iterator[Any]:
    host, port = os.environ.get("INFINITY_TEST_URI", CONFIGS["infinity"]["uri"]).rsplit(":", 1)
    address = NetworkAddress(host, int(port))
    writer = infinity.connect(address)
    reader = None
    name = "stable_like_" + uuid4().hex
    try:
        reader = infinity.connect(address)
        database = writer.create_database(name, ConflictType.Error)
        table = database.create_table("patterns", {"id": {"type": "varchar"}, "label": {"type": "varchar"}})
        table.insert(
            [
                {"id": "underscore", "label": "release_alpha"},
                {"id": "percent", "label": "release%beta"},
                {"id": "ordinary", "label": "releasedelta"},
                {"id": "other", "label": "friend"},
            ]
        )
        observed, _ = reader.get_database(name).get_table("patterns").output(["id"]).to_df()
        assert set(observed["id"]) == {"underscore", "percent", "ordinary", "other"}
        yield reader.get_database(name).get_table("patterns")
    finally:
        try:
            writer.get_database("default_db")
            if reader is not None:
                reader.get_database("default_db")
            writer.drop_database(name, ConflictType.Ignore)
            if reader is not None:
                assert name not in reader.list_databases().db_names
        finally:
            if reader is not None:
                reader.disconnect()
            writer.disconnect()


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("label like 'release_%'", {"underscore", "percent", "ordinary"}),
        ("label not like 'release%'", {"other"}),
        (r"label like 'release\_%'", {"underscore"}),
        ("label like 'release!_%' escape '!'", {"underscore"}),
        ("label not like 'release!_%' escape '!'", {"percent", "ordinary", "other"}),
        ("label like 'release!%%' escape '!'", {"percent"}),
        ("label not like 'release!%%' escape '!'", {"underscore", "ordinary", "other"}),
    ],
)
def test_stable_like_escape_and_negation_with_sdk_readback(stable_table: Any, condition: str, expected: set[str]) -> None:
    result, _ = stable_table.output(["id"]).filter(condition).to_df()
    assert set(result["id"]) == expected

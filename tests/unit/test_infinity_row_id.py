import sys
from types import ModuleType

import pandas as pd


def _stub_settings_module():
    settings_stub = ModuleType("common.settings")
    settings_stub.INFINITY = {"db_name": "default_db", "uri": "127.0.0.1:23817"}
    sys.modules.setdefault("common.settings", settings_stub)


def _get_wrapped_singleton_class(singleton_factory):
    return next(cell.cell_contents for cell in singleton_factory.__closure__ if isinstance(cell.cell_contents, type))


def test_get_fields_maps_infinity_row_id_to_requested_row_id_function():
    _stub_settings_module()
    from core.utils.infinity_conn import InfinityConnection

    infinity_connection_class = _get_wrapped_singleton_class(InfinityConnection)
    conn = object.__new__(infinity_connection_class)
    res = pd.DataFrame(
        [
            {
                "id": "chunk-1",
                "row_id": 42,
                "content": "alpha",
            }
        ]
    )

    fields = conn.get_fields(res, ["row_id()", "content_with_weight"])

    assert fields == {
        "chunk-1": {
            "row_id()": 42,
            "content_with_weight": "alpha",
        }
    }

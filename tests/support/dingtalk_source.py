"""Controlled SDK boundary; production pagination and document conversion run intact."""

from types import SimpleNamespace
from typing import Any


class DingTalkSource:
    def __init__(self, mode: str = "complete") -> None:
        self.mode = mode
        self.calls: list[tuple[str, str]] = []

    def get_all_sheets_with_options(self, *args: Any) -> Any:
        if self.mode == "sheets-error":
            raise PermissionError("sheets denied")
        return SimpleNamespace(body=SimpleNamespace(value=[] if self.mode == "empty" else [SimpleNamespace(id="sheet", name="Sheet"), SimpleNamespace(id="other", name="Other")]))

    def list_records_with_options(self, table: str, sheet: str, request: Any, *args: Any) -> Any:
        self.calls.append((sheet, request.next_token))
        if sheet == "other":
            if self.mode == "listing-error":
                raise PermissionError("second sheet denied")
            return SimpleNamespace(body=SimpleNamespace(records=[SimpleNamespace(id="retained", fields={})] if self.mode == "multi" else [], next_token=None))
        if self.mode == "malformed":
            return SimpleNamespace(body=None)
        if request.next_token and self.mode == "page-error":
            raise PermissionError("second page denied")
        records = [SimpleNamespace(id="retained", fields={"name": "retained"})] if not request.next_token else []
        if self.mode == "invalid-id":
            records[0].id = None
        if self.mode == "invalid-fields":
            records[0].fields = None
        token = "page-2" if not request.next_token or self.mode == "cycle" else None
        return SimpleNamespace(body=SimpleNamespace(records=records, next_token=token))

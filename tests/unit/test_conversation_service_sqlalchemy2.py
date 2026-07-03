from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from api.db.services.conversation_service import ConversationService


class ScalarResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class ScalarOnlySession(Session):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.statements = []

    def scalars(self, stmt):
        self.statements.append(stmt)
        return ScalarResult(self.rows)


class ConversationRow(SimpleNamespace):
    def to_dict(self):
        return dict(self.__dict__)


def test_get_list_uses_sqlalchemy_2_select_scalars_without_legacy_query():
    db = ScalarOnlySession(
        [
            ConversationRow(
                id="session-1",
                dialog_id="chat-1",
                name="Demo",
                message=[],
                reference=[],
                user_id="user-1",
                create_time=1,
                create_date=None,
                update_time=2,
                update_date=None,
            )
        ]
    )

    rows = ConversationService.get_list(
        db,
        dialog_id="chat-1",
        page_number=2,
        items_per_page=25,
        orderby="create_time",
        is_desc=True,
        name="Demo",
        user_id="user-1",
    )

    assert rows == [
        {
            "id": "session-1",
            "dialog_id": "chat-1",
            "name": "Demo",
            "message": [],
            "reference": [],
            "user_id": "user-1",
            "create_time": 1,
            "create_date": None,
            "update_time": 2,
            "update_date": None,
        }
    ]
    assert len(db.statements) == 1
    assert db.statements[0]._limit_clause is not None
    assert db.statements[0]._offset_clause is not None


def test_get_list_page_size_zero_disables_pagination():
    db = ScalarOnlySession([])

    ConversationService.get_list(
        db,
        dialog_id="chat-1",
        page_number=1,
        items_per_page=0,
        orderby="create_time",
        is_desc=True,
    )

    assert len(db.statements) == 1
    assert db.statements[0]._limit_clause is None
    assert db.statements[0]._offset_clause is None


def test_get_list_rejects_unknown_order_field():
    db = ScalarOnlySession([])

    with pytest.raises(ValueError, match="is not a valid attribute"):
        ConversationService.get_list(
            db,
            dialog_id="chat-1",
            page_number=1,
            items_per_page=30,
            orderby="not_a_column",
            is_desc=True,
        )

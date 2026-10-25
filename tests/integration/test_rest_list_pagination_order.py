"""Real SQL verifies total ordering of the REST consumers' tied-key pages."""

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import API4Conversation, Conversation, Dialog, Knowledgebase, MCPServer, Memory, Search, Tenant, User, UserCanvas, UserTenant
from api.db.services.api_service import API4ConversationService
from api.db.services.canvas_service import UserCanvasService
from api.db.services.conversation_service import ConversationService
from api.db.services.dialog_service import DialogService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.mcp_server_service import MCPServerService
from api.db.services.memory_service import MemoryService
from api.db.services.search_service import SearchService
from tests.support.database import scratch_database
from tests.support.resource_journal import ResourceJournal

METHODS = ["datasets", "dataset_tenant", "dataset_offset", "chats", "agents", "searches", "mcp", "chat_sessions", "agent_sessions"]
MODELS = [Knowledgebase, Dialog, UserCanvas, Search, MCPServer, Memory, Conversation, API4Conversation]


@pytest.fixture(scope="module")
def pagination_rows(tmp_path_factory: pytest.TempPathFactory, postgres_service: None) -> Iterator[dict[str, Any]]:
    journal = ResourceJournal("rest-list-pagination-tied-keys-fixture")
    owner, outsider = journal.run_id, journal.run_id[::-1]
    membership = "a" + journal.run_id[1:]
    rows = [{"id": journal.run_id[:24] + f"{index:08x}", "name": "keep-alpha" if index % 2 == 0 else "keep-beta", "create_time": 1700000000000, "update_time": 1700000000000} for index in range(205)]
    peer = {**rows[0], "id": journal.run_id[:24] + f"{205:08x}"}
    parent, other_parent = rows[0]["id"], peer["id"]
    by_model: dict[Any, list[dict[str, Any]]] = {
        Knowledgebase: [
            {**row, "tenant_id": owner, "created_by": owner, "parser_id": "naive" if index % 3 == 0 else "manual", "embd_id": "scratch", "permission": "me", "status": "1"}
            for index, row in enumerate(rows)
        ],
        Dialog: [{**row, "tenant_id": owner, "llm_id": "scratch"} for row in rows],
        UserCanvas: [
            {
                "id": row["id"],
                "title": row["name"],
                "user_id": owner,
                "permission": "me",
                "canvas_category": "agent_canvas",
                "tags": "alpha" if index % 2 == 0 else "beta",
                "create_time": row["create_time"],
                "update_time": row["update_time"],
            }
            for index, row in enumerate(rows)
        ],
        Search: [{**row, "tenant_id": owner, "created_by": owner} for row in rows],
        MCPServer: [
            {**row, "tenant_id": owner, "url": "https://example.invalid/mcp", "server_type": "streamable-http", "create_time": row["create_time"] + index % 3} for index, row in enumerate(rows)
        ],
        Memory: [
            {
                **row,
                "tenant_id": owner,
                "embd_id": "scratch",
                "llm_id": "scratch",
                "permissions": "me",
                "memory_type": 1 if index % 3 == 0 else 2,
                "storage_type": "table" if index % 2 == 0 else "graph",
            }
            for index, row in enumerate(rows)
        ],
        Conversation: [{**row, "dialog_id": parent, "user_id": owner if index % 2 == 0 else outsider, "message": [row["name"]]} for index, row in enumerate(rows)],
        API4Conversation: [
            {
                **row,
                "dialog_id": parent,
                "user_id": owner if index % 2 == 0 else outsider,
                "message": [row["name"]],
                "exp_user_id": "exp-alpha" if index % 2 == 0 else "exp-beta",
                "dsl": {"kept": True},
            }
            for index, row in enumerate(rows)
        ],
    }
    for model, records in by_model.items():
        foreign = {**records[0], "id": peer["id"]}
        if model is UserCanvas:
            foreign["user_id"] = outsider
        elif model in {Conversation, API4Conversation}:
            foreign["dialog_id"] = other_parent
        else:
            foreign["tenant_id"] = outsider
            if "created_by" in foreign:
                foreign["created_by"] = outsider
        records.append(foreign)
    by_model[Knowledgebase].extend(
        [{**by_model[Knowledgebase][0], "id": journal.run_id[:24] + f"{206 + index:08x}", **change} for index, change in enumerate([{"status": "0"}, {"permission": "unknown"}])]
    )
    with scratch_database(tmp_path_factory.mktemp("rest-list-pagination")) as engine:
        journal.event(
            "reserve-sql",
            database=engine.url.database,
            users=[owner, outsider],
            tenants=[owner, outsider],
            memberships=[membership],
            models={model.__tablename__: [row["id"] for row in records] for model, records in by_model.items()},
        )
        with engine.begin() as db:
            db.execute(sa.insert(User), [{"id": user, "email": user + "@pagination.test", "nickname": "pagination", "password": "unused"} for user in (owner, outsider)])
            db.execute(sa.insert(Tenant), [{"id": user, "name": "pagination", "llm_id": "", "embd_id": "", "asr_id": "", "img2txt_id": "", "parser_ids": "naive"} for user in (owner, outsider)])
            db.execute(sa.insert(UserTenant), {"id": membership, "tenant_id": owner, "user_id": owner, "role": "owner", "invited_by": owner})
            for model, records in by_model.items():
                # All205 update_time values remain identical; scramble insertion
                # order so its incidental SQL order cannot satisfy the contract.
                db.execute(sa.insert(model), [records[(index * 73) % 205] for index in range(205)] + records[205:])
        with engine.connect() as independent:
            for model in MODELS:
                assert independent.scalar(sa.select(sa.func.count(sa.distinct(model.update_time))).where(model.id.in_([row["id"] for row in rows]))) == 1
            before = {model.__tablename__: list(independent.execute(sa.select(model.__table__).order_by(model.id))) for model in MODELS}
        yield {"engine": engine, "owner": owner, "outsider": outsider, "rows": rows, "by_model": by_model, "parent": parent, "other_parent": other_parent, "journal": journal}
        with engine.connect() as independent:
            after = {model.__tablename__: list(independent.execute(sa.select(model.__table__).order_by(model.id))) for model in MODELS}
        assert after == before
        journal.event("assertions", all_eight_sql_domains_unchanged=True, identical_update_timestamps_preserved=True)


def fetch(env: dict[str, Any], method: str, page: int, orderby: str, descending: bool, **filters: Any) -> tuple[list[dict[str, Any]], int | None]:
    owner = env["owner"]
    with Session(env["engine"]) as db:
        if method == "datasets":
            return KnowledgebaseService.get_list(db, [owner], owner, page, 100, orderby, descending, filters.get("id"), filters.get("name"), filters.get("keywords"), filters.get("parser_id"))
        if method == "dataset_tenant":
            return KnowledgebaseService.get_by_tenant_ids(db, [owner], owner, page, 100, orderby, descending, filters.get("keywords"), filters.get("parser_id"))
        if method == "dataset_offset":
            return KnowledgebaseService.get_by_tenant_ids_by_offset(db, [owner], owner, (page - 1) * 100, 100, orderby, descending), None
        if method == "chats":
            return DialogService.get_by_tenant_ids(
                db, [owner], owner, page, 100, orderby, descending, keywords=filters.get("keywords"), id=filters.get("id"), name=filters.get("name"), owner_ids_only=True
            )
        if method == "agents":
            return UserCanvasService.get_by_tenant_ids(
                db, [owner], owner, page, 100, "title" if orderby == "name" else orderby, descending, filters.get("keywords"), filters.get("canvas_category"), filters.get("tags")
            )
        if method == "searches":
            return SearchService.get_by_tenant_ids(db, [owner], owner, page, 100, orderby, descending, filters.get("keywords"), owner_ids_only=True)
        if method == "mcp":
            # The active REST handler fetches all SQL rows and then slices them;
            # also verify its native LIMIT path agrees with those same pages.
            all_rows = MCPServerService.get_servers(db, owner, filters.get("ids"), 0, 0, orderby, descending, filters.get("keywords")) or []
            native = MCPServerService.get_servers(db, owner, filters.get("ids"), page, 100, orderby, descending, filters.get("keywords")) or []
            sliced = all_rows[(page - 1) * 100 : page * 100]
            assert native == sliced
            return sliced, len(all_rows)
        if method == "chat_sessions":
            return ConversationService.get_list(db, env["parent"], page, 100, orderby, descending, filters.get("id"), filters.get("name"), filters.get("user_id")), None
        if method == "agent_sessions":
            total, rows = API4ConversationService.get_list(
                db,
                env["parent"],
                owner,
                page,
                100,
                orderby,
                descending,
                id=filters.get("id"),
                user_id=filters.get("user_id"),
                include_dsl=filters.get("include_dsl", True),
                keywords=filters.get("keywords", ""),
                exp_user_id=filters.get("exp_user_id"),
            )
            return rows, total
        raise AssertionError(method)


def expected_ids(env: dict[str, Any], method: str, orderby: str, descending: bool) -> list[str]:
    if method == "mcp":
        rows = sorted(env["by_model"][MCPServer][:205], key=lambda row: (row[orderby], row["id"]), reverse=descending)
        # Preserve MCP's preexisting create_time DESC before requested order.
        rows = sorted(rows, key=lambda row: row["create_time"], reverse=True)
    else:
        rows = sorted(env["rows"], key=lambda row: (row[orderby], row["id"]), reverse=descending)
    return [row["id"] for row in rows]


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("orderby", ["update_time", "create_time", "name"])
@pytest.mark.parametrize("descending", [False, True], ids=["asc", "desc"])
def test_pages_with_tied_primary_keys(pagination_rows: dict[str, Any], method: str, orderby: str, descending: bool) -> None:
    env = pagination_rows
    expected = expected_ids(env, method, orderby, descending)
    pages = [fetch(env, method, page, orderby, descending) for page in (1, 2, 3)]
    assert [len(rows) for rows, _ in pages] == [100, 100, 5]
    assert all(total in (None, 205) for _, total in pages)
    ids = [row["id"] for rows, _ in pages for row in rows]
    assert len(set(ids)) == 205 and ids == expected
    assert {row["id"] for row in pages[0][0]}.isdisjoint(row["id"] for row in pages[1][0])
    assert [row["id"] for row in fetch(env, method, 2, orderby, descending)[0]] == expected[100:200]
    if method == "dataset_offset":
        with pytest.raises(IndexError, match="Offset is out"):
            fetch(env, method, 4, orderby, descending)
    else:
        empty, total = fetch(env, method, 4, orderby, descending)
        assert empty == [] and total in (None, 205)
    env["journal"].event("page-assertions", method=method, orderby=orderby, descending=descending, page_lengths=[100, 100, 5], exact_sorted_ids=True, distinct_ids=205)


@pytest.mark.parametrize("method", ["datasets", "dataset_tenant", "chats", "agents", "searches", "mcp", "chat_sessions", "agent_sessions"])
@pytest.mark.parametrize("descending", [False, True])
def test_pagination_keeps_original_filters(pagination_rows: dict[str, Any], method: str, descending: bool) -> None:
    env = pagination_rows
    filters: dict[str, Any] = {"keywords": "keep-alpha"}
    if method == "chat_sessions":
        filters = {"name": "keep-alpha", "user_id": env["owner"]}
    if method == "agent_sessions":
        filters.update(user_id=env["owner"], exp_user_id="exp-alpha", include_dsl=False)
    if method in {"datasets", "dataset_tenant"}:
        filters["parser_id"] = "naive"
    if method == "agents":
        filters.update(canvas_category="agent_canvas", tags="alpha")
    rows, total = fetch(env, method, 1, "update_time", descending, **filters)
    expected = [
        identifier
        for identifier in expected_ids(env, method, "update_time", descending)
        if int(identifier[-8:], 16) % 2 == 0 and (method not in {"datasets", "dataset_tenant"} or int(identifier[-8:], 16) % 3 == 0)
    ]
    assert [row["id"] for row in rows] == expected[:100]
    assert total is None or total == len(expected)
    if method == "agent_sessions":
        assert all("dsl" not in row for row in rows)
    if method in {"datasets", "chats", "chat_sessions", "agent_sessions"}:
        selected = env["rows"][0]
        rows, total = fetch(env, method, 1, "update_time", descending, id=selected["id"], name=selected["name"])
        assert [row["id"] for row in rows] == [selected["id"]] and total in (None, 1)
    if method == "mcp":
        selected = env["rows"][0]
        rows, total = fetch(env, method, 1, "update_time", descending, ids=[selected["id"]])
        assert total == 1 and [row["id"] for row in rows] == [selected["id"]]


def test_memory_fixed_descending_order_and_narrowing_filters(pagination_rows: dict[str, Any]) -> None:
    env = pagination_rows
    with Session(env["engine"]) as db:
        pages = [MemoryService.get_by_filter(db, {}, page=page, page_size=100, user_id=env["owner"]) for page in (1, 2, 3, 4)]
        assert [len(rows) for rows, _ in pages] == [100, 100, 5, 0]
        expected = sorted((row["id"] for row in env["rows"]), reverse=True)
        assert [row["id"] for rows, _ in pages for row in rows] == expected
        assert all(total == 205 for _, total in pages)
        filtered, total = MemoryService.get_by_filter(db, {"storage_type": "table", "memory_type": ["raw"]}, keywords="keep-alpha", user_id=env["owner"])
        assert total == 35 and {row["id"] for row in filtered} == {row["id"] for index, row in enumerate(env["rows"]) if index % 6 == 0}
        assert MemoryService.get_by_filter(db, {"tenant_id": []}, user_id=env["owner"]) == ([], 0)
        assert MemoryService.get_by_filter(db, {"tenant_id": [env["owner"]]}, user_id=env["outsider"]) == ([], 0)
    env["journal"].event("page-assertions", method="memory", fixed_descending=True, distinct_ids=205, no_asc_parameter_in_existing_contract=True)


def test_lists_preserve_tenant_and_parent_namespaces(pagination_rows: dict[str, Any]) -> None:
    env = pagination_rows
    owned = {row["id"] for row in env["rows"]}
    outsider = {**env, "owner": env["outsider"]}
    for method in ["datasets", "dataset_tenant", "chats", "agents", "searches", "mcp"]:
        rows, _ = fetch(outsider, method, 1, "update_time", True)
        assert owned.isdisjoint(row["id"] for row in rows)
    with pytest.raises(IndexError, match="Offset is out"):
        fetch(outsider, "dataset_offset", 1, "update_time", True)
    for method in ["chat_sessions", "agent_sessions"]:
        peer = {**env, "parent": env["other_parent"]}
        rows, total = fetch(peer, method, 1, "update_time", True)
        assert len(rows) == 1 and owned.isdisjoint(row["id"] for row in rows) and total in (None, 1)
        missing = {**env, "parent": "missing-parent"}
        rows, total = fetch(missing, method, 1, "update_time", True)
        assert rows == [] and total in (None, 0)
    env["journal"].event("assertions", owner_scopes_preserved=True, both_session_parent_filters_preserved=True)

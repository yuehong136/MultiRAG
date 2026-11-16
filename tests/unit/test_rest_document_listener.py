"""The REST fixture holds a kernel-selected socket despite busy old candidates."""

import ast
import socket
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from tests.support.resource_journal import ResourceJournal


def test_rest_fixture_holds_socket_when_old_candidate_is_occupied() -> None:
    path = Path(__file__).parents[1] / "support" / "rest_document_contract.py"
    fixture = next(node for node in ast.parse(path.read_text()).body if isinstance(node, ast.FunctionDef) and node.name == "rest_documents")
    block = next(node for node in fixture.body if isinstance(node, ast.With))
    start = next(i for i, node in enumerate(block.body) if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "listener")
    end = next(i for i, node in enumerate(block.body[start:], start) if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "server")
    # Execute the real fixture's socket reservation statements; importing the
    # fixture itself would initialize unrelated application/storage resources.
    setup = compile(ast.Module(body=block.body[start:end], type_ignores=[]), str(path), "exec")
    journal = ResourceJournal("rest-listener-busy-candidate")
    journal.event("reserve-replacement-listener", host="127.0.0.1", port=0)
    with ExitStack() as cleanup, socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        occupied.settimeout(1)
        busy_port = occupied.getsockname()[1]
        journal.event("listener-bound", host="127.0.0.1", port=busy_port)
        journal.event("reserve-fixture-listener", host="127.0.0.1", port=0)
        namespace: dict[str, Any] = {"socket": socket, "cleanup": cleanup, "port": busy_port, "journal": journal}
        exec(setup, namespace)
        listener = namespace["listener"]
        assert namespace["port"] == listener.getsockname()[1] != busy_port
        listener.listen()
        listener.settimeout(1)
        for endpoint, message in [(listener, b"fixture"), (occupied, b"other-owner")]:
            with socket.create_connection(endpoint.getsockname(), timeout=1) as client, endpoint.accept()[0] as accepted:
                client.sendall(message)
                accepted.settimeout(1)
                assert accepted.recv(32) == message
        assert occupied.fileno() != -1
    assert listener.fileno() == occupied.fileno() == -1
    journal.event("listener-unit-cleanup", fixture_closed=True, replacement_closed_by_owner=True)

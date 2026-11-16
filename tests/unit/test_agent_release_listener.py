"""Exercise the fixture's actual teardown body without booting the API/SQL stack."""

import ast
import socket
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.support.resource_journal import ResourceJournal


def cleanup_callback(listener: socket.socket, report: dict[str, Any], journal: ResourceJournal, socket_factory: Callable[[], socket.socket] = socket.socket) -> Callable[[], None]:
    # Compile the original nested function, rather than copy its implementation
    # or import the integration fixture's application/resource dependencies.
    path = Path(__file__).parents[1] / "support" / "agent_update_release.py"
    fixture = next(node for node in ast.parse(path.read_text()).body if isinstance(node, ast.FunctionDef) and node.name == "release_api")
    function = next(node for node in ast.walk(fixture) if isinstance(node, ast.FunctionDef) and node.name == "close_listener")
    namespace: dict[str, Any] = {"listener": listener, "report": report, "journal": journal, "socket": SimpleNamespace(socket=socket_factory), "ExitStack": ExitStack}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["close_listener"]


def test_release_listener_closes_original_socket() -> None:
    journal = ResourceJournal("release-listener-close")
    journal.event("reserve-listener", host="127.0.0.1", port=0)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        journal.event("listener-bound", host="127.0.0.1", port=port)
        report: dict[str, Any] = {"port": port, "cleanup": {}}
        cleanup_callback(listener, report, journal)()
        assert listener.fileno() == -1
        assert report["cleanup"]["listener_absent"] is True
        assert report["cleanup"]["listener_probe_source"][1] != port


def test_release_listener_excludes_self_connection_source() -> None:
    journal = ResourceJournal("release-listener-source-collision")
    journal.event("reserve-listener", host="127.0.0.1", port=0)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        journal.event("listener-bound", host="127.0.0.1", port=port)
        listener.close()
        probes: list[socket.socket] = []

        class ProbeSocket(socket.socket):
            def bind(self, address: Any) -> None:
                # Deterministically model bind(0) choosing the former endpoint.
                super().bind(("127.0.0.1", port) if len(probes) == 1 else address)

        def create_probe() -> socket.socket:
            probe = ProbeSocket()
            probes.append(probe)
            return probe

        report: dict[str, Any] = {"port": port, "cleanup": {}}
        cleanup_callback(listener, report, journal, create_probe)()
        assert len(probes) == 2
        assert all(probe.fileno() == -1 for probe in probes)
        assert report["cleanup"]["listener_probe_source"][1] != port
        assert report["cleanup"]["listener_absent"] is True


def test_release_listener_rejects_reused_endpoint_without_closing_it() -> None:
    journal = ResourceJournal("release-listener-reused-endpoint")
    journal.event("reserve-listener", host="127.0.0.1", port=0)
    with socket.socket() as original, socket.socket() as replacement:
        original.bind(("127.0.0.1", 0))
        port = original.getsockname()[1]
        journal.event("listener-bound", host="127.0.0.1", port=port)
        original.close()
        journal.event("reserve-replacement-listener", host="127.0.0.1", port=port)
        replacement.bind(("127.0.0.1", port))
        replacement.listen()
        replacement.settimeout(1)
        report: dict[str, Any] = {"port": port, "cleanup": {}}
        with pytest.raises(AssertionError, match="destination"):
            cleanup_callback(original, report, journal)()
        assert "listener_absent" not in report["cleanup"]
        assert replacement.fileno() != -1
        # The failed probe actually reached the replacement, which remains ours
        # to close; the teardown must never kill a different endpoint's owner.
        with replacement.accept()[0] as accepted:
            assert accepted.getpeername()[1] != port
    journal.event("replacement-listener-cleanup-readback", original_socket_closed=original.fileno() == -1, replacement_socket_closed=replacement.fileno() == -1)

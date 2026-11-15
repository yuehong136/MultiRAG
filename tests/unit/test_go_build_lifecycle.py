"""The Go launcher starts both built binaries and reaps its own sibling."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.support.resource_journal import ResourceJournal


@pytest.mark.parametrize("first", ["admin", "api"])
def test_go_launcher_returns_child_failure_and_stops_sibling(tmp_path: Path, first: str) -> None:
    journal = ResourceJournal("go-build-launcher")
    binaries: dict[str, Path] = {}
    for name in ("admin", "api"):
        path = tmp_path / name
        binaries[name] = path
        journal.event("reserve-file", path=str(path), purpose="controlled Go launcher child")
        sibling = tmp_path / (("api" if name == "admin" else "admin") + ".pid")
        action = f"while [ ! -f {shlex.quote(str(sibling))} ]; do sleep 0.05; done\nexit 7\n" if name == first else "trap 'exit 0' TERM\nwhile true; do sleep 0.1; done\n"
        path.write_text(f"#!/bin/bash\necho $$ > {shlex.quote(str(tmp_path / (name + '.pid')))}\n" + action)
        path.chmod(0o700)
    build = Path(__file__).resolve().parents[2] / "build.sh"
    command = [
        "bash",
        "-c",
        f"source {shlex.quote(str(build))} --help\nADMIN_SERVER_BINARY={shlex.quote(str(binaries['admin']))}\nMULTIRAG_SERVER_BINARY={shlex.quote(str(binaries['api']))}\nrun",
    ]
    journal.event("reserve-process", command=command, cwd=str(tmp_path), group="inherited registered runner group")
    process = subprocess.Popen(command, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    journal.event("spawned", pid=process.pid, pgid=os.getpgid(process.pid))
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 7, (stdout, stderr)
    for name in ("admin", "api"):
        pid = int((tmp_path / (name + ".pid")).read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        journal.event("process-cleanup-readback", pid=pid, absent=True)


@pytest.mark.parametrize("platform,library", [("Darwin", "liboffice_oxide.a"), ("Linux", "liboffice_oxide.so")])
def test_office_build_uses_prepared_platform_library(tmp_path: Path, platform: str, library: str) -> None:
    """A flag/config check; native parser execution is covered separately."""
    prefix = tmp_path / "office"
    (prefix / "include/office_oxide_c").mkdir(parents=True)
    (prefix / "lib").mkdir()
    (prefix / "include/office_oxide_c/office_oxide.h").touch()
    (prefix / "lib" / library).touch()
    archive = tmp_path / "native/librag_tokenizer_c_api.a"
    archive.parent.mkdir()
    archive.touch()
    commands = tmp_path / "go-commands.txt"
    build = Path(__file__).resolve().parents[2] / "build.sh"
    setup = [
        f"source {shlex.quote(str(build))} --help",
        f"uname() {{ echo {platform}; }}",
        "check_pcre2() { return 0; }",
        f'go() {{ printf "%s\\n" "$*" >> {shlex.quote(str(commands))}; touch "$3"; }}',
        f"PROJECT_ROOT={shlex.quote(str(tmp_path))}",
        f"BUILD_DIR={shlex.quote(str(archive.parent))}",
        f"OFFICE_OXIDE_PREFIX={shlex.quote(str(prefix))}",
    ]
    for variable, name in (
        ("MULTIRAG_SERVER_BINARY", "multirag_server"),
        ("ADMIN_SERVER_BINARY", "admin_server"),
        ("MULTIRAG_CLI_BINARY", "multirag_cli"),
        ("INGESTOR_BINARY", "ingestor"),
    ):
        setup.append(f"{variable}={shlex.quote(str(tmp_path / name))}")
    command = [
        "bash",
        "-c",
        "\n".join([*setup, "build_go"]),
    ]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=True, timeout=5)
    assert f"-I{prefix}/include/office_oxide_c" in result.stdout
    if platform == "Darwin":
        assert str(prefix / "lib" / library) in result.stdout
        assert "-framework Security -framework CoreFoundation -framework SystemConfiguration" in result.stdout
        assert "-loffice_oxide" not in result.stdout
    else:
        assert f"-L{prefix}/lib -loffice_oxide -Wl,-rpath,{prefix}/lib" in result.stdout
    assert [line.rsplit(" ", 1)[-1] for line in commands.read_text().splitlines()] == ["cmd/server_main.go", "cmd/admin_server.go", "cmd/multirag_cli.go", "cmd/ingestor.go"]

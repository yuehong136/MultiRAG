"""Exercise actual integration guards across pytest function/module/session scopes."""

import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path


def test_lazy_settings_undo_preserves_higher_scope_overrides(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent("""\
            import pytest
            from collections.abc import Iterator
            from common import settings
            from tests.integration.conftest import _defrost_settings_facade as _defrost_settings_facade
            from tests.integration.conftest import _defrost_settings_facade_module as _defrost_settings_facade_module

            @pytest.fixture(scope='session', autouse=True)
            def session_override() -> Iterator[object]:
                value = object()
                with pytest.MonkeyPatch.context() as patch:
                    patch.setattr(settings, 'REGISTER_ENABLED', value)
                    yield value
            """),
    )
    (tmp_path / "test_1_module.py").write_text(
        textwrap.dedent("""\
            import pytest
            from collections.abc import Iterator
            from common import resources, settings

            MODULE_OVERRIDE = object()
            @pytest.fixture(scope='module', autouse=True)
            def module_override() -> Iterator[None]:
                with pytest.MonkeyPatch.context() as patch:
                    patch.setattr(settings, 'DOC_ENGINE', MODULE_OVERRIDE)
                    yield

            def test_first_function_creates_real_undo_shadow(monkeypatch: pytest.MonkeyPatch, session_override: object) -> None:
                assert settings.DOC_ENGINE is MODULE_OVERRIDE
                assert settings.REGISTER_ENABLED is session_override
                original, temporary = object(), object()
                resources._state['storage'] = original
                monkeypatch.setattr(settings, 'STORAGE_IMPL', temporary)
                monkeypatch.undo()
                assert vars(settings)['STORAGE_IMPL'] is original

            def test_next_case_reads_new_resource_and_keeps_module_override(session_override: object) -> None:
                assert 'STORAGE_IMPL' not in vars(settings)
                replacement = object()
                resources._state['storage'] = replacement
                assert settings.STORAGE_IMPL is replacement
                assert settings.DOC_ENGINE is MODULE_OVERRIDE
                assert settings.REGISTER_ENABLED is session_override
            """),
    )
    (tmp_path / "test_2_module.py").write_text(
        textwrap.dedent("""\
            import pytest
            from common import resources, settings

            def test_module_undo_shadow_does_not_leak(session_override: object) -> None:
                assert 'DOC_ENGINE' not in vars(settings)
                assert settings.REGISTER_ENABLED is session_override
                assert 'STORAGE_IMPL' not in vars(settings)
                replacement = object()
                resources._state['storage'] = replacement
                assert settings.STORAGE_IMPL is replacement

            def test_session_override_survives_another_function_patch(monkeypatch: pytest.MonkeyPatch, session_override: object) -> None:
                monkeypatch.setattr(settings, 'REGISTER_ENABLED', object())
                monkeypatch.undo()
                assert settings.REGISTER_ENABLED is session_override
            """),
    )
    report = tmp_path / "scope-report.xml"
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", str(tmp_path), "-q", "--confcutdir=" + str(tmp_path), "--junitxml=" + str(report)],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    suite = ET.parse(report).getroot().find("testsuite")
    assert suite is not None
    assert {name: int(suite.attrib[name]) for name in ("tests", "failures", "errors", "skipped")} == {"tests": 4, "failures": 0, "errors": 0, "skipped": 0}

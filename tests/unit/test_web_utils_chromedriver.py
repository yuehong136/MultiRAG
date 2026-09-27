"""Regression for webdriver-manager selecting ChromeDriver archive notices."""

import os
from pathlib import Path

import pytest

from api.utils import web_utils


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-bit regression")
def test_managed_chromedriver_uses_binary_when_cache_points_to_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    notice = tmp_path / "THIRD_PARTY_NOTICES.chromedriver"
    notice.write_text("notice")
    notice.chmod(0o755)
    binary = tmp_path / "chromedriver"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o644)
    monkeypatch.setattr(web_utils.ChromeDriverManager, "install", lambda self: str(notice))

    service = web_utils._managed_chromedriver_service()

    assert service.path == str(binary)
    assert os.access(binary, os.X_OK)

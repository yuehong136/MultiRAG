"""Readiness waits reject false positives and retain bounded failure evidence."""

from typing import Any

import pytest
import requests

from scripts import smoke


def test_wait_rejects_http_200_without_pong_and_limits_last_attempt(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    now = [0.0]
    timeouts: list[float] = []

    def get(url: str, *, timeout: float) -> Any:
        assert url.endswith("/api/v1/system/ping")
        timeouts.append(timeout)
        now[0] += 0.5
        return type("Response", (), {"status_code": 200, "text": "login page"})()

    monkeypatch.setattr(smoke.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(smoke.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.setattr(smoke.requests, "get", get)
    assert not smoke.wait_for_ping("http://local", 3)
    assert timeouts == [3, 0.5] and now[0] == 3
    assert "HTTP 200; expected pong" in capsys.readouterr().err


def test_wait_reports_connection_failure_and_accepts_pong(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    now = [0.0]

    def get(url: str, *, timeout: float) -> Any:
        now[0] += timeout
        raise requests.ConnectionError("secret must not be copied to diagnostics")

    monkeypatch.setattr(smoke.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(smoke.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.setattr(smoke.requests, "get", get)
    assert not smoke.wait_for_ping("http://local", 1)
    diagnostics = capsys.readouterr().err
    assert "ConnectionError" in diagnostics and "secret" not in diagnostics
    monkeypatch.setattr(smoke.requests, "get", lambda *args, **kwargs: type("Response", (), {"status_code": 200, "text": '{"data":"pong"}'})())
    assert smoke.wait_for_ping("http://local", 1)

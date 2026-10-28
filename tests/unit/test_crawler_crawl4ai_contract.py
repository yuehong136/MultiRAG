"""Crawl4AI configuration and cleanup at the agent tool boundary."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from crawl4ai import BrowserConfig, CacheMode, CrawlerRunConfig

from agent.tools import crawler as module


@pytest.mark.parametrize("extract_type", ["html", "markdown", "content"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_crawler_uses_current_config_and_closes(monkeypatch: pytest.MonkeyPatch, extract_type: str, outcome: str) -> None:
    events: list[str] = []
    proxy = "http://proxy.example:8080"

    class ControlledCrawler:
        def __init__(self, *, config: BrowserConfig) -> None:
            assert config.proxy_config.server == proxy
            assert config.verbose is True
            events.append("constructed")

        async def __aenter__(self) -> Any:
            events.append("entered")
            return self

        async def __aexit__(self, *args: Any) -> None:
            events.append("closed")

        async def arun(self, *, url: str, config: CrawlerRunConfig) -> Any:
            assert url == "https://public.example/article"
            assert config.cache_mode is CacheMode.BYPASS
            events.append("called")
            if outcome == "failure":
                raise RuntimeError("crawl failed")
            if outcome == "cancelled":
                raise asyncio.CancelledError
            return SimpleNamespace(cleaned_html="<p>page</p>", markdown="markdown page", extracted_content="content page")

    monkeypatch.setattr(module, "AsyncWebCrawler", ControlledCrawler)
    tool = object.__new__(module.Crawler)
    tool._param = SimpleNamespace(proxy=proxy, extract_type=extract_type)
    monkeypatch.setattr(tool, "check_if_canceled", lambda _context: False)
    if outcome == "success":
        assert await tool.get_web("https://public.example/article") == {"html": "<p>page</p>", "markdown": "markdown page", "content": "content page"}[extract_type]
    else:
        with pytest.raises(RuntimeError if outcome == "failure" else asyncio.CancelledError):
            await tool.get_web("https://public.example/article")
    assert events == ["constructed", "entered", "called", "closed"]


async def test_cancelled_crawler_does_not_allocate(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(**kwargs: Any) -> None:
        raise AssertionError("cancelled tool must not allocate a browser")

    monkeypatch.setattr(module, "AsyncWebCrawler", forbidden)
    tool = object.__new__(module.Crawler)
    monkeypatch.setattr(tool, "check_if_canceled", lambda _context: True)
    assert await tool.get_web("https://public.example/article") is None

"""Injected boundaries for source snapshots and bounded model analysis."""

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession


class AnalysisResult(TypedDict):
    proposals: list[dict[str, Any]]
    model_reference: str
    config_reference: str
    usage: dict[str, Any]


Authorize = Callable[[AsyncSession, str], Awaitable[None]]
Snapshotter = Callable[[str, list[int], list[str], int], Awaitable[dict[str, Any]]]
InputValidator = Callable[[AsyncSession, str, dict[str, Any]], Awaitable[None]]
Analyzer = Callable[[str, dict[str, Any], str, int], Awaitable[AnalysisResult]]

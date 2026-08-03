"""Registry for published MultiRAG execution target adapters."""

from __future__ import annotations

from api.channel_execution.errors import TargetTypeUnsupportedError
from api.channel_execution.protocols import TargetExecutor


class TargetExecutorRegistry:
    """Maps stable target type names to executor implementations."""

    def __init__(self, executors: list[TargetExecutor] | None = None) -> None:
        self._executors: dict[str, TargetExecutor] = {}
        for executor in executors or []:
            self.register(executor)

    def register(self, executor: TargetExecutor) -> None:
        target_type = executor.target_type
        if not target_type.startswith("multirag."):
            raise ValueError("Target type must use the multirag namespace.")
        if target_type in self._executors:
            raise ValueError(f"Target executor already registered: {target_type}")
        self._executors[target_type] = executor

    def get(self, target_type: str) -> TargetExecutor:
        executor = self._executors.get(target_type)
        if executor is None:
            raise TargetTypeUnsupportedError()
        return executor

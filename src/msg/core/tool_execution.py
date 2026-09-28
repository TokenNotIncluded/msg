"""Local tool execution port, independent of worker orchestration and backends.

A runner returns a local artifact, never a committed ResourceRef. The worker
validates it, rechecks current authority/attempt/deadline and publishes it in its
existing transaction. Implementing this Protocol grants no execution authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import JsonMap, NetworkPolicy, ToolSpec


@dataclass(frozen=True, slots=True)
class ToolResult:
    path: Path
    media_type: str
    metadata: dict


@runtime_checkable
class ToolRunner(Protocol):
    async def __call__(
        self, tool: ToolSpec, arguments: JsonMap,
        policies: tuple[NetworkPolicy, ...], directory: Path, /
    ) -> ToolResult: ...

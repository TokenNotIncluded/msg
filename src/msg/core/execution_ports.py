"""Narrow trusted assembly dependencies, not resource-configurable hooks."""
from __future__ import annotations

from typing import Protocol
from .contracts import MetadataSession
from .models import Event, ExecutionContext, JsonMap, OperationRequest, ResourceRef


class PacketDecoder(Protocol):
    def __call__(self, value: object, max_bytes: int, /) -> OperationRequest: ...


class ProjectionReader(Protocol):
    async def __call__(self, context: ExecutionContext, request: OperationRequest,
                       session: MetadataSession, ref: ResourceRef, /) -> JsonMap: ...


class EventProjector(Protocol):
    async def __call__(self, session: MetadataSession, event: Event, /) -> None: ...

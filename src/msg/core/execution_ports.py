"""Fixed executor dependencies, not plugin hooks or a workflow interface.

ResultProjection is a permission-checked read in the existing transaction.
TransactionalEventNotifications may enqueue durable jobs in that transaction;
it must never perform external I/O or commit it. Execution, auth and receipt
ownership remain in OperationExecutor.
"""

from typing import Protocol

from msg.core.contracts import MetadataSession
from msg.core.models import Event, ExecutionContext, JsonMap, OperationRequest, ResourceRef


class ResultProjection(Protocol):
    async def __call__(
        self,
        context: ExecutionContext,
        request: OperationRequest,
        session: MetadataSession,
        resource: ResourceRef,
        *,
        fields: tuple[str, ...],
    ) -> JsonMap: ...


class TransactionalEventNotifications(Protocol):
    async def __call__(self, session: MetadataSession, event: Event) -> None: ...

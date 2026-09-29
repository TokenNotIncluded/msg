"""Work-list deadlines are enforced before scanning and before projecting rows."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from read_only_evidence import readonly_evidence
from test_collaboration_list_bounds import create_records, observe_batches
from test_service import call, register

from msg.plugins import collaboration_resources


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['request', 'offer', 'checkpoint', 'proposal'])
@pytest.mark.parametrize('stage', ['before_query', 'after_query'])
async def test_expired_work_list_budget_never_projects_content(installed, monkeypatch, kind, stage):
    app, _ = installed
    key, subject, _ = await register(app, 'work-list-deadline')
    await create_records(app, key, subject, kind, 1)
    # Only the work-list handler's clock changes. Authentication, recovery,
    # principal selection, database reads and target ownership remain real.
    readings = iter([float('inf')] if stage == 'before_query' else [0.0, float('inf')])
    projection = AsyncMock(side_effect=AssertionError('expired request projected work content'))
    async with readonly_evidence(app, monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(
                collaboration_resources,
                'time',
                SimpleNamespace(monotonic=lambda: next(readings, float('inf'))),
            )
            patch.setattr(collaboration_resources, '_project', projection)
            async with observe_batches(app, monkeypatch) as (batches, violations):
                result = await call(
                    app, 'communication.' + kind + '_list', {'limit': 1}, key=key, subject=subject
                )
                assert not violations, repr(violations)
                assert result.status == 'error' and result.error.code == 'query_cost_exceeded'
                assert len(batches) == (0 if stage == 'before_query' else 1)
                projection.assert_not_called()

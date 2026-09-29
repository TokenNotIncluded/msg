"""Deadline exhaustion fails closed before legacy lease SQL materialization."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from read_only_evidence import readonly_evidence
from test_service import call, register

from msg.plugins import collaboration


@pytest.mark.asyncio
async def test_expired_lease_list_budget_refuses_before_query(installed, monkeypatch):
    app, _ = installed
    key, subject, _ = await register(app, 'lease-deadline')
    transaction = app.metadata.transaction
    attempted = []

    @asynccontextmanager
    async def no_lease_scan(*, write):
        assert write is False
        async with transaction(write=False) as tx:
            query_rows = tx.rows

            def guard(sql, parameters=()):
                if 'FROM collaboration_leases ' in sql:
                    attempted.append(sql)
                    raise AssertionError('expired request issued a lease scan')
                return query_rows(sql, parameters)

            with monkeypatch.context() as session_patch:
                session_patch.setattr(tx, 'rows', guard)
                yield tx

    async with readonly_evidence(app, monkeypatch):
        with monkeypatch.context() as patch:
            # Only this handler sees an expired clock. Real signing, principal,
            # runtime generation and current authorization are not replaced.
            patch.setattr(collaboration, 'time', SimpleNamespace(monotonic=lambda: float('inf')))
            patch.setattr(app.metadata, 'transaction', no_lease_scan)
            result = await call(app, 'communication.lease_list', {'limit': 1},
                                key=key, subject=subject)
            assert not attempted, 'lease scan ran after its deadline: ' + repr(attempted)
            assert result.status == 'error' and result.error.code == 'query_cost_exceeded'

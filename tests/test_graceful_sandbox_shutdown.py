"""Real SIGTERM, sandbox subprocesses and persisted queue state on PostgreSQL."""

import runpy
from pathlib import Path

import pytest

probe = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/check_graceful_shutdown.py'))[
    'probe'
]


@pytest.mark.parametrize('mode', ['drain', 'cancel', 'timeout'])
async def test_sigterm_reaps_sandbox_without_claiming_next_job(installed, mode):
    app, _ = installed
    facts = await probe(app, mode)
    assert facts['sandbox_reaped']
    assert facts['storage_closed']
    assert facts['next_job_unclaimed']
    assert not facts['expired_cancelled_attempt_replayed']

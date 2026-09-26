"""Disposable PostgreSQL databases shared by tests and conformance checks."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote

import psycopg
import pytest
from psycopg import sql


@pytest.fixture(scope="session")
def pg_cluster():
    configured = os.environ.get("MSG_TEST_POSTGRES_URL_TEMPLATE")
    if configured:
        yield configured
        return
    root = Path(tempfile.mkdtemp(prefix="msg-test-pg-", dir="/tmp"))
    data = root / "data"
    socket = root / "socket"
    socket.mkdir()
    try:
        subprocess.run(
            ["initdb", "-D", str(data), "-A", "trust", "--no-instructions"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["pg_ctl", "-D", str(data), "-l", str(root / "server.log"),
             "-o", f"-k {socket} -h '' -p 5432", "-w", "start"],
            check=True, capture_output=True, text=True,
        )
        yield f'postgresql://lightjunction@localhost:5432/{{database}}?host={quote(str(socket),safe="")}'
    finally:
        if data.exists():
            subprocess.run(
                ["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                check=False, capture_output=True, text=True,
            )
        shutil.rmtree(root)


@pytest.fixture
def pg_dsn(pg_cluster):
    name = "msg_test_" + uuid.uuid4().hex
    with psycopg.connect(pg_cluster.format(database="postgres"), autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield pg_cluster.format(database=name)
    finally:
        with psycopg.connect(pg_cluster.format(database="postgres"), autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))

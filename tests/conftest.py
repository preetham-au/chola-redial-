"""Every test runs against a throwaway DB, in seed mode, under DRY_RUN."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Set before anything imports api.db, which reads these at call time.
_TMP = Path(tempfile.mkdtemp(prefix="redial-test-"))
os.environ["REDIAL_DB"] = str(_TMP / "test.db")
os.environ["DRY_RUN"] = "1"
os.environ["LEADS_SOURCE"] = "seed"


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from api.db import init_db
    from engine.seed import populate
    from api.main import app

    conn = init_db()
    # The full seed: 14 campaigns, so per-campaign assertions have enough leads
    # per (bucket x disposition) cell to be stable.
    populate(conn)
    conn.close()
    with TestClient(app) as test_client:
        yield test_client

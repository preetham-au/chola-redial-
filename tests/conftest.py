"""Every test runs against a throwaway DB, in seed mode, under DRY_RUN."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Set before anything imports api.db, which reads these at call time.
_TMP = Path(tempfile.mkdtemp(prefix="redial-test-"))
os.environ["REDIAL_DB"] = str(_TMP / "test.db")
# The dry-run switch persists itself to the .env now. Pointed at a path that does
# not exist so the suite cannot rewrite a developer's real one -- `save_env_value`
# treats an absent file as "nothing to persist to" and says so in its return.
os.environ["REDIAL_ENV_FILE"] = str(_TMP / "absent.env")
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


@pytest.fixture
def pin_clock(monkeypatch):
    """Freeze the time of day every dial-window guard reads. This test only.

    A test that asks the wall clock whether it may run is a test that stops
    running. The waves got their own bands on 13 Sep 2026 -- morning to 13:30,
    afternoon after it -- and eleven tests that had always passed began skipping
    themselves from 13:30 IST onward, with nothing in a green suite saying the
    coverage had gone. Pinning the hour is what makes a band test about the band.

    `now_ist` is defined once in api.db and imported BY NAME into api.day,
    api.routes_core, api.dial_log and the test modules, so each of them holds its
    own reference and patching the definition site alone leaves every importer on
    the real clock. Rather than maintain a list of importers that rots the next
    time somebody adds one, every module still pointing at the original is
    repointed -- which is also the answer to "which target actually takes effect".

    Only the time of day moves; the DATE is left alone, so today's plan is still
    filed under today. monkeypatch undoes all of it at teardown, which matters
    here: `client` above is session-scoped, so one database outlives every test
    and a clock left pinned would follow it into the next file.
    """
    import api.db

    def pin(hour: int, minute: int = 0):
        real = api.db.now_ist
        frozen = real().replace(hour=hour, minute=minute, second=0, microsecond=0)
        # `lambda: frozen`, captured BEFORE the patch -- never `lambda:
        # now_ist().replace(...)`, which is the replacement calling itself and
        # recurses until the stack ends.
        for module in list(sys.modules.values()):
            if getattr(module, "now_ist", None) is real:
                monkeypatch.setattr(module, "now_ist", lambda: frozen)
        return frozen

    return pin

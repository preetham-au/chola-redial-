"""SQLite access for the redial console. One connection factory, no ORM."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from engine.red_engine import NEVER_DIAL
from engine.seed import TEST_NUMBERS

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def load_env(path: Path | None = None) -> None:
    """Read `KEY=value` lines from the project .env into os.environ.

    Real environment variables always win (`setdefault`), so a shell export or a
    Render dashboard value is never overridden by a file on disk. Absent file =
    no-op, which is what keeps the seed path credential-free.
    """
    try:
        text = (path or ROOT / ".env").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def purge_campaigns(conn: sqlite3.Connection, keep_ids: Iterable[int]) -> int:
    """Drop every campaign not in `keep_ids`, with its leads, configs and runs.

    Seed ids (1-16) and warehouse ids (1400+) do not overlap, so without this a
    `sync` after a `seed` would leave the invented campaign names sitting next
    to the real ones in the console.
    """
    keep = ",".join(str(int(i)) for i in keep_ids) or "-1"
    where = f"campaign_id NOT IN ({keep})"
    conn.execute(f"DELETE FROM leads WHERE {where}")
    conn.execute(f"DELETE FROM config WHERE {where}")
    conn.execute(f"DELETE FROM runs WHERE {where}")     # cascades plan_items/decisions
    dropped = conn.execute(f"DELETE FROM campaigns WHERE id NOT IN ({keep})").rowcount
    conn.commit()
    return dropped


def db_path() -> str:
    """Read at call time so tests can point REDIAL_DB at a temp file."""
    return os.environ.get("REDIAL_DB") or str(ROOT / "redial.db")


def dry_run() -> bool:
    """Live dialling requires an explicit DRY_RUN=0. Anything else is a dry run."""
    return (os.environ.get("DRY_RUN") or "1").strip() not in {"0", "false", "False"}


def leads_source() -> str:
    return (os.environ.get("LEADS_SOURCE") or "seed").strip().lower()


def formi_token() -> str | None:
    """The bearer for a live Formi write, or None if nothing is configured.

    FORMI_API_KEY is the name the rest of the Chola tooling uses -- mark_paid,
    upload_leads and the redial scripts all read it from the same .env. This app
    asked for FORMI_TOKEN instead, so a .env that worked everywhere else left
    every live dial and every bulk stage commit failing on "FORMI_TOKEN is not
    set", which reads like a missing credential rather than a misspelled one.

    FORMI_TOKEN still wins when both are set, so an operator who exported the
    name this app used to document does not silently start using a different
    key.
    """
    for name in ("FORMI_TOKEN", "FORMI_API_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


NO_TOKEN = ("No Formi credential: set FORMI_API_KEY (or FORMI_TOKEN) in the "
            "environment or .env")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session():
    """A connection that is actually closed afterwards.

    `with sqlite3.connect(...)` only ends the transaction, it leaves the handle
    open — which on Windows keeps a lock on the file and breaks the tests' temp
    databases.
    """
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    conn = conn or connect()
    # `CREATE TABLE IF NOT EXISTS` cannot add a column to a table that already
    # exists, so an older redial.db needs the new column adding by hand -- and
    # before the script runs, because the script indexes it.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(leads)")}
    if columns and "phone" not in columns:
        conn.execute("ALTER TABLE leads ADD COLUMN phone TEXT")
    if columns and "queued_today" not in columns:
        conn.execute("ALTER TABLE leads ADD COLUMN queued_today INTEGER NOT NULL DEFAULT 0")
    item_columns = {r["name"] for r in conn.execute("PRAGMA table_info(plan_items)")}
    if item_columns and "phone" not in item_columns:
        conn.execute("ALTER TABLE plan_items ADD COLUMN phone TEXT")
    campaign_columns = {r["name"] for r in conn.execute("PRAGMA table_info(campaigns)")}
    if campaign_columns and "autopilot" not in campaign_columns:
        conn.execute("ALTER TABLE campaigns ADD COLUMN autopilot INTEGER NOT NULL DEFAULT 0")
    if campaign_columns and "autopilot_note" not in campaign_columns:
        conn.execute("ALTER TABLE campaigns ADD COLUMN autopilot_note TEXT NOT NULL DEFAULT ''")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    return conn


# Every timestamp in this service is naive IST by convention (see schema.sql on
# plan_items.scheduled_time). `datetime.now()` is naive *server-local*, which is
# only the same thing when the box happens to run on IST -- in UTC it is 5h30m
# behind, enough to stamp and plan against the wrong calendar day. India has no
# DST, so a fixed +05:30 is exact and needs no tzdata installed on the host.
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """Current IST wall-clock time, naive, to match everything stored."""
    return datetime.now(IST).replace(tzinfo=None)


def now_iso() -> str:
    return now_ist().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# The contract's default config, verbatim. Every campaign starts at version 1
# with this body; a PUT inserts version N+1 and nothing is ever mutated.
DEFAULT_CONFIG: dict[str, Any] = {
    "dial_window": {"start": "09:30", "end": "19:00"},
    "frequency_table": [
        {"bucket": "F1", "label": "Warm-up",          "from_dte": 45, "to_dte": 32, "calls_per_week": 2, "calls_per_day": 0},
        {"bucket": "F2", "label": "Early engagement", "from_dte": 31, "to_dte": 24, "calls_per_week": 2, "calls_per_day": 0},
        {"bucket": "F3", "label": "Building urgency", "from_dte": 23, "to_dte": 16, "calls_per_week": 3, "calls_per_day": 0},
        {"bucket": "F4", "label": "High frequency",   "from_dte": 15, "to_dte": 8,  "calls_per_week": 3, "calls_per_day": 0},
        {"bucket": "F5", "label": "Critical window",  "from_dte": 7,  "to_dte": 1,  "calls_per_week": 0, "calls_per_day": 2},
        {"bucket": "E0", "label": "Expiry window",    "from_dte": 0,  "to_dte": -1, "calls_per_week": 0, "calls_per_day": 2},
        {"bucket": "F6", "label": "Grace period",     "from_dte": -2, "to_dte": -3, "calls_per_week": 0, "calls_per_day": 2},
    ],
    "bucket_priority": ["M0", "E0", "F6", "F5", "F4", "F3", "F2", "F1", "D0"],
    "auto_dispositions": ["did_not_pick", "hung_up", "unreachable", "rnr",
                          "beep_tone_number_busy_not_reachable_switched_off",
                          "voicemail", "telephony_failed", "dialer_nc",
                          "new", "fresh", "not_dialed", ""],
    # bucket -> slugs that bucket alone may auto-dial. Absent or empty = inherit
    # `auto_dispositions`, so {} is exactly the previous behaviour.
    "bucket_dispositions": {},
    # Who gets the SECOND call of the day in F5/E0/F6/M0. The client's rule is
    # "2nd call only if the 1st was not answered", so this is the no-contact set:
    # a lead who picked up this morning is not called again this afternoon.
    # Empty would mean everyone, which is the historic (wrong) behaviour.
    "second_call_dispositions": ["did_not_pick", "hung_up", "hung_up_no_contact",
                                 "unreachable", "rnr",
                                 "beep_tone_number_busy_not_reachable_switched_off",
                                 "voicemail", "voicemail_ivr", "telephony_failed",
                                 "dialer_nc", "new", "fresh", "not_dialed", ""],
    "mandatory_days": [1, 0],
    # The only dispositions a mandatory day (RED−1, RED) may NOT override. The
    # client's rule is "all cases excluding the renewed and DND cases" — so on
    # those two days everything else is called whatever its disposition says.
    "never_dial": list(NEVER_DIAL),
    "calls_per_day_cap": 2,
    "same_day_gap_hours": 3.0,
    "shift_from_last_hours": 2.0,
    "max_per_minute": 12,
    "max_per_run": 5000,
    "max_attempts": 0,
    # The ONLY numbers /api/test-call/trigger will dial. Anything else is 422.
    # Sourced from the seed so the allow-list and the seeded lead cannot drift.
    "test_numbers": list(TEST_NUMBERS),
}


def current_config(conn: sqlite3.Connection, campaign_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT version, created_at, body FROM config WHERE campaign_id=? "
        "ORDER BY version DESC LIMIT 1", (campaign_id,)).fetchone()
    if row is None:
        return insert_config(conn, campaign_id, DEFAULT_CONFIG)
    return {"version": row["version"], "created_at": row["created_at"], **json.loads(row["body"])}


def insert_config(conn: sqlite3.Connection, campaign_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """Append a new version. Never updates an existing row."""
    row = conn.execute("SELECT MAX(version) AS v FROM config WHERE campaign_id=?",
                       (campaign_id,)).fetchone()
    version = (row["v"] or 0) + 1
    created = now_iso()
    body = {k: v for k, v in body.items() if k not in ("version", "created_at")}
    conn.execute("INSERT INTO config (campaign_id, version, created_at, body) VALUES (?,?,?,?)",
                 (campaign_id, version, created, json.dumps(body)))
    conn.commit()
    return {"version": version, "created_at": created, **body}
